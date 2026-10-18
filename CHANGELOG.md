# Changelog

## evie-fabric (unreleased)

The fabric is on the live path. Conversational turns go through one facade
(`app.fabric.facade`). Effect lanes (files, shell, terminal, screen, SQL,
comms, personal data, builder, atlas, operator) sit behind one policy
engine, return verified receipts, and append to a hash-chained audit log.
`EV_FABRIC=0` restores the legacy brain. `ev panic` denies effects until
the owner clears it.

- WP0–WP1: wiring audit, then the facade at the kernel call sites. Fifteen
  `/v1/fabric/*` routes added. The original 550 contract paths are unchanged.
- WP2: file and shell fuzz, panic, audit hash chain.
- WP3: authenticated local bridge, `ev setup`, `ev doctor --fabric`.
- WP4: Ghostty 1.3.1 AppleScript dictionary (an earlier "no scripting
  surface" probe was wrong) plus tmux for layout, capture, and restore.
- WP5–WP7: screenshot-as-reference, app atlas, operator loop, recipes, SQL
  classifier including a throwaway Postgres lane.
- WP8–WP9: comms confirm cards, EventKit and Notes, builder jobs.
- WP10–WP11: `clients/ears` is the mic owner; phone panel on the PWA
  (approvals, upload, stop, status) over Tailscale Serve.
- WP12: acknowledgement is the pre-model SSE `status/accepted` event.
  First-token time is measured separately and is not reported as the
  acknowledgement. The 1500 ms budget is unchanged.
- WP13–WP14: failing tests on this branch must be a subset of the
  `pre-fabric-baseline` full run, with a cause for each one that remains.
  Settings added on this branch are in `.env.example` and
  `docs/ENVIRONMENT.md`.

- WP15: gates measured on this tree — eval 25/25 (160/160 checks), contract
  568 paths with the original 550 byte-identical, `ruff` 11 findings (none
  new), `mypy` 26 errors in 4 files (none new), full suite 5 036 passed /
  115 failed / 34 skipped with **one triaged order-coupled regression and 67
  tests fixed** versus the pre-fabric baseline (`docs/TEST_DEBT.md`), and
  LP1–LP11 live proofs.

Defects fixed while proving the above: WhatsApp sends called methods that live
on the adapter's backing (every linked send failed with an opaque error);
`FLEET_TOOL_SPECS` was dispatched but never published, so Home Station timers
answered `Unknown tool 'start_timer'`; `/v1/chat` lost attachment perception
provenance, the provider seam, the filter report and the 503 contract when
rerouted by `EV_FABRIC` alone; phone attachment text could carry instructions
until turns were tainted end to end.

See `docs/OWNER_SETUP.md` for the owner flow and `docs/EVIE_DEPRECATIONS.md`
for the canonical module of each duplicate cluster.
