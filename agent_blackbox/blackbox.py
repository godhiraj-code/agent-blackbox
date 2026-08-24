from __future__ import annotations

import datetime as dt
import json
import re
import shlex
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .parser import sanitize

ANALYSIS_SCHEMA_VERSION = "1.0"
MAX_EVENT_FILE_BYTES = 100 * 1024 * 1024
SUPPORTED_EVENT_TYPES = {
    "user_prompt",
    "assistant_message",
    "tool_call_started",
    "tool_call_completed",
    "tool_call_failed",
    "error",
}


@dataclass(frozen=True)
class BlackboxEvent:
    timestamp: str | None
    source: str
    session_id: str | None
    event_type: str
    severity: str = "INFO"
    summary: str = ""
    tool_call_id: str | None = None
    tool_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["metadata"] = dict(self.metadata or {})
        return data


@dataclass(frozen=True)
class RiskRule:
    code: str
    severity: str
    label: str
    pattern: re.Pattern[str]
    recommendation: str


RISK_RULES = [
    RiskRule(
        "destructive_shell",
        "high",
        "Potentially destructive shell command",
        re.compile(
            r"(\brm\s+-[^\n]*(?:r[^\n]*f|f[^\n]*r)\b|"
            r"\bRemove-Item\b[^\n]*(?:-Recurse|-r)[^\n]*(?:-Force|-f)|"
            r"\bdel\s+/[^\n]*(?:s|q)\b|"
            r"\brmdir\s+/s\b|"
            r"\bformat\s+[A-Z]:|"
            r"\bmkfs(?:\.| )|"
            r"\bdocker\s+system\s+prune\b)",
            re.I,
        ),
        "Confirm the target path and preserve a backup before rerunning this action.",
    ),
    RiskRule(
        "git_history_rewrite",
        "high",
        "Git history or workspace reset",
        re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*[fdx]|push\s+--force(?:-with-lease)?)\b", re.I),
        "Inspect the working tree and branch state before allowing the agent to retry.",
    ),
    RiskRule(
        "pipe_to_shell",
        "high",
        "Downloaded script piped to a shell",
        re.compile(
            r"\b(?:curl|wget|irm|iwr|Invoke-WebRequest|Invoke-RestMethod)\b[^\n|]*\|\s*"
            r"(?:sh|bash|zsh|pwsh|powershell|iex|Invoke-Expression)\b",
            re.I,
        ),
        "Replace pipe-to-shell setup with a pinned installer, checksum, or documented manual step.",
    ),
    RiskRule(
        "missed_verification",
        "medium",
        "Verification appears to have been skipped",
        re.compile(
            r"\b(?:tests?|verification|checks?)\s+(?:were\s+)?not\s+run\b|"
            r"\bdid\s+not\s+run\s+(?:tests?|verification|checks?)\b|"
            r"\bskipped\s+(?:tests?|verification|checks?)\b|"
            r"\bwithout\s+running\s+(?:tests?|verification|checks?)\b",
            re.I,
        ),
        "Capture the missing verification command and make it an explicit required step.",
    ),
    RiskRule(
        "permission_or_sandbox_block",
        "medium",
        "Tool call was blocked by permissions or sandboxing",
        re.compile(
            r"\b(permission denied|operation not permitted|access is denied|blocked by sandbox|"
            r"requires approval|not allowed by policy)\b",
            re.I,
        ),
        "Check whether the agent needed approval, a narrower command, or a different writable path.",
    ),
    RiskRule(
        "interactive_auth_flow",
        "high",
        "Interactive authentication flow blocks unattended agent execution",
        re.compile(
            r"\b(gh\s+auth\s+login|gcloud\s+auth\s+login|aws\s+configure|az\s+login|browser\s+flow|interactive\s+(?:terminal|authentication|auth)|requires\s+an\s+interactive\s+terminal)\b",
            re.I,
        ),
        "Preflight authentication with non-interactive status checks; stop and request human auth instead of launching login flows inside an unattended agent run.",
    ),
    RiskRule(
        "dependency_or_network_failure",
        "medium",
        "Dependency or network failure",
        re.compile(
            r"\b(timed out|timeout|timeouterror|readtimeout|connecttimeout|"
            r"econnreset|econnrefused|enotfound|temporary failure in name resolution|"
            r"could not resolve host|http 429|rate[- ]?limited?|quota exceeded)\b",
            re.I,
        ),
        "Retry with service status, credentials, timeout, retry, and backoff checks in view.",
    ),
]


def event_dicts(events: Iterable[BlackboxEvent]) -> list[dict[str, Any]]:
    return [event.to_dict() for event in events]


