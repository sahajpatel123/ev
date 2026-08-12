EV FLEET LAW — binding on all 20 agents

1. WORKTREE. Shared worktree /Users/sahajpatel/Code/ev. Never commit, push,
   rebase, force-push, stash, or revert unless the human explicitly orders it.
   Leave a reviewable working tree.

2. EXCLUSIVE PATHS. Edit only paths in your OWNS list. Editing outside OWNS
   invalidates your entire report. Need something outside it? STOP, and write a
   DEPENDENCY NOTE naming the agent number who owns it.

3. SHARED APPEND-ONLY FILES. Makefile, .env.example, compose.yaml,
   docs/ENVIRONMENT.md, app/config.py, app/models.py, app/schemas.py, and
   app/api/{core,ev,edith,companion,tools}.py are shared. Rules:
     - Append inside a block marked  # --- AGENT <N> <CODENAME> ---
     - Never modify, reorder, reformat, or delete another agent's lines.
     - Never change an existing endpoint signature, table column, or setting
       default. Additive only.

4. DEPENDENCIES ARE AGENT 2 ONLY. Do not touch pyproject.toml or uv.lock.
   Put every package you need in a DEP REQUEST line in your report, with the
   reason and the wheel size. Import it lazily and guard it so the suite still
   passes before Agent 2 lands it.

5. MIGRATIONS. Set down_revision to the Alembic head that existed when you
   started. Never edit another agent's migration. CONDUCTOR linearizes the chain
   at merge. CREATE EXTENSION vector runs on PostgreSQL only — SQLite upgrades
   must stay clean.

6. API CONTRACT IS ADDITIVE-ONLY. backend/eval/contract_v1.json locks 261
   operations. Never hand-edit it. New endpoints are fine; changed or removed
   ones are not. CONDUCTOR regenerates via make update-contract.

7. OFFLINE CI IS SACRED. The full suite must pass on a laptop with no API keys
   and no model weights downloaded. Therefore:
     - Every real engine degrades to a deterministic double when weights are
       absent, and sets degraded=true on its result.
     - Tests that need weights use pytest.mark.skipif — skip, never fail.
     - At least one test must exercise the REAL factory entry point. Never
       reimplement production logic inside a test.

8. NO LYING IN CODE. Banned outright:
     - Fabricated confidence values.
     - Falling back to echoing the caller's own input as if it were a result.
     - Trusting a client-supplied security claim (liveness, hashes, scores).
     - Silent application of trained weights or filter policy.
   If you cannot make something real on this hardware, say so in the report.
   An honest gap is a passing grade; a disguised stub is a failing one.

9. RESOURCE BUDGET. Read docs/MODEL_BUDGET.md. Every model you add registers
   with the ModelArbiter declaring name, license, source_url, sha256, disk_mb,
   resident_mb, peak_mb, tier. Exceeding your allocation is rejected work.
   Never load a model outside the arbiter. Never download without a checksum.

10. ETHICS. Owner-consented data only. Public datasets must be license-checked
    and license-recorded. No stranger identification. No ambient raw media to
    any model. No dependence loops or fabricated intimacy.

11. BANS. No multi-user or guest mode. No new product domains ("Domain 20").
    No public exposure of API ports. No AR hardware as a hard goal.
    No nested clients/** ownership.

12. REPORT FOOTER IS MANDATORY. No footer means the work is incomplete
    regardless of quality.

13. INFERENCE TOPOLOGY. Reasoning runs through a hosted API (DeepSeek). No
    agent may place a local LLM on a required path. Small local models are
    permitted and preferred ONLY where an API is impossible or clearly worse:
    wake word (continuous mic), OCR (Apple Vision, free), speaker verification
    (biometric privacy), face embedding. Everything else uses an API provider
    behind the existing seam. Every remote path must pass a
    remote_processing_allowed() gate.
