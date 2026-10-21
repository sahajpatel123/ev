# Release state

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
