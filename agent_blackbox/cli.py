from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
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
    html_postmortem,
    load_recorded_run,
    diagnose_recorded_run,
    diagnose_latest_recording,
    recorded_run_postmortem,
)
from .blackbox import analyze_blackbox_events, load_blackbox_events
from .autopsy import build_autopsy, rank_fight, render_autopsy_html, render_fight_html
from .repo_preflight import load_manifest, run_repo_preflight
from .version import __version__


def run_gateway_status() -> tuple[int, str]:
    try:
        proc = subprocess.run(_prepare_command(["hermes", "gateway", "status"]), capture_output=True, text=True, shell=False, timeout=60)
        return proc.returncode, sanitize((proc.stdout or proc.stderr).strip())
    except Exception as exc:
        return 127, f"Could not run gateway status: {sanitize(str(exc))}"


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
    status_code, status_text = run_gateway_status()
    print("\nGateway status:\n" + status_text)
    return 0 if status_code == 0 else 1


def _read_gateway_events(args: argparse.Namespace) -> list[Event] | None:
    path = log_path(Path(args.hermes_home))
    if not path.exists():
        print(f"gateway log not found: {path}", file=sys.stderr)
        return None
    try:
        return read_events(args.minutes, Path(args.hermes_home))
    except (OSError, ValueError) as exc:
        print(f"could not read gateway log: {sanitize(str(exc))}", file=sys.stderr)
        return None


def cmd_gateway(args: argparse.Namespace) -> int:
    events = _read_gateway_events(args)
    if events is None:
        return 2
    print_gateway(events, args.platform)
    return 0


def cmd_why(args: argparse.Namespace) -> int:
    events = _read_gateway_events(args)
    if events is None:
        return 2
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
    return _diagnosis_exit_code(diagnosis["status"], getattr(args, "always_zero", False))


def cmd_export(args: argparse.Namespace) -> int:
    events = _read_gateway_events(args)
    if events is None:
        return 2
    diagnosis = diagnose_no_response(events, args.platform)
    text = postmortem(events, diagnosis, args.platform)
    out = Path(args.out)
    _atomic_write_text(out, text, private=True)
    print(f"wrote {out}")
    return _diagnosis_exit_code(diagnosis["status"], getattr(args, "always_zero", False))


def cmd_export_html(args: argparse.Namespace) -> int:
    events = _read_gateway_events(args)
    if events is None:
        return 2
    diagnosis = diagnose_no_response(events, args.platform)
    html_content = html_postmortem(events, diagnosis, args.platform)
    out = Path(args.out)
    _atomic_write_text(out, html_content, private=True)
    print(f"wrote {out}")
    return _diagnosis_exit_code(diagnosis["status"], getattr(args, "always_zero", False))


def cmd_search(args: argparse.Namespace) -> int:
    events = _read_gateway_events(args)
    if events is None:
        return 2
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
    _atomic_write_text(out, postmortem(events, diagnosis, args.platform), private=True)
    print(f"\nSanitized postmortem written to: {out}")
    return 0


def parse_log_line_internal(line: str) -> Event | None:
    # helper for demo cmd
    from .parser import parse_log_line
    return parse_log_line(line)


def cmd_cron(args: argparse.Namespace) -> int:
    try:
        job = find_cron_job(args.job, Path(args.hermes_home))
    except (OSError, ValueError) as exc:
        print(f"could not resolve cron job: {sanitize(str(exc))}", file=sys.stderr)
        return 2
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
    return _diagnosis_exit_code(diagnosis["status"], getattr(args, "always_zero", False))


def cmd_export_cron(args: argparse.Namespace) -> int:
    try:
        job = find_cron_job(args.job, Path(args.hermes_home))
    except (OSError, ValueError) as exc:
        print(f"could not resolve cron job: {sanitize(str(exc))}", file=sys.stderr)
        return 2
    if not job:
        print(f"No cron job found for {args.job!r}")
        return 2
    diagnosis = diagnose_cron_job(job, Path(args.hermes_home))
    out = Path(args.out)
    _atomic_write_text(out, cron_postmortem(diagnosis), private=True)
    print(f"wrote {out}")
    return _diagnosis_exit_code(diagnosis["status"], getattr(args, "always_zero", False))


