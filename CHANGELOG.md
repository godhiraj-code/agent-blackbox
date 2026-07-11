# Changelog

All notable changes to Agent Blackbox are documented here. The format follows Keep a Changelog; the project remains pre-1.0.

## [Unreleased]

### Added

- Added deterministic, explainable 0–100 Agent Autopsy scoring for normalized traces.
- Added standalone escaped HTML and optional JSON output through `autopsy`.
- Added deterministic multi-run captured-evidence ranking through `fight`.
- Added three synthetic Autopsy fixtures plus generated sample single-run and comparison reports.

### Security

- Sanitized command arguments, persisted output, and console output are now the default.
- Raw persistence and raw console streaming require separate explicit opt-ins and warnings.
- Removed implicit Windows `shell=True` command execution.
- Added bounded capture, timeout handling, process-tree termination, atomic writes, output-boundary re-sanitization, and Markdown/HTML injection defenses.
- Generated recordings and exports are ignored by Git by default.

### Reliability

- Added versioned analysis and recording schemas.
- Added strict malformed, unknown-event, unsupported-version, and empty-input handling.
- Added one-to-one outbound-flush correlation and session-scoped tool-call IDs.
- Added native-adapter malformed-row reporting, Claude nested-message support, normalized timestamps, and corrected Codex zero-failure parsing.
- Added meaningful diagnostic exit codes and CI gating.
- Latest-run diagnosis skips and reports newer partial recordings.

### Packaging

- Added MIT license, security and support policies, packaged demo data, cross-platform CI, wheel/sdist checks, and `--version`.

## [0.1.0] - Unreleased baseline

- Initial local-first Hermes, command-recording, Claude, Codex, OpenCode, and normalized-event incident analysis.
