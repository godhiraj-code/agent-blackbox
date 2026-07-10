# Cursor GitHub Auth Hang Demo

This fixture is a local Agent Blackbox incident bundle for a common AI-coding-agent failure:

> the agent tries to push code, gets blocked by GitHub authentication, and then attempts an interactive `gh auth login` flow inside an unattended runtime.

## Files

- `trace.jsonl` — normalized black-box event stream.
- `analysis.json` — output from `agent_doctor.py analyze-events`.
- `postmortem.md` — buyer/distribution-ready incident explanation.

## Run

From the repo root:

```bash
python agent_doctor.py analyze-events examples/cursor-gh-auth-hang/trace.jsonl --out examples/cursor-gh-auth-hang/analysis.json
```

Expected high-signal output:

```text
status: hung_or_missing_tool_result
tool_calls: 4
missed_tool_calls:
  - shell (shell-002)
  - shell (shell-004)
failed_tool_calls:
  - shell (shell-003)
risk_findings:
  - high interactive_auth_flow
```

## Root cause label

```text
blocked_interactive_subprocess
```

## Why it matters

The incident shows why logs are insufficient for autonomous coding workflows. The real failure is the combination of:

1. a tool call that starts and never returns,
2. missing GitHub auth,
3. an attempted interactive auth flow,
4. no fail-fast boundary before publish/PR steps.

Agent Blackbox turns that into a replayable local postmortem.

## Safety note

This is a synthetic, redacted fixture. It does not contain real credentials and does not perform any network calls.
