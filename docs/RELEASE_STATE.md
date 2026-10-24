# Release state

## 2026-10-24: captured at the start of the final acceptance run

Commands run in `/Users/sahajpatel/Code/evie-cc` (worktree of the same repository).

| ref | commit | subject |
| --- | --- | --- |
| main | `9e07f72` | merge: acceptance gauntlet harness |
| evie-fabric | `9c2ddba` | fix: finish the fabric release gates with measured proofs |
| evie-acceptance | `33faa69` | docs: CLAUDE.md with the standing rules and decisions for the acceptance run |
| evie-closing | `70eb28b` | feat: add a scripted gauntlet the lanes can actually fail |
| evie-cc (this run) | `1425fb5` | docs(build-log): handoff to Claude Code with measured state |
| pre-fabric-baseline | `bcf1db6` | iPhone relay + mesh handoff, memory tools, presence redesign (58 files) |
| fabric-v1-start | `e3acaff` | fabric: amend report §4 with full-suite delta |

- `git cat-file -t evie-fabric-v1` is `tag` (object `338cfcd`). It points at commit `9e07f72`, the
  current `main`. The 2026-10-19 section below says `16504cd`. The tag moved once more after
  that section was written. This is the third position it has held.
- `git merge-base --is-ancestor evie-fabric main` succeeds: every `evie-fabric` commit is in `main`.
- `main` is an ancestor of `evie-cc`. `evie-cc` is 7 commits ahead: `7b57ab0`, `7eb5158`, `7574930`,
  `fee8906`, `c0cacc5` (from `evie-acceptance`), then `33faa69` and `1425fb5` from this run.
- Production `ev.api` reports git SHA `c0cacc5` (`/v1/health` -> `git.sha`). That commit is not on
  `main`. It was started with `launchctl kickstart` on 2026-10-22 from the main working tree
  (`/Users/sahajpatel/Code/ev`, branch `evie-acceptance`), not through `scripts/deploy_production.sh`.
- The main working tree has 5 modified and 2 untracked macOS files (2026-10-22). They are not part
  of any commit and are left as they are.

Single merged state for this run: all work lands on `evie-cc`. The final step is one no-ff
merge of `evie-cc` into `main`, with `evie-fabric-v1` re-pointed at that merge commit.
That merge and tag are recorded here when they happen.

## 2026-10-19 (historical)


Captured before the acceptance worktree. Commands were run in
`/Users/sahajpatel/Code/ev` on 2026-10-19.

## Writers

`pgrep -fl 'pytest|opencode|claude|codex|gauntlet'` showed only
`opencode serve --service` (pid 1291) and an `opencode` helper (pid 4225).
No Python, Swift, or Markdown file under the repo had changed in the
previous 15 minutes. The daemon was left running. It was not editing this tree.

## Revisions

| ref | commit | subject |
| --- | --- | --- |
| evie-fabric / HEAD at capture | `9c2ddba` | fix: finish the fabric release gates with measured proofs |
| main | `16504cd` | merge: evie-fabric final gates |
| evie-fabric-v1 before this job | `10fd342` | merge: evie-fabric v1 (report provenance) |
| evie-fabric-v1 after this job | `16504cd` | same commit as main |
| pre-fabric-baseline | `bcf1db6` | iPhone relay + mesh handoff |
| fabric-v1-start | `e3acaff` | fabric: amend report |

`git merge-base --is-ancestor evie-fabric main` succeeded. Every
`evie-fabric` commit is in `main`. `main` has four merge commits that are
not on `evie-fabric`, which is what no-ff merges look like.

The local tag `evie-fabric-v1` was deleted (`ce434f4` annotated tag pointing
at `10fd342`) and recreated on `16504cd`. It will move again onto the final
acceptance merge. It was not pushed.

## Worktree

`git worktree add -b evie-closing /Users/sahajpatel/Code/evie-closing evie-fabric`

Acceptance edits happen on `evie-closing` at that path, starting from `9c2ddba`.
