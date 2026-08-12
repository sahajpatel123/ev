# PEOPLE (ROSTER) — consented face recognition, public-figure biodata

**Owner:** Agent 7 · **Scope:** `backend/app/people/**`, `backend/app/ev/people.py`,
`backend/app/api/people.py`, `backend/alembic/versions/a7c0d0c0d7a1_people_roster.py`,
`tests/test_people_recognition.py`, `tests/test_people_biodata.py`.

The ROSTER feature gives EV the *legal* EDITH moment: when the owner shows a
photo or live frame of a person who **enrolled with consent**, EV can say who
they are. Public figures resolve from Wikidata/Wikipedia by name with
attribution. Everyone else — every face that is not an enrolled template —
resolves to `unknown`, **by construction**.

---

## The line that is architectural, not advisory

1. **Enrollment requires a named person and a recorded consent record.**
   `POST /v1/training/consent` with `track: "face_enrollment"` must be granted
   before `POST /v1/people/enrollments` will write a template. The enrollment
   service enforces this (`require_consent`), and a test proves a template is
   never written without it.
2. **Strangers stay strangers.** Recognition only compares against enrolled
   templates. A below-threshold crop returns `unknown: true` and writes no
   recognition log and no entity resolution. There is no code path that
   attempts to identify a non-enrolled person.
3. **Face templates are biometric data.** Mean templates and per-sample
   templates are Fernet-encrypted at rest (scrypt-derived key from
   `EV_MASTER_KEY`, same primitive as voiceprints) and are destroyed by the
   per-person erasure path.
4. **Public-figure biodata is public and attributed.** Wikidata (CC0) and
   Wikipedia (CC BY-SA 4.0) are queried **by name**, cached with source URLs
   and license strings on every field, and never merged into a private person
   record without explicit human confirmation (`POST /v1/people/{name}/biodata/link`).

## Enrollment

```bash
# 1. Consent (one time, explicit)
curl -X POST http://localhost:8000/v1/training/consent \
  -H "Authorization: Bearer $EV_MASTER_KEY" \
  -d '{"track":"face_enrollment","purpose":"face enrollment from owner photos"}'

# 2. Enroll from >=5 aligned crops (YuNet crops from Agent 6)
curl -X POST http://localhost:8000/v1/people/enrollments \
  -H "Authorization: Bearer $EV_MASTER_KEY" \
  -d '{"person_name":"Ada","photos":[{"image_b64":"...","quality":0.92,"confidence":0.98}, ...]}'
```

Or via CLI:

```bash
cd backend
EV_FACE_PROVIDER=sface uv run python -m app.people.cli enroll \
  --name "Ada" --photos ~/photos/ada/ --quality 0.92 --confidence 0.98 \
  --grant-consent --reason "owner-provided photos with consent"
```

Enforcement:

- Minimum 5 photos per person (`EV_FACE_MIN_PHOTOS`).
- Low-quality or low-confidence crops are refused
  (`EV_FACE_QUALITY_FLOOR`, `EV_FACE_CONFIDENCE_FLOOR`).
- The mean template and every per-sample template are encrypted before they
  touch the database. Raw photos are never stored.
- Re-enrollment creates a new version; the previous current version is
  superseded but retained for audit/rollback.

## Recognition

```bash
curl -X POST http://localhost:8000/v1/people/recognize \
  -H "Authorization: Bearer $EV_MASTER_KEY" \
  -d '{"image_b64":"...","quality":0.9,"confidence":0.97,"source":"live_frame"}'
```

- Cosine match against **enrolled** mean templates only.
- Threshold is ROC-calibrated, not guessed:
  `POST /v1/people/calibrate` (or the CLI `calibrate` command) computes
  TAR/FAR from labeled same-person and different-person trial pairs with the
  active embedder. The report includes the full ROC curve and the threshold
  chosen for the target FAR.
- Model matches write `RecognitionLog(source="model")`. They are **pending**
  until the human confirms: `POST /v1/people/recognitions/{id}/confirm`
  flips it to `source="user"`. Corrections are recorded in the command ledger
  and act as training signal.