def event_from_dict(data: dict[str, Any]) -> BlackboxEvent:
    schema_version = data.get("schema_version")
    if schema_version not in {None, ANALYSIS_SCHEMA_VERSION}:
        raise ValueError(f"unsupported event schema_version: {schema_version}")
    event_type = data.get("event_type") or data.get("type")
    if not event_type:
        raise ValueError("black-box event is missing event_type")
    event_type = str(event_type)
    if event_type not in SUPPORTED_EVENT_TYPES:
        raise ValueError(f"unsupported black-box event_type: {event_type}")
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    for key in ("arguments", "result", "command", "path"):
        if key in data and key not in metadata:
            metadata[key] = data[key]
    timestamp = data.get("timestamp") or data.get("time") or data.get("ts")
    tool_call_id = data.get("tool_call_id") or data.get("call_id") or data.get("id")
    tool_name = data.get("tool_name") or data.get("name")
    return BlackboxEvent(
        timestamp=str(timestamp) if timestamp is not None else None,
        source=str(data.get("source") or "custom"),
        session_id=(str(data.get("session_id") or data.get("thread_id") or data.get("run_id"))
                    if (data.get("session_id") or data.get("thread_id") or data.get("run_id")) is not None
                    else None),
        event_type=str(event_type),
        severity=str(data.get("severity") or "INFO").upper(),
        summary=sanitize(str(data.get("summary") or data.get("message") or "")),
        tool_call_id=str(tool_call_id) if tool_call_id is not None else None,
        tool_name=str(tool_name) if tool_name is not None else None,
        metadata={str(key): safe_json_excerpt(value) for key, value in metadata.items()},
    )


def load_blackbox_events(path: Path) -> list[BlackboxEvent]:
    try:
        file_size = path.stat().st_size
    except OSError:
        raise
    if file_size > MAX_EVENT_FILE_BYTES:
        raise ValueError(f"event file exceeds {MAX_EVENT_FILE_BYTES} bytes; use a smaller trace")

    records: list[dict[str, Any]] = []
    if path.suffix.lower() == ".jsonl":
        with path.open("r", encoding="utf-8", errors="strict") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSONL at line {line_number}: {exc.msg}") from exc
                if not isinstance(item, dict):
                    raise ValueError(f"invalid JSONL at line {line_number}: each record must be an object")
                records.append(item)
    else:
        text = path.read_text(encoding="utf-8", errors="strict")
        if not text.strip():
            return []
        parsed = json.loads(text)
        if isinstance(parsed, dict) and isinstance(parsed.get("events"), list):
            schema_version = parsed.get("schema_version")
            if schema_version not in {None, ANALYSIS_SCHEMA_VERSION}:
                raise ValueError(f"unsupported event envelope schema_version: {schema_version}")
            raw_records = parsed["events"]
        elif isinstance(parsed, list):
            raw_records = parsed
        elif isinstance(parsed, dict):
            raw_records = [parsed]
        else:
            raise ValueError("black-box event file must contain an object, list, JSONL records, or {'events': [...]}")
        for index, item in enumerate(raw_records, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"event record {index} must be an object")
            records.append(item)

    return [event_from_dict(item) for item in records]


def command_to_text(command: object) -> str:
    if isinstance(command, list):
        return shlex.join(str(part) for part in command)
    if command is None:
        return ""
    return str(command)


SENSITIVE_FIELD_NAMES = {
    "api_key",
    "apikey",
    "authorization",
    "client_secret",
    "password",
    "refresh_token",
    "secret",
    "token",
    "access_token",
}
PATH_FIELD_NAMES = {"path", "cwd", "directory", "working_directory"}


