# E.V. (E.V.I.E.) — Research Findings & Feature Selection

**Version 1.0** — compiled from 2026 *Spider-Man: Brand New Day* press coverage,
released script pages, director notes, the Marvel Cinematic Universe wiki, and
TechTimes' science analysis. Every selected feature is traceable to a source.

## 1. What E.V. is

E.V. (E.V.I.E.), voiced by Naomi Watts, is Peter Parker's **self-created AI
assistant** — built without Stark tech, in a small apartment, "a kid genius with
limited funds." Director Destin Daniel Cretton's script note calls her "sadly, the
closest thing Peter has to a friend." Sources: EW-released script pages (via
TheDirect, Cosmic Book News, Fast Company, Nerdist), TechTimes.

## 2. Capability inventory (with sources)

| # | Capability | What the film shows | Sources |
| --- | --- | --- | --- |
| 1 | Self-built companion | Entirely Peter's own creation; no Stark hand-me-down | Looper, SlashFilm, TechTimes |
| 2 | Conversational companion | Primary conversational partner; fills an emotional gap | TechTimes, SlashFilm |
| 3 | Suit + workbench integration | Runs in the suit and at his home workstation | ComingSoon, TheDirect |
| 4 | HUD in mask lenses | Information displayed in the eye lenses like Karen | Marvel Fandom wiki |
| 5 | Targeting calibration | "Check the calibration on my targeting matrix" | Fast Company, Cosmic Book News |
| 6 | Body scan / vitals | "Body scan shows organic webs, heightened senses… increased agility"; monitors biosignals | ComingSoon, TheDirect, TechTimes |
| 7 | Web-shooter telemetry | Monitors web-shooter systems/data | Koranmanado, SekBerNews, ComicBasics |
| 8 | Combat analysis | Analyzes tactical scenarios; times bomb explosions; calculates distances; deciphers Jean's mental-control range (10.5 m) in seconds | vsbattles analysis, ComingSoon, TechTimes |
| 9 | Police-scanner alerts | Flags crime as it happens; monitors police scanners | Sojourners, ComingSoon, NetflixJunkie |
| 10 | City-scale facial recognition | Scans NYC camera infrastructure to locate Jean Grey | TechTimes |
| 11 | Spider-sense supplements | Timely alerts that supplement natural spider-sense | SlashFilm, EasternHerald |
| 12 | Research assistance | Helps with scientific research | SlashFilm, EasternHerald |
| 13 | Navigation | Assists with street navigation in New York | Koranmanado, ComicBasics |
| 14 | Emotional/psychological insight | Points out Peter is isolated and losing what makes him human; ties body state to trauma/stress | ComicBook.com |
| 15 | Diagnostics/calibration | Runs numbers, calibrations, analysis | Marvel Fandom wiki, script |
| 16 | Fabricator | Homemade fabricator patches the suit; enhanced 3D printer | TheDirect, Digital Spy |
| 17 | Less intrusive than Karen | Present and useful, but Peter is "left to his own devices" | Polygon |
| 18 | Real-world power wall | Wearable LLM needs 5–15 W; batteries hold 40–100 Wh; Snapdragon Wear Elite runs ~2B models; neuromorphic (IBM NorthPole, Intel Loihi 2) is the trajectory | TechTimes |
| 19 | Companion-dependence risk | MIT/OpenAI 2025 RCT: heavy AI-companion use correlates with loneliness and dependency | TechTimes (affective computing research) |
| 20 | Surveillance law | City-scale biometric scanning is illegal under GDPR Art. 9, EU AI Act, Illinois BIPA | TechTimes |

## 3. Predecessor lineage (bonus features)

| AI | Features | Our analog |
| --- | --- | --- |
| Karen | Training Wheels Protocol, 576 web-shooter combinations, optimal tactical selection, threat analysis, intimidation mode | Skill/mode recommendations; "calibrate for this situation" |
| E.D.I.T.H. | AR glasses, drone network, global tactical network, auto-identify people nearby, target designation | HUD-ready schemas; person finder over user-owned data; permissioned action cards |
| J.A.R.V.I.S./F.R.I.D.A.Y. | Home/lab automation, diagnostics, scientific analysis | Workbench dashboard + self-diagnostics |

## 4. Feature selection (what we build)

Ethical boundary: **no city-scale facial recognition, no biometric scanning of
strangers, no surveillance of other people.** We adapt each cool capability to a
single-user, permissioned, local-first product:

