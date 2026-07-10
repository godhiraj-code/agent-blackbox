from __future__ import annotations

import datetime as dt
import html
import json
import re
import shlex
from pathlib import Path
from typing import Iterable

from .blackbox import command_to_text, detect_risk_findings, find_recordings
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
        "latest_output_path": output_path.name if output_path else None,
        "latest_output_excerpt": sanitize(output_text[:1200]) if output_text else None,
        "root_cause": root_cause,
        "recommendations": recommendations,
    }


def cron_postmortem(diagnosis: dict) -> str:
    output_excerpt = html.escape(
        sanitize(diagnosis["latest_output_excerpt"] or "No output file found."),
        quote=False,
    )
    fence = _markdown_fence(output_excerpt)
    lines = [
        "# Cron Job Incident Postmortem",
        "",
        f"Generated at: {dt.datetime.now().isoformat(sep=' ', timespec='seconds')}",
        f"Job: `{_markdown_inline(diagnosis['name'])}` (`{_markdown_inline(diagnosis['job_id'])}`)",
        "",
        "## Diagnosis",
        "",
        f"Status: `{_markdown_inline(diagnosis['status'])}`",
        f"Root cause: `{_markdown_inline(diagnosis['root_cause'])}`",
        f"Last status: `{_markdown_inline(diagnosis['last_status'])}`",
        f"Last run: `{_markdown_inline(diagnosis['last_run_at'])}`",
        f"Next run: `{_markdown_inline(diagnosis['next_run_at'])}`",
        f"Script: `{_markdown_inline(diagnosis['script'])}`",
        f"No-agent mode: `{_markdown_inline(diagnosis['no_agent'])}`",
        "",
        "## Last Error",
        "",
        _markdown_text(diagnosis["last_error"] or "No last_error recorded."),
        "",
        "## Latest Output",
        "",
        f"Path: `{_markdown_inline(diagnosis['latest_output_path'])}`",
        "",
        f"{fence}text",
        output_excerpt,
        fence,
        "",
        "## Recommended Next Steps",
        "",
    ]
    lines.extend(f"- {_markdown_text(item)}" for item in diagnosis["recommendations"])
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
    flush_index = 0
    for index, msg in enumerate(ordered_inbound, start=1):
        msg_ts = msg.timestamp
        next_flush = None
        while flush_index < len(ordered_flushes):
            candidate = ordered_flushes[flush_index]
            flush_index += 1
            if msg_ts is None or candidate.timestamp is None or candidate.timestamp >= msg_ts:
                next_flush = candidate
                break
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
        lines.extend(f"- {_markdown_text(note)}" for note in diagnosis["notes"])
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
        lines.append(
            f"- `{_markdown_inline(ts)}` **{_markdown_text(event.event_type)}** "
            f"`{_markdown_inline(event.platform or '-')}` — {_markdown_text(event.message)}"
        )
    lines.extend(["", "## Recommended Next Step", ""])
    if diagnosis["status"] == "response_flushed":
        lines.append("Treat this as a latency/busy-turn UX problem. Add active-turn visibility before touching Discord config.")
    elif diagnosis["status"] == "agent_busy_or_response_pending":
        lines.append("Inspect active tool calls/processes and gateway agent logs for a stuck turn.")
    else:
        lines.append("Increase the time window, inspect raw gateway logs, and verify gateway status.")
    lines.append("")
    return "\n".join(lines)


