# Evie Tailnet policy

Single-owner Tailnet posture for the Home Station Mac + 2 iPhones. This is the
previously missing piece behind the `acl_note` string in
`backend/app/device_gateway/tailscale.py`: phones may reach the Home Station's
Serve HTTPS port **only** — never Postgres, Redis, RQ, or helper sockets.

## What the policy says

- `tag:evie-home` — the Mac running `ev.api` on 127.0.0.1:8000 + `tailscale
  serve --https=443`.
- `tag:evie-phone` — the 2 owner iPhones (16 Pro + SE).
- One ACL: phones → home `:443`, accept. Everything else is denied by default
  (no phone↔phone path: mesh coordination goes through the server; push goes
  through APNs).
- No `ssh` section: Tailscale SSH stays disabled tailnet-wide.
- Funnel is not part of the policy file (it is per-node state) and must stay
  **off** — the backend refuses to apply Serve while Funnel is on.

## Apply

1. Paste `tailnet-policy.json` into the Tailscale admin console
   (Access controls → Edit policy file) and save.
2. Tag the nodes (owner terminal on the Mac, Tailscale app on each iPhone shows
   the node name):
   `tailscale set --advertise-tags=tag:evie-home` on the Mac, then in the
   admin console assign `tag:evie-phone` to both iPhones.
3. Verify from a phone on cellular (Wi-Fi off): `https://<home>.ts.net/evie/`
   loads; `https://<home>.ts.net:5432/` (or any non-443 port) does not.

## Guardrails

- `backend/tests/test_tailscale_policy.py` locks the invariants: valid JSON,
  no accept-all rule, every accept targets home:443 from phone tags only, no
  SSH section, no Funnel reference.
- Never add a rule with `dst` ports other than 443, or `src` wider than
  `tag:evie-phone`, without updating that test and this file together.
