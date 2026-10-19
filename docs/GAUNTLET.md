# Gauntlet

`ev gauntlet` runs a generated corpus through the real fabric lanes. The
planner in `backend/gauntlet/brain.py` is generic: verbs, filenames, quoted
text, and folder names. It does not read case ids or oracles.

`backend/gauntlet/corpus.py` builds the cases. Oracles are attached when the
case is constructed, before `run_case`. DEV uses one sentence frame and seed
range. FRESH uses a different frame. About 70% of cases are DEV.

Worlds are temporary directories with Desktop, Documents, Downloads, Code,
and Projects. They are deleted after every case. A leftover directory raises.

```sh
cd backend
uv run ev gauntlet --split dev --limit 60
make gauntlet-smoke
make gauntlet
```

Scripted pass@1 is not a model score. Real-brain runs are sampled when a
full pass would exceed 25% of the monthly cost cap.