- Below threshold → `unknown: true`, no log, no identity attempt.

### Embedder providers

| Provider | Engine | At-rest | Degraded |
| --- | --- | --- | --- |
| `hash` (default) | deterministic signed-hash embedding | encrypted | `degraded=true` — dev/test only, never passed off as face recognition |
| `sface` | OpenCV Zoo SFace ONNX (Apache-2.0, LFW 0.9940, 128-dim output, 37 MB) | encrypted | falls back to the deterministic double + `degraded=true` when weights are missing |

`sface` is registered with the ModelArbiter as `face-sface`
(on-demand tier, 37 MB resident, never resident alongside ASR; loaded
per-use and evicted after inference). It consumes **aligned crops from
Agent 6's YuNet detector**; ROSTER deliberately has no detector of its own.
Pinned artifact: `face_recognition_sface_2021dec.onnx`
sha256 `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79`
(Hugging Face LFS oid), Apache-2.0, 38,696,353 bytes. The embedding
dimension is read from the ONNX output at load time (128 for the 2021dec
model), never assumed.

> **DEPENDENCY NOTE — Agent 2 (FOUNDRY):** add the `face-sface` row to the
> locked roster in `docs/MODEL_BUDGET.md` (on_demand · 37 MB resident · 37 MB
> disk · Apache-2.0). The runtime registry entry is already pinned and
> verified, and `Makefile` already exposes `uv sync --extra face`.

## Evaluation status (ROC + rejection)

Offline production-path run (deterministic `hash` embedder, SQLite, 5 enrolled
people × 10 crops, held-out stranger set):

- ROC-calibrated threshold: `1.0` at FAR `1e-3`
- TAR at that operating point: `1.0` (100%)
- Genuine pairs: 225 · Impostor pairs: 200 · ROC points: 4
- Non-enrolled faces rejected: **50/50 (100%)** — hard gate satisfied offline
- `degraded: true` — this is the development double, not SFace

The **real acceptance gate** (TAR ≥ 95% at FAR = 1e-3 on ≥5 consented people
× ≥10 real photos, held-out, plus 50 real non-enrolled faces) is wired and
ready: `POST /v1/people/calibrate` (or the CLI) computes the ROC from the
active embedder, and `--apply` persists the calibrated threshold into every
enrollment. It awaits the owner's consent-approved photo set, which only the
human can provide.

Live checks already passing (2026-08-11):

- **SFace real inference:** `face-sface.onnx` downloaded and sha256-verified
  (37 MB, Apache-2.0); synthetic-crop smoke test returns `provider=sface`,
  `degraded=false`, normalized 128-dim embedding, and the model is evicted
  from the on-demand slot immediately after inference.
- **Public-figure biodata (live):** 10/10 names resolved from Wikidata +
  Wikipedia with per-field attribution — Ada Lovelace, Marie Curie, Alan
  Turing, Rosa Parks, Albert Einstein, Frida Kahlo, Nelson Mandela, Jane
  Austen, Nikola Tesla, Katherine Johnson. Occupations and notable works are
  Wikidata `CC0` (item URL attached), summaries are Wikipedia `CC BY-SA 4.0`
  (article URL attached), dates resolve, `degraded=false`, and TTL caching
  returns `cached=true` on repeat lookups.

### Running the acceptance gates

One command runs the whole gate with a proper held-out split:

```bash
cd backend
EV_FACE_PROVIDER=sface uv run python -m app.people.eval \
  --people-dir ~/roster/people \
  --strangers-dir ~/roster/strangers \
  --quality 0.9 --confidence 0.95 --grant-consent \
  --report eval/roster-last-run.json
```

