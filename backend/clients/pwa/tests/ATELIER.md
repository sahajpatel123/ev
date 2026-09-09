# Atelier — material and craft pass

The owner rejected the visible procedural construction of the breath mark and
asked for a realistic, premium feel without restoring dashboard complexity.
This pass retains the quiet room's four idle actions.

## Decisions and evidence

- Replace all folded-wire drawing with a photographic-style frosted-glass pebble.
  The selected artwork was **generated**, not photographed in the real world.
  Its physical-looking texture, stable edge and single light source are the
  intended improvement. This is not a claim of an AI-free asset or perfection.
- Keep the object rigid and still at idle. Voice amplitude drives a discrete
  five-bar level indicator; connecting/thinking moves a short progress stroke.
  Idle and reduced-motion states run no animation loop. Never imply active
  microphone access just to decorate the home screen.
- Relax display-type tracking; use the same locally available serif family on
  home, pairing and panels. Keep utility text in the system sans serif. Increase
  small descriptions and owner-message text. No downloaded font.
- Give Talk a solid, tactile treatment and a recognizable microphone symbol.
  Use a state-dependent Read/Set aside disclosure, with one small chevron.
- Group tools into Quick actions, Your day, Your memory and Your space. Filtering
  hides empty groups. Internal destinations use chevrons, not external-link arrows.
- Reserve an independent area for session Stop while a panel is open; measured
  bar height determines the panel's bottom edge. It no longer covers list content.
- Simplify pairing copy, put connection diagnostics in a disclosure, and enforce
  separation between explanatory text and the code label.

## Artwork provenance and delivery

Built-in image-generation tool mode; no CLI/API fallback. Final source asset:
[assets/presence-material-v1.webp](../assets/presence-material-v1.webp).

The generated transparent PNG was encoded with the installed cwebp tool, retaining
alpha, at 768×768, quality 88: **31,034 bytes**. It is also embedded in presence.js
so the existing release hash covers it, offline caching includes it, and no new
route or separate request is necessary. The browser test checks byte-for-byte
parity between the embedded image and the source WebP.

Encoding command from the repository root:

```sh
cwebp -q 88 -m 6 -resize 768 0 /Users/sahajpatel/.codex/generated_images/01a07e35-cbbc-7f71-b68e-9ef24b5f97ec/exec-652b6ab1-ee41-47df-a271-fa9f6410d167.png -o backend/clients/pwa/assets/presence-material-v1.webp
```

Final generation prompt:

> Use case: product-mockup. Asset type: a single photographic cutout centerpiece for a premium, minimal iPhone voice companion interface. Primary request: create one physically believable, beautifully crafted small frosted optical-glass pebble, a softly irregular oval, smooth rounded solid object, front three-quarter macro product photograph. A thin softly polished edge, cloudy milk-glass interior, subtle champagne warmth and very faint cool grey refraction. The silhouette is simple, calm, almost a worry stone, wider than tall, with restrained real material microtexture. No hole, no torus, no ribbons, no wires. Lighting: one large softbox from upper left, physically coherent falloff and realistic gentle shadow/reflection at its base; no dramatic lighting or iridescent rainbow. Composition: centered complete object with 20 percent breathing room around, the object fills approximately 60 percent width and 45 percent height of a square canvas. Genuine transparent alpha background; no floor plane, no solid backdrop, no checkerboard painted into image. The object should feel like a real luxury industrial-design sample photographed on a macro lens, not an abstract AI orb or an illustration. No text, no logos, no sparkles, no neon, no floating particles, no UI mockup. Keep fine edges and natural translucency for compositing on ivory and graphite app backgrounds.

## Verification

Run the actual static client with synthetic API, camera and voice-state fixtures:

```sh
node --check backend/clients/pwa/app.js
node --check backend/clients/pwa/presence.js
git diff --check
/opt/homebrew/opt/python@3.14/bin/python3.14 backend/clients/pwa/tests/quiet_room_ui.py
# From backend:
uv run ruff check clients/pwa/tests/quiet_room_ui.py
uv run mypy clients/pwa/tests/quiet_room_ui.py
uv run python -m app.scripts.gen_release_manifest
uv run pytest -q tests/test_release_contract.py tests/test_pwa_audio.py tests/test_iphone_capability_plan.py
```

Screenshots: /private/tmp/evie-atelier-ui. The seven browser configurations cover
375×667 and 402×874 in light/dark, 320×568, landscape 667×375, and a 375×667
doubled-computed-font-size reflow check. This is not an on-device Dynamic Type test.

Checks include material decode, idle animation shutdown, active level response,
idle resize proportions, reduced-motion stability, four idle actions, tool/group filtering, focus return,
modal Stop placement, draft recovery, reply disclosure, camera denial/cancellation,
theme metadata, and text-palette contrast of at least 4.5:1. Contrast checks cover
the named palette pairs, not a complete accessibility certification.

Final browser result: seven configurations passed. Minimum measured palette
contrast: 5.01:1 in light mode and 6.43:1 in dark mode. A stalled direct browser
Promise wait in the camera-denial fixture was replaced by an instrumented,
10-second bounded completion check; the full run then passed.

Focused backend regression result: 68 passed. Full repository suite not run.
No production deployment, commit, push, dependency-manifest or backend-engine edit.

## What is still not real

On-device Safari keyboard/safe-area and VoiceOver behavior, actual permission
dialogs, real voice playback, rendering energy use, and the owner's subjective
perception of richness still require physical-device review. These fixture
screenshots do not establish those outcomes. Artwork is generated; it is not
a captured photograph.
