# Agent Blackbox

Local-first incident postmortem debugger for AI coding agents.

## What it is

Agent Blackbox helps developers answer:

- Why did this AI-agent run fail?
- Did the agent ignore instructions, loop, or hallucinate success?
- Which files, commands, tools, or platform events mattered?
- Did it touch risky files or run risky commands?
- What warnings were unrelated noise?
- What should I change before running the agent again?
- Can I export a sanitized postmortem without leaking secrets?

The first MVP starts with Hermes gateway logs because we hit the pain ourselves. Later adapters should support Claude Code, Cursor, Codex, OpenCode, and other coding agents — but the product is **not** trying to be another generic trace viewer.

## Why this exists

AI coding agents are powerful but opaque. Existing LLM observability platforms are mostly cloud/instrumentation-heavy, and local trace viewers already exist. Agent Blackbox starts from the developer incident workflow: logs, sessions, terminal commands, file diffs, risky actions, failure classification, and shareable postmortems on your machine.

## Privacy posture

- Local-only by default.
- No telemetry.
- No uploads.
- Sanitized exports by default.
- Raw logs may contain sensitive data; treat them carefully.

## Current MVP Commands

```bash
python agent_doctor.py doctor
python agent_doctor.py gateway --minutes 180
python agent_doctor.py why-no-response --platform discord --minutes 180
python agent_doctor.py export-postmortem --minutes 180 --out exports/latest-postmortem.md
python agent_doctor.py cron "Atomic Mail"
python agent_doctor.py export-cron-postmortem "Atomic Mail" --out exports/atomicmail-cron-postmortem.md
python agent_doctor.py search "Discord"
```

The `why-no-response` command now correlates each inbound message with the next outbound flush and reports per-message latency/pending status. This avoids the weak first-pass behavior where one flush inside a time window could make a later unresponded message look healthy.

## Try the Demo

No Hermes installation or private logs are needed:

```bash
python agent_doctor.py demo
```

The command runs a safe sample incident through the real parser and diagnosis
engine, prints the reconstructed timeline, and writes a sanitized report to
`exports/demo-postmortem.md`.

For the architecture and design decisions, read
[`docs/blog/building-a-local-first-flight-recorder.md`](docs/blog/building-a-local-first-flight-recorder.md).

## Current Status

Seed MVP focused on Hermes gateway diagnostics.

Docs:

- `docs/research/2026-06-20-agent-blackbox-market-research.md`
- `docs/plans/2026-06-20-product-requirements.md`
- `docs/plans/2026-06-20-implementation-plan.md`

## Non-goals

- Not a LangSmith/Langfuse clone.
- Not a cloud-first SaaS.
- Not enterprise governance first.
- Not a generic token-cost dashboard.