RUN_MARKERS = [
    (
        "likely_tool_or_network_failure",
        "timeout or network",
        re.compile(
            r"\b(timed out|timeout|timeouterror|readtimeout|connecttimeout|etimedout|"
            r"econnreset|econnrefused|enotfound|connection reset|connection refused|"
            r"network is unreachable|temporary failure in name resolution|dns)\b",
            re.I,
        ),
    ),
    (
        "likely_tool_or_network_failure",
        "rate limit",
        re.compile(r"\b(rate[- ]?limit|rate limited|too many requests|http 429|429|quota exceeded)\b", re.I),
    ),
    (
        "likely_test_failure",
        "pytest failure",
        re.compile(r"(=+\s*failures\s*=+|short test summary info|^FAILED\s+.+::|\b\d+\s+failed\b)", re.I),
    ),
    (
        "likely_test_failure",
        "test assertion",
        re.compile(r"(\bAssertionError\b|\btests? failed\b|\btest failure\b)", re.I),
    ),
    (
        "likely_exception",
        "traceback",
        re.compile(r"Traceback \(most recent call last\):", re.I),
    ),
    (
        "likely_exception",
        "exception",
        re.compile(r"(\bUnhandled exception\b|\b[A-Za-z_][A-Za-z0-9_.]*(Error|Exception):|\bpanic:)", re.I),
    ),
]

RUN_STATUS_PRIORITY = [
    "likely_tool_or_network_failure",
    "likely_test_failure",
    "likely_exception",
]
MAX_RECORDING_BYTES = 50 * 1024 * 1024


def load_recorded_run(path: Path) -> dict:
    path = Path(path)
    if path.stat().st_size > MAX_RECORDING_BYTES:
        raise ValueError(f"recording exceeds {MAX_RECORDING_BYTES} bytes")
    record = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    if not isinstance(record, dict):
        raise ValueError("recording JSON must contain an object")
    schema_version = record.get("schema_version")
    if schema_version not in {None, "1.0"}:
        raise ValueError(f"unsupported recording schema_version: {schema_version}")
    if "exit_code" not in record:
        raise ValueError("recording is missing exit_code")
    if "sanitized_output" not in record and "raw_output" not in record:
        raise ValueError("recording must contain sanitized_output or legacy raw_output")
    if "command" in record and not isinstance(record["command"], (list, str)):
        raise ValueError("recording command must be a list or string")
    return record


def diagnose_recorded_run(record: dict) -> dict:
    output = _record_output(record)
    evidence = _run_evidence(output)
    exit_code = _int_or_none(record.get("exit_code"))
    marker_statuses = {item["status"] for item in evidence}
    risk_findings = []
    risk_findings.extend(detect_risk_findings(command_to_text(record.get("command")), "command"))
    risk_findings.extend(_dedupe_risks(risk_findings, detect_risk_findings(output, "output")))

    status = "success"
    for candidate in RUN_STATUS_PRIORITY:
        if candidate in marker_statuses:
            status = candidate
            break
    else:
        if exit_code != 0:
            status = "failed_command"
        elif any(item["severity"] == "high" for item in risk_findings):
            status = "completed_with_risks"

    if exit_code != 0:
        evidence.append(
            {
                "status": "failed_command",
                "marker": "non-zero exit code",
                "line": None,
                "snippet": f"Command exited with status {exit_code}.",
            }
        )

    return {
        "status": status,
        "exit_code": exit_code,
        "evidence": evidence,
        "risk_findings": risk_findings[:12],
        "recommended_next_step": _run_recommendation(status),
        "output_source": "sanitized_output" if record.get("sanitized_output") is not None else "raw_output_sanitized_fallback",
        "output_excerpt": _output_excerpt(output),
    }


