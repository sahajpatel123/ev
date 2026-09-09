# Selective home-screen icon polish

Only four home controls changed: conversation lines, microphone, pencil, plus.
Visible labels were removed; accessible names and existing event bindings stay.
The microphone switches to a Stop square while connecting or talking. Secondary
targets remain 44 × 44 CSS pixels; Talk is a centered 56 × 56 circle. The page
layout, centerpiece, other labels, and feature behavior were not redesigned.

── REPORT ──

FILES TOUCHED: `backend/clients/pwa/index.html`,
`backend/clients/pwa/app.js`, `backend/clients/pwa/style.css`,
`backend/clients/pwa/release.json`,
`backend/clients/pwa/tests/home_icons_test.js`, and this report.

COMMANDS RUN:

```sh
node --check backend/clients/pwa/app.js
node --test backend/clients/pwa/tests/home_icons_test.js
node --test backend/clients/pwa/tests/presence_motion_test.js
git diff --check -- backend/clients/pwa
# From backend:
.venv/bin/python -m app.scripts.gen_release_manifest
.venv/bin/python -m pytest -q tests/test_release_contract.py
```

MEASURED NUMBERS: 3 icon/accessibility/source-render checks, 6 unchanged
presence checks, and 10 release tests passed. Node crypto SHA-256 comparison
confirmed all 13 generated asset hashes match the current files. Syntax and
whitespace checks passed. Tap dimensions are CSS assertions, not device measurements.

MODELS ADDED: None.
DEP REQUEST: None.
DEPENDENCY NOTES: None for this selective change.
HUMAN APPROVALS: No new approvals, commit, push, or server restart.
WHAT IS STILL NOT REAL: This icon revision has not been visually verified on
a physical iPhone or tested with VoiceOver. Previous live-voice acceptance gaps
remain outside this selective cosmetic change.

Agents: qa-director (read-only accessibility/state review).
