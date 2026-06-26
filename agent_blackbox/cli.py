from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from .parser import (
    Event,
    DEFAULT_HERMES_HOME,
    DEFAULT_DEMO_LOG,
    log_path,
    read_events,
    find_cron_job,
    sanitize,
)
from .diagnosis import (
    diagnose_cron_job,
    cron_postmortem,
    diagnose_no_response,
    postmortem,
)


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
        if (event := parse_log_line_internal(line)) is not None
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


def parse_log_line_internal(line: str) -> Event | None:
    # helper for demo cmd
    from .parser import parse_log_line
    return parse_log_line(line)


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


def cmd_record(args: argparse.Namespace) -> int:
    cmd_args = list(args.cmd_args or [])
    if cmd_args and cmd_args[0] == "--":
        cmd_args = cmd_args[1:]
    if not cmd_args:
        print("record requires a command to run", file=sys.stderr)
        return 2

    start = dt.datetime.now(dt.timezone.utc)
    captured: list[str] = []
    captured_lock = threading.Lock()

    popen_cmd: list[str] | str = cmd_args
    use_shell = False
    if os.name == "nt":
        popen_cmd = subprocess.list2cmdline(cmd_args)
        use_shell = True

    proc = subprocess.Popen(
        popen_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        shell=use_shell,
    )

    def stream_output(stream, terminal) -> None:
        try:
            for line in iter(stream.readline, ""):
                terminal.write(line)
                terminal.flush()
                with captured_lock:
                    captured.append(line)
        finally:
            stream.close()

    threads = [
        threading.Thread(target=stream_output, args=(proc.stdout, sys.stdout), daemon=True),
        threading.Thread(target=stream_output, args=(proc.stderr, sys.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    exit_code = proc.wait()
    for thread in threads:
        thread.join()

    end = dt.datetime.now(dt.timezone.utc)
    duration_seconds = (end - start).total_seconds()
    raw_output = "".join(captured)
    sanitized_output = sanitize(raw_output)
    run_id = f"{start.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"run_{run_id}.json"

    record = {
        "id": run_id,
        "name": args.name or " ".join(cmd_args),
        "command": cmd_args,
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "duration_seconds": duration_seconds,
        "exit_code": exit_code,
        "raw_output": raw_output,
        "sanitized_output": sanitized_output,
    }
    out_path.write_text(json.dumps(record, indent=2), encoding="utf-8")

    print(f"[FLIGHT RECORDER] Command finished with exit code {exit_code} in {duration_seconds:.2f}s.")
    print(f"[FLIGHT RECORDER] Recording saved to: {out_path}")
    return exit_code


def cmd_claude(args: argparse.Namespace) -> int:
    from .claude import find_claude_projects, summarize_claude_session, DEFAULT_CLAUDE_DIR
    claude_dir = Path(args.claude_dir) if args.claude_dir else DEFAULT_CLAUDE_DIR
    projects = find_claude_projects(claude_dir)
    
    if not projects:
        print(f"No Claude Code projects or transcripts found under {claude_dir}.")
        return 0
        
    if args.session:
        # User specified a specific session file or session id
        target_path = None
        for proj_name, sessions in projects.items():
            for p in sessions:
                if p.stem == args.session or p.name == args.session or str(p) == args.session:
                    target_path = p
                    break
            if target_path:
                break
        
        if not target_path:
            p_direct = Path(args.session)
            if p_direct.exists():
                target_path = p_direct
                
        if not target_path:
            print(f"Could not locate session transcript matching: {args.session}")
            return 1
            
        summary = summarize_claude_session(target_path)
        if "error" in summary:
            print(f"Error: {summary['error']}")
            return 1
            
        print(f"Claude Code Session: {summary['session_id']}")
        print(f"Path: {summary['path']}")
        print(f"Last Modified: {summary['last_modified']}")
        print(f"Duration: {summary['duration_seconds']}s ({summary['start_time']} to {summary['end_time']})")
        print(f"User Prompts: {summary['user_prompts']}")
        print(f"Assistant Turns: {summary['assistant_turns']}")
        print(f"Tool Calls: {summary['tool_calls']}")
        print(f"Tools Used: {', '.join(summary['tools_used']) if summary['tools_used'] else 'none'}")
        print(f"Errors: {summary['errors_count']}")
        print(f"Status: {summary['status']}")
        return 0

    print("Claude Code Projects & Sessions:")
    print("=" * 60)
    for proj_name, sessions in projects.items():
        print(f"\nProject: {proj_name}")
        for p in sessions:
            mtime = dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            sz_bytes = p.stat().st_size
            print(f"  - {p.name:<40} {mtime} ({sz_bytes} bytes)")
    print("\nTo inspect a specific session, run:")
    print("  agent-doctor claude --session <session-id-or-filename>")
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

    rec = sub.add_parser("record", help="Run a command and save a flight recording")
    rec.add_argument("--name", default=None, help="Human-friendly name for this run")
    rec.add_argument("--out-dir", default="runs", help="Directory to store recordings")
    rec.add_argument("cmd_args", nargs=argparse.REMAINDER, help="Command and arguments to run")
    rec.set_defaults(func=cmd_record)

    cl = sub.add_parser("claude", help="Analyze and summarize local Claude Code sessions")
    cl.add_argument("--session", default=None, help="Specific session ID, filename or absolute path to parse")
    cl.add_argument("--claude-dir", default=None, help="Override default Claude Code configuration directory")
    cl.set_defaults(func=cmd_claude)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
