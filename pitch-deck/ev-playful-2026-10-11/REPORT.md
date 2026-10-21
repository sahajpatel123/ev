# EV — Keep the why

The proposed seed ask is now **$7,000** in the editable PowerPoint and PDF. The original headline size, placement, color and approved composition were preserved. Slides 1–4 are SHA-256-identical to the approved prior render set in both PPTX reimport and PDF page renders.

The speaker-note budget now follows the retained native chart allocation: Product 55% / $3,850; Reliability 25% / $1,750; Pilots and operations 20% / $1,400. These add to $7,000. The annual-license proposal, indicative program duration and proof milestones retain their previous wording. Pricing, financing and milestones remain proposals; no funded runway is guaranteed.

Both final files are in `output/`. The complete existing cutout prompts remain in `.build/rebuild-assets.json`; the current authoring source is `.build/rebuild.mjs`. All text, diagrams, connectors and the allocation chart remain editable. The chart includes an embedded workbook snapshot. Percentage overlays are native editable text derived from the same authoring data; update them when changing chart values directly in PowerPoint.

The final fifth slide and its PDF page were visually reviewed for the wider amount. The complete PDF was scanned, and automated render comparisons prove the other four pages are unchanged. Package integrity, geometry, font policy, native chart/workbook checks and artifact-tool reimport pass. Microsoft PowerPoint itself was not opened. Authoring fonts remain Avenir Next, Georgia and Marker Felt; PDF fonts are embedded with Unicode mappings, including the existing arrow glyph fallback.

── REPORT ──

FILES TOUCHED: Only `pitch-deck/ev-playful-2026-10-11/**`, listed in `INVENTORY.txt`: authoring source, QA expectations, final delivery files and private validation/render/report artifacts. Superseded private intermediate entries are recorded in `.build/seed-cleanup.json`. The set of 15 pre-existing modified tracked application paths is unchanged. No application source, settings, dependencies, database, deployment state or Git history was modified by this edit. The two previously deleted older deck folders remain absent, and only the latest PPTX and PDF remain in output.

COMMANDS RUN: These authoring, PDF conversion, page rendering and QA commands passed with exit 0. The PPTX and PDF edit-operation markers each ran once this turn. Guarded task-local Python updated the authored ask and note allocations, copied the finalized outputs, verified hashes, cleaned obsolete intermediates and compared tracked paths. Read-only inspections used `rg`, `cat`, `sed`, `git diff --name-only` and `view_image`. No backend lint/typecheck/test was run for this artifact-only edit; no CI claim is made.

```sh
RUNTIME_NODE_MODULES=/Users/sahajpatel/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules /Users/sahajpatel/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node pitch-deck/ev-playful-2026-10-11/.build/rebuild.mjs seed7000 > pitch-deck/ev-playful-2026-10-11/.build/build-seed7000.log 2>&1
FONTCONFIG_FILE=/Users/sahajpatel/Code/ev/pitch-deck/ev-playful-2026-10-11/.build/fonts.conf /Users/sahajpatel/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override/soffice --headless --convert-to pdf --outdir /Users/sahajpatel/Code/ev/pitch-deck/ev-playful-2026-10-11/.build/pdf-seed7000 /Users/sahajpatel/Code/ev/pitch-deck/ev-playful-2026-10-11/output/EV_Keep_The_Why_seed7000.pptx
pdftoppm -scale-to 1280 -png pitch-deck/ev-playful-2026-10-11/.build/pdf-seed7000/EV_Keep_The_Why_seed7000.pdf pitch-deck/ev-playful-2026-10-11/.build/pdf-seed7000/page
/Users/sahajpatel/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 pitch-deck/ev-playful-2026-10-11/.build/qa.py > pitch-deck/ev-playful-2026-10-11/.build/qa-seed7000.log
```

MEASURED NUMBERS: 5 slides, 5 PDF pages, 88 authored editable text boxes, 6 native connectors, 1 native chart, 1 embedded workbook, 5 speaker-note parts and 5 embedded RGBA cutouts. Allocation 55% + 25% + 20% = 100%; $3,850 + $1,750 + $1,400 = $7,000. Eight unchanged-page render hashes match the approved source set. Finalizer: 0 geometry findings and 0 warnings. PPTX: 10,974,258 bytes; PDF: 1,826,660 bytes. PPTX SHA-256: `ecb6cbba9be886581c2331ce747c4bb9f22015980cf757ac66c9559f3eeb4536`. PDF SHA-256: `21a818c9882b3765dc76d85e60ce8f2d98c9ec30a4aa5b569e24cfeca1fbc1f8`. QA confirms the new ask in the PDF and PPTX, updated amounts in notes, and no former ask or budget amounts in delivered text.

MODELS ADDED: None. Existing design cutouts were reused; no images or product weights were generated/downloaded for this edit.

DEP REQUEST: None. Existing bundled Node, Python, LibreOffice and system Poppler were used.

DEPENDENCY NOTES: Root owns task artifacts and updates. `art_direction` supplies focused read-only typography and export review. No numbered product-owner dependency.

HUMAN APPROVALS: The user explicitly requested the seed amount replacement. No additional approval was required. No commit, push, external message, publication or deployment occurred.

WHAT IS STILL NOT REAL: Financing, paid demand, renewals and milestone attainment remain unverified proposals. The retained duration is an indicative program and does not establish funded runway. Generated imagery and example conversations remain illustrative. Native Microsoft PowerPoint rendering was not directly tested; artifact-tool reimport and LibreOffice/PDF were inspected.

Agents: art_direction.
