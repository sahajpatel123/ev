#!/bin/zsh
# Ensure a stable self-signed code-signing identity exists for EV.
#
# Why this matters: macOS TCC ties each permission grant to the app's code
# signature (its "designated requirement"). An ad-hoc signature changes its
# CDHash on every rebuild, so each rebuild looks like a brand-new app and every
# previously granted permission (Microphone, Camera, …) is silently forgotten.
# A fixed self-signed certificate makes the identity stable across rebuilds, so
# grants survive `./scripts/package.sh`.
#
# Usage: ./scripts/signing.sh   (creates the identity if missing; prints its name)

set -euo pipefail

IDENTITY="EV Code Signing"

if security find-identity -v -p codesigning 2>/dev/null | grep -q "$IDENTITY"; then
  echo "$IDENTITY"
  exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "[signing] creating self-signed code-signing identity: $IDENTITY" >&2
cat > "$TMP/csr.conf" <<EOF
[req]
distinguished_name = dn
prompt = no
[dn]
CN = $IDENTITY
[x509_ext]
basicConstraints = critical,CA:TRUE
keyUsage = critical,digitalSignature,keyEncipherment,keyCertSign
extendedKeyUsage = codeSigning
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
EOF
openssl req -x509 -newkey rsa:2048 -keyout "$TMP/key.pem" -out "$TMP/cert.pem" \
  -days 3650 -nodes -config "$TMP/csr.conf" -extensions x509_ext >/dev/null 2>&1
openssl pkcs12 -export -legacy -out "$TMP/cert.p12" -inkey "$TMP/key.pem" -in "$TMP/cert.pem" \
  -passout pass:evsign >/dev/null 2>&1

security import "$TMP/cert.p12" \
  -k "$HOME/Library/Keychains/login.keychain-db" \
  -T /usr/bin/codesign \
  -P evsign >/dev/null 2>&1 || true
security import "$TMP/cert.pem" \
  -k "$HOME/Library/Keychains/login.keychain-db" >/dev/null 2>&1 || true
security add-trusted-cert -d -r trustRoot \
  -k "$HOME/Library/Keychains/login.keychain-db" \
  "$TMP/cert.pem" >/dev/null 2>&1

if security find-identity -v -p codesigning 2>/dev/null | grep -q "$IDENTITY"; then
  echo "$IDENTITY"
else
  echo "[signing] failed to create identity: $IDENTITY" >&2
  exit 1
fi
