from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from pathlib import Path

from .blackbox import (
    BlackboxEvent,
    analyze_blackbox_events,
    parse_iso_timestamp,
    safe_json_excerpt,
)

DEFAULT_CODEX_DIR = Path.home() / ".codex"


def find_codex_sessions(codex_dir: Path = DEFAULT_CODEX_DIR) -> list[dict]:
    """Locate Codex threads and their rollout transcript JSONL files."""
    db_path = codex_dir / "state_5.sqlite"
    sessions: list[dict] = []

    # Try database discovery first
    if db_path.exists():
        try:
            with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=2) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, title, rollout_path, cwd, model, tokens_used, updated_at
                    FROM threads
                    ORDER BY updated_at DESC;
                """)
                rows = cursor.fetchall()
            for r in rows:
                tid, title, rollout_path_str, cwd, model, tokens, mtime_ts = r
                if rollout_path_str:
                    path = Path(rollout_path_str)
                    if (
                        path.is_file()
                        and not path.is_symlink()
                        and path.resolve().is_relative_to(codex_dir.resolve())
                    ):
                        sessions.append({
                            "id": tid,
                            "title": title or "Untitled Session",
                            "path": path,
                            "cwd": cwd,
                            "model": model,
                            "tokens_used": tokens or 0,
                            "updated_at": dt.datetime.fromtimestamp(mtime_ts)
                        })
            return sessions
        except (OSError, sqlite3.Error):
            pass

    # Fallback to filesystem discovery under ~/.codex/sessions
    sessions_dir = codex_dir / "sessions"
    if sessions_dir.exists():
        for p in sorted(
            (
                path
                for path in sessions_dir.glob("**/rollout-*.jsonl")
                if path.is_file() and not path.is_symlink()
            ),
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        ):
            mtime = dt.datetime.fromtimestamp(p.stat().st_mtime)
            # Try to extract the ID from filename rollout-YYYY-MM-DDTHH-MM-SS-<uuid>.jsonl
            stem = p.stem
            parts = stem.split("-")
            tid = parts[-1] if len(parts) > 1 else stem
            sessions.append({
                "id": tid,
                "title": f"Session {tid[:8]}...",
                "path": p,
                "cwd": "Unknown",
                "model": "Unknown",
                "tokens_used": 0,
                "updated_at": mtime
            })
    return sessions


def iter_codex_events(session_path: Path) -> list[BlackboxEvent]:
    """Normalize a Codex rollout JSONL transcript into black-box events."""
    events: list[BlackboxEvent] = []
    session_id = session_path.stem.replace("rollout-", "")

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
                        source="codex",
                        session_id=session_id,
                        event_type="error",
                        severity="ERROR",
                        summary=f"Malformed JSON record skipped at line {line_number}",
                    )
                )
                continue

            ts_str = data.get("timestamp")
            msg_type = data.get("type")
            payload = data.get("payload", {}) if isinstance(data.get("payload", {}), dict) else {}

            if msg_type == "event_msg":
                ev_type = payload.get("type")
                if ev_type == "user_message":
                    events.append(
                        BlackboxEvent(
                            timestamp=ts_str,
                            source="codex",
                            session_id=session_id,
                            event_type="user_prompt",
                            summary="User prompt received",
                        )
                    )
                elif ev_type == "agent_message":
                    events.append(
                        BlackboxEvent(
                            timestamp=ts_str,
                            source="codex",
                            session_id=session_id,
                            event_type="assistant_message",
                            summary="Assistant message emitted",
                        )
                    )

            elif msg_type == "response_item":
                payload_type = payload.get("type")
                if payload_type == "function_call":
                    tool_name = payload.get("name") or "unknown_tool"
                    events.append(
                        BlackboxEvent(
                            timestamp=ts_str,
                            source="codex",
                            session_id=session_id,
                            event_type="tool_call_started",
                            summary=f"Tool call started: {tool_name}",
                            tool_call_id=payload.get("call_id") or payload.get("id"),
                            tool_name=tool_name,
                            metadata={"arguments": safe_json_excerpt(payload.get("arguments"))},
                        )
                    )
                elif payload_type == "function_call_output":
                    result = safe_json_excerpt(payload.get("output") or payload.get("content") or payload.get("result"))
                    event_type = "tool_call_failed" if _looks_like_failed_tool_output(payload, result) else "tool_call_completed"
                    events.append(
                        BlackboxEvent(
                            timestamp=ts_str,
                            source="codex",
                            session_id=session_id,
                            event_type=event_type,
                            severity="ERROR" if event_type == "tool_call_failed" else "INFO",
                            summary="Tool call output received",
                            tool_call_id=payload.get("call_id"),
                            metadata={"result": result},
                        )
                    )
                elif payload_type == "message" and payload.get("role") == "assistant":
                    events.append(
                        BlackboxEvent(
                            timestamp=ts_str,
                            source="codex",
                            session_id=session_id,
                            event_type="assistant_message",
                            summary="Assistant message emitted",
                        )
                    )

            if _record_mentions_error(data):
                events.append(
                    BlackboxEvent(
                        timestamp=ts_str,
                        source="codex",
                        session_id=session_id,
                        event_type="error",
                        severity="ERROR",
                        summary=safe_json_excerpt(data, max_chars=260),
                    )
                )

    return events


def summarize_codex_session(session_path: Path) -> dict:
    """Parse a Codex rollout JSONL transcript and generate a privacy-safe summary."""
    start_time: dt.datetime | None = None
    end_time: dt.datetime | None = None

    mtime = dt.datetime.fromtimestamp(session_path.stat().st_mtime)

    try:
        events = iter_codex_events(session_path)
    except Exception as exc:
        return {
            "error": f"Failed to parse transcript: {exc}",
            "path": str(session_path),
            "mtime": mtime.isoformat()
        }

    for event in events:
        ts = parse_iso_timestamp(event.timestamp)
        if ts is None:
            continue
        if start_time is None or ts < start_time:
            start_time = ts
        if end_time is None or ts > end_time:
            end_time = ts

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
        "session_id": session_path.stem.replace("rollout-", ""),
        "path": str(session_path),
        "last_modified": mtime.isoformat(sep=" "),
        "start_time": start_time.isoformat(sep=" "),
        "end_time": end_time.isoformat(sep=" "),
        "duration_seconds": duration_seconds,
        "user_prompts": analysis["user_prompts"],
        "assistant_turns": analysis["assistant_turns"],
        "tool_calls": analysis["tool_calls"],
        "tools_used": analysis["tools_used"],
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


def _looks_like_failed_tool_output(payload: dict, result: str) -> bool:
    status = str(payload.get("status") or "").lower()
    if status in {"failed", "error", "errored"}:
        return True
    lowered = result.lower()
    return bool(
        re.search(
            r"(?:\berror:|\btraceback\b|\bpermission denied\b|\btimed out\b|"
            r"\b[1-9]\d*\s+failed\b|\b(?:command|tool|test|build|request)\s+failed\b)",
            lowered,
        )
    )


def _record_mentions_error(data: dict) -> bool:
    if data.get("level") == "error":
        return True
    payload = data.get("payload")
    if isinstance(payload, dict) and payload.get("type") in {"error", "model_error"}:
        return True
    return False
