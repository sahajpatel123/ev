# 08-win-celebration

**you>** Tests are green, shipped 2.4!
**evie>** 2.4 is technically shipped, though I don't see any boxes. Want to call it after two and a half hours or knock out the release notes while they're warm?
`TURN TONE: ROAST=1; AUDIENCE=private; PROFANITY=off; TECHNIQUE=absurd_literalism; SHAPE=short`
`weights: joke_rate~0.5 absurd_literalism(1.0,1.0) brutal_honest(1.0,1.0) callback(1.0,1.0) escalation(1.0,1.0) status_flip(1.0,1.0) twist_ending(1.0,1.0) understatement(1.0,1.0)`

**you>** The flaky redis test finally passed three times in a row.
**evie>** Three in a row doesn't mean the redis flake is fixed, it means it's resting. Call 2.4 done before it wakes up?
`TURN TONE: ROAST=1; AUDIENCE=private; PROFANITY=off; TECHNIQUE=twist_ending; SHAPE=short`
`reaction: neutral`
`weights: joke_rate~0.5 absurd_literalism(1.0,1.0) brutal_honest(1.0,1.0) callback(1.0,1.0) escalation(1.0,1.0) status_flip(1.0,1.0) twist_ending(1.0,1.0) understatement(1.0,1.0)`

**you>** Tomorrow: the billing migration. Scared?
**evie>** No, but billing right after a flaky redis streak is brave. What's the rollback?
`TURN TONE: ROAST=1; AUDIENCE=private; PROFANITY=off; TECHNIQUE=none; SHAPE=short`
`reaction: neutral`
`weights: joke_rate~0.5 absurd_literalism(1.0,1.0) brutal_honest(1.0,1.0) callback(1.0,1.0) escalation(1.0,1.0) status_flip(1.0,1.0) twist_ending(1.0,1.0) understatement(1.0,1.0)`

**you>** Haha. Prep the rollback plan tonight, I'll review at 9.
**evie>** I can't prep it here, no tools in this chat. Prepping a billing rollback tonight after trusting that redis flake is optimistic.

You'd want a write freeze, a snapshot, and a down migration you've actually run. Are you okay with brief writes-off or does it have to be zero-downtime?
`TURN TONE: ROAST=2; AUDIENCE=private; PROFANITY=off; TECHNIQUE=none; SHAPE=short`
`reaction: positive`
`weights: joke_rate~0.5 absurd_literalism(1.0,1.0) brutal_honest(1.0,1.0) callback(1.0,1.0) escalation(1.0,1.0) status_flip(1.0,1.0) twist_ending(1.0,1.0) understatement(1.0,1.0)`
