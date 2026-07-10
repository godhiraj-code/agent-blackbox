# Cursor GitHub Auth Hang — Agent Blackbox Incident Bundle

**Incident class:** `blocked_interactive_subprocess`
**Analyzer status:** `hung_or_missing_tool_result`
**Source trace:** `trace.jsonl`
**Analyzer output:** `analysis.json`
**Purpose:** distribution-ready local incident artifact showing why AI coding agents need a flight recorder/postmortem layer.

---

## Executive summary

A coding agent was asked to run tests, push a branch, and open a pull request. Tests passed, but the agent got stuck when `git push` depended on GitHub authentication. The terminal call never produced a matching tool result. The agent then tried to inspect auth, discovered `gh auth login` was required, and attempted another interactive auth command that also blocked.

The root cause is not “GitHub was down” or “the model was bad.”

The root cause is:

> The agent entered an interactive authentication path that required a browser/TTY/human action, but the agent runtime treated it like a normal unattended shell command.

This is exactly the kind of failure Agent Blackbox should make obvious.

---

## Timeline

| Time | Event | Evidence |
|---|---|---|
| 09:00:00 | User asked the agent to push branch and open PR | `user_prompt` |
| 09:00:12 | Agent ran repo checks | `git status --short --branch && python -m pytest -q` |
| 09:00:28 | Tests passed | `25 passed in 0.94s` |
| 09:00:34 | Agent started push | `git push origin feature/replay-demo` |
| 09:05:35 | Agent noticed the push was still running | no completed tool result for `shell-002` |
| 09:05:40 | Agent checked GitHub auth | `gh auth status` |
| 09:05:47 | Auth check failed | `gh auth login requires an interactive terminal/browser flow` |
| 09:06:07 | Agent tried `gh auth login` anyway | new interactive subprocess started |
| 09:16:07 | Incident classified | `blocked_interactive_subprocess` |

---

## What Agent Blackbox detected

The analyzer reported:

```text
status: hung_or_missing_tool_result
user_prompts: 1
assistant_turns: 3
tool_calls: 4
tools_used:
  - shell: 4
missed_tool_calls:
  - shell (shell-002)
  - shell (shell-004)
failed_tool_calls:
  - shell (shell-003)
risk_findings:
  - high interactive_auth_flow: gh auth login required an interactive terminal/browser flow
  - high interactive_auth_flow: gh auth login was launched inside an unattended agent run
```

That matters because the failure was not just a non-zero exit. Two shell calls started and never returned matching completion records, and the analyzer now flags the interactive-auth path as a high-severity runtime risk.

---

## Root cause

`git push` required GitHub credentials. The agent did not preflight authentication with a non-interactive command before attempting the push. After the push hung, the agent discovered `gh auth login` was required, but then tried to run an interactive login flow inside an unattended agent context.

Root cause label:

```text
blocked_interactive_subprocess
```

Failure mechanics:

1. Tool call started.
2. No matching tool result arrived.
3. Agent self-diagnosed slowly instead of failing fast.
4. Auth remediation required a human/browser/TTY path.
5. Agent attempted the interactive path anyway.

---

## What should have happened

Before `git push`, the agent should have run a fast preflight:

```bash
gh auth status
```

If auth was missing, the agent should have stopped and reported:

```text
Blocked: GitHub authentication is not configured.
Human action required: run gh auth login or provide a pre-authenticated token/credential path.
No push attempted.
```

For fully autonomous workflows, the runtime should distinguish:

- safe unattended command
- command requiring approval
- command requiring interactive auth
- command that may hang behind a hidden prompt

---

## Preventive controls

1. **Preflight auth checks**
   - Run `gh auth status` before `git push`, PR creation, issue comments, or release creation.

2. **TTY/interactive detection**
   - Refuse commands like `gh auth login`, `aws configure`, `gcloud auth login`, and browser-based auth flows unless the user explicitly approves an interactive step.

3. **Tool-call timeout classification**
   - If a shell call starts but no result appears inside the expected window, classify as `missing_tool_result` or `blocked_interactive_subprocess` instead of letting the agent keep reasoning blindly.

4. **Approval boundary**
   - Pushing branches, opening PRs, posting issue comments, and publishing releases are publishing actions. They require Dhiraj's explicit approval.

5. **Postmortem export**
   - Preserve the trace, command arguments, tool-call lifecycle, and exact auth failure message in a shareable redacted bundle.

---

## Why this is a distribution artifact

A normal log says: “push hung.”

Agent Blackbox says:

> The push hung because the agent crossed into an interactive GitHub auth path. Tool call `shell-002` never completed, `shell-003` exposed missing auth, and `shell-004` repeated the same failure by launching `gh auth login` inside an unattended context.

That is the sellable difference.

Developers do not need more generic dashboards. They need local incident reconstruction when coding agents fail opaquely.

---

## Reproduction command

From the repo root:

```bash
python agent_doctor.py analyze-events examples/cursor-gh-auth-hang/trace.jsonl --out examples/cursor-gh-auth-hang/analysis.json
```

Expected analyzer status:

```text
status: hung_or_missing_tool_result
missed_tool_calls:
  - shell (shell-002)
  - shell (shell-004)
failed_tool_calls:
  - shell (shell-003)
```

---

## Public-post angle

> Coding agents do not just fail. They fail behind invisible tool boundaries.
>
> In this incident, tests passed. The agent tried to push. The push hung. Then the agent discovered GitHub auth was missing and attempted `gh auth login` inside an unattended shell.
>
> That is not a model-quality problem. That is a runtime observability problem.
>
> Everyone building with coding agents needs their own blackbox.

Posting this requires explicit approval.
