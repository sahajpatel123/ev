# KEY_STATUS — which key the brain actually loads (M0, 2026-10-28)

## The variable and the source

- Every brain path reads `settings.openrouter_api_key`, i.e. env **`EV_OPENROUTER_API_KEY`**.
- The value does NOT come from the shell or any repo `.env`: importing
  `app.config` runs `_load_production_secret_overlay()`, which injects
  **`~/.ev/secrets/production.env`** (chmod 0600, outside the repo) into
  `os.environ` before pydantic reads anything. Keys already in the process
  environment win; repo `.env` files carry no key (`.env.api-first` has only a
  FILL-ME placeholder; `backend/.env` does not exist).
- The bare shell var `OPENROUTER_API_KEY` (no `EV_` prefix) is **never read by
  any backend path**. It is a different, capped key — the source of the 403s.

## Numbers (GET /api/v1/key with the backend-loaded key)

| Field | Backend key (`~/.ev/secrets/production.env`) | Bare shell key (decoy) |
|---|---|---|
| label (masked) | `[REDACTED]...bc5` | `[REDACTED]...05a` |
| limit | 10 (USD) | 0.001 (USD) |
| limit_remaining | **0.1016** | 0 |
| limit_reset | None | None |
| usage | 9.8984 | 0.0077 |

Eval key (`EV_OPENROUTER_EVAL_KEY`): **UNSET** — every suite would spend the live key.

## Verdict

The brain is reachable but nearly broke: **~$0.10 left on the live production
key**. Round A and Round B must be run lean (small prompts, minimal tokens,
counted calls), or not at all until funded.

## Single action for the owner

On the OpenRouter Keys page: **raise the credit limit on the `[REDACTED]...bc5`
key** (or replace it with the funded key in `~/.ev/secrets/production.env`),
and set a separate `EV_OPENROUTER_EVAL_KEY` so evals stop spending the live key.
