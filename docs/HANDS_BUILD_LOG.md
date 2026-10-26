# HANDS build log (CLI 4)

Newest entry last. This log is CLI 4's only. Read the newest entry first after any
compaction. Format per work package: done / failing / assumptions / next (5 lines).

## SETUP — worktree, venv, contracts (2026-10-26)
- done: worktree `/Users/sahajpatel/Code/evie-hands` on branch `evie-hands` from `main` @ bf0412c. `main` is the newest integration base (it contains the evie-acceptance merge; `evie-brain-program` is CLI 1's branch, one WIP commit ahead, not an integration base). Own venv (`uv sync --frozen --extra s3 --extra dev`, exit 0) so the shared editable `.pth` pointing at the main tree is never imported. `scripts/gui-lock` + `docs/contracts/GUI_LOCK.md` (H-I3) smoke-tested in a temp lock dir. `docs/contracts/SKILLS_API.md` v1.0.0 drafted.
- failing: none yet.
- assumptions: CLI 1 is editing `/Users/sahajpatel/Code/ev` (branch evie-brain-program, dirty tree), so I never touch that tree (R1). Python 3.14.6, tmux 3.6b, Ghostty 1.3.1 (native AppleScript: new tab/split/input text/perform action, `working directory` per terminal). `sdef` CLI needs full Xcode here; I read the .sdef files from the app bundles directly. Installed browsers: Chrome, Safari (no Arc). Installed coding CLIs: claude 2.1.310, codex 0.162.1, opencode 2.1.2, muse 1.4.13, gemini 0.45.1 (five different resume syntaxes, so flags are discovered from `--help`, never assumed).
- next: HANDS_RECON.md with measured behavior, then the skills package skeleton (receipts, runtime, guilock), coding-session discovery, layout-tree inference, Ghostty new-tab/split engine.
