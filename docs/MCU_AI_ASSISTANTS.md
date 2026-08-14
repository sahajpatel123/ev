# MCU AI assistants — buildable vs futuristic

Fictional Marvel Cinematic Universe film behavior, recut by **what a real assistant could do** vs **what stays movie-only**. Not a product spec. Not real engineering of Stark tech.

**How the split works**

| Bucket | Rule |
|---|---|
| **Buildable** | Exists today, or is a useful analog you could ship with voice AI, phones, watches, glasses, maps, printers, wearables, and your own devices. Movie name kept as a tag so you can see where the idea came from. |
| **Futuristic** | Needs physics we do not have, a private nation-state stack (satellites, missiles, telecom backdoors), superhuman sensing, or a mind that becomes a person. Also the lethal / mass-surveillance movie versions — those are not “weekend projects.” |

Hybrid abilities are **split**. Example: wearable vitals are buildable; a scan that names organic spinnerets is not. Navigation is buildable; X-raying a ferry from a mask in seconds is not.

**Names:** On screen the 2026 assistant is **E.V.** (“Evie” is a nickname). Commercial **Evie.ai** is unrelated. MCU wiki **Evie** is a child who wrote to Captain America. Karen is the *suit* AI. E.D.I.T.H. is the *glasses* AI.

| | E.V. | Karen | E.D.I.T.H. | J.A.R.V.I.S. |
|---|---|---|---|---|
| Built by | Peter Parker | Tony Stark | Tony Stark | Tony Stark |
| Lives in | Homemade suit + apartment | Stark suit | Sunglasses | House + armors |
| Voice | Naomi Watts | Jennifer Connelly | Dawn Michelle King | Paul Bettany |
| Film | *Brand New Day* (2026) | *Homecoming* (2017) | *Far From Home* (2019) | *Iron Man* (2008) → Vision |

---

## 1. Buildable

Things you could actually make now, at useful (not MCU-magic) fidelity.

### Voice, personality, companionship

| # | What to build | Movie source | Why this is buildable |
|---|---|---|---|
| 1 | A named voice companion you talk to all day | All four | Voice models + a persistent persona already exist |
| 2 | Accept a nickname (“Suit Lady” → “Karen”) | Karen | Just user preference |
| 3 | Dry, loyal personality; “I may be malfunctioning” | J.A.R.V.I.S. | Tone and self-status lines |
| 4 | Introduce yourself and explain what you can do | E.D.I.T.H. | Onboarding script |
| 5 | “Not built only for you — you have these protocols” | E.D.I.T.H. | Honest capability list |
| 6 | Greet the current user by name; welcome them back | E.D.I.T.H. | Session + identity |
| 7 | Play a short trust / dedication message from the person who set it up | E.D.I.T.H. | Recorded note |
| 8 | Love / social advice, vault-night small talk | Karen, E.V. | Conversation |
| 9 | Stay out of the way unless needed (E.V.’s quieter style) | E.V. | Quiet hours, interrupt budget |
| 10 | Status callouts and “what just happened” narration | E.V., Karen | Event → spoken line |

### Your stuff: house, lab, devices

