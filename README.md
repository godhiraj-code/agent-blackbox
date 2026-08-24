# Agent Autopsy by Agent Blackbox

[![CI](https://github.com/godhiraj-code/agent-blackbox/actions/workflows/ci.yml/badge.svg)](https://github.com/godhiraj-code/agent-blackbox/actions/workflows/ci.yml)

**Score what an AI agent actually did, then put captured runs head-to-head.**

Agent Autopsy turns normalized local traces into standalone, sanitized HTML evidence reports. It gives one run a deterministic 0–100 score with every deduction exposed, while Agent Fight ranks two or more runs using the same captured-evidence contract. No cloud, telemetry, model call, or external report assets are required.

```bash
agent-blackbox autopsy examples/agent-autopsy/clean-verified.jsonl --out autopsy.html --json-out score.json
agent-blackbox fight Clean=examples/agent-autopsy/clean-verified.jsonl Risky=examples/agent-autopsy/risky-false-success.jsonl Hung=examples/agent-autopsy/hung-authentication.jsonl --out fight.html --json-out fight.json
```

> Scores describe captured execution evidence, not task correctness, safety, or universal model quality.

Agent Blackbox remains a local-first flight recorder and CI diagnostic gate for AI agent runs. It captures commands, reconstructs tool calls, diagnoses failures, and creates sanitized postmortems without uploading private traces.

## What it is

Agent Blackbox helps developers answer:

- Why did this AI-agent run fail?
- Did the agent ignore instructions, loop, or hallucinate success?
- Which files, commands, tools, or platform events mattered?
- Did it touch risky files or run risky commands?
- What warnings were unrelated noise?
- What should I change before running the agent again?
- Can I export a sanitized postmortem without leaking secrets?

The project started with Hermes gateway incidents and now includes a normalized event model for command recordings, Claude Code transcripts, Codex rollouts, OpenCode sessions, and custom JSON/JSONL adapters. It reports tool failures, missing tool results, risky commands, skipped verification, malformed evidence, and unsupported formats instead of silently declaring success.

Agent Blackbox helps teams inspect and diagnose captured behavior. It does **not** certify that an agent is safe, compliant, or trustworthy, and it is not tamper-proof evidence.

## Install

Agent Blackbox requires Python 3.10 or newer. From a checked-out release:

```bash
git clone https://github.com/godhiraj-code/agent-blackbox.git
cd agent-blackbox
python -m pip install .
agent-blackbox --version
agent-blackbox demo
```

For development and tests:

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

Use a tagged release artifact for team pilots; do not install from an arbitrary dirty working tree. The project is not published to PyPI yet.

## Why this exists

AI coding agents are powerful but opaque. Existing LLM observability platforms are mostly cloud/instrumentation-heavy, and local trace viewers already exist. Agent Blackbox starts from the developer incident workflow: logs, sessions, terminal commands, file diffs, risky actions, failure classification, and shareable postmortems on your machine.

## Privacy posture

- Local-only by default.
- No telemetry and no network upload path.
- New command recordings persist only sanitized output and sanitized command arguments by default.
- Console/CI output is sanitized by default; raw streaming requires `record --show-raw-console`.
- Raw output retention is opt-in with `record --store-raw` and emits a warning.
- Recording files are written atomically with owner-only mode where the operating system honors POSIX permissions.
- Capture is bounded to 10 MiB by default; use `--max-capture-bytes` to change the limit.
- Source agent logs and opt-in raw recordings can still contain secrets or personal data. Keep them out of source control and apply your organization's retention policy.

## Core commands

```bash
agent-blackbox doctor
agent-blackbox run-doctor --runs-dir runs --out exports/latest-run-postmortem.md
agent-blackbox record --name "blackbox smoke" --out-dir runs -- python -c "print('hello from smoke')"
agent-blackbox diagnose-run runs/run_<id>.json --out exports/run-postmortem.md
agent-blackbox analyze-events path/to/events.jsonl --out exports/events-analysis.json
agent-blackbox analyze-events path/to/events.jsonl --format json --fail-on-incident
agent-blackbox autopsy path/to/events.jsonl --out exports/autopsy.html --json-out exports/autopsy.json
agent-blackbox autopsy path/to/events.jsonl --out exports/autopsy.html --fail-on-incident
agent-blackbox fight Baseline=path/to/baseline.jsonl Candidate=path/to/candidate.jsonl --out exports/fight.html --json-out exports/fight.json
agent-blackbox repo-preflight --repo path/to/repo --manifest preflight.json --json-out preflight-report.json
agent-blackbox claude --session <session-id-or-filename>
agent-blackbox codex --session <session-id-or-filename>
agent-blackbox opencode --session <session-id-or-export-json>

python agent_doctor.py doctor
python agent_doctor.py gateway --minutes 180
python agent_doctor.py why-no-response --platform discord --minutes 180
python agent_doctor.py export-postmortem --minutes 180 --out exports/latest-postmortem.md
python agent_doctor.py cron "Atomic Mail"
python agent_doctor.py export-cron-postmortem "Atomic Mail" --out exports/atomicmail-cron-postmortem.md
python agent_doctor.py record --name "blackbox smoke" --out-dir exports -- python -c "print('hello from smoke')"
python agent_doctor.py diagnose-run exports/run_<id>.json --out exports/run-postmortem.md
python agent_doctor.py run-doctor --runs-dir exports --out exports/latest-run-postmortem.md
python agent_doctor.py analyze-events path/to/events.jsonl --out exports/events-analysis.json
python agent_doctor.py search "Discord"
```

`run-doctor` diagnoses the newest `run_*.json` recording, so users do not need to copy exact recording paths during an incident. Recorded-run postmortems include failure evidence and risk findings such as destructive shell commands, pipe-to-shell installers, sandbox/permission blocks, dependency/network failures, skipped verification language, and redacted secret exposure.

Diagnostic commands use their findings as process exit codes by default: `why-no-response`, `cron`, and their export variants return `1` for an unhealthy diagnosis and `2` for missing or invalid input. Add `--always-zero` when a report is informational and must not gate a wrapper. `autopsy --fail-on-incident` provides the same opt-in CI gate for normalized traces while keeping report generation successful by default.

Recording is sanitized-only by default:

```bash
agent-blackbox record --timeout-seconds 1800 --max-capture-bytes 10485760 -- your-agent command
# Only when incident response genuinely requires the unsanitized stream:
agent-blackbox record --store-raw -- your-agent command
# Only when unsanitized terminal output is also required:
agent-blackbox record --show-raw-console -- your-agent command
```

`--store-raw` can persist credentials, source code, personal data, and model context. Do not use it in shared CI runners or check its output into Git. The redactor is defense in depth, not a proof that arbitrary data is anonymous.

### Repository-scope preflight

`repo-preflight` is a deterministic final-diff gate for agent work. A JSON manifest pins a baseline commit, lists the paths the task is allowed to change, optionally protects paths and limits the total change count, and checks repository invariants before a publish or push decision:

```json
{
  "baseline": "HEAD",
  "allow_paths": ["src/feature/**", "tests/test_feature.py"],
  "protected_paths": ["src/data/**"],
  "max_changed_files": 6,
  "invariants": [
    {"name": "routes remain", "type": "text_required", "glob": "src/routes.*", "regex": "/blog"},
    {"name": "corpus remains", "type": "glob_count", "glob": "content/*.md", "min": 10}
  ]
}
```

The command includes untracked files and exits `1` when a change is outside intended scope, touches a protected path, exceeds the file budget, or violates a `glob_count`, `text_required`, or `text_forbidden` invariant. It does not decide whether an extra change is bad; it blocks mixed or unexplained work so a human or higher-level workflow can separate it first. See `examples/repo-preflight/` for an honest blocked report from a local mixed-worktree case.

Claude, Codex, and OpenCode session commands use the shared black-box analyzer to identify:

- tool calls that started but never produced a result
- tool calls whose output looks failed
- tools used and call counts
- high-signal risk findings from tool arguments and results

For agents without a native adapter yet, `analyze-events` accepts JSON or JSONL records shaped like:

```json
{"timestamp":"2026-06-30T01:02:04Z","source":"custom-agent","session_id":"session-1","event_type":"tool_call_started","tool_call_id":"tool-1","tool_name":"shell","arguments":"pytest"}
```

Supported high-signal event types include `user_prompt`, `assistant_message`, `tool_call_started`, `tool_call_completed`, `tool_call_failed`, and `error`.

The supported normalized contract is schema major version `1`. A JSON envelope should use:

```json
{"schema_version":"1.0","events":[{"event_type":"user_prompt","source":"custom-agent","session_id":"session-1"}]}
```

Unsupported schema versions, unknown event types, malformed records, non-object records, and empty input fail closed with exit code `2`; they are never reported as a completed run.

Normalized analysis also reports evidence quality: missing or invalid timestamps, duplicate records, and tool starts without matching results are surfaced as explicit gaps. Recommendations are derived from the captured evidence and remain separate from task-correctness claims.

### Exit codes

- `0`: healthy/completed analysis or successful utility command
- `1`: an incident, unhealthy dependency, failed/hung run, or risky/non-completed adapter result was diagnosed
- `2`: invalid CLI usage or invalid/unsupported input

`diagnose-run` and `run-doctor` support `--always-zero` only for report-only integrations that intentionally do not gate CI.

The `why-no-response` command performs FIFO timestamp correlation between inbound messages and outbound flushes and reports per-message latency/pending status. A flush is consumed once, so one send event cannot make several messages look healthy. This is still a heuristic when the source log does not expose a stable message/response correlation ID; do not treat it as delivery proof.

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

## Reliability contract

Agent Blackbox is designed to fail closed:

- malformed, empty, unknown, or unsupported evidence never becomes a clean run
- tool-call IDs are scoped by source and session
- one outbound flush cannot satisfy multiple inbound messages
- native adapters surface rejected transcript records
- interrupted and timed-out commands terminate their process trees
- recordings and reports are bounded and written atomically
- machine-readable analysis can gate CI with nonzero exit codes

The regression suite covers parsing, recording, normalized analysis, Agent Autopsy scoring/reporting, and Agent Fight ranking. Wheel, sdist, fresh-install, packaged-demo, privacy-default recording, and artifact-content smoke checks are release gates. Cross-platform CI is defined for Windows, macOS, and Linux; treat platform support as verified only for release commits whose matrix is green.

## Current status

Hardened pre-1.0 pilot focused on local-first agent incident debugging:

- Hermes gateway and cron diagnostics
- command flight recording and latest-run diagnosis
- Claude Code transcript summaries
- Codex rollout summaries with missing/failed tool-call detection
- OpenCode storage/export summaries with missing/failed tool-call detection
- Markdown and HTML postmortem exports
- privacy-safe sanitized output in generated reports

Docs:

- `docs/research/2026-06-20-agent-blackbox-market-research.md`
- `docs/plans/2026-06-20-product-requirements.md`
- `docs/plans/2026-06-20-implementation-plan.md`
- `docs/plans/2026-07-10-production-readiness.md`
- `SECURITY.md`
- `SUPPORT.md`
- `CHANGELOG.md`

## Non-goals

- Not a LangSmith/Langfuse clone.
- Not a cloud-first SaaS.
- Not enterprise governance first.
- Not a generic token-cost dashboard.