def cmd_record(args: argparse.Namespace) -> int:
    cmd_args = list(args.cmd_args or [])
    if cmd_args and cmd_args[0] == "--":
        cmd_args = cmd_args[1:]
    if not cmd_args:
        print("record requires a command to run", file=sys.stderr)
        return 2
    if args.max_capture_bytes < 0 or args.timeout_seconds < 0:
        print("record limits must be zero or positive", file=sys.stderr)
        return 2

    start = dt.datetime.now(dt.timezone.utc)
    captured: list[str] = []
    captured_bytes = 0
    capture_truncated = False
    captured_lock = threading.Lock()
    max_capture_bytes = max(0, int(args.max_capture_bytes))

    popen_cmd = _prepare_command(cmd_args)

    popen_kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
        "shell": False,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(popen_cmd, **popen_kwargs)
    except OSError as exc:
        print(f"could not start command: {sanitize(str(exc))}", file=sys.stderr)
        return 127

    def stream_output(stream, terminal) -> None:
        nonlocal captured_bytes, capture_truncated
        in_private_key = False
        try:
            for line in iter(stream.readline, ""):
                if args.show_raw_console:
                    terminal.write(line)
                elif "-----BEGIN " in line and "PRIVATE KEY-----" in line:
                    in_private_key = True
                    terminal.write("[REDACTED_SECRET]\n")
                elif in_private_key:
                    if "-----END " in line and "PRIVATE KEY-----" in line:
                        in_private_key = False
                else:
                    terminal.write(sanitize(line))
                terminal.flush()
                encoded = line.encode("utf-8")
                with captured_lock:
                    remaining = max_capture_bytes - captured_bytes
                    if remaining > 0:
                        chunk = encoded[:remaining].decode("utf-8", errors="replace")
                        captured.append(chunk)
                        captured_bytes += len(chunk.encode("utf-8"))
                    if len(encoded) > max(remaining, 0):
                        capture_truncated = True
        finally:
            stream.close()

    threads = [
        threading.Thread(target=stream_output, args=(proc.stdout, sys.stdout), daemon=True),
        threading.Thread(target=stream_output, args=(proc.stderr, sys.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    timed_out = False
    interrupted = False
    try:
        timeout = float(args.timeout_seconds) if args.timeout_seconds else None
        exit_code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop_process(proc)
        exit_code = 124
    except KeyboardInterrupt:
        interrupted = True
        _stop_process(proc)
        exit_code = 130
    for thread in threads:
        thread.join(timeout=5)

    end = dt.datetime.now(dt.timezone.utc)
    duration_seconds = (end - start).total_seconds()
    raw_output = "".join(captured)
    if timed_out:
        raw_output += f"\n[AGENT_BLACKBOX] command timed out after {args.timeout_seconds} seconds.\n"
    if interrupted:
        raw_output += "\n[AGENT_BLACKBOX] recording interrupted by user.\n"
    if capture_truncated:
        raw_output += f"\n[AGENT_BLACKBOX] captured output truncated at {max_capture_bytes} bytes.\n"
    sanitized_output = sanitize(raw_output)
    run_id = f"{start.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
    out_dir = Path(args.out_dir)
    out_path = out_dir / f"run_{run_id}.json"

    stored_command = cmd_args if args.store_raw else [sanitize(str(part)) for part in cmd_args]
    record = {
        "schema_version": "1.0",
        "id": run_id,
        "name": sanitize(args.name or " ".join(cmd_args)),
        "command": stored_command,
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "duration_seconds": duration_seconds,
        "exit_code": exit_code,
        "sanitized_output": sanitized_output,
        "capture": {
            "max_bytes": max_capture_bytes,
            "captured_bytes": captured_bytes,
            "truncated": capture_truncated,
        },
        "privacy": {
            "raw_output_stored": bool(args.store_raw),
            "raw_console_shown": bool(args.show_raw_console),
        },
    }
    if args.store_raw:
        record["raw_output"] = raw_output
    _atomic_write_text(out_path, json.dumps(record, indent=2), private=True)

    print(f"[FLIGHT RECORDER] Command finished with exit code {exit_code} in {duration_seconds:.2f}s.")
    print(f"[FLIGHT RECORDER] Sanitized recording saved to: {out_path}")
    if args.store_raw:
        print("[FLIGHT RECORDER] WARNING: raw output retention was explicitly enabled.", file=sys.stderr)
    if args.show_raw_console:
        print("[FLIGHT RECORDER] WARNING: raw console streaming was explicitly enabled.", file=sys.stderr)
    return exit_code


def _prepare_command(cmd_args: list[str]) -> list[str]:
    resolved_executable = shutil.which(cmd_args[0])
    prepared = [resolved_executable or cmd_args[0], *cmd_args[1:]]
    if os.name == "nt" and Path(prepared[0]).suffix.lower() in {".cmd", ".bat"}:
        return [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/s",
            "/c",
            subprocess.list2cmdline(prepared),
        ]
    return prepared


def _stop_process(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            pass


def _atomic_write_text(path: Path, content: str, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if private:
            os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
        if private:
            os.chmod(path, 0o600)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def cmd_diagnose_run(args: argparse.Namespace) -> int:
    try:
        record = load_recorded_run(Path(args.recording))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"could not read recording: {exc}", file=sys.stderr)
        return 2

    diagnosis = diagnose_recorded_run(record)
    report = recorded_run_postmortem(record, diagnosis)
    out = Path(args.out)
    _atomic_write_text(out, report, private=True)
    print(f"wrote {out}")
    print(f"status: {diagnosis['status']}")
    return _diagnosis_exit_code(diagnosis["status"], args.always_zero)


def cmd_run_doctor(args: argparse.Namespace) -> int:
    runs_dir = Path(args.runs_dir)
    try:
        path, record, diagnosis = diagnose_latest_recording(runs_dir)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"could not read latest recording: {exc}", file=sys.stderr)
        return 2

    if path is None or record is None or diagnosis is None:
        print(f"No run_*.json recordings found under {runs_dir}.")
        print("Create one with: agent-doctor record --name \"smoke\" -- <agent command>")
        return 1

    print(f"latest_recording: {path}")
    if diagnosis.get("invalid_newer_recordings_skipped"):
        print(f"warning: skipped {diagnosis['invalid_newer_recordings_skipped']} newer invalid recording(s)")
    print(f"id: {record.get('id') or 'unknown'}")
    print(f"name: {record.get('name') or 'unnamed run'}")
    print(f"status: {diagnosis['status']}")
    print(f"exit_code: {diagnosis['exit_code']}")

    if diagnosis["evidence"]:
        print("evidence:")
        for item in diagnosis["evidence"][:5]:
            line = f" line={item['line']}" if item["line"] else ""
            print(f"  - {item['status']}:{line} {item['marker']} :: {item['snippet']}")
    else:
        print("evidence: none")

    if diagnosis.get("risk_findings"):
        print("risk_findings:")
        for item in diagnosis["risk_findings"][:5]:
            print(f"  - {item['severity']} {item['code']} ({item['scope']}): {item['snippet']}")
    else:
        print("risk_findings: none")

    print(f"recommended_next_step: {diagnosis['recommended_next_step']}")

    if args.out:
        out = Path(args.out)
        _atomic_write_text(out, recorded_run_postmortem(record, diagnosis), private=True)
        print(f"wrote {out}")
    return _diagnosis_exit_code(diagnosis["status"], args.always_zero)


def cmd_analyze_events(args: argparse.Namespace) -> int:
    try:
        events = load_blackbox_events(Path(args.events))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"could not read black-box events: {exc}", file=sys.stderr)
        return 2

    if not events:
        print("black-box event input contained no events", file=sys.stderr)
        return 2

    analysis = analyze_blackbox_events(events)
    if args.format == "json":
        print(json.dumps(analysis, indent=2))
    else:
        _print_blackbox_analysis(analysis)

    if args.out:
        out = Path(args.out)
        _atomic_write_text(out, json.dumps(analysis, indent=2), private=True)
        if args.format != "json":
            print(f"wrote {out}")
    if args.fail_on_incident and analysis["status"] != "completed":
        return 1
    return 0


def _load_analysis(path: str) -> dict:
    events = load_blackbox_events(Path(path))
    if not events:
        raise ValueError("black-box event input contained no events")
    return analyze_blackbox_events(events)


def cmd_autopsy(args: argparse.Namespace) -> int:
    try:
        report = build_autopsy(_load_analysis(args.trace), Path(args.trace).stem)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"could not create autopsy: {sanitize(str(exc))}", file=sys.stderr)
        return 2
    _atomic_write_text(Path(args.out), render_autopsy_html(report), private=True)
    if args.json_out:
        _atomic_write_text(Path(args.json_out), json.dumps(report, indent=2), private=True)
    card = report["scorecard"]
    print(f"verdict: {card['verdict']}")
    print(f"score: {card['score']}/100")
    print(f"wrote {args.out}")
    if args.json_out:
        print(f"wrote {args.json_out}")
    if getattr(args, "fail_on_incident", False) and report["scorecard"]["status"] != "completed":
        return 1
    return 0


def _fight_entries(values: list[str]) -> list[tuple[str, str]]:
    entries = []
    names = set()
    for value in values:
        if "=" not in value:
            raise ValueError("fight entries must use NAME=PATH")
        name, path = value.split("=", 1)
        name = name.strip()
        path = path.strip()
        if not name or not path:
            raise ValueError("fight entries require a non-empty NAME and PATH")
        if name in names:
            raise ValueError("fight entry names must be unique")
        names.add(name)
        entries.append((name, path))
    if len(entries) < 2:
        raise ValueError("fight requires at least two NAME=PATH entries")
    return entries


def cmd_fight(args: argparse.Namespace) -> int:
    try:
        entries = _fight_entries(args.entries)
        report = rank_fight((name, _load_analysis(path)) for name, path in entries)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"could not create fight: {sanitize(str(exc))}", file=sys.stderr)
        return 2
    _atomic_write_text(Path(args.out), render_fight_html(report), private=True)
    if args.json_out:
        _atomic_write_text(Path(args.json_out), json.dumps(report, indent=2), private=True)
    print(f"winner: {report['winner']}")
    for run in report["runs"]:
        print(f"#{run['rank']} {run['name']}: {run['scorecard']['score']}/100")
    print(f"wrote {args.out}")
    if args.json_out:
        print(f"wrote {args.json_out}")
    return 0


