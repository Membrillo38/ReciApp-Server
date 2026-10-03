# Apple trust anchor

`apple-root-ca-g3.pem` is the public Apple Root CA - G3 downloaded from Apple's PKI site:

- Source: https://www.apple.com/certificateauthority/AppleRootCA-G3.cer
- SHA-256 fingerprint: `63343abfb89a6a03ebb57e9b3f5fa7be7c4f5c756f3017b3a8c488c3653e9179`
- Expires: 2039-04-30

The certificate is a public trust anchor, not an APNs private key. `APPLE_ROOT_CA_PEM` can override it for a future Apple trust-anchor rotation.
