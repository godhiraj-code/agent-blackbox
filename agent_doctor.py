from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_HERMES_HOME = Path.home() / "AppData" / "Local" / "hermes"
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
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


def diagnose_no_response(events: Iterable[Event], platform: str) -> dict:
    platform = platform.lower()
    events = list(events)
    connected = [e for e in events if e.event_type == "gateway_connected" and e.platform == platform]
    inbound = [e for e in events if e.event_type == "gateway_inbound_message" and e.platform == platform]
    flushes = [e for e in events if e.event_type == "gateway_outbound_flush" and (e.platform == platform or platform in e.message.lower())]
    cache = [e for e in events if e.event_type == "agent_cache_invalidated"]
    unrelated_noise = [
        e for e in events
        if e.event_type == "gateway_reconnect_warning" and e.platform and e.platform != platform
    ]
    same_platform_errors = [
        e for e in events
        if e.severity in {"WARNING", "ERROR", "CRITICAL"} and e.platform == platform
    ]

    status = "unknown_needs_deeper_trace"
    if not connected and not inbound:
        status = "message_not_seen_or_gateway_down"
    elif inbound and flushes:
        latest_inbound_ts = max((e.timestamp for e in inbound if e.timestamp), default=None)
        later_flushes = [e for e in flushes if latest_inbound_ts is None or (e.timestamp and e.timestamp >= latest_inbound_ts)]
        status = "response_flushed" if later_flushes else "response_may_have_flushed_before_latest_message"
    elif inbound and not flushes:
        status = "agent_busy_or_response_pending"
    elif connected and not inbound:
        status = "gateway_connected_but_no_recent_message"

    return {
        "status": status,
        "connected_count": len(connected),
        "inbound_count": len(inbound),
        "flush_count": len(flushes),
        "cache_invalidation_count": len(cache),
        "unrelated_noise_count": len(unrelated_noise),
        "same_platform_error_count": len(same_platform_errors),
        "latest_inbound": inbound[-1].message if inbound else None,
        "latest_flush": flushes[-1].message if flushes else None,
        "notes": build_notes(status, unrelated_noise, cache, same_platform_errors),
    }


def build_notes(status: str, unrelated_noise: list[Event], cache: list[Event], same_platform_errors: list[Event]) -> list[str]:
    notes: list[str] = []
    if status == "response_flushed":
        notes.append("The gateway saw the inbound message and later flushed a Discord response. This points to delay/busy-turn, not a dead Discord gateway.")
    if status == "agent_busy_or_response_pending":
        notes.append("The gateway saw the inbound message but no outbound flush was found in the selected window. The agent may still be busy, stuck in a tool call, or failed before response delivery.")
    if unrelated_noise:
        platforms = sorted({e.platform or "unknown" for e in unrelated_noise})
        notes.append(f"Unrelated platform reconnect noise was present: {', '.join(platforms)}.")
    if cache:
        notes.append("Agent cache invalidation/cross-process write was observed; this can happen when the same gateway session changes while a turn is active.")
    if same_platform_errors:
        notes.append("Same-platform warnings/errors were present and should be inspected.")
    return notes


def run_gateway_status() -> str:
    try:
        proc = subprocess.run("hermes gateway status", capture_output=True, text=True, shell=True, timeout=60)
        return sanitize((proc.stdout or proc.stderr).strip())
    except Exception as exc:
        return f"Could not run gateway status: {exc}"


def print_gateway(events: list[Event], platform: str | None = None) -> None:
    for event in events:
        if platform and event.platform != platform and platform not in event.message.lower():
            continue
        if event.event_type in {"gateway_connected", "gateway_inbound_message", "gateway_outbound_flush", "gateway_reconnect_warning", "agent_cache_invalidated", "warning_or_error"}:
            ts = event.timestamp.isoformat(sep=" ") if event.timestamp else "unknown-time"
            print(f"{ts} [{event.severity}] {event.event_type} {event.platform or '-'} :: {event.message}")