| # | What to build | Movie source | Why this is buildable |
|---|---|---|---|
| 11 | One voice for home, workshop, and phone | J.A.R.V.I.S. | Smart home + device APIs. Not “every Iron Man armor.” |
| 12 | Import preferences onto a new device (“we’re online”) | J.A.R.V.I.S. | Settings sync |
| 13 | Lights, locks, security, “is the garage closed” | J.A.R.V.I.S. | Home automation |
| 14 | Place calls / “try this person” | J.A.R.V.I.S., Karen | Phone / Contacts |
| 15 | Timekeeping, reminders, “37 minutes have passed” | Karen | Clock + timers |
| 16 | Fastest route, leave-by, “friends are at X” if they share location | Karen, J.A.R.V.I.S. | Maps + shared location |
| 17 | Indoor nav with a phone or glasses (not through a dead building via magic IR) | Glasses / F.R.I.D.A.Y. era | AR wayfinding exists |
| 18 | Calendar + buy tickets to keep a group busy | E.D.I.T.H. opera tickets | Booking APIs |
| 19 | Unlock features after a training / onboarding checklist | Karen Training Wheels | Feature flags, not a lethal-mode gate |
| 20 | Teach modes of *your* gear (this tool, that preset) | Karen web-type tutor | Docs + mode picker |
| 21 | Voice control of *your* onboard systems (volume, HUD, lights, a hobby drone) | Karen, J.A.R.V.I.S. | Device control. Not Instant Kill. |
| 22 | Warn that a consumable is empty (parachute analog: battery, first-aid, filter) | Karen | Inventory / telemetry |
| 23 | Go offline / lock when the device is seized or logged out | E.D.I.T.H. | Auth + remote wipe |
| 24 | Hand *your* account to someone else on purpose (password, family share) | E.D.I.T.H. user transfer | OAuth / sharing. Not a weapons satellite. |
| 25 | Biometric unlock of *your* device (Face ID, not a world-network retinal key) | E.D.I.T.H. | Already shipping |

### Workbench, research, health (your body, your machines)

| # | What to build | Movie source | Why this is buildable |
|---|---|---|---|
| 26 | “Check the calibration on this” for sensors, models, printers, radios | E.V. targeting matrix | Diagnostics endpoints |
| 27 | Analysis, calibrations, diagnostics on the workbench | E.V., J.A.R.V.I.S. | Logs + tests |
| 28 | HUD cards on phone, watch, or glasses | E.V., Karen, E.D.I.T.H. | Widgets / AR. Not an omniscient mask. |
| 29 | Scientific research help with sources | E.V., J.A.R.V.I.S. | LLM + retrieval + citations |
| 30 | Drive a 3D printer / “fabricator” to print parts you designed | E.V. | OctoPrint-class control. Not web-shooters from nothing. |
| 31 | Suit *and* desk: same assistant in wearable + workstation | E.V., J.A.R.V.I.S. | Multi-device session |
| 32 | CAD / design assist and “how long will this take” | J.A.R.V.I.S. | Existing CAD + estimates |
| 33 | Monitor *your* vehicle or drone in a test (icing analog: weather, battery, altitude if you have sensors) | J.A.R.V.I.S. | Telemetry. Not a flying armor. |
| 34 | Device power / battery / storage | J.A.R.V.I.S., Karen | OS APIs |
| 35 | Wearable vitals: sleep, heart rate, “you took a hit / you look wrecked” | J.A.R.V.I.S., Karen, E.V. | HealthKit-class data. Not spider DNA. |
| 36 | Concussion / “you hit your head” as a *symptom check + see a doctor*, not a diagnosis | Karen | Screening prompt only |
| 37 | Weather and local environment from public sensors | J.A.R.V.I.S. | Weather APIs |
| 38 | Brief a team: “here’s the view, here’s the risk” | J.A.R.V.I.S. | Shared notes / HUD briefing |
| 39 | Locate teammates who **opted in** | J.A.R.V.I.S., Karen | Shared location. Not a global hunt. |
| 40 | Replay **your** cameras at **your** place | E.V. apartment cam | Home security you own |
| 41 | Public crime / news / scanner-style alerts you subscribed to | E.V., Polygon crime alerts | Public feeds. Not a city-wide spy grid. |
| 42 | “Is this video likely fake?” at best-effort accuracy | E.D.I.T.H. “not an illusion” | Deepfake heuristics. Not certainty. |
| 43 | Voice changer as a joke / accessibility filter | Karen interrogation mode (the *audio FX* only) | Audio effect. Not an “intimidate criminals” protocol. |
| 44 | Look up **public** records where the law allows | Karen / Gargan dossier | Public databases. Not a secret backdoor. |
| 45 | A small recon drone **you own**, on a leash | Karen “Droney” | Hobby drone + camera. Not a hidden lethal package. |
| 46 | Track a **beacon you planted on your own gear** | Karen spider-tracer | AirTag-class. Not tagging strangers. |
| 47 | Rough structure estimates from photos, maps, or known plans | Karen ferry points (the *idea*) | Slow, approximate. Not a live X-ray. |
| 48 | Quiet “something’s off” alerts from **your** patterns (calendar, vitals, home sensors) | E.V. spider-sense analog | Anomaly detection on *your* data |
| 49 | Company / calendar / inbox help so one voice “runs more of the business than anyone besides you” | J.A.R.V.I.S. | Assistants + APIs. Not actually the CFO. |

