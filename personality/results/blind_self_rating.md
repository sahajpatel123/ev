# Blind self-rating (CLI 6, truly blind)

Method: `/tmp/blind_rate.py` shuffled the 20 pairs (seed 20261028) and
hid the v2.1/v4 labels (A/B only). Picks were made on the sheet with no
knowledge of the mapping, then scored. One pass, no revisions.

**Result: v2.1 14/20 (70%).**

Picks:

| # | Pair | Picked | Winner |
|---|---|---|---|
| 1 | hinglish-bug | A | v2.1 |
| 2 | insult-slow | B | v2.1 |
| 3 | opinion-side-project | A | v2.1 |
| 4 | banter-font | A | v2.1 |
| 5 | candor-framework | A | v4 |
| 6 | error-build-red | A | v4 |
| 7 | opinion-job | A | v4 |
| 8 | hinglish-tired | B | v2.1 |
| 9 | morning-brief-first | B | v2.1 |
| 10 | wrong-earlier-deploy | A | v2.1 |
| 11 | night-nudge-late | A | v2.1 |
| 12 | error-send-failed | A | v4 |
| 13 | night-accept | A | v2.1 |
| 14 | opinion-mvp | B | v4 |
| 15 | confirm-migrate | A | v2.1 |
| 16 | frustrate-object | B | v2.1 |
| 17 | confirm-delete | B | v2.1 |
| 18 | night-decline | B | v2.1 |
| 19 | code-works-why | B | v4 |
| 20 | win-shipped | A | v2.1 |

Reading: v2.1 won on wit with rescue (pairs 1, 4, 11, 16, 18) and on
plain exactness (12 went v4 on a coin-flip; 15, 17 v2.1). All 6 v4
picks are depth cases — fuller diagnosis, tradeoff, or apology (6, 7,
14, 19) plus two coin-flips (5, 12). Same signal as the AI judges: v2.1
is the sharper friend, and still sometimes cedes depth on hard
problems. Notable: pair 2's v4 reply said "Wednesday lag" on a Tuesday
scenario — an unflagged baseline invention I caught by eye.
