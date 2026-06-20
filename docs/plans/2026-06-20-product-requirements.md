# Agent Blackbox Product Requirements

Date: 2026-06-20  
Repo: `C:\Users\dhira\agent-blackbox`

## 1. Product Thesis

AI coding agents are becoming operational infrastructure, but their behavior is still opaque. Developers increasingly ask:

- Did my message reach the agent?
- Why did the agent not respond?
- What tool call hung?
- What files did it change?
- Which command failed?
- What prompt finally fixed the issue?
- Can I share a sanitized postmortem without leaking code or secrets?

**Agent Blackbox** is a local-first flight recorder for AI coding agents. It records, reconstructs, searches, and explains local agent activity without uploading private prompts, logs, or code.

## 2. Positioning

### One-line positioning

**Local-first flight recorder for AI coding agents.**

### Longer positioning

Replay, search, diagnose, and export what Claude Code, Cursor, Codex, Hermes, OpenCode, and similar agents actually did — locally and privately.

### Anti-positioning

This is not a generic LangSmith/Langfuse clone. It is not an enterprise LLM observability platform. It is not a token dashboard.

## 3. Initial Wedge

Start with Hermes because:

- We own the pain and environment.
- Logs/session DB/cron/gateway events exist locally.
- We already had a real diagnostic incident: Discord appeared unresponsive while the gateway had actually received the message and the agent was busy.
- Hermes users are agent-native and understand tool calls, gateways, cron, and background jobs.

Then expand to Claude Code, Cursor, Codex, and OpenCode.

## 4. MVP Use Cases

### P0 — Must work first

1. **Gateway non-response diagnosis**
   - Input: platform/chat or recent time window.
   - Output: whether message was received, whether the gateway was connected, whether agent was busy, whether response was flushed, related warnings/errors.

2. **Recent run timeline**
   - Show inbound events, outbound flushes, errors/warnings, cache invalidations, cron triggers, and process state.

3. **Markdown postmortem export**
   - Generate a human-readable incident report from local logs.

4. **Privacy-safe defaults**
   - Local only.
   - No network calls.
   - Redact obvious secrets/tokens.
   - Never upload without explicit user action.

5. **Hermes path auto-detection**
   - Support Windows Hermes home: `C:\Users\dhira\AppData\Local\hermes`.
   - Later support macOS/Linux paths.

### P1 — Sellable developer utility

1. Search sessions/logs by text.
2. Timeline grouped by agent session.
3. Tool-call summary.
4. File-change/diff summary where available.
5. Provider/model failure classification.
6. Local HTML report export.
7. Claude Code transcript import.
8. Cursor SQLite/import support.

### P2 — Pro features

1. Cross-agent unified replay.
2. Secret redaction rules and preview.
3. Bookmarks/tags.
4. Prompt extraction.
5. Similar session search.
6. Team/shared encrypted archive.
7. GitHub issue export.
8. OTEL import/export.

## 5. User Stories

### Solo developer

> As a developer using AI coding agents, I want to know what my agent did while I was away so I can trust, debug, or revert the work.

### Consultant

> As an AI automation consultant, I want to generate clean postmortems of agent runs so I can show clients what happened without leaking secrets.

### Small team lead

> As a team lead adopting coding agents, I want to audit risky commands, failed tests, and repeated loops before agents become unmanageable.

## 6. Initial CLI

Command name for MVP: `agent_doctor.py` initially, later package as `agent-doctor`.

Planned commands:

```bash
python agent_doctor.py doctor
python agent_doctor.py gateway --minutes 120
python agent_doctor.py why-no-response --platform discord --minutes 120
python agent_doctor.py export-postmortem --minutes 120 --out incident.md
python agent_doctor.py search "Discord Connected"
```

## 7. Data Model

Use a normalized local SQLite DB later. MVP can parse logs directly.

Future tables:

- `sources`: hermes, claude_code, cursor, codex, opencode.
- `events`: timestamp, source, event_type, severity, platform, session_id, message, raw_line_hash.
- `sessions`: session_id, source, started_at, ended_at, status, summary.
- `tool_calls`: session_id, tool_name, started_at, ended_at, status, args_redacted, result_summary.
- `file_changes`: session_id, path, change_type, diff_summary.
- `incidents`: generated diagnosis reports.

## 8. Event Types

Start with Hermes gateway logs:

- `gateway_connected`
- `gateway_inbound_message`
- `gateway_outbound_flush`
- `gateway_reconnect_warning`
- `platform_error`
- `agent_cache_invalidated`
- `cron_run`
- `process_status`
- `unknown_warning`
- `unknown_error`

## 9. Diagnosis Rules

### Why no response?

Given a platform and time window:

1. Did gateway log a platform connection?
2. Did it log inbound message from user?
3. Did it log outbound flush after inbound?
4. Were there errors/warnings between inbound and flush?
5. Was another platform reconnect loop noisy but unrelated?
6. Was the agent session cache invalidated or cross-process write detected?
7. Was the process alive according to gateway status?

Output a direct diagnosis:

- `message_not_received`
- `gateway_disconnected`
- `agent_busy_or_turn_in_progress`
- `response_flushed`
- `platform_noise_unrelated`
- `unknown_needs_deeper_trace`

## 10. Security and Privacy

Default posture:

- Local-only.
- No telemetry.
- No upload.
- Redact API keys, bearer tokens, private key blocks, common secret patterns.
- Mark raw log export as sensitive.
- Provide sanitized export as default.

## 11. Monetization

### Free

- Hermes gateway diagnosis.
- Basic recent timeline.
- Basic Markdown postmortem.

### Pro

- Claude/Cursor/Codex imports.
- Cross-agent timeline.
- Search across all local agent sessions.
- Diff view.
- HTML/PDF export.
- Redaction previews.
- Prompt extraction.

Pricing:

- Founding Pro: $99/year.
- Later: $15/mo or $120/year.

### Service

- $499 AI coding-agent workflow audit.

## 12. Success Metrics

### Product

- Diagnose the latest Hermes Discord non-response incident from logs in under 5 seconds.
- Generate a readable Markdown incident report.
- 0 network calls in MVP.
- 0 unredacted obvious secrets in sanitized exports.

### Business

- 10 design partners.
- 5 active users from GitHub/X/HN.
- 3 paid founding licenses or 1 workflow audit within 30 days of public demo.

## 13. Non-Goals

- Enterprise SOC2/SSO.
- Full SaaS backend.
- Production LLM app tracing clone.
- Replacing Langfuse/LangSmith.
- Multi-user team sync in MVP.
- Automatic code rollback.
