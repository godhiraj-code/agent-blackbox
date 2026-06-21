from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Iterable

from .parser import Event, sanitize, latest_cron_output


def diagnose_cron_job(job: dict, hermes_home: Path) -> dict:
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