def cmd_claude(args: argparse.Namespace) -> int:
    from .claude import find_claude_projects, summarize_claude_session, DEFAULT_CLAUDE_DIR
    claude_dir = Path(args.claude_dir) if args.claude_dir else DEFAULT_CLAUDE_DIR
    projects = find_claude_projects(claude_dir)

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
            if p_direct.is_file() and not p_direct.is_symlink():
                target_path = p_direct

        if not target_path:
            print(f"Could not locate session transcript matching: {args.session}")
            return 1

        summary = summarize_claude_session(target_path)
        if "error" in summary:
            print(f"Error: {summary['error']}")
            return 1

        print(f"Claude Code Session: {summary['session_id']}")
        print(f"Path: {Path(summary['path']).name}")
        print(f"Last Modified: {summary['last_modified']}")
        print(f"Duration: {summary['duration_seconds']}s ({summary['start_time']} to {summary['end_time']})")
        print(f"User Prompts: {summary['user_prompts']}")
        print(f"Assistant Turns: {summary['assistant_turns']}")
        print(f"Tool Calls: {summary['tool_calls']}")
        print(f"Tools Used: {', '.join(summary['tools_used']) if summary['tools_used'] else 'none'}")
        print(f"Errors: {summary['errors_count']}")
        print(f"Status: {summary['status']}")
        _print_session_findings(summary)
        return 0 if summary["status"] == "completed" else 1

    if not projects:
        print(f"No Claude Code projects or transcripts found under {claude_dir}.")
        return 1

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


