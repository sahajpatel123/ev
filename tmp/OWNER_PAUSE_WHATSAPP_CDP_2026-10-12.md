# OWNER PAUSE — WhatsApp CDP headless Chrome (2026-10-12)

## Reason
Phantom "Chrome opened" detection. Root cause was Evie's dedicated headless
Chrome (ex-PID 55565, `--headless=new`, `--user-data-dir=~/.ev/chrome-cdp`,
`--remote-debugging-port=9222`, `https://web.whatsapp.com/`), up ~22h since
Oct 11 19:42, PPID 1, invisible in Dock. Owner needs Chrome back under manual
control with no background automation for now.

## Cancel / do not do while paused
- Do NOT call `ensure_running` / `ensure_ready` / `send` / `search_chats` /
  `read_recent` in `backend/app/ev/messaging/whatsapp_cdp.py`.
- Do NOT run `scripts/whatsapp_cdp_supervise.py`.
- Do NOT install or `load` `launchd/ev.whatsapp_cdp.plist` (verified NOT loaded;
  only on-demand calls can relaunch).
- Do NOT start QR re-link. Park WhatsApp sends; report `cdp_paused_by_owner`.
- Check `~/.ev/whatsapp-cdp.PAUSED` — if present, stay off.

## What was done (owner-directed, no code edits)
- `pkill -TERM -f "Google Chrome.*chrome-cdp"` — all chrome-cdp procs gone.
- Verified: `pgrep -i chrome` empty, `curl 127.0.0.1:9222/json/version` refused,
  no `LISTEN` on 9222. Default Chrome profile untouched.
- `ev.api` / `talk` / `scheduler` / `worker` / `collector` left running.
  No commits, no deploys, no migrations, no plist loads/unloads.
- Markers: `~/.ev/whatsapp-cdp.PAUSED` + this file.
- Stale `SingletonLock -> ...-55565` left as-is; Chrome recreates on next launch.

## Resume
Owner says resume. Then delete `~/.ev/whatsapp-cdp.PAUSED` + this file and run
the supervisor once.
