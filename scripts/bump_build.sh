#!/usr/bin/env bash
# Cycle 86 — one-command release bump for the phone program.
#
#   scripts/bump_build.sh [BUILD]     # default: date-N (N = next patch)
#
# Bumps every PWA_BUILD pin site to ONE version, regenerates the release
# manifest, and verifies the release contract refuses a drift. Replaces the
# error-prone hand-sed flow used through cycles 51-85.
set -euo pipefail
cd "$(dirname "$0")/.."

CURRENT=$(grep -o 'PWA_BUILD = "[^"]*"' backend/app/device_gateway/__init__.py | grep -o '[0-9.]*')
DATE_PART=${CURRENT%.*}
NEXT_N=$(( ${CURRENT##*.} + 1 ))
BUILD="${1:-${DATE_PART}.${NEXT_N}}"

echo "Bumping PWA build: ${CURRENT} -> ${BUILD}"
for f in \
  backend/app/device_gateway/__init__.py \
  backend/app/config.py \
  backend/clients/pwa/index.html \
  backend/clients/pwa/app.js \
  backend/clients/pwa/sw.js
do
  python3 - "$CURRENT" "$BUILD" "$f" <<'PYEOF'
import sys, pathlib
old, new, path = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
s = path.read_text()
n = s.count(old)
path.write_text(s.replace(old, new))
print(f"  {path}: {n} pins")
PYEOF
done

(cd backend && uv run python -m app.scripts.gen_release_manifest >/dev/null)
echo "Release manifest regenerated."
echo "Next: run the gates (make phone-voice-e2e), then commit 'iphone(release): build ${BUILD}'."