def cmd_codex(args: argparse.Namespace) -> int:
    from .codex import find_codex_sessions, summarize_codex_session, DEFAULT_CODEX_DIR
    codex_dir = Path(args.codex_dir) if args.codex_dir else DEFAULT_CODEX_DIR
    sessions = find_codex_sessions(codex_dir)

    if args.session:
        # User specified a specific session file or session id
        target_path = None
        for s in sessions:
            if s["id"] == args.session or s["path"].stem == args.session or s["path"].name == args.session or str(s["path"]) == args.session:
                target_path = s["path"]
                break

        if not target_path:
            p_direct = Path(args.session)
            if p_direct.is_file() and not p_direct.is_symlink():
                target_path = p_direct

        if not target_path:
            print(f"Could not locate session transcript matching: {args.session}")
            return 1

        summary = summarize_codex_session(target_path)
        if "error" in summary:
            print(f"Error: {summary['error']}")
            return 1

        print(f"Codex Session: {summary['session_id']}")
        print(f"Path: {Path(summary['path']).name}")
        print(f"Last Modified: {summary['last_modified']}")
        print(f"Duration: {summary['duration_seconds']}s ({summary['start_time']} to {summary['end_time']})")
        print(f"User Prompts: {summary['user_prompts']}")
        print(f"Assistant Turns: {summary['assistant_turns']}")
        print(f"Tool Calls: {summary['tool_calls']}")

        print("Tools Used:")
        if summary['tools_used']:
            for name, count in sorted(summary['tools_used'].items(), key=lambda x: x[1], reverse=True):
                print(f"  - {name}: {count} call(s)")
        else:
            print("  none")

        print(f"Errors: {summary['errors_count']}")
        print(f"Status: {summary['status']}")

        _print_session_findings(summary)

        return 0 if summary["status"] == "completed" else 1

    if not sessions:
        print(f"No Codex sessions found under {codex_dir}.")
        return 1

    print("Codex Sessions:")
    print("=" * 60)
    for s in sessions:
        mtime = s["updated_at"].strftime("%Y-%m-%d %H:%M:%S")
        print(f"- {s['id']}  {mtime}  Model: {s['model']}  Tokens: {s['tokens_used']}")
        print(f"  Title: {s['title'][:70]}")
        print(f"  Path:  {s['path'].name}")
        print()
    print("To inspect a specific session, run:")
    print("  agent-doctor codex --session <session-id-or-filename>")
    return 0



