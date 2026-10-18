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

## LP4 Ghostty/tmux real layout — PASS

2026-10-18T00:21:37Z. Hybrid layout (real Ghostty window + tmux session,
2 echo panes) via `GhosttyLayout.open(confirm=True)`:

```text
open: True win: tab-group-7c98addea0 sess: ev-layout-26c13b9a
readback-a: True | readback-b: True
save-spec: True
close: True
```

The Ghostty window was visible during the run and closed after; tmux
read-back verified both pane markers; the spec was saved as `lp4-live`.

## LP5 layout restore from saved spec — PASS

2026-10-18T00:23:17Z. Default tmux server verified empty first, then:

```text
open: True ev-layout-e2c21127
server-killed
reopen: True
restored-a: True | restored-b: True
close: True
```

`tmux kill-server` between open and restore; both echo markers verified
after the restore; server left clean (`no server running`).

## LP6 screenshot-to-layout loop — PASS

2026-10-18T00:45:18Z. Real Ghostty window (2 echo panes) → per-terminal
dump markers → full-screen bridge capture → OCR artifact → close:

```text
open: True
visible-markers: True
capture: True 850619 bytes
artifact: geometry panes: 5 conf: 0.35 ocr-lines: 31 untrusted: True
close: True
```

Geometry inference is honestly capped (0.35 here); the 5 bands cover the
whole screen (menu bar, notifications included), which is why restore
plans come from the known layout spec, not from artifact bands. No VLM
keys on this host, so the VLM branch stays scripted-test-only.

## LP7 scratch SQL (SQLite + throwaway Postgres) — PASS

SQLite 2026-10-18T06:26:40Z (`~/Desktop/evie-live-proof/lp7.db`):

```text
ddl: True
seed: 1
preview: True affected: 1 rolled_back: True
still-a: True
commit: True affected: 1
now-b: True
ddl-snapshot: True
```

Postgres 2026-10-18T06:29:22Z (throwaway `ev-liveproof-pg` container, removed after):

```text
ensure: True
ddl: True
preview: True affected: 1 rolled_back: True
rows-after-preview: [('0',)]
commit: True
rows-after-commit: [('x',)]
```

Previews verify rollback (sqlite: data re-read; postgres: dump-hash compare
with random session tokens stripped); DDL snapshots recorded for restore.
