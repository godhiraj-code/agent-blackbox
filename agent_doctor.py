from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_HERMES_HOME = Path.home() / "AppData" / "Local" / "hermes"
DEFAULT_DEMO_LOG = Path(__file__).parent / "examples" / "demo_gateway.txt"
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
    files = sorted(out_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def diagnose_cron_job(job: dict, hermes_home: Path = DEFAULT_HERMES_HOME) -> dict:
    job_id = job.get("id", "")
    output_path = latest_cron_output(job_id, hermes_home)
    output_text = output_path.read_text(encoding="utf-8", errors="ignore") if output_path else ""
    last_error = job.get("last_error") or ""
    combined = f"{last_error}\n{output_text}".lower()

    status = "ok" if job.get("last_status") == "ok" else "cron_error"
    root_cause = "none" if status == "ok" else "unknown"
    recommendations: list[str] = []

    if status == "ok":
        recommendations.append("Latest cron run completed successfully. If output is empty, that is expected for a silent no_agent watchdog with no new mail.")
    if "script timed out" in combined or "timed out after" in combined or "timeoutexpired" in combined:
        root_cause = "script_timeout"
        status = "script_timed_out"
        recommendations.append("The cron wrapper hit its 120s script timeout. The failing layer is the script/subprocess, not Discord delivery.")
    if "atomicmail_poll.py" in combined and "npx" in combined or job.get("script") == "atomicmail_poll.py":
        recommendations.append("For Atomic Mail, inspect the nested npx atomicmail jmap_request call; it is the likely blocking dependency.")
    if job.get("no_agent"):
        recommendations.append("This is a no_agent script-only watchdog: empty stdout is silent, but non-zero exit/timeout becomes a cron error alert.")
    if job.get("last_delivery_error"):
        recommendations.append("Delivery also reported an error; inspect last_delivery_error separately.")
    if not recommendations:
        recommendations.append("Inspect the latest cron output file and rerun the script manually with a shorter internal timeout.")

    return {
        "job_id": job_id,
        "name": job.get("name"),
        "status": status,
        "last_status": job.get("last_status"),
        "last_run_at": job.get("last_run_at"),
        "next_run_at": job.get("next_run_at"),
        "script": job.get("script"),
        "no_agent": job.get("no_agent"),
        "last_error": sanitize(last_error) if last_error else None,
        "latest_output_path": str(output_path) if output_path else None,
        "latest_output_excerpt": sanitize(output_text[:1200]) if output_text else None,
        "root_cause": root_cause,
        "recommendations": recommendations,
    }


def cron_postmortem(diagnosis: dict) -> str:
    lines = [
        "# Cron Job Incident Postmortem",
        "",
        f"Generated at: {dt.datetime.now().isoformat(sep=' ', timespec='seconds')}",
        f"Job: `{diagnosis['name']}` (`{diagnosis['job_id']}`)",
        "",
        "## Diagnosis",
        "",
        f"Status: `{diagnosis['status']}`",
        f"Root cause: `{diagnosis['root_cause']}`",
        f"Last status: `{diagnosis['last_status']}`",
        f"Last run: `{diagnosis['last_run_at']}`",
        f"Next run: `{diagnosis['next_run_at']}`",
        f"Script: `{diagnosis['script']}`",
        f"No-agent mode: `{diagnosis['no_agent']}`",
        "",
        "## Last Error",
        "",
        diagnosis["last_error"] or "No last_error recorded.",
        "",
        "## Latest Output",
        "",
        f"Path: `{diagnosis['latest_output_path']}`",
        "",
        "```text",
        diagnosis["latest_output_excerpt"] or "No output file found.",
        "```",
        "",
        "## Recommended Next Steps",
        "",
    ]
    lines.extend(f"- {item}" for item in diagnosis["recommendations"])
    lines.append("")
    return "\n".join(lines)


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

    message_trace = correlate_inbound_flushes(inbound, flushes)

    status = "unknown_needs_deeper_trace"
    if not connected and not inbound:
        status = "message_not_seen_or_gateway_down"
    elif inbound and flushes:
        latest = message_trace[-1] if message_trace else None
        if latest and latest["responded"]:
            status = "response_flushed"
        elif any(not item["responded"] for item in message_trace):
            status = "agent_busy_or_response_pending"
        else:
            status = "response_may_have_flushed_before_latest_message"
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
        "message_trace": message_trace,
        "notes": build_notes(status, unrelated_noise, cache, same_platform_errors),
    }