def cmd_opencode(args: argparse.Namespace) -> int:
    from .opencode import DEFAULT_OPENCODE_DIR, find_opencode_sessions, summarize_opencode_session

    opencode_dir = Path(args.opencode_dir) if args.opencode_dir else DEFAULT_OPENCODE_DIR
    sessions = find_opencode_sessions(opencode_dir)

    if args.session:
        target_path = None
        storage_dir = None
        for session in sessions:
            path = session["path"]
            if session["id"] == args.session or path.stem == args.session or path.name == args.session or str(path) == args.session:
                target_path = path
                storage_dir = session.get("storage_dir")
                break

        if not target_path:
            direct = Path(args.session)
            if direct.is_file() and not direct.is_symlink():
                target_path = direct

        if not target_path:
            print(f"Could not locate OpenCode session/export matching: {args.session}")
            return 1

        summary = summarize_opencode_session(target_path, storage_dir=storage_dir)
        if "error" in summary:
            print(f"Error: {summary['error']}")
            return 1

        print(f"OpenCode Session: {summary['session_id']}")
        print(f"Path: {Path(summary['path']).name}")
        print(f"Last Modified: {summary['last_modified']}")
        print(f"Duration: {summary['duration_seconds']}s ({summary['start_time']} to {summary['end_time']})")
        print(f"User Prompts: {summary['user_prompts']}")
        print(f"Assistant Turns: {summary['assistant_turns']}")
        print(f"Tool Calls: {summary['tool_calls']}")
        print("Tools Used:")
        if summary["tools_used"]:
            for name, count in sorted(summary["tools_used"].items(), key=lambda item: item[1], reverse=True):
                print(f"  - {name}: {count} call(s)")
        else:
            print("  none")
        print(f"Errors: {summary['errors_count']}")
        print(f"Status: {summary['status']}")
        _print_session_findings(summary)
        return 0 if summary["status"] == "completed" else 1

    if not sessions:
        print(f"No OpenCode sessions found under {opencode_dir}.")
        print("Tip: OpenCode stores data under ~/.local/share/opencode; exported JSON can be inspected with --session <path>.")
        return 1

    print("OpenCode Sessions:")
    print("=" * 60)
    for session in sessions:
        mtime = session["updated_at"].strftime("%Y-%m-%d %H:%M:%S")
        print(f"- {session['id']}  {mtime}  Project: {session['project']}  Messages: {session['message_count']}")
        print(f"  Title: {session['title'][:70]}")
        print(f"  Directory: {Path(session['directory']).name if session['directory'] else '-'}")
        print(f"  Path: {session['path'].name}")
        print()
    print("To inspect a specific session, run:")
    print("  agent-doctor opencode --session <session-id-or-export-json>")
    return 0


