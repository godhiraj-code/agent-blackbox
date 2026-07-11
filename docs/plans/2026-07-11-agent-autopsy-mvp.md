# Agent Autopsy MVP Implementation Plan

> **For Hermes:** Implement task-by-task and verify every public claim with executable evidence.

**Goal:** Turn Agent Blackbox into a visually compelling Agent Autopsy workflow that scores one captured run and compares multiple runs in an Agent Fight report.

**Architecture:** Keep the existing normalized event analyzer as the evidence engine. Add a dependency-free `autopsy.py` presentation/scoring layer that accepts analysis dictionaries, computes deterministic explainable scores, and emits self-contained HTML. Wire two commands into the existing argparse CLI: `autopsy` for one JSON/JSONL event trace and `fight` for two or more traces. No cloud, telemetry, database, JavaScript framework, or LLM dependency.

**Tech Stack:** Python 3.10+, stdlib argparse/json/html/pathlib, pytest.

---

## Acceptance criteria

1. `agent-blackbox autopsy TRACE --out report.html --json-out score.json` loads existing normalized events, runs the existing analyzer, computes a 0-100 score, prints the verdict, and writes self-contained sanitized HTML plus optional JSON.
2. Score is deterministic and explainable through explicit penalties for failed calls, missing tool results, errors, high/medium risk findings, and skipped verification. It must not pretend to measure correctness when evidence is unavailable.
3. HTML shows verdict, score, evidence-confidence disclaimer, metrics, risk findings, tool usage, and sanitized timeline. All untrusted values are HTML-escaped.
4. `agent-blackbox fight NAME=TRACE NAME=TRACE [...] --out fight.html --json-out fight.json` compares at least two runs, ranks by score with deterministic tie-breaking, and labels the result as evidence comparison rather than universal model quality.
5. Add three small synthetic fixtures representing a clean verified run, a risky false-success run, and a hung authentication run. No real credentials or private paths.
6. Add focused regression tests for scoring, malformed/empty input, HTML escaping/redaction, ranking, CLI output files, and argument validation.
7. README begins with the Agent Autopsy value proposition and contains copy-paste demos for autopsy and fight without erasing the honest Agent Blackbox limitations.
8. Full test suite passes with plugin autoload disabled. Wheel and sdist build. Fresh wheel install can run both commands against packaged or copied fixtures.
9. No push, release, package publication, or social post without explicit approval.

## Non-goals

- Live interception of every internal agent operation.
- Claims of tamper-proof evidence, security certification, or measured task correctness.
- Cloud dashboards, accounts, payments, hosted report links, or enterprise policy management.
- Supporting arbitrary vendor transcript formats beyond existing adapters and normalized event JSON/JSONL.

## Tasks

### Task 1: Scoring contract

Create `agent_blackbox/autopsy.py` with a deterministic scorecard and tests. Scores expose every deduction and preserve evidence limitations.

### Task 2: Single-run report

Add escaped, self-contained HTML generation and the `autopsy` CLI command. Test malicious HTML-shaped input and secret-shaped values.

### Task 3: Agent Fight

Add comparison/ranking logic and `fight` CLI command. Require at least two uniquely named `NAME=PATH` entries and deterministic ranking.

### Task 4: Demo evidence and docs

Add synthetic fixtures under `examples/agent-autopsy/`, generated sample reports under that example directory only if sanitized, and concise README commands.

### Task 5: Release verification

Run targeted and full tests, build artifacts, fresh-install smoke, HTML inspection, secret-pattern scan, `git diff --check`, and working-tree review. Commit locally on `feat/agent-autopsy-mvp`; do not push.
