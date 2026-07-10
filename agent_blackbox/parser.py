from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HERMES_HOME = Path.home() / "AppData" / "Local" / "hermes"
DEFAULT_DEMO_LOG = Path(__file__).parent / "data" / "demo_gateway.txt"
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"(?i)\bauthorization\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._\-~+/=]+"),
    re.compile(
        r"(?i)\b(api[_-]?key|token|secret|password|access[_-]?token|refresh[_-]?token|client[_-]?secret)"
        r"\b\s*[:=]\s*[\"']?[^\"'\s,;]+[\"']?"
    ),
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{20,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    re.compile(r"(?i)\bhttps?://[^\s/:@]+:[^\s/@]+@"),
    re.compile(
        r"(?i)(?:--(?:api[-_]?key|token|password|secret|access[-_]?token|client[-_]?secret))"
        r"(?:=|\s+)[^\s]+"
    ),
    re.compile(
        r"(?i)(?:[\"']?(?:api[_-]?key|token|secret|password|access[_-]?token|refresh[_-]?token|client[_-]?secret)[\"']?)"
        r"\s*:\s*[\"'][^\"']*[\"']"
    ),
    re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),
]


@dataclass(frozen=True)
class Event:
    timestamp: dt.datetime | None
    event_type: str
    severity: str
    platform: str | None
    message: str
    raw: str


def parse_timestamp(line: str) -> dt.datetime | None:
    match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3}", line)
    if not match:
        return None
    return dt.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")


def sanitize(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    return redacted


def parse_log_line(line: str) -> Event | None:
    ts = parse_timestamp(line)
    severity = "INFO"
    sev_match = re.search(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b", line)
    if sev_match:
        severity = sev_match.group(1)

    lower = line.lower()
    platform = None
    if "discord" in lower:
        platform = "discord"
    elif "telegram" in lower:
        platform = "telegram"
    elif "whatsapp" in lower:
        platform = "whatsapp"

    event_type = "log_line"
    if "connected as" in lower and platform:
        event_type = "gateway_connected"
    elif "inbound message:" in lower:
        event_type = "gateway_inbound_message"
        platform_match = re.search(r"platform=([a-zA-Z0-9_\-]+)", line)
        if platform_match:
            platform = platform_match.group(1).lower()
    elif "flushing text batch" in lower:
        event_type = "gateway_outbound_flush"
    elif "reconnecting" in lower or "connect attempt" in lower or "connection failed" in lower or "timed out" in lower:
        event_type = "gateway_reconnect_warning"
    elif "agent cache invalidated" in lower:
        event_type = "agent_cache_invalidated"
    elif severity in {"WARNING", "ERROR", "CRITICAL"}:
        event_type = "warning_or_error"

    return Event(ts, event_type, severity, platform, sanitize(line.strip()), line.strip())


def log_path(hermes_home: Path = DEFAULT_HERMES_HOME) -> Path:
    return hermes_home / "logs" / "gateway.log"


def cron_jobs_path(hermes_home: Path = DEFAULT_HERMES_HOME) -> Path:
    return hermes_home / "cron" / "jobs.json"


def cron_output_dir(job_id: str, hermes_home: Path = DEFAULT_HERMES_HOME) -> Path:
    return hermes_home / "cron" / "output" / job_id


def read_events(minutes: int, hermes_home: Path = DEFAULT_HERMES_HOME) -> list[Event]:
    path = log_path(hermes_home)
    if not path.exists():
        return []
    cutoff = dt.datetime.now() - dt.timedelta(minutes=minutes)
    events: list[Event] = []
    for line in path.read_text(errors="ignore", encoding="utf-8").splitlines():
        event = parse_log_line(line)
        if not event:
            continue
        if event.timestamp and event.timestamp < cutoff:
            continue
        events.append(event)
    return events


def read_cron_jobs(hermes_home: Path = DEFAULT_HERMES_HOME) -> list[dict]:
    path = cron_jobs_path(hermes_home)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("jobs", [])


def find_cron_job(job_ref: str, hermes_home: Path = DEFAULT_HERMES_HOME) -> dict | None:
    needle = job_ref.lower()
    for job in read_cron_jobs(hermes_home):
        if job.get("id", "").lower() == needle:
            return job
        if needle in job.get("name", "").lower():
            return job
    return None


def latest_cron_output(job_id: str, hermes_home: Path = DEFAULT_HERMES_HOME) -> Path | None:
    out_dir = cron_output_dir(job_id, hermes_home)
    if not out_dir.exists():
        return None
    files = sorted(
        (path for path in out_dir.glob("*.md") if path.is_file() and not path.is_symlink()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None
