# Agent Blackbox Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build a local-first MVP that diagnoses Hermes gateway/agent responsiveness issues and exports readable postmortems.

**Architecture:** Start as a Python CLI reading local Hermes logs directly. Keep parsing rules small and testable. Later add SQLite indexing and adapters for Claude Code, Cursor, Codex, and OpenCode.

**Tech Stack:** Python 3.11+, stdlib only for MVP, pytest for tests, optional FastAPI dashboard later.

---

## Phase 0: MVP Already Seeded

The repository should contain:

- `agent_doctor.py`: single-file CLI for initial Hermes gateway diagnosis.
- `test_agent_doctor.py`: unit tests for parser and diagnosis rules.
- `README.md`: usage and positioning.
- `docs/research/2026-06-20-agent-blackbox-market-research.md`
- `docs/plans/2026-06-20-product-requirements.md`

## Task 1: Harden Hermes Log Parser

**Objective:** Parse Hermes gateway log lines into structured events.

**Files:**

- Modify: `agent_doctor.py`
- Test: `test_agent_doctor.py`

**Steps:**

1. Add parser support for:
   - Discord connected;
   - inbound message;
   - outbound flush;
   - Telegram/network warnings;
   - agent cache invalidation;
   - generic warning/error.
2. Add tests with representative log lines copied from real Hermes logs.
3. Run:

```bash
python -m pytest test_agent_doctor.py -q
```

Expected: all tests pass.

## Task 2: Implement `why-no-response`

**Objective:** Explain whether a message was received, response flushed, or agent likely busy.

**Files:**

- Modify: `agent_doctor.py`
- Test: `test_agent_doctor.py`

**Rules:**

- If no platform connection and no inbound: report `message_not_seen_or_gateway_down`.
- If inbound exists and outbound flush exists after inbound: report `response_flushed`.
- If inbound exists but no flush: report `agent_busy_or_response_pending`.
- If unrelated Telegram errors exist during a Discord incident: label as `platform_noise_unrelated`.
- If cache invalidation appears: include as diagnostic note.

## Task 3: Markdown Postmortem Export

**Objective:** Generate sanitized incident report.

**Files:**

- Modify: `agent_doctor.py`
- Test: `test_agent_doctor.py`

**Output format:**

```markdown
# Agent Incident Postmortem

## Diagnosis
...

## Timeline
...

## Warnings / Noise
...

## Recommended Next Step
...
```

## Task 4: Real Hermes Smoke Test

**Objective:** Prove it works on current machine.

**Command:**

```bash
python agent_doctor.py why-no-response --platform discord --minutes 180
python agent_doctor.py export-postmortem --minutes 180 --out exports/latest-postmortem.md
```

**Expected:**

- It sees Discord connected.
- It sees Dhiraj inbound message.
- It sees outbound flush.
- It notes Telegram noise as unrelated.
- It writes a Markdown report.

## Task 5: Package CLI

**Objective:** Convert single script into installable CLI.

**Files:**

- Create: `pyproject.toml`
- Move: `agent_blackbox/cli.py`
- Move: `agent_blackbox/parser.py`
- Move: `agent_blackbox/diagnosis.py`
- Move: `tests/test_parser.py`

**CLI entrypoint:**

```toml
[project.scripts]
agent-doctor = "agent_blackbox.cli:main"
```

## Task 6: Add Claude Code Adapter Spike

**Objective:** Detect likely Claude Code transcript locations and list candidate files without parsing sensitive content yet.

**Rules:**

- No upload.
- No raw transcript print by default.
- Only show paths/counts/timestamps.

## Task 7: Landing Page / README Launch Asset

**Objective:** Create the public-facing narrative.

**Sections:**

- Problem;
- What it does;
- Local-first privacy;
- Demo command;
- Roadmap;
- Pricing/founding user CTA.

## Verification Checklist

- [ ] Unit tests pass.
- [ ] Real Hermes log smoke test passes.
- [ ] No network calls.
- [ ] Sanitized export redacts obvious tokens.
- [ ] README explains this is not a generic Langfuse clone.