def cmd_repo_preflight(args: argparse.Namespace) -> int:
    try:
        manifest = load_manifest(Path(args.manifest))
        report = run_repo_preflight(Path(args.repo), manifest)
    except (OSError, json.JSONDecodeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"repo preflight failed: {sanitize(str(exc))}", file=sys.stderr)
        return 2

    if args.json_out:
        _atomic_write_text(Path(args.json_out), json.dumps(report, indent=2), private=True)
        print(f"wrote {args.json_out}")
    print(f"status: {report['status']}")
    print(f"baseline: {report['baseline']}")
    print(f"changed_file_count: {report['changed_file_count']}")
    for finding in report["findings"]:
        path = f" ({finding['path']})" if finding.get("path") else ""
        print(f"finding: {finding['code']}{path}: {finding['message']}")
    return 0 if report["status"] == "pass" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-blackbox", description="Local-only incident forensics for AI agent runs")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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
    why.add_argument("--always-zero", action="store_true", help="Write the diagnosis without using its status as the process exit code")
    why.set_defaults(func=cmd_why)

    export = sub.add_parser("export-postmortem")
    export.add_argument("--platform", default="discord")
    export.add_argument("--minutes", type=int, default=180)
    export.add_argument("--out", required=True)
    export.add_argument("--always-zero", action="store_true", help="Write the report without using its diagnosis as the process exit code")
    export.set_defaults(func=cmd_export)

    export_html = sub.add_parser("export-html", aliases=["visual"], help="Export a beautiful dark-themed HTML visual report")
    export_html.add_argument("--platform", default="discord")
    export_html.add_argument("--minutes", type=int, default=180)
    export_html.add_argument("--out", required=True)
    export_html.add_argument("--always-zero", action="store_true", help="Write the report without using its diagnosis as the process exit code")
    export_html.set_defaults(func=cmd_export_html)

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
    cron.add_argument("--always-zero", action="store_true", help="Print the diagnosis without using its status as the process exit code")
    cron.set_defaults(func=cmd_cron)

    export_cron = sub.add_parser("export-cron-postmortem")
    export_cron.add_argument("job", help="Cron job id or name substring")
    export_cron.add_argument("--out", required=True)
    export_cron.add_argument("--always-zero", action="store_true", help="Write the report without using its diagnosis as the process exit code")
    export_cron.set_defaults(func=cmd_export_cron)

    rec = sub.add_parser("record", help="Run a command and save a privacy-safe flight recording")
    rec.add_argument("--name", default=None, help="Human-friendly name for this run")
    rec.add_argument("--out-dir", default="runs", help="Directory to store recordings")
    rec.add_argument(
        "--store-raw",
        action="store_true",
        help="Opt in to storing unsanitized command output (may contain secrets or personal data)",
    )
    rec.add_argument(
        "--show-raw-console",
        action="store_true",
        help="Opt in to streaming unsanitized output to the terminal or CI log",
    )
    rec.add_argument(
        "--max-capture-bytes",
        type=int,
        default=10 * 1024 * 1024,
        help="Maximum combined stdout/stderr bytes retained in the recording (default: 10485760)",
    )
    rec.add_argument(
        "--timeout-seconds",
        type=float,
        default=0,
        help="Stop the command after this many seconds; 0 disables the timeout",
    )
    rec.add_argument("cmd_args", nargs=argparse.REMAINDER, help="Command and arguments to run")
    rec.set_defaults(func=cmd_record)

    diag_run = sub.add_parser("diagnose-run", help="Turn a saved command recording into a Markdown postmortem")
    diag_run.add_argument("recording", help="Path to a run_*.json file created by record")
    diag_run.add_argument("--out", required=True, help="Path to write the Markdown postmortem")
    diag_run.add_argument("--always-zero", action="store_true", help="Return 0 even when an incident is diagnosed")
    diag_run.set_defaults(func=cmd_diagnose_run)

    run_doctor = sub.add_parser("run-doctor", aliases=["latest-run"], help="Diagnose the newest saved command recording")
    run_doctor.add_argument("--runs-dir", default="runs", help="Directory containing run_*.json recordings")
    run_doctor.add_argument("--out", default=None, help="Optional path to write a Markdown postmortem")
    run_doctor.add_argument("--always-zero", action="store_true", help="Return 0 even when an incident is diagnosed")
    run_doctor.set_defaults(func=cmd_run_doctor)

    analyze_events = sub.add_parser("analyze-events", help="Analyze normalized black-box events from JSON or JSONL")
    analyze_events.add_argument("events", help="Path to a JSON/JSONL file containing normalized black-box events")
    analyze_events.add_argument("--out", default=None, help="Optional path to write the JSON analysis")
    analyze_events.add_argument("--format", choices=["human", "json"], default="human", help="Stdout format")
    analyze_events.add_argument(
        "--fail-on-incident",
        action="store_true",
        help="Exit 1 when the analysis status is not completed; useful as a CI gate",
    )
    analyze_events.set_defaults(func=cmd_analyze_events)

    autopsy = sub.add_parser("autopsy", help="Score one normalized event trace and write a standalone HTML report")
    autopsy.add_argument("trace", help="Path to normalized JSON or JSONL events")
    autopsy.add_argument("--out", required=True, help="Path to write the standalone HTML report")
    autopsy.add_argument("--json-out", default=None, help="Optional path to write the score and sanitized evidence as JSON")
    autopsy.add_argument("--fail-on-incident", action="store_true", help="Exit 1 when captured evidence is not a completed run")
    autopsy.set_defaults(func=cmd_autopsy)

    fight = sub.add_parser("fight", help="Rank two or more named traces by captured evidence score")
    fight.add_argument("entries", nargs="+", metavar="NAME=PATH", help="Uniquely named normalized event trace")
    fight.add_argument("--out", required=True, help="Path to write the standalone comparison HTML")
    fight.add_argument("--json-out", default=None, help="Optional path to write the comparison as JSON")
    fight.set_defaults(func=cmd_fight)

    cl = sub.add_parser("claude", help="Analyze and summarize local Claude Code sessions")
    cl.add_argument("--session", default=None, help="Specific session ID, filename or absolute path to parse")
    cl.add_argument("--claude-dir", default=None, help="Override default Claude Code configuration directory")
    cl.set_defaults(func=cmd_claude)

    cod = sub.add_parser("codex", help="Analyze and summarize local Codex sessions")
    cod.add_argument("--session", default=None, help="Specific session ID, filename or absolute path to parse")
    cod.add_argument("--codex-dir", default=None, help="Override default Codex configuration directory")
    cod.set_defaults(func=cmd_codex)

    opc = sub.add_parser("opencode", aliases=["open-code"], help="Analyze and summarize local OpenCode sessions or exported JSON")
    opc.add_argument("--session", default=None, help="Specific session ID, filename, absolute path, or OpenCode export JSON to parse")
    opc.add_argument("--opencode-dir", default=None, help="Override default OpenCode data directory")
    opc.set_defaults(func=cmd_opencode)

    preflight = sub.add_parser("repo-preflight", help="Block agent changes that violate intended file scope or repository invariants")
    preflight.add_argument("--repo", default=".", help="Git repository to inspect")
    preflight.add_argument("--manifest", required=True, help="JSON file describing baseline, intended paths, and invariants")
    preflight.add_argument("--json-out", default=None, help="Optional path to write the machine-readable report")
    preflight.set_defaults(func=cmd_repo_preflight)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def _diagnosis_exit_code(status: str, always_zero: bool = False) -> int:
    if always_zero:
        return 0
    return 0 if status in {"success", "completed", "response_flushed"} else 1


