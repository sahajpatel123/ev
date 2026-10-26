# GUI lock (shared by CLI 1 and CLI 4)

One advisory lock serialises everything that touches the real desktop: GUI
apps, tmux sessions, browsers, EventKit, Notes, screenshots, Accessibility.
Two sessions poking Ghostty and Chrome at once produce failures that look like
product bugs, so both take the same lock.

## Protocol

| Item | Value |
| --- | --- |
| Lock | the directory `~/.ev/locks/gui` (`mkdir` is atomic; no file locks) |
| Owner file | `~/.ev/locks/gui/owner`, `key=value` lines: `owner`, `pid`, `started_epoch`, `started_iso`, `purpose`, `max_hold_s` |
| Hold limit | 20 minutes at a time. Release and re-acquire for longer work. |
| Stale | the recorded pid is dead AND the lock is older than 30 minutes |
| Breaking a stale lock | allowed; the breaker logs it (the script does) to `~/.ev/locks/gui-lock.log` |
| Release | `rm -r ~/.ev/locks/gui` (the script also checks you are the owner) |

Pass `--pid` of a **long-lived** process of yours (your CLI session), not the
short-lived shell that runs the script. The default is the parent pid.

## Script

`scripts/gui-lock` (stdlib Python, in the `evie-hands` branch; copy it or call
it by path):

```sh
scripts/gui-lock acquire --owner CLI1 --pid $PPID --purpose "live LP4" [--wait 120]
scripts/gui-lock status [--json]        # exit 0 free, 1 held
scripts/gui-lock release --owner CLI1   # refuses if another owner holds it (--force overrides)
```

Python callers use the same logic: `from app.fabric.skills.guilock import gui_lock`
(context manager; raises `GuiLockBusy` with the holder when held).

Plain-shell equivalent if the script is not at hand:

```sh
mkdir -p ~/.ev/locks && mkdir ~/.ev/locks/gui && \
  printf 'owner=CLI1\npid=%s\nstarted_epoch=%s\npurpose=x\n' $$ $(date +%s) > ~/.ev/locks/gui/owner
...
rm -r ~/.ev/locks/gui
```

## Rules

1. Take the lock before the first real-GUI, tmux, browser, EventKit, Notes or
   screenshot step of a test or a live proof. Offline unit tests need no lock.
2. Hold it for at most 20 minutes. If a run needs more, release between phases.
3. Never delete a lock that is alive or younger than 30 minutes. `status`
   prints `stale` only when both conditions hold.
4. `EV_HANDS_LOCK_DIR` overrides the lock root (tests only).
5. The lock is advisory. It does not stop the owner using the machine; tests
   must still only touch windows, tabs and sessions they created.