def recorded_run_postmortem(record: dict, diagnosis: dict | None = None) -> str:
    diagnosis = diagnosis or diagnose_recorded_run(record)
    command = _format_command(record.get("command"))
    lines = [
        "# Recorded Run Postmortem",
        "",
        f"Generated at: {dt.datetime.now().isoformat(sep=' ', timespec='seconds')}",
        "",
        "## Run",
        "",
        f"- ID: `{_markdown_inline(record.get('id') or 'unknown')}`",
        f"- Name: `{_markdown_inline(record.get('name') or 'unnamed run')}`",
        f"- Command: `{_markdown_inline(command)}`",
        f"- Started: `{_markdown_inline(record.get('start_time') or 'unknown')}`",
        f"- Ended: `{_markdown_inline(record.get('end_time') or 'unknown')}`",
        f"- Duration: `{_format_duration(record.get('duration_seconds'))}`",
        f"- Exit code: `{diagnosis['exit_code']}`",
        "",
        "## Classification",
        "",
        f"Status: `{diagnosis['status']}`",
        "",
        "## Evidence",
        "",
    ]
    if diagnosis["evidence"]:
        for item in diagnosis["evidence"]:
            line = f"line {item['line']}" if item["line"] else "run metadata"
            lines.append(
                f"- `{_markdown_inline(item['status'])}` {_markdown_text(item['marker'])} "
                f"({_markdown_text(line)}): {_markdown_text(item['snippet'])}"
            )
    else:
        lines.append("- No strong failure markers were found in sanitized output.")

    if diagnosis.get("risk_findings"):
        lines.extend(["", "## Risk Findings", ""])
        for item in diagnosis["risk_findings"]:
            line = f"line {item['line']}" if item.get("line") else "record metadata"
            lines.append(
                f"- `{_markdown_inline(item['severity'])}` {_markdown_text(item['label'])} "
                f"({_markdown_text(item['scope'])}, {_markdown_text(line)}): "
                f"{_markdown_text(item['snippet'])} Recommendation: {_markdown_text(item['recommendation'])}"
            )
    else:
        lines.extend(["", "## Risk Findings", "", "- No risky commands, skipped checks, blocked tools, or redacted secrets were detected."])

    excerpt = html.escape(diagnosis["output_excerpt"], quote=False)
    fence = _markdown_fence(excerpt)
    lines.extend(
        [
            "",
            "## Sanitized Output Excerpt",
            "",
            f"Source: `{diagnosis['output_source']}`",
            "",
            f"{fence}text",
            excerpt,
            fence,
            "",
            "## Recommended Next Step",
            "",
            diagnosis["recommended_next_step"],
            "",
        ]
    )
    return "\n".join(lines)


def diagnose_latest_recording(runs_dir: Path) -> tuple[Path | None, dict | None, dict | None]:
    recordings = find_recordings(runs_dir)
    if not recordings:
        return None, None, None
    invalid_count = 0
    for path in recordings:
        try:
            record = load_recorded_run(path)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            invalid_count += 1
            continue
        diagnosis = diagnose_recorded_run(record)
        diagnosis["invalid_newer_recordings_skipped"] = invalid_count
        return path, record, diagnosis
    raise ValueError(f"no valid recordings found; rejected {invalid_count} incomplete or invalid file(s)")


def _record_output(record: dict) -> str:
    if record.get("sanitized_output") is not None:
        return sanitize(str(record.get("sanitized_output") or ""))
    return sanitize(str(record.get("raw_output") or ""))


def _run_evidence(output: str, max_items: int = 8) -> list[dict]:
    evidence: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line_number, line in enumerate(output.splitlines(), start=1):
        snippet = line.strip()
        if not snippet:
            continue
        for status, marker, pattern in RUN_MARKERS:
            if not pattern.search(line):
                continue
            key = (status, snippet)
            if key in seen:
                break
            seen.add(key)
            evidence.append(
                {
                    "status": status,
                    "marker": marker,
                    "line": line_number,
                    "snippet": _clip(snippet, 240),
                }
            )
            break
        if len(evidence) >= max_items:
            break
    return evidence


def _run_recommendation(status: str) -> str:
    if status == "success":
        return "No failure was detected. Use this as a baseline, and inspect the full recording only if the run still behaved incorrectly."
    if status == "likely_tool_or_network_failure":
        return "Retry after checking network/service status and credentials; if it repeats, add timeout, retry, or backoff handling around the failing tool call."
    if status == "likely_test_failure":
        return "Reproduce the focused failing test, fix the regression, then rerun the agent command."
    if status == "likely_exception":
        return "Start at the first traceback or exception in the sanitized excerpt, reproduce the command locally, and patch that code path."
    if status == "completed_with_risks":
        return "Review the risk findings before trusting the run; rerun with explicit verification and narrower tool permissions where possible."
    return "Re-run the command locally with the same arguments and inspect the final non-zero exit context before retrying the agent."


