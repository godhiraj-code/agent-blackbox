from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

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
                list(p_path.glob("*.jsonl")) + list(p_path.glob("session-*.json")),
                key=lambda p: p.stat().st_mtime,
                reverse=True
            )
            if transcripts:
                projects[p_path.name] = transcripts
    return projects


def summarize_claude_session(session_path: Path) -> dict:
    """Parse a Claude Code JSONL transcript and generate a privacy-safe summary."""
    user_prompts_count = 0
    assistant_turns_count = 0
    tool_calls_count = 0
    errors_count = 0
    tools_used: set[str] = set()
    start_time: dt.datetime | None = None
    end_time: dt.datetime | None = None

    mtime = dt.datetime.fromtimestamp(session_path.stat().st_mtime)

    # If it is a standard JSONL file
    if session_path.suffix == ".jsonl":
        try:
            with session_path.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except Exception:
                        continue

                    # Extract timestamp
                    ts_str = data.get("timestamp")
                    if ts_str:
                        try:
                            # Parse ISO timestamp (e.g. 2026-03-20T10:00:01Z)
                            ts = dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            if start_time is None or ts < start_time:
                                start_time = ts
                            if end_time is None or ts > end_time:
                                end_time = ts
                        except Exception:
                            pass

                    # Analyze message roles / types
                    role = data.get("role")
                    msg_type = data.get("type")

                    if role == "user":
                        user_prompts_count += 1
                    elif role == "assistant":
                        assistant_turns_count += 1
                        # Check for tool calls inside assistant message
                        content = data.get("content")
                        if isinstance(content, list):
                            for part in content:
                                if isinstance(part, dict) and part.get("type") == "tool_use":
                                    tool_calls_count += 1
                                    tool_name = part.get("name")
                                    if tool_name:
                                        tools_used.add(tool_name)
                    elif msg_type == "tool_result":
                        # Some versions log tool result as separate top-level types
                        pass
                    elif msg_type == "error" or data.get("level") == "error":
                        errors_count += 1
        except Exception as exc:
            return {
                "error": f"Failed to parse transcript: {exc}",
                "path": str(session_path),
                "mtime": mtime.isoformat()
            }

    # Default to file mtime if no timestamps in JSONL
    if start_time is None:
        start_time = mtime
    if end_time is None:
        end_time = mtime

    duration_seconds = int((end_time - start_time).total_seconds())

    return {
        "session_id": session_path.stem.replace("session-", ""),
        "path": str(session_path),
        "last_modified": mtime.isoformat(sep=" "),
        "start_time": start_time.isoformat(sep=" "),
        "end_time": end_time.isoformat(sep=" "),
        "duration_seconds": duration_seconds,
        "user_prompts": user_prompts_count,
        "assistant_turns": assistant_turns_count,
        "tool_calls": tool_calls_count,
        "tools_used": sorted(list(tools_used)),
        "errors_count": errors_count,
        "status": "completed" if errors_count == 0 else "completed_with_errors"
    }
