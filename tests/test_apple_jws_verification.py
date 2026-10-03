import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509.oid import NameOID

import app.apple_notifications as apple_notifications


def _b64url(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def _signed_jws():
    root_key = ec.generate_private_key(ec.SECP384R1())
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Apple JWS test root")])
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    root_certificate = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(root_key, hashes.SHA384())
    )
    intermediate_key = ec.generate_private_key(ec.SECP256R1())
    intermediate_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Apple JWS test intermediate")])
    intermediate_certificate = (
        x509.CertificateBuilder()
        .subject_name(intermediate_name)
        .issuer_name(root_name)
        .public_key(intermediate_key.public_key())
        .serial_number(x509.random_serial_number())
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(root_key, hashes.SHA384())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Apple JWS test leaf")])
    leaf_certificate = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(intermediate_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(intermediate_key, hashes.SHA256())
    )
    root_pem = root_certificate.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    header = _b64url(json.dumps({
        "alg": "ES256",
        "x5c": [
            base64.b64encode(leaf_certificate.public_bytes(serialization.Encoding.DER)).decode("ascii"),
            base64.b64encode(intermediate_certificate.public_bytes(serialization.Encoding.DER)).decode("ascii"),
        ],
    }, separators=(",", ":")).encode())
    payload = _b64url(json.dumps({"bundleId": "com.membri.reciapp"}, separators=(",", ":")).encode())
    signing_input = header + b"." + payload
    der_signature = leaf_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_signature)
    raw_signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return (signing_input + b"." + _b64url(raw_signature)).decode("ascii"), root_pem


def test_verify_jws_accepts_es256_raw_signature_and_escaped_root_pem(monkeypatch):
    compact, root_pem = _signed_jws()
    monkeypatch.setattr(
        apple_notifications.settings,
        "apple_root_ca_pem",
        root_pem.replace("\n", "\\n"),
    )

    assert apple_notifications.verify_jws(compact) == {"bundleId": "com.membri.reciapp"}


def test_verify_jws_rejects_tampered_payload(monkeypatch):
    compact, root_pem = _signed_jws()
    monkeypatch.setattr(apple_notifications.settings, "apple_root_ca_pem", root_pem)
    header, _, signature = compact.split(".")
    changed_payload = _b64url(b'{"bundleId":"com.attacker.app"}').decode("ascii")

    with pytest.raises(InvalidSignature):
        apple_notifications.verify_jws(f"{header}.{changed_payload}.{signature}")