def _format_command(command: object) -> str:
    if isinstance(command, list):
        return shlex.join(str(part) for part in command)
    if command is None:
        return "unknown"
    return str(command)


def _format_duration(value: object) -> str:
    try:
        return f"{float(value):.2f}s"
    except (TypeError, ValueError):
        return "unknown"


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _output_excerpt(output: str, max_chars: int = 4000) -> str:
    output = output.strip("\n")
    if not output:
        return "No output captured."
    if len(output) <= max_chars:
        return output
    head = max_chars // 2
    tail = max_chars - head
    return f"{output[:head].rstrip()}\n\n... [truncated] ...\n\n{output[-tail:].lstrip()}"


def _markdown_inline(value: object) -> str:
    return _markdown_text(value).replace("`", "'").replace("\n", " ")


def _markdown_text(value: object) -> str:
    text = html.escape(sanitize(str(value or "")), quote=False)
    for character in ("\\", "`", "!", "[", "]", "(", ")"):
        text = text.replace(character, f"\\{character}")
    return text.replace(r"\[REDACTED_SECRET\]", "[REDACTED_SECRET]")


def _markdown_fence(text: str) -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return fence


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _dedupe_risks(existing: list[dict], incoming: list[dict]) -> list[dict]:
    seen = {(item["code"], item.get("scope"), item.get("snippet")) for item in existing}
    fresh = []
    for item in incoming:
        key = (item["code"], item.get("scope"), item.get("snippet"))
        if key in seen:
            continue
        seen.add(key)
        fresh.append(item)
    return fresh


