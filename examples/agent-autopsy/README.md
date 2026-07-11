# Agent Autopsy synthetic demo

These traces are fictional and contain no real credentials, private paths, or user data.

```bash
agent-blackbox autopsy examples/agent-autopsy/clean-verified.jsonl --out examples/agent-autopsy/clean-verified.html --json-out examples/agent-autopsy/clean-verified-score.json
agent-blackbox fight Clean=examples/agent-autopsy/clean-verified.jsonl Risky=examples/agent-autopsy/risky-false-success.jsonl Hung=examples/agent-autopsy/hung-authentication.jsonl --out examples/agent-autopsy/fight.html --json-out examples/agent-autopsy/fight.json
```

Scores compare captured execution evidence. They do not measure task correctness or universal model quality.
