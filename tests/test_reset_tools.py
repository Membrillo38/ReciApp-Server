from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import authenticated_readiness
import reset_execute
import reset_guard
import reset_preflight
import reset_approve_rehearsal
import reset_rehearsal


TARGET_REF = "reciapp-postgres"
TARGET_HOST = "reciapp-postgres"


def writer_evidence(**overrides):
    value = {
        "target_ref": TARGET_REF,
        "target_host": TARGET_HOST,
        **{name: True for name in reset_guard.REQUIRED_WRITER_CONTROLS},
        "in_flight_processes": 0,
        "reviewed_by": "operator",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    value.update(overrides)
    return value


def test_writer_freeze_requires_every_external_writer_and_fresh_evidence():
    reset_guard.validate_writer_evidence(writer_evidence(), target_ref=TARGET_REF, target_host=TARGET_HOST)
    with pytest.raises(reset_guard.ResetGuardError, match="auth_signups_disabled"):
        reset_guard.validate_writer_evidence(
            writer_evidence(auth_signups_disabled=False),
            target_ref=TARGET_REF,
            target_host=TARGET_HOST,
        )
    with pytest.raises(reset_guard.ResetGuardError, match="no older than 30 minutes"):
        reset_guard.validate_writer_evidence(
            writer_evidence(observed_at=(datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()),
            target_ref=TARGET_REF,
            target_host=TARGET_HOST,
        )


def test_table_inventory_rejects_unknown_public_auth_and_fk_dependents():
    valid = {
        "public": sorted(set(reset_guard.PUBLIC_DELETE_TABLES) | set(reset_guard.PUBLIC_PRESERVE_TABLES)),
        "auth": sorted(set(reset_guard.AUTH_DELETE_TABLES) | set(reset_guard.AUTH_PRESERVE_TABLES)),
        "dependencies": ["public.profiles->auth.users"],
    }
    reset_guard.validate_table_inventory(valid)
    for mutation in (
        lambda value: value["public"].append("future_user_data"),
        lambda value: value["auth"].append("future_tokens"),
        lambda value: value["dependencies"].append("private.hidden_users->auth.users"),
    ):
        candidate = json.loads(json.dumps(valid))
        mutation(candidate)
        with pytest.raises(reset_guard.ResetGuardError):
            reset_guard.validate_table_inventory(candidate)

    preserved_dependency = json.loads(json.dumps(valid))
    preserved_dependency["dependencies"].append("public.app_settings->auth.users")
    with pytest.raises(reset_guard.ResetGuardError, match="dependent tables"):
        reset_guard.validate_table_inventory(preserved_dependency)


def test_inventory_queries_fk_dependencies_for_every_delete_target():
    sql = reset_preflight._inventory_sql()
    for table in reset_guard.PUBLIC_DELETE_TABLES:
        assert f"('public', '{table}')" in sql
    for table in reset_guard.AUTH_DELETE_TABLES:
        assert f"('auth', '{table}')" in sql


def test_pg17_guard_checks_every_dump_restore_tool():
    calls = []

    def runner(command, **kwargs):
        calls.append(command[0])
        return subprocess.CompletedProcess(command, 0, f"{command[0]} (PostgreSQL) 17.6\n", "")

    reset_guard.validate_pg17(runner)
    assert calls == ["psql", "pg_dump", "pg_dumpall", "pg_restore"]


def test_receipts_are_integrity_checked_and_rehearsal_is_bound_to_backup(tmp_path):
    path = tmp_path / "receipt.json"
    reset_guard.write_receipt(path, {"version": 1, "kind": "test", "value": 7})
    receipt = reset_guard.load_json(path)
    reset_guard.validate_receipt_integrity(receipt)
    receipt["value"] = 8
    with pytest.raises(reset_guard.ResetGuardError, match="digest"):
        reset_guard.validate_receipt_integrity(receipt)

    rehearsal = {
        "version": 1,
        "source_target_ref": TARGET_REF,
        "encrypted_backup_sha256": "a" * 64,
        "preflight_receipt_sha256": "b" * 64,
        "decrypt_ok": True,
        "restore_exit_ok": True,
        "counts_match": True,
        "app_settings_match": True,
        "schema_fingerprint_match": True,
        "migration_fingerprint_match": True,
        "auth_config_match": True,
        "review_approved": True,
        "disposable_target": "throwaway-17",
        "reviewed_by": "operator",
    }
    reset_guard.validate_rehearsal_receipt(
        rehearsal,
        backup_sha256="a" * 64,
        target_ref=TARGET_REF,
    )
    rehearsal["counts_match"] = False
    with pytest.raises(reset_guard.ResetGuardError, match="counts_match"):
        reset_guard.validate_rehearsal_receipt(
            rehearsal,
            backup_sha256="a" * 64,
            target_ref=TARGET_REF,
        )


def test_rehearsal_requires_separate_post_restore_human_approval(tmp_path):
    path = tmp_path / "unapproved.json"
    reset_guard.write_receipt(path, {
        "version": 1,
        "kind": "reciapp-restoration-rehearsal",
        "source_target_ref": TARGET_REF,
        "encrypted_backup_sha256": "a" * 64,
        "preflight_receipt_sha256": "b" * 64,
        "disposable_target": "throwaway-17",
        "decrypt_ok": True,
        "restore_exit_ok": True,
        "counts_match": True,
        "app_settings_match": True,
        "schema_fingerprint_match": True,
        "migration_fingerprint_match": True,
        "auth_config_match": True,
        "review_approved": False,
        "reviewed_by": None,
    })
    unapproved = reset_guard.load_json(path)
    with pytest.raises(reset_guard.ResetGuardError, match="review_approved"):
        reset_guard.validate_rehearsal_receipt(
            unapproved,
            backup_sha256="a" * 64,
            target_ref=TARGET_REF,
        )
    approved = reset_approve_rehearsal.approve_rehearsal(
        unapproved,
        reviewed_by="operator",
        expected_disposable_target="throwaway-17",
    )
    approved_path = tmp_path / "approved.json"
    reset_guard.write_receipt(approved_path, approved)
    reset_guard.validate_rehearsal_receipt(
        reset_guard.load_json(approved_path),
        backup_sha256="a" * 64,
        target_ref=TARGET_REF,
    )


def test_rehearsal_requires_exact_host_database_marker_and_empty_target(monkeypatch):
    monkeypatch.setenv("PGHOST", "disposable.internal")
    reset_rehearsal._validate_rehearsal_host(
        source_host=TARGET_HOST,
        rehearsal_host="disposable.internal",
    )
    with pytest.raises(reset_guard.ResetGuardError, match="PGHOST"):
        reset_rehearsal._validate_rehearsal_host(
            source_host=TARGET_HOST,
            rehearsal_host="other.internal",
        )
    marker = "A" * 32
    identity = {
        "database": "reciapp_rehearsal",
        "database_comment": f"reciapp-reset-disposable:{marker}",
        "user_relation_count": 0,
        "nondefault_extension_count": 0,
    }
    reset_rehearsal._validate_disposable_identity(
        identity,
        expected_database="reciapp_rehearsal",
        marker=marker,
        require_empty=True,
    )
    for changed, message in (
        ({**identity, "database": "postgres"}, "database"),
        ({**identity, "database_comment": "wrong"}, "marker"),
        ({**identity, "user_relation_count": 1}, "new and empty"),
        ({**identity, "nondefault_extension_count": 1}, "new and empty"),
    ):
        with pytest.raises(reset_guard.ResetGuardError, match=message):
            reset_rehearsal._validate_disposable_identity(
                changed,
                expected_database="reciapp_rehearsal",
                marker=marker,
                require_empty=True,
            )


def test_reset_sql_is_one_transaction_exact_and_preserves_configuration():
    sql = reset_execute.build_reset_sql(
        public_tables=list(reset_guard.PUBLIC_DELETE_TABLES) + list(reset_guard.PUBLIC_PRESERVE_TABLES),
        auth_tables=list(reset_guard.AUTH_DELETE_TABLES) + list(reset_guard.AUTH_PRESERVE_TABLES),
        app_settings_fingerprint="a" * 64,
        migration_fingerprint="b" * 64,
        auth_config_fingerprint="c" * 64,
        expected_public_counts={table: 1 for table in reset_guard.PUBLIC_DELETE_TABLES},
        expected_auth_counts={table: 1 for table in reset_guard.AUTH_DELETE_TABLES},
    )
    assert sql.startswith("BEGIN;") and sql.endswith("COMMIT;\n")
    assert "CASCADE" not in sql.upper()
    assert "TRUNCATE" not in sql.upper()
    assert "DELETE FROM public.app_settings" not in sql
    assert "DELETE FROM auth.oauth_clients" not in sql
    assert "DELETE FROM auth.users;" in sql
    assert "LOCK TABLE auth.users IN ACCESS EXCLUSIVE MODE;" in sql
    assert sql.index("count changed after approved backup") < sql.index("DELETE FROM public.api_request_logs;")
    for table in reset_guard.PUBLIC_DELETE_TABLES:
        assert sql.count(f"DELETE FROM public.{table};") == 1

    with pytest.raises(reset_guard.ResetGuardError, match="incomplete public counts"):
        reset_execute.build_reset_sql(
            public_tables=list(reset_guard.PUBLIC_DELETE_TABLES) + list(reset_guard.PUBLIC_PRESERVE_TABLES),
            auth_tables=list(reset_guard.AUTH_DELETE_TABLES) + list(reset_guard.AUTH_PRESERVE_TABLES),
            app_settings_fingerprint="a" * 64,
            migration_fingerprint="b" * 64,
            auth_config_fingerprint="c" * 64,
            expected_public_counts={},
            expected_auth_counts={table: 1 for table in reset_guard.AUTH_DELETE_TABLES},
        )


def test_reset_rejects_count_drift_and_receipts_from_another_preflight():
    baseline = {
        "active_jobs": 0,
        "storage_objects": 0,
        "public_counts": {"profiles": 1},
        "auth_counts": {"users": 1},
        "app_settings_fingerprint": "a",
        "schema_fingerprint": "b",
        "migration_fingerprint": "c",
        "auth_config_fingerprint": "d",
    }
    reset_execute.validate_current_reset_snapshot(dict(baseline), baseline)
    changed = json.loads(json.dumps(baseline))
    changed["auth_counts"]["users"] = 2
    with pytest.raises(reset_guard.ResetGuardError, match="row counts changed"):
        reset_execute.validate_current_reset_snapshot(changed, baseline)

    preflight = {"receipt_sha256": "a" * 64}
    backup = {"preflight_receipt_sha256": "a" * 64}
    rehearsal = {"preflight_receipt_sha256": "a" * 64}
    reset_execute.validate_receipt_chain(preflight, backup, rehearsal)
    rehearsal["preflight_receipt_sha256"] = "b" * 64
    with pytest.raises(reset_guard.ResetGuardError, match="Approved rehearsal"):
        reset_execute.validate_receipt_chain(preflight, backup, rehearsal)


def test_authenticated_runner_reports_only_aggregate_ids_and_latency(capsys):
    calls = []
    secret = "header.payload.signature"

    class Response:
        status = 200

        def __init__(self, request_id):
            self.headers = {"X-Request-ID": request_id}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return b'{"items":[]}'

    def opener(request, timeout):
        calls.append(request)
        return Response(f"request-{len(calls)}")

    result = authenticated_readiness.run_acceptance(
        "https://api.example.com",
        secret,
        100,
        expected_host="api.example.com",
        opener=opener,
    )
    assert result["requests"] == 202
    assert result["request_ids_present"] == 202
    assert result["request_ids_unique"] == 202
    assert secret not in json.dumps(result)
    assert calls[0].get_header("Authorization") is None
    assert calls[2].get_header("Authorization") == f"Bearer {secret}"
    assert capsys.readouterr().out == ""


def test_authenticated_runner_error_redacts_token_and_body():
    secret = "header.payload.signature"

    def opener(request, timeout):
        raise RuntimeError(f"provider body and {secret}")

    with pytest.raises(RuntimeError) as caught:
        authenticated_readiness.run_acceptance(
            "https://api.example.com", secret, 100,
            expected_host="api.example.com", opener=opener
        )
    assert secret not in str(caught.value)
    assert "provider body" not in str(caught.value)


def test_authenticated_runner_fails_closed_on_missing_ids_and_slow_p95(monkeypatch):
    calls = 0

    def missing_id_request(base_url, path, *, token, opener):
        nonlocal calls
        calls += 1
        return authenticated_readiness.Probe(path, 200, 1, None if calls == 1 else f"id-{calls}")

    monkeypatch.setattr(authenticated_readiness, "_request", missing_id_request)
    with pytest.raises(RuntimeError, match="missing_request_id"):
        authenticated_readiness.run_acceptance(
            "https://api.example.com", "token", 100,
            expected_host="api.example.com",
        )

    def slow_request(base_url, path, *, token, opener):
        latency = 900 if path == "/v1/me/recipes" else 1
        return authenticated_readiness.Probe(path, 200, latency, f"id-{path}-{token}")

    monkeypatch.setattr(authenticated_readiness, "_request", slow_request)
    with pytest.raises(RuntimeError, match="hot_read_p95_exceeded"):
        authenticated_readiness.run_acceptance(
            "https://api.example.com", "token", 100,
            expected_host="api.example.com",
        )


def test_authenticated_runner_preserves_api_path_prefix():
    requests = []

    class Response:
        status = 200
        headers = {"X-Request-ID": "request-id"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return b'{"items":[]}'

    def opener(request, timeout):
        requests.append(request.full_url)
        return Response()

    result = authenticated_readiness.run_acceptance(
        "https://api.example.com/reciapp",
        "token",
        100,
        expected_host="api.example.com",
        opener=opener,
    )

    assert result["status"] == "ok"
    assert requests[0] == "https://api.example.com/reciapp/health"
    assert requests[1] == "https://api.example.com/reciapp/ready"
    assert requests[2] == "https://api.example.com/reciapp/v1/me/recipes"


def test_authenticated_runner_rejects_unexpected_hosts_and_all_redirects():
    called = []

    def opener(request, timeout):
        called.append(request)
        raise AssertionError("must reject before network")

    with pytest.raises(ValueError, match="RECIAPP_EXPECTED_API_HOST"):
        authenticated_readiness.run_acceptance(
            "https://evil.example.com",
            "token",
            100,
            expected_host="api.example.com",
            opener=opener,
        )
    assert called == []
    for unsafe_path in ("/../admin", "/%2e%2e/admin", "/api//v1"):
        with pytest.raises(ValueError, match="API base URL"):
            authenticated_readiness.run_acceptance(
                f"https://api.example.com{unsafe_path}",
                "token",
                100,
                expected_host="api.example.com",
                opener=opener,
            )
    handler = authenticated_readiness._NoRedirectHandler()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example.com") is None
