from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Iterable

from .blackbox import BlackboxEvent, analyze_blackbox_events, safe_json_excerpt

DEFAULT_OPENCODE_DIR = Path.home() / ".local" / "share" / "opencode"


def find_opencode_sessions(opencode_dir: Path = DEFAULT_OPENCODE_DIR) -> list[dict[str, Any]]:
    """Locate OpenCode session metadata files without printing transcript content."""
    sessions: list[dict[str, Any]] = []
    for storage_dir in _candidate_storage_dirs(opencode_dir):
        session_root = storage_dir / "session"
        if not session_root.exists():
            continue
        for path in sorted(
            (
                candidate
                for candidate in session_root.glob("**/ses_*.json")
                if candidate.is_file() and not candidate.is_symlink()
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            data = _read_json_object(path)
            session_id = str(data.get("id") or data.get("sessionID") or path.stem)
            project = path.parent.name
            message_dir = storage_dir / "message" / session_id
            sessions.append(
                {
                    "id": session_id,
                    "title": str(data.get("title") or data.get("summary") or "Untitled Session"),
                    "path": path,
                    "storage_dir": storage_dir,
                    "project": project,
                    "directory": data.get("directory") or data.get("cwd") or data.get("path") or "Unknown",
                    "updated_at": _coerce_datetime(
                        data.get("time_updated")
                        or data.get("updated")
                        or data.get("updatedAt")
                        or data.get("time")
                        or data.get("createdAt"),
                        fallback=dt.datetime.fromtimestamp(path.stat().st_mtime),
                    ),
                    "message_count": len(list(message_dir.glob("*.json"))) if message_dir.exists() else 0,
                }
            )
    return sessions


def iter_opencode_events(session_path: Path, storage_dir: Path | None = None) -> list[BlackboxEvent]:
    """Normalize an OpenCode exported JSON file or local session storage into black-box events."""
    parsed = _read_json(session_path)
    session_id = _session_id(parsed, session_path)
    records = _records_from_export(parsed)

    if not records and storage_dir is not None:
        message_dir = storage_dir / "message" / session_id
        if message_dir.exists():
            records = [
                _read_json(path)
                for path in sorted(message_dir.glob("*.json"))
                if path.is_file() and not path.is_symlink()
            ]

    events: list[BlackboxEvent] = []
    pending_without_id: list[str] = []
    for record in records:
        events.extend(_events_from_record(record, session_id, pending_without_id))
    return events


def summarize_opencode_session(session_path: Path, storage_dir: Path | None = None) -> dict[str, Any]:
    """Parse an OpenCode session/export and generate a privacy-safe summary."""
    mtime = dt.datetime.fromtimestamp(session_path.stat().st_mtime)
    try:
        events = iter_opencode_events(session_path, storage_dir=storage_dir)
    except Exception as exc:
        return {"error": f"Failed to parse OpenCode session: {exc}", "path": str(session_path), "mtime": mtime.isoformat()}

    start_time: dt.datetime | None = None
    end_time: dt.datetime | None = None
    for event in events:
        parsed_ts = _parse_event_time(event.timestamp)
        if parsed_ts is None:
            continue
        if start_time is None or parsed_ts < start_time:
            start_time = parsed_ts
        if end_time is None or parsed_ts > end_time:
            end_time = parsed_ts

    if start_time is None:
        start_time = mtime
    if end_time is None:
        end_time = mtime

    analysis = analyze_blackbox_events(events)
    return {
        "session_id": session_path.stem if session_path.stem.startswith("ses_") else _session_id(_read_json(session_path), session_path),
        "path": str(session_path),
        "last_modified": mtime.isoformat(sep=" "),
        "start_time": start_time.isoformat(sep=" "),
        "end_time": end_time.isoformat(sep=" "),
        "duration_seconds": int((end_time - start_time).total_seconds()),
        "user_prompts": analysis["user_prompts"],
        "assistant_turns": analysis["assistant_turns"],
        "tool_calls": analysis["tool_calls"],
        "tools_used": analysis["tools_used"],
        "errors_count": len(analysis["errors"]),
        "tool_hangs": analysis["missed_tool_calls"],
        "tool_failures": analysis["failed_tool_calls"],
        "risk_findings": analysis["risk_findings"],
        "blackbox_status": analysis["status"],
        "status": "hung" if analysis["missed_tool_calls"] else analysis["status"],
    }


def _candidate_storage_dirs(opencode_dir: Path) -> list[Path]:
    candidates = [opencode_dir / "storage"]
    project_dir = opencode_dir / "project"
    if project_dir.exists():
        candidates.extend(sorted(project_dir.glob("*/storage")))
    # Some old builds stored storage directly under the app data directory.
    candidates.append(opencode_dir)
    seen: set[Path] = set()
    result: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve() if candidate.exists() else candidate
        if resolved not in seen:
            seen.add(resolved)
            result.append(candidate)
    return result


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8", errors="ignore"))


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        data = _read_json(path)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _records_from_export(parsed: Any) -> list[Any]:
    if isinstance(parsed, list):
        return parsed
    if not isinstance(parsed, dict):
        return []
    for key in ("messages", "message", "events", "items"):
        value = parsed.get(key)
        if isinstance(value, list):
            return value
    if isinstance(parsed.get("parts"), list):
        return [parsed]
    return []


def _events_from_record(record: Any, session_id: str, pending_without_id: list[str]) -> list[BlackboxEvent]:
    if not isinstance(record, dict):
        return []

    events: list[BlackboxEvent] = []
    ts = _timestamp(record)
    role = str(record.get("role") or record.get("author") or "").lower()
    kind = str(record.get("type") or "").lower()

    if role == "user" or kind in {"user", "user_message", "user-prompt"}:
        events.append(BlackboxEvent(timestamp=ts, source="opencode", session_id=session_id, event_type="user_prompt", summary="User prompt received"))
    elif role == "assistant" or kind in {"assistant", "assistant_message", "assistant-message"}:
        events.append(BlackboxEvent(timestamp=ts, source="opencode", session_id=session_id, event_type="assistant_message", summary="Assistant message emitted"))

    for part in _parts(record):
        if not isinstance(part, dict):
            continue
        part_ts = _timestamp(part) or ts
        tool_invocation = part.get("toolInvocation") if isinstance(part.get("toolInvocation"), dict) else None
        node = tool_invocation or part
        part_type = str(part.get("type") or node.get("type") or "").lower().replace("_", "-")
        state = str(node.get("state") or node.get("status") or "").lower()
        tool_name = node.get("toolName") or node.get("tool_name") or node.get("name") or node.get("tool")
        call_id = node.get("toolCallId") or node.get("tool_call_id") or node.get("callID") or node.get("call_id") or node.get("id")

        if _is_tool_start(part_type, state, node):
            if call_id is None:
                call_id = f"{tool_name or 'tool'}:{len(pending_without_id) + 1}"
                pending_without_id.append(str(call_id))
            events.append(
                BlackboxEvent(
                    timestamp=part_ts,
                    source="opencode",
                    session_id=session_id,
                    event_type="tool_call_started",
                    summary=f"Tool call started: {tool_name or 'unknown_tool'}",
                    tool_call_id=str(call_id),
                    tool_name=str(tool_name or "unknown_tool"),
                    metadata={"arguments": safe_json_excerpt(node.get("args") or node.get("input") or node.get("arguments"))},
                )
            )

        if _is_tool_result(part_type, state, node):
            if call_id is None and pending_without_id:
                call_id = pending_without_id.pop(0)
            failed = state in {"error", "failed", "errored"} or bool(node.get("isError") or node.get("is_error"))
            result = node.get("result") or node.get("output") or node.get("content") or node.get("error")
            events.append(
                BlackboxEvent(
                    timestamp=part_ts,
                    source="opencode",
                    session_id=session_id,
                    event_type="tool_call_failed" if failed else "tool_call_completed",
                    severity="ERROR" if failed else "INFO",
                    summary="Tool result received",
                    tool_call_id=str(call_id) if call_id is not None else None,
                    tool_name=str(tool_name) if tool_name else None,
                    metadata={"result": safe_json_excerpt(result)},
                )
            )

    if kind in {"error", "model_error"} or str(record.get("level") or "").lower() == "error":
        events.append(BlackboxEvent(timestamp=ts, source="opencode", session_id=session_id, event_type="error", severity="ERROR", summary=safe_json_excerpt(record, max_chars=260)))

    return events


def _parts(record: dict[str, Any]) -> list[Any]:
    parts: list[Any] = []
    for key in ("parts", "content"):
        value = record.get(key)
        if isinstance(value, list):
            parts.extend(value)
        elif isinstance(value, dict):
            parts.append(value)
    if record.get("toolInvocation") or record.get("toolCallId") or record.get("tool_call_id"):
        parts.append(record)
    return parts


def _is_tool_start(part_type: str, state: str, node: dict[str, Any]) -> bool:
    if part_type in {"tool-use", "tool-call", "tool-invocation"} and state in {"", "call", "partial-call", "pending", "running"}:
        return True
    return bool((node.get("toolName") or node.get("tool_name") or node.get("tool")) and (node.get("args") is not None or node.get("input") is not None or node.get("arguments") is not None) and state not in {"result", "done", "completed", "error", "failed"})


def _is_tool_result(part_type: str, state: str, node: dict[str, Any]) -> bool:
    if part_type in {"tool-result", "tool-output"}:
        return True
    return state in {"result", "done", "completed", "error", "failed", "errored"} or any(key in node for key in ("result", "output", "error"))


def _timestamp(data: dict[str, Any]) -> str | None:
    value = data.get("timestamp") or data.get("time") or data.get("createdAt") or data.get("updatedAt")
    parsed = _coerce_datetime(value, fallback=None)
    return parsed.isoformat() if parsed else (str(value) if isinstance(value, str) and value else None)


def _session_id(parsed: Any, path: Path) -> str:
    if isinstance(parsed, dict):
        session = parsed.get("session") if isinstance(parsed.get("session"), dict) else parsed
        value = session.get("id") or session.get("sessionID") or session.get("session_id")
        if value:
            return str(value)
    return path.stem


def _coerce_datetime(value: Any, fallback: dt.datetime | None) -> dt.datetime | None:
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return dt.datetime.fromtimestamp(seconds)
    if isinstance(value, str) and value:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
            return parsed
        except ValueError:
            return fallback
    return fallback


def _parse_event_time(value: str | None) -> dt.datetime | None:
    return _coerce_datetime(value, fallback=None)