def html_postmortem(events: list[Event], diagnosis: dict, platform: str) -> str:
    import html

    # Determine accent colors/classes based on status
    status = diagnosis.get("status", "unknown")
    status_class = "banner-warning"
    status_badge_class = "badge-warning"
    if status == "response_flushed":
        status_class = "banner-success"
        status_badge_class = "badge-success"
    elif status in {"agent_busy_or_response_pending", "unknown_needs_deeper_trace"}:
        status_class = "banner-warning"
        status_badge_class = "badge-warning"
    else:
        status_class = "banner-danger"
        status_badge_class = "badge-danger"

    # Build timeline items
    timeline_items_html = []
    for event in events:
        if event.event_type == "log_line":
            continue
        ts = event.timestamp.isoformat(sep=" ") if event.timestamp else "unknown-time"
        severity = event.severity
        evt_type = event.event_type
        platform_name = event.platform or "-"
        msg = event.message

        # Get badge class for severity
        sev_class = "badge-secondary"
        if severity in {"WARNING"}:
            sev_class = "badge-warning"
        elif severity in {"ERROR", "CRITICAL"}:
            sev_class = "badge-danger"

        # Get badge class for event type
        evt_class = "badge-secondary"
        if evt_type == "gateway_connected":
            evt_class = "badge-info"
        elif evt_type == "gateway_inbound_message":
            evt_class = "badge-info"
        elif evt_type == "gateway_outbound_flush":
            evt_class = "badge-success"
        elif evt_type == "gateway_reconnect_warning":
            evt_class = "badge-warning"
        elif evt_type == "agent_cache_invalidated":
            evt_class = "badge-warning"
        elif evt_type == "warning_or_error":
            evt_class = "badge-danger"

        item_html = f"""
        <div class="timeline-item" data-type="{html.escape(evt_type)}" data-severity="{html.escape(severity)}">
            <div class="timeline-dot"></div>
            <div class="timeline-content">
                <div class="event-header">
                    <div style="display: flex; gap: 0.5rem; align-items: center;">
                        <span class="badge {evt_class}">{html.escape(evt_type)}</span>
                        <span class="badge {sev_class}">{html.escape(severity)}</span>
                        <span class="badge badge-secondary">{html.escape(platform_name)}</span>
                    </div>
                    <span class="event-time">{html.escape(ts)}</span>
                </div>
                <div class="event-message">{html.escape(msg)}</div>
            </div>
        </div>
        """
        timeline_items_html.append(item_html)

    # Build trace transcript (chat bubbles)
    trace_html = []
    if diagnosis.get("message_trace"):
        for item in diagnosis["message_trace"]:
            index = item["index"]
            timestamp = item["timestamp"] or "unknown-time"
            inbound_msg = item["inbound"]
            responded = item["responded"]
            flush_msg = item["flush"]
            latency_seconds = item["latency_seconds"]

            # User bubble
            user_bubble = f"""
            <div class="chat-bubble bubble-user">
                <div class="bubble-meta">User / #{index} / {html.escape(timestamp)}</div>
                <div class="event-message">{html.escape(inbound_msg)}</div>
            </div>
            """

            # Agent bubble
            if responded:
                latency_text = f'<span class="latency-pill">Responded ({latency_seconds}s latency)</span>'
                agent_bubble = f"""
                <div class="chat-bubble bubble-agent">
                    <div class="bubble-meta">Agent Response</div>
                    <div class="event-message">{html.escape(flush_msg or "")}</div>
                    {latency_text}
                </div>
                """
            else:
                agent_bubble = f"""
                <div class="chat-bubble" style="background-color: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.2);">
                    <div class="bubble-meta" style="color: #f87171;">Response Pending / Stuck</div>
                    <div class="event-message" style="color: #94a3b8; font-style: italic;">No outbound flush found for this message.</div>
                </div>
                """

            trace_html.append(f"""
            <div class="trace-pair">
                {user_bubble}
                {agent_bubble}
            </div>
            """)
    else:
        trace_html.append('<div style="color: var(--text-muted); font-style: italic;">No message correlation trace found in the selected time window.</div>')

    # Recommended Next Steps
    rec_step = ""
    if diagnosis["status"] == "response_flushed":
        rec_step = "Treat this as a latency/busy-turn UX problem. Add active-turn visibility before touching Discord config."
    elif diagnosis["status"] == "agent_busy_or_response_pending":
        rec_step = "Inspect active tool calls/processes and gateway agent logs for a stuck turn."
    else:
        rec_step = "Increase the time window, inspect raw gateway logs, and verify gateway status."

    # Notes html
    notes_items = []
    if diagnosis.get("notes"):
        for note in diagnosis["notes"]:
            notes_items.append(f"<li>{html.escape(note)}</li>")
    else:
        notes_items.append("<li>No specific notes generated.</li>")

    html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Agent Incident Postmortem - {html.escape(platform)}</title>
    <style>
        :root {{
            --bg-color: #0b0f19;
            --card-bg: #151d30;
            --border-color: #24324f;
            --text-main: #f1f5f9;
            --text-muted: #94a3b8;

            --primary: #3b82f6;
            --success: #10b981;
            --warning: #f59e0b;
            --danger: #ef4444;
            --info: #06b6d4;
        }}

        body {{
            background-color: var(--bg-color);
            color: var(--text-main);
            font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            padding: 2rem 1rem;
            margin: 0;
            line-height: 1.5;
        }}

        .container {{
            max-width: 1100px;
            margin: 0 auto;
        }}

        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 1.5rem;
            margin-bottom: 2rem;
            flex-wrap: wrap;
            gap: 1rem;
        }}

        .title-area {{
            display: flex;
            flex-direction: column;
        }}

        .title {{
            font-size: 1.875rem;
            font-weight: 700;
            letter-spacing: 0;
            margin: 0;
        }}

        .subtitle {{
            color: var(--text-muted);
            font-size: 0.875rem;
            margin-top: 0.25rem;
        }}

        .badge {{
            display: inline-flex;
            align-items: center;
            border-radius: 9999px;
            padding: 0.25rem 0.75rem;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
        }}

        .badge-success {{ background-color: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }}
        .badge-warning {{ background-color: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }}
        .badge-danger {{ background-color: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }}
        .badge-info {{ background-color: rgba(59, 130, 246, 0.15); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.3); }}
        .badge-secondary {{ background-color: rgba(148, 163, 184, 0.15); color: #cbd5e1; border: 1px solid rgba(148, 163, 184, 0.3); }}

        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 1.5rem;
            margin-bottom: 2rem;
        }}

        .card {{
            background-color: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 0.75rem;
            padding: 1.25rem;
        }}

        .metric-title {{
            color: var(--text-muted);
            font-size: 0.875rem;
            font-weight: 500;
            margin: 0 0 0.5rem 0;
        }}

        .metric-value {{
            font-size: 1.75rem;
            font-weight: 700;
            margin: 0;
        }}

        .diagnosis-banner {{
            padding: 1.5rem;
            border-radius: 0.75rem;
            margin-bottom: 2rem;
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
        }}

        .banner-success {{ background-color: rgba(16, 185, 129, 0.08); border: 1px solid rgba(16, 185, 129, 0.2); border-left: 4px solid var(--success); }}
        .banner-warning {{ background-color: rgba(245, 158, 11, 0.08); border: 1px solid rgba(245, 158, 11, 0.2); border-left: 4px solid var(--warning); }}
        .banner-danger {{ background-color: rgba(239, 68, 68, 0.08); border: 1px solid rgba(239, 68, 68, 0.2); border-left: 4px solid var(--danger); }}

        .section-title {{
            font-size: 1.25rem;
            font-weight: 600;
            margin-top: 2.5rem;
            margin-bottom: 1.25rem;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 0.5rem;
        }}

        .trace-container {{
            display: flex;
            flex-direction: column;
            gap: 1.5rem;
            margin-bottom: 2rem;
        }}

        .trace-pair {{
            border-left: 2px dashed var(--border-color);
            padding-left: 1.5rem;
            margin-left: 1rem;
        }}

        .chat-bubble {{
            max-width: 85%;
            border-radius: 0.75rem;
            padding: 1rem;
            margin-bottom: 0.75rem;
        }}

        .bubble-user {{
            background-color: rgba(59, 130, 246, 0.1);
            border: 1px solid rgba(59, 130, 246, 0.2);
        }}

        .bubble-agent {{
            background-color: rgba(16, 185, 129, 0.1);
            border: 1px solid rgba(16, 185, 129, 0.2);
        }}

        .bubble-meta {{
            font-size: 0.75rem;
            color: var(--text-muted);
            margin-bottom: 0.5rem;
            font-weight: 500;
        }}

        .latency-pill {{
            display: inline-block;
            font-size: 0.75rem;
            font-weight: 600;
            background-color: rgba(16, 185, 129, 0.2);
            color: #34d399;
            padding: 0.125rem 0.5rem;
            border-radius: 9999px;
            margin-top: 0.5rem;
        }}

        .timeline {{
            display: flex;
            flex-direction: column;
            position: relative;
            margin-top: 1rem;
        }}

        .timeline::before {{
            content: "";
            position: absolute;
            top: 0;
            bottom: 0;
            left: 21px;
            width: 2px;
            background-color: var(--border-color);
        }}

        .timeline-item {{
            display: flex;
            align-items: flex-start;
            margin-bottom: 1rem;
            position: relative;
            padding-left: 3rem;
        }}

        .timeline-dot {{
            position: absolute;
            left: 14px;
            top: 16px;
            width: 16px;
            height: 16px;
            border-radius: 50%;
            background-color: var(--border-color);
            border: 4px solid var(--bg-color);
            z-index: 10;
        }}

        .timeline-item[data-severity="WARNING"] .timeline-dot {{
            background-color: var(--warning);
        }}

        .timeline-item[data-severity="ERROR"] .timeline-dot,
        .timeline-item[data-severity="CRITICAL"] .timeline-dot {{
            background-color: var(--danger);
        }}

        .timeline-item[data-type="gateway_inbound_message"] .timeline-dot {{
            background-color: var(--primary);
        }}

        .timeline-item[data-type="gateway_outbound_flush"] .timeline-dot {{
            background-color: var(--success);
        }}

        .timeline-item[data-type="gateway_connected"] .timeline-dot {{
            background-color: var(--info);
        }}

        .timeline-content {{
            background-color: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 0.5rem;
            padding: 0.75rem 1rem;
            width: 100%;
        }}

        .event-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 0.5rem;
            flex-wrap: wrap;
            gap: 0.5rem;
        }}

        .event-time {{
            font-size: 0.75rem;
            color: var(--text-muted);
            font-family: monospace;
        }}

        .event-message {{
            font-family: monospace;
            font-size: 0.875rem;
            white-space: pre-wrap;
            word-break: break-all;
        }}

        .controls {{
            display: flex;
            gap: 1rem;
            flex-wrap: wrap;
            margin-bottom: 1.5rem;
            align-items: center;
        }}

        .search-input {{
            background-color: var(--card-bg);
            border: 1px solid var(--border-color);
            color: var(--text-main);
            border-radius: 0.375rem;
            padding: 0.5rem 1rem;
            font-size: 0.875rem;
            flex-grow: 1;
            min-width: 200px;
        }}

        .search-input:focus {{
            outline: none;
            border-color: var(--primary);
        }}

        .filter-btn {{
            background-color: #1e293b;
            border: 1px solid var(--border-color);
            color: var(--text-muted);
            border-radius: 0.375rem;
            padding: 0.5rem 1rem;
            font-size: 0.875rem;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s;
        }}

        .filter-btn:hover {{
            color: var(--text-main);
            background-color: #24324f;
        }}

        .filter-btn.active {{
            background-color: var(--primary);
            color: white;
            border-color: var(--primary);
        }}

        ul {{
            margin: 0;
            padding-left: 1.25rem;
        }}

        li {{
            margin-bottom: 0.5rem;
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <header class="header">
            <div class="title-area">
                <h1 class="title">AI Flight Recorder Postmortem</h1>
                <div class="subtitle">Generated at {html.escape(dt.datetime.now().isoformat(sep=" ", timespec="seconds"))}</div>
            </div>
            <div>
                <span class="badge badge-info" style="font-size: 0.875rem; padding: 0.375rem 1rem;">Platform: {html.escape(platform)}</span>
            </div>
        </header>

        <!-- Diagnosis Banner -->
        <div class="diagnosis-banner {status_class}">
            <div style="display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap;">
                <span style="font-size: 0.875rem; font-weight: 500; text-transform: uppercase; color: var(--text-muted);">DIAGNOSIS STATUS</span>
                <span class="badge {status_badge_class}">{html.escape(status)}</span>
            </div>
            <h2 style="margin: 0.5rem 0 0 0; font-size: 1.25rem; font-weight: 600;">{html.escape(rec_step)}</h2>
        </div>

        <!-- Metrics Grid -->
        <div class="grid">
            <div class="card">
                <div class="metric-title">Inbound Messages</div>
                <div class="metric-value" style="color: #60a5fa;">{diagnosis["inbound_count"]}</div>
            </div>
            <div class="card">
                <div class="metric-title">Outbound Flushes</div>
                <div class="metric-value" style="color: #34d399;">{diagnosis["flush_count"]}</div>
            </div>
            <div class="card">
                <div class="metric-title">Warnings / Errors</div>
                <div class="metric-value" style="color: #f87171;">{diagnosis["same_platform_error_count"]}</div>
            </div>
            <div class="card">
                <div class="metric-title">Cache Invalidations</div>
                <div class="metric-value" style="color: #fbbf24;">{diagnosis["cache_invalidation_count"]}</div>
            </div>
        </div>

        <!-- Recommendations & Notes -->
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(350px, 1fr)); gap: 1.5rem; margin-bottom: 2rem;">
            <div class="card">
                <h3 style="margin-top: 0; font-size: 1.125rem; font-weight: 600; display: flex; align-items: center; gap: 0.5rem;">
                    Recommended Actions
                </h3>
                <ul>
                    <li><strong>Status code:</strong> <code>{html.escape(status)}</code></li>
                    <li>Verify gateway connectivity with <code>hermes gateway status</code>.</li>
                    <li>If the agent is busy or response is pending, inspect active python/node processes.</li>
                    <li>Consider checking raw logs if the timeline contains errors.</li>
                </ul>
            </div>
            <div class="card">
                <h3 style="margin-top: 0; font-size: 1.125rem; font-weight: 600; display: flex; align-items: center; gap: 0.5rem;">
                    Diagnostic Notes
                </h3>
                <ul>
                    {"".join(notes_items)}
                </ul>
            </div>
        </div>

        <!-- Message Trace -->
        <section>
            <h2 class="section-title">Message Correlation Trace</h2>
            <div class="trace-container">
                {"".join(trace_html)}
            </div>
        </section>

        <!-- Timeline Log -->
        <section>
            <h2 class="section-title">Recording Timeline</h2>

            <div class="controls">
                <input type="text" class="search-input" id="searchBar" oninput="searchTimeline(this.value)" placeholder="Search timeline messages...">
                <div style="display: flex; gap: 0.5rem; flex-wrap: wrap;">
                    <button class="filter-btn active" data-filter="all" onclick="filterTimeline('all')">All</button>
                    <button class="filter-btn" data-filter="inbound" onclick="filterTimeline('inbound')">Inbounds</button>
                    <button class="filter-btn" data-filter="outbound" onclick="filterTimeline('outbound')">Outbounds</button>
                    <button class="filter-btn" data-filter="errors" onclick="filterTimeline('errors')">Errors</button>
                    <button class="filter-btn" data-filter="gateway" onclick="filterTimeline('gateway')">Gateway</button>
                </div>
            </div>

            <div class="timeline" id="timelineList">
                {"".join(timeline_items_html) if timeline_items_html else '<div style="color: var(--text-muted); font-style: italic; padding: 2rem; text-align: center;">No non-log events found in this window.</div>'}
            </div>
        </section>
    </div>

    <script>
        let currentFilter = 'all';
        let searchQuery = '';

        function updateTimeline() {{
            const items = document.querySelectorAll('.timeline-item');
            items.forEach(item => {{
                const itemType = item.getAttribute('data-type');
                const itemSeverity = item.getAttribute('data-severity');
                const text = item.querySelector('.event-message').textContent.toLowerCase();

                let matchesFilter = false;
                if (currentFilter === 'all') {{
                    matchesFilter = true;
                }} else if (currentFilter === 'inbound') {{
                    matchesFilter = (itemType === 'gateway_inbound_message');
                }} else if (currentFilter === 'outbound') {{
                    matchesFilter = (itemType === 'gateway_outbound_flush');
                }} else if (currentFilter === 'errors') {{
                    matchesFilter = (itemSeverity === 'WARNING' || itemSeverity === 'ERROR' || itemSeverity === 'CRITICAL' || itemType === 'warning_or_error');
                }} else if (currentFilter === 'gateway') {{
                    matchesFilter = (itemType === 'gateway_connected' || itemType === 'gateway_reconnect_warning');
                }}

                let matchesSearch = text.includes(searchQuery);

                item.style.display = (matchesFilter && matchesSearch) ? 'flex' : 'none';
            }});
        }}

        function filterTimeline(type) {{
            currentFilter = type;
            const buttons = document.querySelectorAll('.filter-btn');
            buttons.forEach(btn => {{
                if (btn.getAttribute('data-filter') === type) {{
                    btn.classList.add('active');
                }} else {{
                    btn.classList.remove('active');
                }}
            }});
            updateTimeline();
        }}

        function searchTimeline(query) {{
            searchQuery = query.toLowerCase();
            updateTimeline();
        }}
    </script>
</body>
</html>
"""
    return html_template