def postmortem(events: list[Event], diagnosis: dict, platform: str) -> str:
    lines = [
        "# Agent Incident Postmortem",
        "",
        f"Generated at: {dt.datetime.now().isoformat(sep=' ', timespec='seconds')}",
        f"Platform: `{platform}`",
        "",
        "## Diagnosis",
        "",
        f"Status: `{diagnosis['status']}`",
        "",
        "## Counts",
        "",
        f"- Gateway connected events: {diagnosis['connected_count']}",
        f"- Inbound messages: {diagnosis['inbound_count']}",
        f"- Outbound flushes: {diagnosis['flush_count']}",
        f"- Cache invalidations: {diagnosis['cache_invalidation_count']}",
        f"- Unrelated platform noise events: {diagnosis['unrelated_noise_count']}",
        f"- Same-platform errors: {diagnosis['same_platform_error_count']}",
        "",
        "## Notes",
        "",
    ]
    if diagnosis["notes"]:
        lines.extend(f"- {note}" for note in diagnosis["notes"])
    else:
        lines.append("- No specific notes generated.")
    lines.extend(["", "## Timeline", ""])
    for event in events:
        if event.event_type == "log_line":
            continue
        ts = event.timestamp.isoformat(sep=" ") if event.timestamp else "unknown-time"
        lines.append(f"- `{ts}` **{event.event_type}** `{event.platform or '-'}` — {event.message}")
    lines.extend(["", "## Recommended Next Step", ""])
    if diagnosis["status"] == "response_flushed":
        lines.append("Treat this as a latency/busy-turn UX problem. Add active-turn visibility before touching Discord config.")
    elif diagnosis["status"] == "agent_busy_or_response_pending":
        lines.append("Inspect active tool calls/processes and gateway agent logs for a stuck turn.")
    else:
        lines.append("Increase the time window, inspect raw gateway logs, and verify gateway status.")
    lines.append("")
    return "\n".join(lines)


def cmd_doctor(args: argparse.Namespace) -> int:
    print("Agent Blackbox MVP doctor")
    print(f"Hermes home: {args.hermes_home}")
    print(f"Gateway log: {log_path(Path(args.hermes_home))}")
    print("\nGateway status:\n" + run_gateway_status())
    return 0


def cmd_gateway(args: argparse.Namespace) -> int:
    events = read_events(args.minutes, Path(args.hermes_home))
    print_gateway(events, args.platform)
    return 0


def cmd_why(args: argparse.Namespace) -> int:
    events = read_events(args.minutes, Path(args.hermes_home))
    diagnosis = diagnose_no_response(events, args.platform)
    print(f"status: {diagnosis['status']}")
    print(f"connected_count: {diagnosis['connected_count']}")
    print(f"inbound_count: {diagnosis['inbound_count']}")
    print(f"flush_count: {diagnosis['flush_count']}")
    print(f"unrelated_noise_count: {diagnosis['unrelated_noise_count']}")
    for note in diagnosis["notes"]:
        print(f"note: {note}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    events = read_events(args.minutes, Path(args.hermes_home))
    diagnosis = diagnose_no_response(events, args.platform)
    text = postmortem(events, diagnosis, args.platform)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    events = read_events(args.minutes, Path(args.hermes_home))
    q = args.query.lower()
    for event in events:
        if q in event.message.lower():
            ts = event.timestamp.isoformat(sep=" ") if event.timestamp else "unknown-time"
            print(f"{ts} {event.message}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local-first flight recorder for AI coding agents")
    parser.add_argument("--hermes-home", default=str(DEFAULT_HERMES_HOME))
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor")
    doctor.set_defaults(func=cmd_doctor)

    gateway = sub.add_parser("gateway")
    gateway.add_argument("--minutes", type=int, default=180)
    gateway.add_argument("--platform", default=None)
    gateway.set_defaults(func=cmd_gateway)

    why = sub.add_parser("why-no-response")
    why.add_argument("--platform", default="discord")
    why.add_argument("--minutes", type=int, default=180)
    why.set_defaults(func=cmd_why)

    export = sub.add_parser("export-postmortem")
    export.add_argument("--platform", default="discord")
    export.add_argument("--minutes", type=int, default=180)
    export.add_argument("--out", required=True)
    export.set_defaults(func=cmd_export)

    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--minutes", type=int, default=1440)
    search.set_defaults(func=cmd_search)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
