# Security Policy

## Supported surface

Agent Blackbox is pre-1.0 software. Security fixes target the current `main` branch. No released version should be treated as a compliance control or as proof that an agent run was safe.

## Data and trust boundary

Agent traces can contain credentials, source code, personal data, prompts, tool arguments, and command output.

- Processing is local and the project contains no telemetry or upload path.
- Command recordings retain sanitized output by default. Unsanitized retention requires `record --store-raw`.
- Sanitization is defense in depth, not a guarantee of anonymization. Review generated artifacts before sharing them.
- Keep `runs/`, `exports/`, source-agent data directories, and raw recordings out of source control.
- Apply filesystem access controls, encryption at rest, retention limits, and deletion policies appropriate to your organization.
- HTML reports treat log content as untrusted and escape it, but reports should still be opened as local artifacts rather than hosted on an untrusted shared origin.

## Reporting a vulnerability

Do not open a public issue containing a real credential, private trace, customer data, or exploit payload. Send a minimal sanitized reproduction to the maintainer address listed in `pyproject.toml`. Include the affected command, version or commit, impact, and a synthetic reproducer.

## Operational limitations

Agent Blackbox currently detects evidence and heuristics; it does not enforce agent permissions, cryptographically attest events, prevent log tampering, or provide SOC 2/GDPR/HIPAA compliance. A clean report is not proof that a run was trustworthy.
