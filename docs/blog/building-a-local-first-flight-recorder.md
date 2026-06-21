# Building a Local-First Flight Recorder for AI Coding Agents

AI coding agents can edit files, run commands, call tools, and talk through chat gateways. When one suddenly appears to stop responding, the usual debugging experience is primitive: open a large log file, search for a familiar word, and guess which warning matters.

Agent Blackbox is an early experiment in making that experience legible. It is a local-first command-line tool that turns raw Hermes gateway logs into a small event timeline, answers focused questions such as “did my message reach the agent?”, and exports a sanitized incident report.

The project is intentionally small. The current implementation is one Python file, uses only the standard library, and makes no network calls.

## The problem is correlation, not a lack of logs

An agent gateway already records plenty of facts. A typical minute might include a Discord connection, an inbound message, a Telegram reconnect warning, an agent cache update, and an outbound Discord flush. Read as an undifferentiated stream, those facts invite the wrong conclusion: the bright warning line looks like the cause.

The useful question is temporal and platform-specific:

1. Was the target platform connected?
2. Did the gateway receive the user’s message?
3. Was a response flushed after that message?
4. Did warnings concern the same platform or an unrelated one?
5. Was there evidence that the active agent session changed?

Agent Blackbox encodes that sequence as a deterministic diagnosis instead of asking an LLM to interpret private logs.

## The pipeline

The MVP has four stages:

```text
Hermes gateway.log
        |
        v
parse each line into an Event
        |
        v
filter by time window and platform
        |
        v
apply deterministic diagnosis rules
        |
        v
terminal summary + sanitized Markdown postmortem
```

Each parsed `Event` keeps a timestamp, event type, severity, platform, sanitized message, and original line. The parser recognizes a deliberately narrow vocabulary: connections, inbound messages, outbound flushes, reconnect warnings, cache invalidations, and generic warnings or errors.

That narrowness is a feature. The tool can explain exactly why it reached a result, and unit tests can cover each rule.

## How a non-response diagnosis works

The central rule engine groups events for one platform and classifies the evidence:

- No connection and no inbound message means the message may not have been seen or the gateway may be down.
- An inbound message with no later outbound flush means the agent may still be busy, stuck in a tool call, or may have failed before delivery.
- An inbound message followed by an outbound flush means the response reached the gateway’s send path. The incident is more likely latency or busy-turn UX than a dead gateway.
- Reconnect warnings from another platform are counted as unrelated noise rather than promoted to root cause.

These conclusions are intentionally modest. An outbound flush does not prove that a user saw the message, and the absence of a flush does not identify the exact stuck tool. The status names preserve that uncertainty.

## Privacy is part of the architecture

Agent logs can contain prompts, file paths, chat identifiers, and credentials. The MVP therefore stays local, has no telemetry, and sanitizes common secret forms before displaying or exporting log messages. API keys, tokens, passwords, bearer credentials, and private-key blocks are replaced with a redaction marker.

Pattern-based redaction is not a mathematical guarantee. A future release should add configurable rules and a redaction preview. Still, making sanitized output the default is a much safer starting point than treating privacy as an export option added later.

## Try the end-to-end demo

The repository includes a synthetic gateway incident. It contains a Discord message, unrelated Telegram network noise, a cache invalidation, an outbound flush, and a fake token that proves the export redaction path is active.

Run:

```bash
python agent_doctor.py demo
```

The command uses the same parser, diagnosis rules, and report generator as a real invocation. It should classify the incident as `response_flushed`, explain that the Telegram warning is unrelated, and write `exports/demo-postmortem.md` without the fake secret.

For a real Hermes installation, the equivalent workflow is:

```bash
python agent_doctor.py why-no-response --platform discord --minutes 180
python agent_doctor.py export-postmortem --platform discord --minutes 180 --out exports/latest-postmortem.md
```

## Why not start with a dashboard or an LLM?

A dashboard would make the project easier to screenshot but would not improve the evidence model. An LLM could summarize logs fluently, but it would add nondeterminism, cost, possible network exposure, and a harder-to-audit explanation path.

The CLI establishes the more important contract first: normalized events in, reproducible diagnosis out. A local HTML interface can later sit on top of that contract. An optional local or explicitly configured model could help summarize incidents whose event patterns fall outside known rules.

## What comes next

The current parser is Hermes-specific and reads the log file directly. The natural next architectural step is to split source adapters from the normalized event model, then index events in local SQLite. That would enable session grouping, faster search, tool-call timelines, and adapters for Claude Code, Cursor, Codex, and OpenCode.

A first robustness upgrade is already in place: message-level correlation. Instead of asking "did any flush happen in this window?", the tool pairs every inbound message with the next outbound flush and reports pending messages plus latency. That is the difference between a log summary and a usable flight recorder.

The long-term idea is not another cloud observability platform. It is a private, inspectable record of what local coding agents actually did—something closer to a flight recorder than a token dashboard.
