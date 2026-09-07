#!/usr/bin/env bash
#
# setup-notch-signing.sh — one-time setup so the native notch helper
# (NeoNotchHost) keeps its macOS microphone permission across `tauri dev`
# rebuilds.
#
# Why this exists
# ---------------
# `swiftc` leaves NeoNotchHost ad-hoc signed. macOS TCC then identifies the
# binary by its cdhash, which changes on EVERY recompile — so the Live Mode
# mic grant never sticks and you get re-prompted (or silently denied) forever.
#
# This creates a dedicated, self-signed code-signing certificate ("Neo Notch
# Dev") in your login keychain. Signing with it gives the binary a stable
# Designated Requirement (identifier + this cert), which does NOT change when
# you edit the Swift and rebuild. You grant the mic ONCE and it persists.
#
# This is a LOCAL cert. It involves no Apple account and is unrelated to any
# "Apple Development" (employer) identity — build.rs will only ever use this one.
#
# Run once:
#   ./scripts/setup-notch-signing.sh
#
# It is idempotent: re-running reuses the existing cert if present.

set -euo pipefail

CERT_NAME="Neo Notch Dev"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

echo "==> Checking for existing '$CERT_NAME' code-signing identity..."
if security find-identity -v -p codesigning | grep -q "$CERT_NAME"; then
  echo "    Already present. Nothing to create."
else
  echo "==> Creating self-signed code-signing certificate '$CERT_NAME'..."
  TMPDIR_CERT="$(mktemp -d)"
  trap 'rm -rf "$TMPDIR_CERT"' EXIT

  KEY="$TMPDIR_CERT/neo-notch-dev.key"
  CRT="$TMPDIR_CERT/neo-notch-dev.crt"
  P12="$TMPDIR_CERT/neo-notch-dev.p12"
  P12_PASS="neo-notch"

  # Self-signed cert with the Code Signing extended key usage. 10-year validity.
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$KEY" -out "$CRT" -days 3650 \
    -subj "/CN=$CERT_NAME" \
    -addext "keyUsage=critical,digitalSignature" \
    -addext "extendedKeyUsage=critical,codeSigning"

  openssl pkcs12 -export -inkey "$KEY" -in "$CRT" \
    -out "$P12" -passout "pass:$P12_PASS" -name "$CERT_NAME"

  # Import into the login keychain. -T authorizes codesign to use the key;
  # -A additionally allows access without the per-use ACL warning.
  security import "$P12" -k "$KEYCHAIN" -P "$P12_PASS" \
    -T /usr/bin/codesign -A

  echo "==> Certificate imported."
fi

echo
# NOTE on non-interactive signing: we intentionally do NOT run
# `security set-key-partition-list` here — its interactive password path is
# deprecated/flaky. Instead, the FIRST build that signs NeoNotchHost will pop
# one keychain dialog ("codesign wants to access key 'Neo Notch Dev'"). Click
# "Always Allow" once and codesign is authorized for all future builds. This is
# the LOCAL dev key, unrelated to any Apple Development / employer identity.

echo "==> Verifying the identity is visible to codesign (find-identity, no -v):"
# No -v: the self-signed cert is untrusted (CSSMERR_TP_NOT_TRUSTED), so the
# valid-only listing would hide it even though codesign & TCC accept it fine.
if security find-identity -p codesigning | grep -q "$CERT_NAME"; then
  security find-identity -p codesigning | grep "$CERT_NAME"
else
  echo "!! '$CERT_NAME' not found by find-identity. Something went wrong." >&2
  exit 1
fi

echo
echo "==> Done. Next steps:"
echo "    1. Rebuild so build.rs re-signs the helper with this identity:"
echo "         touch src-tauri/macos/NeoNotchHost.swift && yarn start"
echo "       On the first build, click 'Always Allow' on the 'Neo Notch Dev'"
echo "       keychain prompt (it's this local dev key — safe)."
echo "    2. Clear any stale mic grant left by the old ad-hoc binary:"
echo "         tccutil reset Microphone com.neo.desktop.notchhost"
echo "       ('No such bundle identifier' just means nothing to clear — fine.)"
echo "    3. Trigger Live Mode once and APPROVE the microphone prompt for 'Neo'."
echo "       From now on the grant persists across every rebuild."
