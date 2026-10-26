# evhands — Accessibility and pointer helper for HANDS

One native Swift file. AX attribute reads cost microseconds here; the same walk through
System Events JXA costs seconds. Used by `app/fabric/skills/ax.py` for pane geometry
(Ghostty split ratios), `ui.find` / `ui.act` and trusted pointer fallbacks.

```sh
swiftc -O -o ~/.ev/hands/bin/evhands Sources/evhands/main.swift   # what the skills package does on demand
swift build -c release                                            # or via SwiftPM
evhands trusted          # {"ok":true,"trusted":true}
evhands panes Ghostty    # terminal pane frames + text
evhands find "Google Chrome" --role AXButton --name '^skip( ad)?$'
evhands act "Google Chrome" --path 0.4.2.1 --do press
```

Permissions are those of the process that launches it (the terminal while developing,
Evie.app when launched through the bridge). Without Accessibility it prints
`permission_missing` with the exact Settings path; it never guesses.
