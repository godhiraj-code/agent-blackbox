# Agent Blackbox Production-Readiness Gate

Date: 2026-07-10
Status: pre-1.0 hardened pilot CLI; not yet a team trust platform

## Product truth

Agent Blackbox is useful today as a local incident recorder and postmortem analyzer. It is not yet sufficient evidence for teams to blindly trust an autonomous agent. It detects and summarizes evidence; it does not enforce permissions, prove provenance, prevent tampering, or guarantee complete capture.

The correct near-term wedge is a trustworthy local/CI reliability gate for coding-agent runs. Do not build a multi-tenant dashboard, enterprise RBAC, or a generic Langfuse clone before pilots repeatedly use the recorder and CI gate.

## Production hardening completed in this pass

- Sanitized recording, command arguments, and console/CI output by default.
- Explicit warnings for opt-in raw retention and raw console streaming.
- Bounded capture, command timeout, process-tree termination, atomic writes, and restrictive file mode where supported.
- Recording and analysis schema version `1.0`.
- Strict JSON/JSONL input errors with line numbers; native adapters surface malformed transcript rows instead of silently producing false-clean reports.
- Tool-call correlation scoped by source and session.
- One-to-one FIFO message/flush correlation, explicitly documented as a heuristic without stable source IDs.
- JSON-only stdout and `--fail-on-incident` for CI use.
- Package-contained demo fixture verified from a fresh wheel install.
- Generated `runs/` and all `exports/*` artifacts ignored by Git.
- MIT license, security policy, and a Windows/macOS/Linux Python 3.10/3.13 CI matrix.

## Remaining release blockers

### P0 — required before claiming reliable team trust

1. **Tamper evidence and provenance**
   - Add append-only event chaining or signed manifests.
   - Record recorder version, adapter version, host/runtime metadata, command digest, and input/output artifact digests.
   - Detect modified or incomplete recordings.

2. **Adapter contract and conformance kit**
   - Publish a versioned event schema.
   - Add golden fixtures and a TCK for ordering, duplicate IDs, retries, malformed rows, partial writes, clock skew, and tool start/result matching.
   - Mark unsupported source versions as `unknown`, never clean.

3. **Crash-safe streaming storage**
   - Current command capture is bounded in memory and writes the final JSON atomically, but a machine/process crash before finalization loses the run.
   - Move to append-only sanitized JSONL or SQLite WAL during execution, then finalize a manifest.

4. **Platform-level end-to-end CI evidence**
   - The workflow now defines Windows/macOS/Linux coverage, but it has not run remotely because this checkout has no configured Git remote.
   - Add real `.cmd`/`.bat`, POSIX signal, child-process-tree, Unicode, high-volume stdout/stderr, and interrupted-run scenarios.

5. **Privacy verification beyond regexes**
   - Add a structured redaction policy, nested-field traversal tests, path/user identifier controls, entropy/token heuristics with false-positive tests, and a review-before-share command.
   - Document retention and deletion behavior. Do not market regex sanitization as anonymization.

### P1 — required for paid team pilots

- Config file with schema validation and organization-safe defaults.
- Stable exit-code contract and compatibility policy.
- Signed release artifacts, SBOM, dependency scanning, and pinned CI actions.
- Benchmarks for large traces and bounded-memory streaming.
- Support matrix for exact Claude Code, Codex, OpenCode, and Hermes versions.
- Structured incident bundles containing recorder version, evidence confidence, limitations, and remediation status.
- Installation docs for pipx/uv and reproducible uninstall/data-deletion instructions.

### P2 — only after repeated pilot demand

- Shared dashboard and centralized storage.
- SSO/RBAC/audit administration.
- Cloud ingestion and hosted retention.
- Policy enforcement and approval workflows.
- Compliance evidence packs.

## Pilot acceptance gate

A release candidate is ready for a small design-partner pilot when all of these are true:

- Fresh wheel installs and runs the demo on Windows, macOS, and Linux.
- A killed/interrupted recording remains parseable and explicitly incomplete.
- Cross-session duplicate call IDs cannot collide.
- Malformed or unknown source data cannot return a clean result.
- Dummy credentials are absent from console, recording, JSON analysis, Markdown, and HTML.
- The CI gate returns nonzero for missing tool results, failed tools, parse corruption, and high-risk actions.
- Generated artifacts are excluded from Git by default.
- The report states confidence and evidence gaps rather than claiming certainty.

## Verification evidence from this pass

- Scoped suite: `46 passed` with pytest plugin autoload disabled.
- Syntax/diff checks: passed.
- Wheel and sdist build: passed.
- Fresh-wheel install, `--version`, CLI help, packaged demo, and privacy-default recording smoke: passed.
- Artifact content inspection: passed; required package data is present and removed debug probes/stale generated guides are absent.
- Incident fixture: `hung_or_missing_tool_result`, interactive-auth risk detected, CI exit code `1`.
- Canonical global pytest remains blocked before collection by an unrelated broken global LangSmith/Pydantic plugin; the repository CI disables third-party pytest plugin autoload.

## Revenue filter

Do not spend the next month building enterprise surfaces. The next money test is three to five design partners running Agent Blackbox in real coding-agent CI or incident workflows. Charge for a reliability audit plus pilot setup. Use their failed runs to decide whether tamper evidence, crash-safe capture, or adapter breadth is the first paid product requirement.
