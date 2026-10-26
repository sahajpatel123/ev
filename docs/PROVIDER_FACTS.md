# Provider facts — Muse Spark 1.3 on OpenRouter (target brain)

Verified: 2026-10-26T04:46:00Z (fresh fetch by recon; cached snapshot
2026-10-26 10:02 local matches).
URL: `https://openrouter.ai/api/v1/models` (unauthenticated GET).
Docs: `https://openrouter.ai/docs/guides/features/tool-calling`,
`.../best-practices/reasoning-tokens`, `.../best-practices/prompt-caching.md`,
`.../guides/routing/provider-selection`.

## Model listing (both target ids exist)

| Field | meta/muse-spark-1.3-contributor | meta/muse-spark-1.3 |
| --- | --- | --- |
| Context length | 1,048,576 | 1,048,576 |
| Max completion | 943,718 | 943,718 |
| Input modalities | text, image, video, file | text, image, video, file |
| Output modalities | text | text |
| `supported_parameters` includes | tools, tool_choice, reasoning, reasoning_effort, response_format, structured_outputs, temperature, top_p, top_k, max_tokens | same |
| Reasoning | mandatory=true; efforts max/xhigh/high/medium/low/minimal; default medium | same |
| Prompt $/1M tokens | 0.10 | 1.25 |
| Completion $/1M tokens | 0.20 | 4.25 |
| Input cache-read $/1M | 0.002 | 0.15 |
| top_provider.is_moderated | true | true |

Price ratios (standard ÷ contributor): prompt 12.5×, completion 21.25×,
cache-read 75×. Measured probe cost (standard, minimal harness):
~$0.007/turn typical, $0.033 worst (12-step tool spin).

## Capability notes (from docs above)

- Native function calling with parallel tool calls; `tool_choice` supported;
  tools must be re-sent every turn.
- Reasoning effort is a unified `reasoning.effort` parameter; `mandatory=true`
  means never send `none`. Effort unset ⇒ provider default (medium).
- Prompt caching is mostly automatic with sticky routing; `usage.cached_tokens`
  reports hits; `prompt_cache_key`/`session_id` pins routing.
- Data-collection routing: `provider.data_collection` allow/deny, `provider.zdr`
  (zero data retention) exists as a routing option. Per-endpoint data-policy
  tags for these two ids: UNKNOWN (needs model detail/endpoint pages; listing
  shows only `is_moderated=true`). Whether the serving Meta endpoint retains
  prompts for training: UNKNOWN (OpenRouter tag is best-knowledge, not
  definitive; Meta terms not reviewed). Treat contributor tier as
  may-train-on-prompts (owner's stated assumption; probes used synthetic data).

## What the Evie tree has today

- HEAD (working tree): `PROVIDER_REGISTRY={echo,mock,muse}`
  (`backend/app/gateway/providers.py:602`); `MuseSparkProvider`
  (`backend/app/gateway/openrouter_muse.py`) sends `reasoning.effort`,
  `usage.include`, native tools; defaults `EV_BRAIN_MODEL=
  meta/muse-spark-1.3-contributor`, `EV_BRAIN_REASONING_EFFORT=medium`.
  Native tool calling + parallel calls + effort + caching receipts all
  supported in code. Never deployed (live runs `5cf2947`).
- Live commit `5cf2947`: registry has `openrouter` + `deepseek` (+mimo/xai/
  openai/echo/mock). No `muse` entry, no effort parameter. Proven by probe:
  `EV_CHAT_PROVIDER=openrouter` + `EV_OPENROUTER_MODEL=
  meta/muse-spark-1.3-contributor` runs the kernel on the contributor brain
  settings-only (8/8 turns served; effort = provider default medium).

## To run the kernel on Muse Spark natively (ranked)

1. Settings-only today (live code): `EV_CHAT_PROVIDER=openrouter` +
   `EV_OPENROUTER_MODEL=meta/muse-spark-1.3[-contributor]`. Native tools yes;
   effort/pinning uncontrolled; no `usage.cost` plumbing (see dossier §12.5).
2. Deploy HEAD's `muse` provider: adds `reasoning.effort` (low/med/high),
   `provider.sort`, usage include. Needs the same deploy as everything else.
3. For prompt-cache wins: keep system+tools prefix stable across steps (true
   today — identical re-send observed), add `prompt_cache_key` per session,
   and record `usage.cached_tokens` per call (currently dropped).
4. For ZDR/privacy posture: set `provider.zdr` / `data_collection: deny` and
   verify on the endpoint page; confirm Meta retention terms with the owner.