Layout: `people/<Name>/*.jpg` (≥10 aligned crops per consented person) and
`strangers/*.jpg` (≥50 non-enrolled faces). The harness enrolls the first
`N-2` photos per person, calibrates the threshold on a training half of the
score pairs, reports TAR/FAR on the held-out half, and then pushes all 50
strangers through the production resolver. The report includes both ROC
curves and the two acceptance booleans (`acceptance_tar_met`,
`acceptance_rejection_met`). Harness mechanics are covered by
`tests/test_people_eval.py` (synthetic embedder, clearly not face
recognition).

## Public-figure biodata

`GET /v1/people/{name}/biodata` resolves by name:

1. Wikidata SPARQL (`P106` occupations, `P800` notable works, `P569`/`P570`
   dates) — CC0, item URL attached to every field.
2. Wikipedia REST summary — CC BY-SA 4.0, article URL attached.
3. Cached with a TTL (`EV_BIODATA_TTL_SECONDS`, default 7 days), including
   source URLs and license strings, so attribution survives cache hits.

Biodata is never merged into a private person automatically. The explicit
`POST /v1/people/{name}/biodata/link` is the only merge path, and it requires
owner-level trust.

## Unified person view

`GET /v1/people/{name}/whereabouts` fuses, each with provenance/confidence:

- text mentions and related memories (existing behavior)
- enrolled identity (`enrolled`: current active `FaceEnrollment` metadata —
  never ciphertext)
- face sightings (`face_sightings`: pending `model` + confirmed `user`
  recognition log rows)
- voice-identified moments (`voice_sightings`: recognition log + voice events)
- public biodata (`public_biodata`, `biodata_merged` only after explicit link)

## Erasure

`DELETE /v1/people/{entity_id}` destroys:

- `RecognitionLog` rows for the person,
- `FaceSample` rows (per-sample encrypted templates),
- `PublicFigureCache` rows linked to the person,
- marks every `FaceEnrollment` `deleted`, redacted, ciphertext/salt nulled
  (audit rows remain),
- writes a `DataErasureRecord` manifest entry and an access-log entry.

The whole ROSTER biometric sweep is exposed as
`app.people.erasure.erase_all_face_biometrics(...)` so Agent 19 (VAULT) can
include face templates in the global biometric erasure sweep.

> **DEPENDENCY NOTE — Agent 19 (VAULT):** the biometric erasure sweep in
> `backend/app/compliance/erasure.py` currently covers voiceprints and training
> corpus only. Call `app.people.erasure.erase_all_face_biometrics(session,
> reason=..., actor=...)` from `erase_biometric_data` and add face templates to
> the retention policy (`compliance/policy.py`, new category `FACETEMPLATE`),
> then verify end-to-end. ROSTER's per-person `DELETE /v1/people/{id}` is
> already self-contained and tested.

## Honest limits

**What this cannot do**

- It cannot identify strangers, and it will never try. Non-enrolled faces are
  `unknown` by construction; there is no “find who this is” path.
- It cannot enroll someone without consent, and it refuses low-quality or
  low-confidence crops.
- Without real SFace weights and the owner's consent-approved photo set, the
  embedder degrades to a deterministic development double with
  `degraded=true`; those results are not face recognition.
- Without a real ROC run (≥5 people × ≥10 photos, held-out), the threshold is
  an explicit placeholder (`EV_FACE_THRESHOLD`), not a calibrated value.

**What it will not do**

- No bulk scraping, no third-party people-search APIs, no personal-data
  harvesting. Public figures come from Wikidata/Wikipedia by name only.
- No ambient/raw stranger scanning, no automatic merging of public-figure
  records into private people.
- No plaintext biometric vectors at rest. Cosine matching happens in-process
  after decrypting enrolled templates (mirroring the voiceprint design);
  pgvector was deliberately not used for templates because it would require
  plaintext vectors.

**How to delete everything**

1. Per person: `DELETE /v1/people/{entity_id}` (or
   `python -m app.people.cli erase-person --entity-id ...`).
2. All face biometrics: revoke `face_enrollment` consent and run the global
   erasure sweep once Agent 19 wires `erase_all_face_biometrics`.
3. Purge backups that contain the manifest-referenced enrollment IDs; the
   erasure manifest marks `backup_purge_required=true`.
