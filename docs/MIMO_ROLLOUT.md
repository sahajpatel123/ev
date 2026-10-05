# MiMo rollout — Evie's non-speech brain

Owner of this document: Agent 10 CORTEX (gateway).

Evie runs exactly two models. `xiaomi/mimo-v2.6-flash` (OpenRouter) is the
single non-speech brain: typed chat, kernel turns, coded decisions, code
jobs. `gemini-3.8-live-extended-thinking` is speech and hearing. Gemini-decides: Gemini
answers speech directly and calls `delegate_task` for medium-high work,
which MiMo does. There is no fallback brain; offline doubles (`echo`,
`mock`, `hash`) keep the suite green with no keys.

## 1. Architecture (as implemented)

```
owner ask/voice transcript
  -> Gemini Live answers directly, or calls delegate_task
  -> MiMo kernel turn (chat_with_tools over the allowlisted tool bus)
  -> validated result (deterministic handler + policy + audit)
  -> spoken/written result (receipt, or Gemini mouth for voice)
```

| Lane | Config | Model | Provider | Notes |
| --- | --- | --- | --- | --- |
| Brain (text) | `EV_CHAT_PROVIDER=mimo` + `EV_MIMO_ENABLED=true` | `xiaomi/mimo-v2.6-flash` | `openrouter` | OpenAI-compatible chat-completions; typed `DecisionQuestion`s via `decide_via_role` |
| Speech | `EV_VOICE_LIVE_BRAIN=gemini` (or `auto` + key) | `gemini-3.8-live-extended-thinking` | `google` | raw WebSocket `BidiGenerateContent`; paid tier only |
| Perception | existing local engines | ASR/TTS/OCR/wake/vision | local | pixels stay local; only derived text reaches the brain |

Resolver: `backend/app/gateway/roles.py` (`resolve_text_brain`,
`resolve_code_brain`, `resolve_voice_mouth`, `text_role_available`,
`chat_via_role`, `chat_structured_via_role`, `decide_via_role`).

### Provider transport (`backend/app/gateway/openrouter_mimo.py`)

`MimoProvider` subclasses the shared `OpenAICompatibleProvider`:
`POST {base_url}/chat/completions`, SSE streaming, `image_url` media
parts, `complete_raw` for the coding loop's dict round-trip. Before a
request can use the configured API key, the provider requires
`EV_MIMO_ENABLED=true`, `remote_processing_allowed("chat_egress")`, a
valid OpenRouter key, and an allowing provider circuit. The revocable
`chat_egress` consent record is checked too; when absent the call
proceeds but logs a warning so the gap stays visible. OpenRouter usage
and `usage.cost` are preserved as `openrouter_reported`; if usage is
absent, the model-call logger records a conservative estimate instead
of treating the call as free.

## 2. Verification

```bash
cd backend
uv run pytest tests/test_mimo_provider.py tests/test_gateway_api.py \
  tests/test_gateway_unit.py tests/test_gateway_streaming.py -q
uv run python ../scripts/smoke_mimo.py     # exits 2 (SKIP) until the key lands
uv run python ../scripts/dogfood_mimo.py   # one real kernel turn, opt-in
```

## 3. History

Supersedes the JEV-era rollout record (deleted 2026-10-04; JEV, Muse
Spark/Voice, GPT Realtime, Grok, DeepSeek chat, local chat, and opencode
providers removed from the tree — see git history). The two-model cut
kept every offline double and local perception engine: `make test` with
no keys stays green.
