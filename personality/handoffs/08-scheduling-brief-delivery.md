# DEPENDENCY NOTE 08 — to the scheduling/notifications owner (from CLI 6)

## Morning-brief delivery + overnight push policy

- Deliver the `MorningBrief` (schema: `app/persona/nightshift.py`) on the
  owner's first message after 06:00 local, or at `wake_time` when set —
  whichever comes first.
- Overnight, push a notification to the owner only for "on fire" events:
  risk of data loss, or a runaway process. Everything else waits for the
  brief. The sign-off line ("phone stays quiet unless something's on
  fire") must stay true.

No persona import needed on your side; the brief arrives as the
`MorningBrief.to_dict()` payload. Acceptance: one delivery path, quiet
overnights, and the brief's `waiting_for_you[]` items surface as tappable
confirmations in the morning.