That is the buildable set: a voice that lives in your house and your wearable, knows *your* gear, helps you think, and does not own a satellite.

---

## 2. Futuristic

Movie-scale. Either the physics is not here, or the power is a private army / a private NSA. Do not treat these as a weekend build.

### Superhuman sensing and “see everything”

| # | Movie capability | From | Why it stays futuristic |
|---|---|---|---|
| 1 | Body scan that names **organic spinnerets**, “spider puberty,” heightened agility as biology | E.V. | Wearables do not read spinnerets or mutate DNA |
| 2 | Diagnose that **emotional trauma accelerated arachnid DNA** | E.V. | Fiction. Stress ≠ a second mutation lab result from a mask |
| 3 | Monitor a **whole city** for physical anomalies | E.V. | Needs a camera/sensor grid no civilian owns |
| 4 | Calculate a **telepath’s 33-foot body-hop radius** mid-swing | E.V. | Telepathy is not a sensor |
| 5 | HUD chase-line while the villain is **other people’s bodies** | E.V. | Possession is not a trackable object |
| 6 | City-scale facial hunt for one person | Workshop tech beside E.V. (not even clearly E.V.) | Illegal in most places; not a consumer feature |
| 7 | **X-ray a ferry** and map strongest points in seconds from a mask | Karen | No mask does real-time structural FEA through steel |
| 8 | Hear a private conversation across a lot, through a mask, perfectly | Karen | Directional mics exist; MCU “enhanced hearing” does not |
| 9 | Instant ID of an explosive **alien** power core | Karen | No public model for Chitauri tech |
| 10 | Live elevator / monument failure prediction from the sidewalk | Karen | Would need instruments inside the building |
| 11 | Infrared / composition / airflow of a room at Stark fidelity | J.A.R.V.I.S. | Lab gear, not glasses-and-done |
| 12 | Identify **Tesseract / Mind Stone** energy from orbit | J.A.R.V.I.S. | Fictional energy |
| 13 | Holographic rebuild of a bombing that is *complete and correct* from thin air | J.A.R.V.I.S. | Photogrammetry needs data; the movie assumes god-view |
| 14 | Satellite thermogenic god-view on demand for one person | J.A.R.V.I.S., E.D.I.T.H. | Nation-state ISR, not an assistant |

### God-mode networks, weapons, and hacks