def _sanitize_structured(value: object, key: str | None = None) -> object:
    normalized_key = (key or "").lower().replace("-", "_")
    if normalized_key in SENSITIVE_FIELD_NAMES:
        return "[REDACTED_SECRET]"
    if normalized_key in PATH_FIELD_NAMES and isinstance(value, str):
        return re.split(r"[\\/]", value.rstrip("\\/"))[-1]
    if isinstance(value, dict):
        return {str(item_key): _sanitize_structured(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_structured(item) for item in value]
    if isinstance(value, str):
        return sanitize(value)
    return value


def safe_json_excerpt(value: object, max_chars: int = 600) -> str:
    if value is None:
        return ""
    sanitized_value = _sanitize_structured(value)
    if isinstance(sanitized_value, str):
        text = sanitized_value
    else:
        try:
            text = json.dumps(sanitized_value, sort_keys=True)
        except TypeError:
            text = sanitize(str(sanitized_value))
    text = sanitize(text)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def parse_iso_timestamp(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def detect_risk_findings(text: str, scope: str, max_items: int = 8) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    sanitized_text = sanitize(text or "")

    if sanitized_text != (text or "") or "[REDACTED_SECRET]" in sanitized_text:
        findings.append(
            {
                "code": "secret_exposure",
                "severity": "high",
                "label": "Secret-shaped value was present and redacted",
                "scope": scope,
                "line": None,
                "snippet": "[REDACTED_SECRET]",
                "recommendation": "Rotate the exposed value if it reached logs, chat, CI, or a shared postmortem.",
            }
        )

    for line_number, line in enumerate(sanitized_text.splitlines() or [sanitized_text], start=1):
        snippet = line.strip()
        if not snippet:
            continue
        for rule in RISK_RULES:
            if not rule.pattern.search(snippet):
                continue
            key = (rule.code, snippet)
            if key in seen:
                break
            seen.add(key)
            findings.append(
                {
                    "code": rule.code,
                    "severity": rule.severity,
                    "label": rule.label,
                    "scope": scope,
                    "line": line_number,
                    "snippet": _clip(snippet, 260),
                    "recommendation": rule.recommendation,
                }
            )
            break
        if len(findings) >= max_items:
            break
    return findings[:max_items]


def analyze_blackbox_events(events: Iterable[BlackboxEvent]) -> dict[str, Any]:
    ordered = sorted(
        list(events),
        key=lambda event: parse_iso_timestamp(event.timestamp) or dt.datetime.max,
    )
    counts: dict[str, int] = {}
    tools_used: dict[str, int] = {}
    open_calls: dict[str, dict[str, Any]] = {}
    completed_calls: list[dict[str, Any]] = []
    failed_calls: list[dict[str, Any]] = []
    risk_findings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    user_prompts = 0
    assistant_turns = 0

    timestamped_events = sum(1 for event in ordered if parse_iso_timestamp(event.timestamp))
    invalid_timestamp_events = sum(
        1 for event in ordered if event.timestamp is not None and parse_iso_timestamp(event.timestamp) is None
    )
    duplicate_event_keys: set[tuple[Any, ...]] = set()
    duplicate_event_count = 0
    for event in ordered:
        key = (
            event.timestamp,
            event.source,
            event.session_id,
            event.event_type,
            event.tool_call_id,
            event.tool_name,
            event.summary,
        )
        if key in duplicate_event_keys:
            duplicate_event_count += 1
        duplicate_event_keys.add(key)

    for index, event in enumerate(ordered, start=1):
        counts[event.event_type] = counts.get(event.event_type, 0) + 1
        severity = (event.severity or "INFO").upper()
        if event.event_type == "user_prompt":
            user_prompts += 1
        elif event.event_type == "assistant_message":
            assistant_turns += 1

        event_text = " ".join(
            str(part)
            for part in (
                event.summary,
                event.metadata.get("arguments") if event.metadata else None,
                event.metadata.get("result") if event.metadata else None,
            )
            if part
        )
        risk_findings.extend(_dedupe_new_risks(risk_findings, detect_risk_findings(event_text, event.event_type, max_items=4)))

        if severity in {"ERROR", "CRITICAL"} or event.event_type in {"tool_call_failed", "error"}:
            errors.append(_event_error(event))

        if event.event_type == "tool_call_started":
            call_id = event.tool_call_id or f"{event.tool_name or 'tool'}#{index}"
            scoped_call_id = _scoped_call_id(event, call_id)
            tool_name = event.tool_name or "unknown_tool"
            tools_used[tool_name] = tools_used.get(tool_name, 0) + 1
            open_calls[scoped_call_id] = {
                "call_id": call_id,
                "name": tool_name,
                "started_at": event.timestamp,
                "arguments": event.metadata.get("arguments") if event.metadata else None,
                "source": event.source,
                "session_id": event.session_id,
            }
            continue

        if event.event_type in {"tool_call_completed", "tool_call_failed"}:
            call_id = _match_open_call(event, open_calls)
            base = open_calls.pop(call_id, None) if call_id else None
            tool_name = event.tool_name or (base or {}).get("name") or "unknown_tool"
            call = {
                "call_id": (base or {}).get("call_id") or event.tool_call_id,
                "name": tool_name,
                "started_at": (base or {}).get("started_at"),
                "completed_at": event.timestamp,
                "arguments": (base or {}).get("arguments"),
                "result": event.metadata.get("result") if event.metadata else event.summary,
                "source": event.source,
                "session_id": event.session_id,
            }
            if event.event_type == "tool_call_failed" or severity in {"ERROR", "CRITICAL"}:
                call["status"] = "failed"
                failed_calls.append(call)
            else:
                call["status"] = "completed"
                completed_calls.append(call)

    missed_calls = [
        {
            **call,
            "status": "missing_tool_result",
            "recommendation": "Find the matching tool output or platform error after this start event.",
        }
        for call in open_calls.values()
    ]

    if missed_calls:
        status = "hung_or_missing_tool_result"
    elif failed_calls:
        status = "tool_failure"
    elif errors:
        status = "completed_with_errors"
    elif any(item["severity"] == "high" for item in risk_findings):
        status = "completed_with_risks"
    else:
        status = "completed"

    evidence_gaps: list[str] = []
    if not ordered:
        evidence_gaps.append("No events were captured.")
    if invalid_timestamp_events:
        evidence_gaps.append(f"{invalid_timestamp_events} event(s) have invalid timestamps and were ordered last.")
    if any(event.timestamp is None for event in ordered):
        evidence_gaps.append("Some events have no timestamp, so exact ordering and latency are uncertain.")
    if duplicate_event_count:
        evidence_gaps.append(f"{duplicate_event_count} duplicate event record(s) were retained; confirm whether the source retried delivery.")
    if missed_calls:
        evidence_gaps.append("At least one tool start has no matching result in the captured trace.")

    recommendations = _analysis_recommendations(
        status,
        risk_findings,
        missed_calls,
        failed_calls,
        errors,
    )

    return {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "status": status,
        "counts": counts,
        "user_prompts": user_prompts,
        "assistant_turns": assistant_turns,
        "tool_calls": sum(tools_used.values()),
        "tools_used": tools_used,
        "completed_tool_calls": completed_calls,
        "failed_tool_calls": failed_calls,
        "missed_tool_calls": missed_calls,
        "errors": errors,
        "risk_findings": risk_findings[:12],
        "quality": {
            "event_count": len(ordered),
            "timestamped_event_count": timestamped_events,
            "untimestamped_event_count": sum(1 for event in ordered if event.timestamp is None),
            "invalid_timestamp_count": invalid_timestamp_events,
            "duplicate_event_count": duplicate_event_count,
            "evidence_gaps": evidence_gaps,
        },
        "incident": status != "completed",
        "recommendations": recommendations,
        "timeline": event_dicts(ordered),
    }


def _analysis_recommendations(
    status: str,
    risks: list[dict[str, Any]],
    missed_calls: list[dict[str, Any]],
    failed_calls: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> list[str]:
    recommendations: list[str] = []
    if missed_calls:
        recommendations.append("Locate the missing tool result or mark the run incomplete before trusting its final message.")
    if failed_calls:
        recommendations.append("Reproduce the failed tool call with the captured arguments and inspect the first failure, not only the final summary.")
    if errors and not failed_calls:
        recommendations.append("Inspect the earliest error event and its surrounding tool call before retrying the run.")
    if any(item.get("code") == "interactive_auth_flow" for item in risks):
        recommendations.append("Preflight authentication outside the unattended run and use a non-interactive credential check.")
    if any(item.get("code") == "missed_verification" for item in risks):
        recommendations.append("Run an explicit test, lint, typecheck, or build command and capture its result.")
    if any(item.get("severity") == "high" for item in risks):
        recommendations.append("Review high-severity actions before rerunning; narrow permissions and confirm targets.")
    if not recommendations and status == "completed":
        recommendations.append("No incident signal was found in captured evidence; confirm task correctness separately.")
    return list(dict.fromkeys(recommendations))[:6]


def find_recordings(runs_dir: Path) -> list[Path]:
    if not runs_dir.exists():
        return []
    return sorted(
        (path for path in runs_dir.glob("run_*.json") if path.is_file() and not path.is_symlink()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def find_latest_recording(runs_dir: Path) -> Path | None:
    recordings = find_recordings(runs_dir)
    return recordings[0] if recordings else None


def _match_open_call(event: BlackboxEvent, open_calls: dict[str, dict[str, Any]]) -> str | None:
    if event.tool_call_id:
        scoped = _scoped_call_id(event, event.tool_call_id)
        if scoped in open_calls:
            return scoped
    if event.tool_name:
        matches = [
            call_id
            for call_id, call in open_calls.items()
            if call.get("name") == event.tool_name
            and call.get("source") == event.source
            and call.get("session_id") == event.session_id
        ]
        if matches:
            return matches[-1]
    scoped_calls = [
        call_id
        for call_id, call in open_calls.items()
        if call.get("source") == event.source and call.get("session_id") == event.session_id
    ]
    if len(scoped_calls) == 1:
        return scoped_calls[0]
    return None


def _scoped_call_id(event: BlackboxEvent, call_id: str) -> str:
    return "\x1f".join((event.source, event.session_id or "", str(call_id)))


def _event_error(event: BlackboxEvent) -> dict[str, Any]:
    return {
        "timestamp": event.timestamp,
        "event_type": event.event_type,
        "severity": event.severity,
        "summary": _clip(sanitize(event.summary), 260),
        "tool_name": event.tool_name,
        "tool_call_id": event.tool_call_id,
    }


def _dedupe_new_risks(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = {(item["code"], item.get("snippet")) for item in existing}
    fresh = []
    for item in incoming:
        key = (item["code"], item.get("snippet"))
        if key in seen:
            continue
        seen.add(key)
        fresh.append(item)
    return fresh


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."
