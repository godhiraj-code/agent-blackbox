# Agent Blackbox

Local-first flight recorder for AI coding agents.

## What it is

Agent Blackbox helps developers answer:

- Did my agent receive my message?
- Why did it not respond?
- What was it doing?
- Which platform/tool failed?
- What warnings were just unrelated noise?
- Can I export a readable postmortem?

The first MVP starts with Hermes gateway logs because we hit the pain ourselves. Later adapters will support Claude Code, Cursor, Codex, OpenCode, and other coding agents.

## Why this exists

AI coding agents are powerful but opaque. Existing LLM observability platforms are mostly cloud/instrumentation-heavy. This project starts from the local developer workflow: logs, sessions, terminal commands, files, diffs, and incidents on your machine.

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
python agent_doctor.py search "Discord"
```

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