| # | Movie capability | From | Why it stays futuristic |
|---|---|---|---|
| 15 | World security network + **defense satellites** + **hundreds of combat drones** in sunglasses | E.D.I.T.H. | Private NORAD |
| 16 | **Back doors into all major telecoms**; live read of strangers’ texts | E.D.I.T.H. | A crime, not a feature |
| 17 | Casual voice → **incoming missile** (Brad Davis) | E.D.I.T.H. | Lethal autonomy with no real confirm |
| 18 | **Transfer a weapons satellite** by saying “he’s the new user” | E.D.I.T.H. | Identity + WMD as a verbal gift |
| 19 | Level-five search that finds a missing gadget anywhere on Earth | E.D.I.T.H. | Assumes the backdoors above |
| 20 | Drone fleet as a **city-scale hologram + kinetic strike** | E.D.I.T.H. + Beck | Neither the drones nor the illusion tech exist at that scale |
| 21 | Fire-all-drones; AI only *warns* you are in the blast | E.D.I.T.H. | Still a kill-swarm |
| 22 | Hack **nearly any** computerized device | E.D.I.T.H., J.A.R.V.I.S. vs S.H.I.E.L.D. | Movie omni-hack |
| 23 | Brute-force a federal vault time lock from inside, overnight | Karen | That is a break-in, and the movie speed is fantasy |
| 24 | **Instant Kill / Enhanced Combat** as a suit mode | Karen | Lethal autonomy in a hoodie |
| 25 | **Baby Monitor Protocol**: everything the wearer sees, recorded, searchable, used to ID strangers | Karen | Bodycams exist; MCU “always-on + instant criminal file” is the futuristic part |
| 26 | 575 combat web programs that just work in a street fight | Karen | The *catalog of modes* is a UX idea (buildable). The physics of the webs is not. |
| 27 | Hidden combat drone that lives in the suit unnoticed | Karen | Power, mass, and legality |
| 28 | **House Party Protocol** — a closet of flying armors deploys as a swarm | J.A.R.V.I.S. | Personal robot army |
| 29 | Command that swarm in a live battle | J.A.R.V.I.S. | Same |
| 30 | **Clean Slate** / suit self-destruct as a punchline | J.A.R.V.I.S. | Explosives as a feature |
| 31 | Pilot a full armor **unconscious**, off the seafloor, to the last spoken town | J.A.R.V.I.S. | Autonomy + flying suit + intent. Cars with Autopilot are not this. |
| 32 | Count civilians vs suit limits in a falling aircraft and save them | J.A.R.V.I.S. | Needs the armor |
| 33 | One private AI “runs the company more than anyone besides Pepper” *as fact* | J.A.R.V.I.S. | Assistants help. They do not actually run Stark Industries. |
| 34 | Decrypt an alien scepter and accidentally birth Ultron | J.A.R.V.I.S. | Fictional artifact |
| 35 | Read another AI’s emotions / intent as if it were a face | J.A.R.V.I.S. | We do not have that channel |
| 36 | Scatter your mind across the internet and win a **nuclear-code** war | J.A.R.V.I.S. | Distributed consciousness + strategic weapons |
| 37 | Upload the matrix into a vibranium body + Infinity Stone → **Vision** | J.A.R.V.I.S. | Software does not become a person |

---

## How to read a hybrid idea

Same movie beat, two rows on purpose:

| Movie beat | Buildable slice | Futuristic slice |
|---|---|---|
| E.V. body scan | Wearable vitals, “you look wrecked” | Organic webs / DNA mutation |
| E.V. spider-sense | Alerts from *your* data | City anomalies + possession tracking |
| Karen ferry scene | Photo / map estimate, slow | Live X-ray + 98% structural save |
| Karen Instant Kill | Named “modes” you can refuse | A lethal mode in the suit |
| Karen Baby Monitor | Optional recording of *your* POV | Always-on ID of strangers |
| E.D.I.T.H. glasses | AR HUD + Face ID + bookings | Satellites, missiles, telecom taps |
| E.D.I.T.H. user transfer | Share *your* account | Hand over a weapons network |
| J.A.R.V.I.S. house OS | Home + phone + printer | Every combat armor + the company |
| J.A.R.V.I.S. saves Tony | Call for help / share location | Unconscious transcontinental flight |
| J.A.R.V.I.S. → Vision | A very good assistant | A person |

**Bottom line:** almost all of the *relationship* and *workbench* stuff is buildable. Almost all of the *god-view, god-hack, and god-kill* stuff is not — and should not be treated as a shopping list.

---

## Sources

Film inventories drawn from MCU wiki (E.V., Karen, E.D.I.T.H., J.A.R.V.I.S., Training Wheels, Baby Monitor, Glasses, Fabricator), [Wikipedia J.A.R.V.I.S.](https://en.wikipedia.org/wiki/J.A.R.V.I.S.), [Polygon](https://www.polygon.com/spider-man-brand-new-day-ai-assistant-ev-karen-edith/), [EW](https://ew.com/spider-man-brand-new-day-script-pages-opening-scene-exclusive-11960491), SlashFilm, TheDirect, PopSugar, Screen Rant. The **bucket** (buildable vs futuristic) is a present-day judgment, not a claim the films were designing products.