def correlate_inbound_flushes(inbound: list[Event], flushes: list[Event]) -> list[dict]:
    """Pair every inbound message with the next outbound flush after it.

    Window-level counts are not good enough for a flight recorder. If three
    messages arrive and one flush occurs, the tool must show which messages got
    a later send-path event and which are still pending.
    """
    ordered_inbound = sorted(inbound, key=lambda e: e.timestamp or dt.datetime.min)
    ordered_flushes = sorted(flushes, key=lambda e: e.timestamp or dt.datetime.min)
    trace: list[dict] = []
    for index, msg in enumerate(ordered_inbound, start=1):
        msg_ts = msg.timestamp
        next_flush = next(
            (
                flush
                for flush in ordered_flushes
                if msg_ts is None or flush.timestamp is None or flush.timestamp >= msg_ts
            ),
            None,
        )
        latency_seconds = None
        if msg_ts and next_flush and next_flush.timestamp:
            latency_seconds = int((next_flush.timestamp - msg_ts).total_seconds())
        trace.append(
            {
                "index": index,
                "timestamp": msg_ts.isoformat(sep=" ") if msg_ts else None,
                "responded": next_flush is not None,
                "latency_seconds": latency_seconds,
                "inbound": msg.message,
                "flush": next_flush.message if next_flush else None,
            }
        )
    return trace


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
    if diagnosis.get("message_trace"):
        lines.extend(["", "## Message correlation", ""])
        for item in diagnosis["message_trace"]:
            status = "responded" if item["responded"] else "pending"
            latency = f"; latency: {item['latency_seconds']}s" if item["latency_seconds"] is not None else ""
            lines.append(f"- Message #{item['index']} at `{item['timestamp']}`: `{status}`{latency}")
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
    if diagnosis.get("message_trace"):
        print("message_trace:")
        for item in diagnosis["message_trace"]:
            status = "responded" if item["responded"] else "pending"
            latency = f" latency={item['latency_seconds']}s" if item["latency_seconds"] is not None else ""
            print(f"  #{item['index']} {status}{latency} at {item['timestamp']}")
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


def cmd_demo(args: argparse.Namespace) -> int:
    demo_log = Path(args.log)
    if not demo_log.exists():
        print(f"Demo log not found: {demo_log}")
        return 2

    events = [
        event
        for line in demo_log.read_text(encoding="utf-8", errors="ignore").splitlines()
        if (event := parse_log_line(line)) is not None
    ]
    diagnosis = diagnose_no_response(events, args.platform)

    print("Agent Blackbox demo")
    print(f"sample: {demo_log}")
    print(f"events parsed: {len(events)}")
    print_gateway(events, args.platform)
    print("\nDiagnosis")
    print(f"status: {diagnosis['status']}")
    for note in diagnosis["notes"]:
        print(f"note: {note}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(postmortem(events, diagnosis, args.platform), encoding="utf-8")
    print(f"\nSanitized postmortem written to: {out}")
    return 0


def cmd_cron(args: argparse.Namespace) -> int:
    job = find_cron_job(args.job, Path(args.hermes_home))
    if not job:
        print(f"No cron job found for {args.job!r}")
        return 2
    diagnosis = diagnose_cron_job(job, Path(args.hermes_home))
    print(f"job: {diagnosis['name']} ({diagnosis['job_id']})")
    print(f"status: {diagnosis['status']}")
    print(f"root_cause: {diagnosis['root_cause']}")
    print(f"last_status: {diagnosis['last_status']}")
    print(f"last_run_at: {diagnosis['last_run_at']}")
    print(f"next_run_at: {diagnosis['next_run_at']}")
    print(f"script: {diagnosis['script']}")
    print(f"latest_output_path: {diagnosis['latest_output_path']}")
    if diagnosis["last_error"]:
        print(f"last_error: {diagnosis['last_error']}")
    for item in diagnosis["recommendations"]:
        print(f"recommendation: {item}")
    return 0


def cmd_export_cron(args: argparse.Namespace) -> int:
    job = find_cron_job(args.job, Path(args.hermes_home))
    if not job:
        print(f"No cron job found for {args.job!r}")
        return 2
    diagnosis = diagnose_cron_job(job, Path(args.hermes_home))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(cron_postmortem(diagnosis), encoding="utf-8")
    print(f"wrote {out}")
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

    demo = sub.add_parser("demo", help="Run a safe sample incident through the full diagnosis pipeline")
    demo.add_argument("--platform", default="discord")
    demo.add_argument("--log", default=str(DEFAULT_DEMO_LOG))
    demo.add_argument("--out", default="exports/demo-postmortem.md")
    demo.set_defaults(func=cmd_demo)

    cron = sub.add_parser("cron")
    cron.add_argument("job", help="Cron job id or name substring")
    cron.set_defaults(func=cmd_cron)

    export_cron = sub.add_parser("export-cron-postmortem")
    export_cron.add_argument("job", help="Cron job id or name substring")
    export_cron.add_argument("--out", required=True)
    export_cron.set_defaults(func=cmd_export_cron)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
