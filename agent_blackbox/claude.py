from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .blackbox import BlackboxEvent, analyze_blackbox_events, parse_iso_timestamp, safe_json_excerpt

DEFAULT_CLAUDE_DIR = Path.home() / ".claude"


def find_claude_projects(claude_dir: Path = DEFAULT_CLAUDE_DIR) -> dict[str, list[Path]]:
    """Locate Claude Code projects and their session transcript JSONL files."""
    projects_dir = claude_dir / "projects"
    if not projects_dir.exists():
        return {}

    projects: dict[str, list[Path]] = {}
    for p_path in projects_dir.iterdir():
        if p_path.is_dir():
            # Find JSONL or JSON transcript files
            transcripts = sorted(
                (
                    path
                    for path in list(p_path.glob("*.jsonl")) + list(p_path.glob("session-*.json"))
                    if path.is_file() and not path.is_symlink()
                ),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if transcripts:
                projects[p_path.name] = transcripts
    return projects


def iter_claude_events(session_path: Path) -> list[BlackboxEvent]:
    """Normalize a Claude Code transcript into black-box events."""
    events: list[BlackboxEvent] = []
    session_id = session_path.stem.replace("session-", "")
    pending_without_id: list[str] = []

    with session_path.open("r", encoding="utf-8", errors="ignore") as f:
        for line_number, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                events.append(
                    BlackboxEvent(
                        timestamp=None,
                        source="claude",
                        session_id=session_id,
                        event_type="error",
                        severity="ERROR",
                        summary=f"Malformed JSON record skipped at line {line_number}",
                    )
                )
                continue

            ts_str = data.get("timestamp")
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            role = data.get("role") or message.get("role")
            msg_type = data.get("type")
            content = data.get("content") if data.get("content") is not None else message.get("content")

            if role == "user" and not _contains_tool_result(content):
                events.append(
                    BlackboxEvent(
                        timestamp=ts_str,
                        source="claude",
                        session_id=session_id,
                        event_type="user_prompt",
                        summary="User prompt received",
                    )
                )
            elif role == "assistant":
                events.append(
                    BlackboxEvent(
                        timestamp=ts_str,
                        source="claude",
                        session_id=session_id,
                        event_type="assistant_message",
                        summary="Assistant message emitted",
                    )
                )

            for part in _content_parts(content):
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_use":
                    tool_name = part.get("name") or "unknown_tool"
                    call_id = part.get("id")
                    if call_id is None:
                        call_id = f"{tool_name}:{len(pending_without_id) + 1}"
                        pending_without_id.append(call_id)
                    events.append(
                        BlackboxEvent(
                            timestamp=ts_str,
                            source="claude",
                            session_id=session_id,
                            event_type="tool_call_started",
                            summary=f"Tool call started: {tool_name}",
                            tool_call_id=call_id,
                            tool_name=tool_name,
                            metadata={"arguments": safe_json_excerpt(part.get("input"))},
                        )
                    )
                elif part.get("type") == "tool_result":
                    is_error = bool(part.get("is_error"))
                    call_id = part.get("tool_use_id")
                    if call_id is None and pending_without_id:
                        call_id = pending_without_id.pop(0)
                    events.append(
                        BlackboxEvent(
                            timestamp=ts_str,
                            source="claude",
                            session_id=session_id,
                            event_type="tool_call_failed" if is_error else "tool_call_completed",
                            severity="ERROR" if is_error else "INFO",
                            summary="Tool result received",
                            tool_call_id=call_id,
                            metadata={"result": safe_json_excerpt(part.get("content"))},
                        )
                    )

            if msg_type == "tool_result":
                is_error = bool(data.get("is_error"))
                call_id = data.get("tool_use_id")
                if call_id is None and pending_without_id:
                    call_id = pending_without_id.pop(0)
                events.append(
                    BlackboxEvent(
                        timestamp=ts_str,
                        source="claude",
                        session_id=session_id,
                        event_type="tool_call_failed" if is_error else "tool_call_completed",
                        severity="ERROR" if is_error else "INFO",
                        summary="Tool result received",
                        tool_call_id=call_id,
                        metadata={"result": safe_json_excerpt(content)},
                    )
                )
            elif msg_type == "error" or data.get("level") == "error":
                events.append(
                    BlackboxEvent(
                        timestamp=ts_str,
                        source="claude",
                        session_id=session_id,
                        event_type="error",
                        severity="ERROR",
                        summary=safe_json_excerpt(data, max_chars=260),
                    )
                )

    return events


def summarize_claude_session(session_path: Path) -> dict:
    """Parse a Claude Code JSONL transcript and generate a privacy-safe summary."""
    start_time: dt.datetime | None = None
    end_time: dt.datetime | None = None

    mtime = dt.datetime.fromtimestamp(session_path.stat().st_mtime)

    # If it is a standard JSONL file
    if session_path.suffix == ".jsonl":
        try:
            events = iter_claude_events(session_path)
        except Exception as exc:
            return {
                "error": f"Failed to parse transcript: {exc}",
                "path": str(session_path),
                "mtime": mtime.isoformat()
            }
    else:
        events = []

    for event in events:
        ts = parse_iso_timestamp(event.timestamp)
        if ts is None:
            continue
        if start_time is None or ts < start_time:
            start_time = ts
        if end_time is None or ts > end_time:
            end_time = ts

    # Default to file mtime if no timestamps in JSONL
    if start_time is None:
        start_time = mtime
    if end_time is None:
        end_time = mtime

    duration_seconds = int((end_time - start_time).total_seconds())
    analysis = analyze_blackbox_events(events)
    unsupported_input = session_path.stat().st_size > 0 and not any(
        event.event_type != "error" for event in events
    )

    return {
        "session_id": session_path.stem.replace("session-", ""),
        "path": str(session_path),
        "last_modified": mtime.isoformat(sep=" "),
        "start_time": start_time.isoformat(sep=" "),
        "end_time": end_time.isoformat(sep=" "),
        "duration_seconds": duration_seconds,
        "user_prompts": analysis["user_prompts"],
        "assistant_turns": analysis["assistant_turns"],
        "tool_calls": analysis["tool_calls"],
        "tools_used": sorted(analysis["tools_used"].keys()),
        "tool_usage_counts": analysis["tools_used"],
        "errors_count": max(len(analysis["errors"]), 1 if unsupported_input else 0),
        "tool_hangs": analysis["missed_tool_calls"],
        "tool_failures": analysis["failed_tool_calls"],
        "risk_findings": analysis["risk_findings"],
        "blackbox_status": "invalid_or_unsupported_input" if unsupported_input else analysis["status"],
        "status": (
            "invalid_or_unsupported_input"
            if unsupported_input
            else "hung" if analysis["missed_tool_calls"] else analysis["status"]
        ),
    }


def _content_parts(content: object) -> list[object]:
    if isinstance(content, list):
        return content
    if isinstance(content, dict):
        return [content]
    return []


def _contains_tool_result(content: object) -> bool:
    return any(isinstance(part, dict) and part.get("type") == "tool_result" for part in _content_parts(content))
