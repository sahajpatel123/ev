# Live Proofs (evie-fabric final run)

Recorded live runs against real Mac lanes. Each entry: command, output
excerpt, timestamp, verdict. Proofs use `~/Desktop/evie-live-proof/`
(files), harmless `echo` commands (shell), and throwaway lanes only (R2).
The WP15 `make live-proof` harness regenerates this file idempotently.

Host: macOS 27.2 arm64, tmux 3.6b, Ghostty 1.3.1, Python 3.14.6.

## LP1 real file create/edit/undo/delete-to-trash — PASS

2026-10-18T22:09:13Z.

```text
create: True True 0fda15cf1512
overwrite: True True
undo-overwrite: True True
read-back: 'hello live'
delete-to-trash: True True
undo-delete: True True
final-exists: True
```
Every step is the files lane receipt. verified is true only after a read-back.
duration: 0.3s

## LP2 long-running shell session survives API restart — PASS

2026-10-18T22:09:13Z.

```text
started: True sess-433c5b08
reattached: True marker: True
```
The second ShellSessions is a new object. The on-disk registry is the only memory, which is what an API restart has.
duration: 0.1s

## LP3 policy denies secrets and escalates outside-root — PASS

2026-10-18T22:09:13Z.

```text
secret-create: secret path denied ok=False
secret-read: secret path denied ok=False
outside-read: outside roots needs confirm confirm=True
shell-sudo: denied by policy
shell-touch: confirm required needs_confirm=True
shell-echo: LP3-OK
secret-file-absent: True
```
duration: 0.0s

## LP4 Ghostty/tmux real layout — PASS

2026-10-18T22:09:13Z.

```text
backend: ghostty+tmux
open: True sess: ev-layout-3c1aab5d
readback-a: True | readback-b: True
save-spec: True
```
Socket ev-liveproof is a private tmux server. The owner's default server is not killed.
duration: 6.3s

## LP5 layout restore from saved spec — PASS

2026-10-18T22:09:19Z.

```text
server-killed: ev-liveproof (isolated socket, not the default server)
loaded: True
reopen: True
restored-a: True | restored-b: True
```
duration: 2.6s

## LP6 screenshot-to-layout loop — PASS

2026-10-18T22:09:22Z.

```text
open: True
markers: True
capture: True path-exists: True
artifact: method=geometry panes=2 conf=0.35 untrusted=True event=5d33ec61-a8b8-46ff-a78f-6a50ba61faea
saved: True loaded: True reopen: True
restored-a: True | restored-b: True
restore-source: saved spec; geometry did not supply a layout plan
```
duration: 7.0s

## LP7 scratch SQL DML preview/commit/rollback and DDL snapshot — PASS

2026-10-18T22:09:29Z.

```text
sqlite
ddl: True
seed: True
preview: True rolled_back=True
still-a: [('a',)]
commit: True
now-b: [('b',)]
ddl-snapshot: True
postgres
ensure: True
preview: True rolled_back=True
rows-after-preview: [('0',)]
commit: True
rows-after-commit: [('x',)]
```
SQLite file /Users/sahajpatel/Desktop/evie-live-proof/lp7.db. Postgres container ev-liveproof-pg is removed after the proof.
duration: 4.2s

## LP8 real EventKit reminder lifecycle in a throwaway list — PASS

2026-10-18T22:09:33Z.

list-create: ok=True verified=True err=None
reminder-create: ok=True verified=True err=None
reminder-list: ok=True verified=True err=None
read-back: created id present = True
reminder-complete: ok=True verified=True err=None
completed flag after read-back: True
list name resolved on the row: 'Evie Live Proof'
kernel path: pim.act/reminder_delete through run_fabric_tool
lane delete: ok=True backend=eventkit
gone after lane delete: True
cleanup: list deleted ok=True
duration: 1.8s

## LP9 real Notes lifecycle in a throwaway folder — PASS

2026-10-18T22:09:35Z.

folders: ok=True verified=True err=None
note-create: ok=True verified=True err=None
note-list: ok=True verified=True err=None
read-back: created id present = True
note-delete: ok=True verified=True err=None
gone after delete: True
duration: 0.6s

## LP10 Builder job (toy spec, internal loop) — PASS

2026-10-18T22:09:35Z.

```text
plan: True job-350ed039
apply: True state=done
tests: {'passed': 1, 'failed': 0, 'parsed': True}
resume: done
```
Internal loop only. No coding-agent CLI is invoked, so this does not spend a provider quota.
duration: 0.6s

## LP11 PWA + phone panel + pairing + typed turn over real HTTP — PASS

2026-10-18T22:09:36Z.

isolated API healthy on :18001 (scratch DB, EV_ENV=development)
GET /evie -> 200
GET /evie/evie-phone.js -> 200, bytes match tree = True
POST pairing-tokens -> 200
POST /pair -> 200; device token issued = True
GET /v1/fabric/status (device token) -> 200; fabric_enabled=True
GET /v1/fabric/approvals/pending -> 200; count=0
POST /v1/device-gateway/text -> 200; route=None; reply chars=58; ok=None
POST /v1/fabric/cancel -> 200; ok=True
duration: 10.1s