def _print_blackbox_analysis(analysis: dict) -> None:
    print(f"status: {analysis['status']}")
    print(f"incident: {str(bool(analysis.get('incident'))).lower()}")
    print(f"user_prompts: {analysis['user_prompts']}")
    print(f"assistant_turns: {analysis['assistant_turns']}")
    print(f"tool_calls: {analysis['tool_calls']}")

    quality = analysis.get("quality") or {}
    if quality:
        print(
            "evidence_quality: "
            f"events={quality.get('event_count', 0)} "
            f"timestamped={quality.get('timestamped_event_count', 0)} "
            f"gaps={len(quality.get('evidence_gaps') or [])}"
        )

    if analysis.get("tools_used"):
        print("tools_used:")
        for name, count in sorted(analysis["tools_used"].items(), key=lambda item: item[1], reverse=True):
            print(f"  - {name}: {count}")
    else:
        print("tools_used: none")

    if analysis.get("missed_tool_calls"):
        print("missed_tool_calls:")
        for item in analysis["missed_tool_calls"][:5]:
            print(f"  - {item.get('name')} ({item.get('call_id')})")
    else:
        print("missed_tool_calls: none")

    if analysis.get("failed_tool_calls"):
        print("failed_tool_calls:")
        for item in analysis["failed_tool_calls"][:5]:
            print(f"  - {item.get('name')} ({item.get('call_id')})")
    else:
        print("failed_tool_calls: none")

    if analysis.get("risk_findings"):
        print("risk_findings:")
        for item in analysis["risk_findings"][:5]:
            print(f"  - {item['severity']} {item['code']} ({item['scope']}): {item['snippet']}")
    else:
        print("risk_findings: none")

    if analysis.get("evidence_gaps") or quality.get("evidence_gaps"):
        print("evidence_gaps:")
        for item in (quality.get("evidence_gaps") or [])[:5]:
            print(f"  - {item}")
    if analysis.get("recommendations"):
        print("recommendations:")
        for item in analysis["recommendations"]:
            print(f"  - {item}")


def _print_session_findings(summary: dict) -> None:
    if summary.get("tool_hangs"):
        print("\nHung or missing tool results:")
        for hang in summary["tool_hangs"]:
            print(f"  - Tool: {hang.get('name')}")
            print(f"    Call ID: {hang.get('call_id')}")
            if hang.get("started_at"):
                print(f"    Started At: {hang['started_at']}")
            if hang.get("arguments"):
                print(f"    Arguments: {hang['arguments']}")

    if summary.get("tool_failures"):
        print("\nFailed tool calls:")
        for failure in summary["tool_failures"]:
            print(f"  - Tool: {failure.get('name')}")
            print(f"    Call ID: {failure.get('call_id')}")
            if failure.get("result"):
                print(f"    Result: {failure['result']}")

    if summary.get("risk_findings"):
        print("\nRisk findings:")
        for item in summary["risk_findings"][:5]:
            print(f"  - {item['severity']} {item['code']} ({item['scope']}): {item['snippet']}")