| E.V. capability | EV product feature | Notes |
| --- | --- | --- |
| Body scan / vitals | **Health radar** — readiness score (sleep, HRV, resting HR, activity), z-score anomaly detection, morning brief | HealthKit-style data via API now; real HealthKit later |
| Web-shooter telemetry | **Gear telemetry** — device battery/storage/status, "calibrate" diagnostics endpoint | Real device status via clients |
| Targeting calibration | **Calibration diagnostics** — `/v1/diagnostics/calibrate` for retrieval, embeddings, gateway latency | E.V.'s "check the calibration" line |
| Combat analysis | **Tactical mode / HUD briefings** — structured `ev.hud.briefing.v1` with risks, options, decision history; "combat math" helpers (distances, timing, ranges) | No literal combat; high-stakes situations |
| Police-scanner alerts | **Alert radar** — watchlist over user's own events (deadlines, topics, people, projects) with priority + digest + quiet hours | Permissioned sources only |
| City-scale facial recognition | **Person finder** — locate a person entity across *user-owned* memory: last seen, relationships, related events | Explicitly not camera scanning |
| Spider-sense supplements | **EV Sense** — predictive alerts with intervention scoring and "why now?" rationale | Pattern-based, inspectable |
| Research assistance | **Research assistant** — sessions, sources, citations | Memory-grounded; optional web |
| Navigation | **Route briefings** — next-event route/leave-by cards | Apple Maps later |
| Emotional insight | **Anti-dependency companion guardrails** — isolation detector suggests human connection; transparency about AI | Based on the MIT/OpenAI loneliness research; we deliberately build against dependence |
| HUD | **HUD-ready schemas** — `ev.hud.card.v1` / `ev.hud.briefing.v1` rendered on Watch/widget/web | AR later |
| Fabricator | **Maker companion** — projects, BOM, print queue | OctoPrint adapter later |
| Self-built ethos | **Local-first, self-hosted, swappable gateway** | Already the architecture |
| Less intrusive | **Attention budget** — intervention thresholds, quiet hours, digest | Polygon's "less intrusive than Karen" |

## 5. Science side-notes (context, not features)

- Wearable edge AI is real but power-limited: Snapdragon Wear Elite (2B on-device),
  IBM NorthPole (<1 ms/token on 3B), Intel Loihi 2/Hala Point — informs a future
  "local brain" milestone.
- Epigenetics ("spider puberty") maps to real stress-response science; our health
  radar tracks stress-readiness correlations, not mutations.
- Affective computing research (MIT Media Lab/OpenAI 2025) is the strongest argument
  for our anti-dependency guardrails: EV should actively preserve real-world
  relationships, not replace them.

## 6. Research addendum (2026-08-09 second pass)

### 6.1 Confirmed naming

- The full name is **E.V.I.E.**, shortened to **E.V.** (People, Yahoo! Entertainment,
  IMDb news — all 2026-08-03/04 after Naomi Watts publicly confirmed the role).
- Watts' own Instagram line — *"From playing your mom to playing your AI (100% human
  made)"* — plus the script note *"All of his tech needs to have been made by Peter"*
  (Nerdist, 2026-04-28) reinforce the **self-built ethos**: EV's personality and
  architecture must never depend on a vendor's model.

### 6.2 Emotional insight is plot-relevant, not flavor

- E.V. explains Peter's mutation as his body reacting to trauma and emotional
  distress; seeing Ned and MJ again made him realize how alone he was
  (ComicBook.com, 2026-08-01). This maps directly to our **isolation guardrail**:
  the assistant should notice isolation and steer toward human connection.

### 6.3 Karen's Baby Monitor Protocol (predecessor feature worth adapting)

- Karen records suit data continuously and lets Spider-Man review information he
  previously overlooked; she can also **suggest and activate features without an
  explicit command** (Spider-Man Suit MCU wiki, Karen profile).
- EV analog: the **memory timeline + audit trail** is the Baby Monitor Protocol;
  **EV Sense** is the "suggest without command" layer. A concrete addition:
  `POST /v1/continue` reconstructs what the user was doing so nothing is
  overlooked, and tool selection routes intent automatically.

### 6.4 E.V.'s full surface (TheDirect, 2026-07-31)

- E.V. is in the suit **and** at the home workbench beside the homemade fabricator,
  providing info/assistance during crime-fighting and personal life — the
  suit+workbench split is our iOS/Watch + Mac/web/CLI split.
- The Polygon lineage piece (2026-08-01) frames E.V. as the latest in the
  Karen → E.D.I.T.H. → E.V. tradition; the "less intrusive than Karen" design goal
  is our attention-budget policy.

### 6.5 Source URLs for this addendum

- people.com/naomi-watts-reveals-her-top-secret-role-in-spider-man-brand-new-day-12033956
- nerdist.com/article/spider-man-brand-new-day-script-reveals-peter-survives-without-stark-tech
- comicbook.com/movies/feature/why-spider-man-was-mutating-in-brand-new-day
- thedirect.com/article/marvel-studios-naomi-watts-mcu-role
- polygon.com/spider-man-brand-new-day-ai-assistant-ev-karen-edith
- spiderman-films.fandom.com/wiki/Spider-Man_Suit_(MCU_Films) (Karen profile)
