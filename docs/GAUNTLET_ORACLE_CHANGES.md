# Oracle changes

No oracle was edited to match a failing result.

`git -C <dir> log` was classified CONFIRM because the read-only git check
only matched `git log` at the start of the command, and it compared `-C`
against a lowercased command. The classifier now accepts a leading `-c`
path on read-only git verbs. The novel-case oracle still expects the commit
subject `initial` in the command output.
