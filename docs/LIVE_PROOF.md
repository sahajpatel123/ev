# Live Proofs (evie-fabric final run)

Recorded live runs against real Mac lanes. Each entry: command, output
excerpt, timestamp, verdict. Proofs use `~/Desktop/evie-live-proof/`
(files), harmless `echo` commands (shell), and throwaway lanes only (R2).
The WP15 `make live-proof` harness regenerates this file idempotently.

Host: macOS 27.2 arm64, tmux 3.6b, Ghostty 1.3.1, Python 3.14.6.

## LP1 real file create/edit/undo/delete-to-trash — PASS

2026-10-17T23:30:43Z. Lane calls with root `~/Desktop/evie-live-proof/`:

```text
create: True True 0fda15cf1512
overwrite: True True
undo-overwrite: True True
read-back: 'hello live'
delete-to-trash: True True
undo-delete: True True
final-exists: True
```

Every step verified by read-back (`verified=True`); delete moved to
`.ev-trash/`, both undos restored byte-identical content.

## LP2 long-running shell session survives API restart — PASS

2026-10-17T23:30:55Z (recheck 23:31Z). Process 1 started
`echo LP2-ALIVE && sleep 30` via the lane (tmux-backed); a FRESH process
constructed a new `ShellSessions` (simulating an API restart) and
reattached from the on-disk registry:

```text
started: True sess-ad9fce9b
reattached: True alive: True
stream-has-marker: True
cancelled: True
```

Note: an initial `tail -5` check missed the marker because the pane is
mostly blank lines (marker at top) — a check artifact, not a lane bug;
`stream` shows the marker. tmux server left clean (`no server running`).

## LP3 policy denies secrets and escalates outside-root — PASS

2026-10-17T23:32:29Z. Real lane receipts:

```text
secret-create: secret path denied
secret-read: secret path denied
outside-read: outside roots needs confirm | confirm-hint: True
shell-sudo: denied by policy
shell-touch: confirm required | needs_confirm: True
shell-echo: LP3-OK
```

Denied/parked operations created nothing on disk (`lp3.txt` absent);
the AUTO read ran and returned redacted output.
