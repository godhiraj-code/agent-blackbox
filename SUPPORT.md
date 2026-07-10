# Support Policy

Agent Blackbox is pre-1.0 software intended for local evaluation and controlled team pilots.

## Supported versions

Only the most recent tagged 0.x release is supported. Minor 0.x releases may contain breaking schema or CLI changes; these changes must be documented in `CHANGELOG.md`.

## Supported environments

- Python 3.10 through 3.13
- Windows, macOS, and Linux only after the release commit passes the repository CI matrix on all three operating systems
- Local filesystems owned by the invoking user

Network filesystems, shared multi-user evidence directories, and hostile/untrusted local users are not currently supported trust boundaries.

## Getting help

Include the Agent Blackbox version, operating system, Python version, command used, exit code, and a minimal sanitized fixture. Never attach raw agent transcripts, credentials, private source code, or opt-in raw recordings to a public report.

Security vulnerabilities must follow `SECURITY.md`, not a public issue.

## Evidence limitations

Agent Blackbox performs diagnostic analysis. It does not currently provide cryptographic provenance, tamper-proof evidence, compliance attestation, or proof that an agent is safe. Regex and structured-field redaction are defense in depth, not anonymization.
