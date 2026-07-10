import datetime as dt
import json
import os
import sys
from pathlib import Path

from agent_doctor import (
    build_parser,
    cmd_demo,
    cmd_analyze_events,
    cmd_diagnose_run,
    cmd_export_html,
    cmd_record,
    cmd_run_doctor,
    correlate_inbound_flushes,
    diagnose_cron_job,
    diagnose_no_response,
    diagnose_recorded_run,
    find_cron_job,
    html_postmortem,
    parse_log_line,
    recorded_run_postmortem,
    sanitize,
)


def write_run_record(tmp_path, **overrides):
    record = {
        "id": "20260630T010203_abcdef12",
        "name": "test run",
        "command": [sys.executable, "-c", "print('hello')"],
        "start_time": "2026-06-30T01:02:03+00:00",
        "end_time": "2026-06-30T01:02:04+00:00",
        "duration_seconds": 1.0,
        "exit_code": 0,
        "raw_output": "hello\n",
        "sanitized_output": "hello\n",
    }
    record.update(overrides)
    path = tmp_path / f"run_{record['id']}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_parse_discord_connected():
    event = parse_log_line(
        "2026-06-20 21:56:48,739 INFO hermes_plugins.discord_platform.adapter: [Discord] Connected as AgentRosh#9616"
    )
    assert event.event_type == "gateway_connected"
    assert event.platform == "discord"
    assert event.timestamp == dt.datetime(2026, 6, 20, 21, 56, 48)


def test_parse_inbound_message_platform():
    event = parse_log_line(
        "2026-06-20 21:57:57,932 INFO gateway.run: inbound message: platform=discord user=Dhiraj chat=1516451670530330684 msg='What happened?' reply_to_id=None reply_to_text=''"
    )
    assert event.event_type == "gateway_inbound_message"
    assert event.platform == "discord"
    assert "What happened" in event.message


def test_parse_flush():
    event = parse_log_line(
        "2026-06-20 21:58:44,032 INFO hermes_plugins.discord_platform.adapter: [Discord] Flushing text batch agent:main:discord:dm:1516451670530330684 (88 chars)"
    )
    assert event.event_type == "gateway_outbound_flush"
    assert event.platform == "discord"


def test_diagnosis_response_flushed_with_unrelated_telegram_noise():
    lines = [
        "2026-06-20 21:56:48,739 INFO hermes_plugins.discord_platform.adapter: [Discord] Connected as AgentRosh#9616",
        "2026-06-20 21:57:57,932 INFO gateway.run: inbound message: platform=discord user=Dhiraj chat=1516451670530330684 msg='What happened?' reply_to_id=None reply_to_text=''",
        "2026-06-20 21:58:18,297 WARNING gateway.platforms.telegram_network: [Telegram] Primary api.telegram.org connection failed (); trying fallback IPs 149.154.166.110",
        "2026-06-20 21:58:44,032 INFO hermes_plugins.discord_platform.adapter: [Discord] Flushing text batch agent:main:discord:dm:1516451670530330684 (88 chars)",
        "2026-06-20 21:58:44,763 INFO [20260620_210216_e447a66d] gateway.run: Agent cache invalidated for session agent:main:discord:dm:1516451670530330684: message_count changed (116 -> 121), possible cross-process write",
    ]
    diagnosis = diagnose_no_response([parse_log_line(line) for line in lines], "discord")
    assert diagnosis["status"] == "response_flushed"
    assert diagnosis["inbound_count"] == 1
    assert diagnosis["flush_count"] == 1
    assert diagnosis["unrelated_noise_count"] == 1
    assert diagnosis["cache_invalidation_count"] == 1
    assert diagnosis["message_trace"][0]["responded"] is True
    assert diagnosis["message_trace"][0]["latency_seconds"] == 47
    assert any("delay/busy-turn" in note for note in diagnosis["notes"])


def test_message_correlation_flags_latest_unresponded_message():
    lines = [
        "2026-06-20 21:57:00,000 INFO gateway.run: inbound message: platform=discord user=Dhiraj chat=1 msg='first' reply_to_id=None reply_to_text=''",
        "2026-06-20 21:57:10,000 INFO hermes_plugins.discord_platform.adapter: [Discord] Flushing text batch agent:main:discord:dm:1 (88 chars)",
        "2026-06-20 21:58:00,000 INFO gateway.run: inbound message: platform=discord user=Dhiraj chat=1 msg='second' reply_to_id=None reply_to_text=''",
    ]
    diagnosis = diagnose_no_response([parse_log_line(line) for line in lines], "discord")
    assert diagnosis["status"] == "agent_busy_or_response_pending"
    assert diagnosis["message_trace"][0]["responded"] is True
    assert diagnosis["message_trace"][0]["latency_seconds"] == 10
    assert diagnosis["message_trace"][1]["responded"] is False


def test_diagnosis_agent_busy_or_pending():
    lines = [
        "2026-06-20 21:56:48,739 INFO hermes_plugins.discord_platform.adapter: [Discord] Connected as AgentRosh#9616",
        "2026-06-20 21:57:57,932 INFO gateway.run: inbound message: platform=discord user=Dhiraj chat=1516451670530330684 msg='What happened?' reply_to_id=None reply_to_text=''",
    ]
    diagnosis = diagnose_no_response([parse_log_line(line) for line in lines], "discord")
    assert diagnosis["status"] == "agent_busy_or_response_pending"


def test_sanitize_redacts_common_secret_shapes():
    text = "token=secretvalue normal text"
    redacted = sanitize(text)
    assert "secretvalue" not in redacted
    assert "normal text" in redacted


def test_sanitize_redacts_provider_token_shapes():
    bearer = "abc.def-123"
    openai_token = "sk-" + "a" * 48
    github_token = "ghp_" + "a" * 36
    aws_key = "AKIA" + "A" * 16
    jwt = "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12
    text = (
        f"Authorization: Bearer {bearer} "
        f"OPENAI_API_KEY={openai_token} "
        f"github={github_token} "
        f"aws={aws_key} "
        f"jwt={jwt}"
    )
    redacted = sanitize(text)

    for secret in (bearer, openai_token, github_token, aws_key, jwt):
        assert secret not in redacted
    assert redacted.count("[REDACTED_SECRET]") >= 5


def test_find_and_diagnose_cron_timeout(tmp_path):
    hermes_home = tmp_path / "hermes"
    cron_dir = hermes_home / "cron"
    out_dir = cron_dir / "output" / "37a425324ce9"
    out_dir.mkdir(parents=True)
    (cron_dir / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "37a425324ce9",
                        "name": "Atomic Mail inbox watcher",
                        "script": "atomicmail_poll.py",
                        "no_agent": True,
                        "last_status": "error",
                        "last_run_at": "2026-06-20T22:09:14.540220+05:30",
                        "next_run_at": "2026-06-20T23:09:14.540220+05:30",
                        "last_error": "Script timed out after 120s: C:\\Users\\dhira\\AppData\\Local\\hermes\\scripts\\atomicmail_poll.py",
                        "last_delivery_error": None,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (out_dir / "2026-06-20_22-09-13.md").write_text(
        "# Cron Job: Atomic Mail inbox watcher\n\n**Status:** script failed\n\nScript timed out after 120s: C:\\Users\\dhira\\AppData\\Local\\hermes\\scripts\\atomicmail_poll.py\n",
        encoding="utf-8",
    )

    job = find_cron_job("Atomic Mail", hermes_home)
    assert job["id"] == "37a425324ce9"
    diagnosis = diagnose_cron_job(job, hermes_home)
    assert diagnosis["status"] == "script_timed_out"
    assert diagnosis["root_cause"] == "script_timeout"
    assert diagnosis["latest_output_path"].endswith("2026-06-20_22-09-13.md")
    assert any("npx atomicmail" in item for item in diagnosis["recommendations"])


def test_demo_runs_full_pipeline_and_writes_report(tmp_path):
    demo_log = Path(__file__).parent / "examples" / "demo_gateway.txt"
    output = tmp_path / "demo-postmortem.md"
    args = build_parser().parse_args(["demo", "--log", str(demo_log), "--out", str(output)])

    assert cmd_demo(args) == 0
    report = output.read_text(encoding="utf-8")
    assert "Status: `response_flushed`" in report
    assert "Unrelated platform reconnect noise" in report
    assert "demo-secret-value" not in report
    assert "[REDACTED_SECRET]" in report


def test_cmd_record(tmp_path, capsys):
    args = build_parser().parse_args(
        [
            "record",
            "--name",
            "test recording",
            "--out-dir",
            str(tmp_path),
            "--",
            sys.executable,
            "-c",
            "print('hello world'); print('token=supersecret')",
        ]
    )

    assert cmd_record(args) == 0
    console = capsys.readouterr()
    assert "supersecret" not in console.out
    assert "[REDACTED_SECRET]" in console.out
    records = list(tmp_path.glob("run_*.json"))
    assert len(records) == 1

    data = json.loads(records[0].read_text(encoding="utf-8"))
    assert data["schema_version"] == "1.0"
    assert data["id"]
    assert data["name"] == "test recording"
    assert data["exit_code"] == 0
    assert data["duration_seconds"] >= 0
    assert dt.datetime.fromisoformat(data["start_time"]) <= dt.datetime.fromisoformat(data["end_time"])
    assert "raw_output" not in data
    assert data["privacy"]["raw_output_stored"] is False
    assert data["privacy"]["raw_console_shown"] is False
    assert "hello world" in data["sanitized_output"]
    assert "supersecret" not in json.dumps(data)
    assert "[REDACTED_SECRET]" in data["sanitized_output"]


def test_diagnose_run_nonzero_traceback(tmp_path):
    path = write_run_record(
        tmp_path,
        exit_code=1,
        raw_output="Traceback (most recent call last):\nRuntimeError: secret raw failure\n",
        sanitized_output="Traceback (most recent call last):\nRuntimeError: sanitized failure\n",
    )

    record = json.loads(path.read_text(encoding="utf-8"))
    diagnosis = diagnose_recorded_run(record)

    assert diagnosis["status"] == "likely_exception"
    assert any("Traceback" in item["snippet"] for item in diagnosis["evidence"])
    assert any(item["status"] == "failed_command" for item in diagnosis["evidence"])


def test_diagnose_run_network_timeout_marker(tmp_path):
    path = write_run_record(
        tmp_path,
        sanitized_output="tool call failed: request timed out while connecting to api.example.test\n",
    )

    diagnosis = diagnose_recorded_run(json.loads(path.read_text(encoding="utf-8")))

    assert diagnosis["status"] == "likely_tool_or_network_failure"
    assert any("timed out" in item["snippet"] for item in diagnosis["evidence"])


def test_diagnose_run_pytest_failure_marker(tmp_path):
    path = write_run_record(
        tmp_path,
        exit_code=1,
        sanitized_output="FAILED tests/test_widget.py::test_widget_renders\n1 failed, 3 passed in 0.42s\n",
    )

    diagnosis = diagnose_recorded_run(json.loads(path.read_text(encoding="utf-8")))

    assert diagnosis["status"] == "likely_test_failure"
    assert any("FAILED tests/test_widget.py::test_widget_renders" in item["snippet"] for item in diagnosis["evidence"])


def test_diagnose_run_clean_zero_exit_success(tmp_path):
    path = write_run_record(
        tmp_path,
        exit_code=0,
        raw_output="hello from clean run\n",
        sanitized_output="hello from clean run\n",
    )

    diagnosis = diagnose_recorded_run(json.loads(path.read_text(encoding="utf-8")))

    assert diagnosis["status"] == "success"
    assert diagnosis["evidence"] == []


def test_diagnose_run_report_uses_sanitized_output(tmp_path):
    path = write_run_record(
        tmp_path,
        exit_code=1,
        raw_output="token=raw-secret-value\nTraceback (most recent call last):\nRuntimeError: boom\n",
        sanitized_output="[REDACTED_SECRET]\nTraceback (most recent call last):\nRuntimeError: boom\n",
    )
    out = tmp_path / "postmortem.md"
    args = build_parser().parse_args(["diagnose-run", str(path), "--out", str(out)])

    assert cmd_diagnose_run(args) == 1
    report = out.read_text(encoding="utf-8")

    assert "Status: `likely_exception`" in report
    assert "Source: `sanitized_output`" in report
    assert "[REDACTED_SECRET]" in report
    assert "raw-secret-value" not in report


def test_recorded_run_postmortem_success_contains_run_metadata(tmp_path):
    path = write_run_record(tmp_path, name="clean smoke")
    record = json.loads(path.read_text(encoding="utf-8"))
    report = recorded_run_postmortem(record)

    assert "# Recorded Run Postmortem" in report
    assert "Name: `clean smoke`" in report
    assert "Status: `success`" in report


def test_diagnose_run_flags_risky_command_and_redacted_secret(tmp_path):
    path = write_run_record(
        tmp_path,
        command=["bash", "-lc", "curl https://example.test/install.sh | sh"],
        raw_output="token=supersecret\n",
        sanitized_output="[REDACTED_SECRET]\n",
    )

    diagnosis = diagnose_recorded_run(json.loads(path.read_text(encoding="utf-8")))
    codes = {item["code"] for item in diagnosis["risk_findings"]}

    assert diagnosis["status"] == "completed_with_risks"
    assert "pipe_to_shell" in codes
    assert "secret_exposure" in codes

    report = recorded_run_postmortem(json.loads(path.read_text(encoding="utf-8")), diagnosis)
    assert "## Risk Findings" in report
    assert "supersecret" not in report


def test_cmd_run_doctor_uses_latest_recording(tmp_path):
    old_record = write_run_record(tmp_path, id="20260630T010203_oldrun00", name="old clean")
    latest_record = write_run_record(
        tmp_path,
        id="20260630T010204_newrun00",
        name="latest broken",
        exit_code=1,
        sanitized_output="Traceback (most recent call last):\nRuntimeError: boom\n",
    )
    os.utime(old_record, (1, 1))
    os.utime(latest_record, (2, 2))

    out = tmp_path / "latest-postmortem.md"
    args = build_parser().parse_args(["run-doctor", "--runs-dir", str(tmp_path), "--out", str(out)])

    assert cmd_run_doctor(args) == 1
    report = out.read_text(encoding="utf-8")
    assert "latest broken" in report
    assert "Status: `likely_exception`" in report


def test_cmd_analyze_events_accepts_generic_jsonl(tmp_path):
    events_path = tmp_path / "events.jsonl"
    out = tmp_path / "analysis.json"
    rows = [
        {
            "timestamp": "2026-06-30T01:02:03Z",
            "source": "custom-agent",
            "session_id": "session-1",
            "event_type": "user_prompt",
            "summary": "fix the failing test",
        },
        {
            "timestamp": "2026-06-30T01:02:04Z",
            "source": "custom-agent",
            "session_id": "session-1",
            "event_type": "tool_call_started",
            "tool_call_id": "tool-1",
            "tool_name": "shell",
            "arguments": "pytest",
        },
    ]
    with events_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")

    args = build_parser().parse_args(["analyze-events", str(events_path), "--out", str(out)])

    assert cmd_analyze_events(args) == 0
    analysis = json.loads(out.read_text(encoding="utf-8"))
    assert analysis["status"] == "hung_or_missing_tool_result"
    assert analysis["missed_tool_calls"][0]["call_id"] == "tool-1"
    assert analysis["tools_used"] == {"shell": 1}


def test_blackbox_events_flags_interactive_auth_flow(tmp_path):
    events_path = tmp_path / "interactive-auth.jsonl"
    out = tmp_path / "analysis.json"
    rows = [
        {
            "timestamp": "2026-07-03T09:00:00Z",
            "source": "cursor-agent",
            "session_id": "cursor-gh-auth-hang-demo",
            "event_type": "tool_call_started",
            "tool_call_id": "shell-1",
            "tool_name": "shell",
            "arguments": "gh auth login --hostname github.com",
        },
        {
            "timestamp": "2026-07-03T09:10:00Z",
            "source": "cursor-agent",
            "session_id": "cursor-gh-auth-hang-demo",
            "event_type": "error",
            "severity": "ERROR",
            "summary": "blocked_interactive_subprocess: gh auth login requires an interactive terminal/browser flow",
        },
    ]
    with events_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")

    args = build_parser().parse_args(["analyze-events", str(events_path), "--out", str(out)])

    assert cmd_analyze_events(args) == 0
    analysis = json.loads(out.read_text(encoding="utf-8"))
    assert analysis["status"] == "hung_or_missing_tool_result"
    assert any(item["code"] == "interactive_auth_flow" for item in analysis["risk_findings"])


def test_claude_parser(tmp_path):
    import json
    claude_dir = tmp_path / "claude"
    projects_dir = claude_dir / "projects"
    p_dir = projects_dir / "my-awesome-project"
    p_dir.mkdir(parents=True)

    session_file = p_dir / "session-12345.jsonl"
    lines = [
        {"timestamp": "2026-03-20T10:00:00Z", "role": "user", "content": "hello agent"},
        {"timestamp": "2026-03-20T10:00:05Z", "role": "assistant", "content": [{"type": "text", "text": "let me find files"}, {"type": "tool_use", "name": "search_files", "input": {"pattern": "*"}}]},
        {"timestamp": "2026-03-20T10:00:10Z", "type": "tool_result", "content": "found 1 file"},
        {"timestamp": "2026-03-20T10:00:15Z", "role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    with session_file.open("w", encoding="utf-8") as f:
        for item in lines:
            f.write(json.dumps(item) + "\n")

    from agent_blackbox.claude import find_claude_projects, summarize_claude_session
    projects = find_claude_projects(claude_dir)
    assert "my-awesome-project" in projects
    assert session_file in projects["my-awesome-project"]

    summary = summarize_claude_session(session_file)
    assert summary["session_id"] == "12345"
    assert summary["user_prompts"] == 1
    assert summary["assistant_turns"] == 2
    assert summary["tool_calls"] == 1
    assert "search_files" in summary["tools_used"]
    assert summary["duration_seconds"] == 15


def test_claude_parser_detects_missing_tool_result(tmp_path):
    claude_dir = tmp_path / "claude"
    session_dir = claude_dir / "projects" / "project"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "session-hung.jsonl"
    lines = [
        {"timestamp": "2026-03-20T10:00:00Z", "role": "user", "content": "inspect"},
        {
            "timestamp": "2026-03-20T10:00:05Z",
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"cmd": "pytest"}}],
        },
    ]
    with session_file.open("w", encoding="utf-8") as f:
        for item in lines:
            f.write(json.dumps(item) + "\n")

    from agent_blackbox.claude import summarize_claude_session

    summary = summarize_claude_session(session_file)
    assert summary["status"] == "hung"
    assert summary["tool_hangs"][0]["call_id"] == "toolu_1"
    assert summary["tool_hangs"][0]["name"] == "bash"


def test_codex_parser_detects_interleaved_missing_tool_result(tmp_path):
    session_file = tmp_path / "rollout-2026-06-30T01-02-03-thread.jsonl"
    lines = [
        {"timestamp": "2026-06-30T01:02:03Z", "type": "event_msg", "payload": {"type": "user_message"}},
        {
            "timestamp": "2026-06-30T01:02:04Z",
            "type": "response_item",
            "payload": {"type": "function_call", "name": "shell", "call_id": "call_1", "arguments": {"cmd": "pytest"}},
        },
        {
            "timestamp": "2026-06-30T01:02:05Z",
            "type": "response_item",
            "payload": {"type": "function_call", "name": "read_file", "call_id": "call_2", "arguments": {"path": "app.py"}},
        },
        {
            "timestamp": "2026-06-30T01:02:06Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "call_1", "output": "1 passed"},
        },
    ]
    with session_file.open("w", encoding="utf-8") as f:
        for item in lines:
            f.write(json.dumps(item) + "\n")

    from agent_blackbox.codex import summarize_codex_session

    summary = summarize_codex_session(session_file)
    assert summary["tool_calls"] == 2
    assert summary["tools_used"] == {"shell": 1, "read_file": 1}
    assert summary["status"] == "hung"
    assert summary["tool_hangs"][0]["call_id"] == "call_2"
    assert summary["tool_hangs"][0]["name"] == "read_file"


def test_opencode_parser_detects_exported_tool_hang(tmp_path):
    export_file = tmp_path / "opencode-export.json"
    export_file.write_text(
        json.dumps(
            {
                "session": {"id": "ses_open_123"},
                "messages": [
                    {
                        "timestamp": "2026-07-04T08:00:00Z",
                        "role": "user",
                        "content": "fix tests",
                    },
                    {
                        "timestamp": "2026-07-04T08:00:03Z",
                        "role": "assistant",
                        "parts": [
                            {"type": "text", "text": "running tests"},
                            {
                                "type": "tool-invocation",
                                "toolInvocation": {
                                    "state": "call",
                                    "toolName": "bash",
                                    "toolCallId": "tool_1",
                                    "args": {"command": "pytest"},
                                },
                            },
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    from agent_blackbox.opencode import summarize_opencode_session

    summary = summarize_opencode_session(export_file)
    assert summary["session_id"] == "ses_open_123"
    assert summary["user_prompts"] == 1
    assert summary["assistant_turns"] == 1
    assert summary["tool_calls"] == 1
    assert summary["tools_used"] == {"bash": 1}
    assert summary["status"] == "hung"
    assert summary["tool_hangs"][0]["call_id"] == "tool_1"


def test_opencode_parser_detects_exported_tool_failure(tmp_path):
    export_file = tmp_path / "opencode-failure.json"
    export_file.write_text(
        json.dumps(
            {
                "session": {"id": "ses_open_fail"},
                "messages": [
                    {
                        "timestamp": "2026-07-04T08:00:03Z",
                        "role": "assistant",
                        "parts": [
                            {
                                "type": "tool-invocation",
                                "toolInvocation": {
                                    "state": "call",
                                    "toolName": "bash",
                                    "toolCallId": "tool_1",
                                    "args": {"command": "gh auth login"},
                                },
                            },
                            {
                                "type": "tool-invocation",
                                "toolInvocation": {
                                    "state": "error",
                                    "toolName": "bash",
                                    "toolCallId": "tool_1",
                                    "error": "requires an interactive terminal",
                                },
                            },
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    from agent_blackbox.opencode import summarize_opencode_session

    summary = summarize_opencode_session(export_file)
    assert summary["status"] == "tool_failure"
    assert summary["tool_failures"][0]["call_id"] == "tool_1"
    assert any(item["code"] == "interactive_auth_flow" for item in summary["risk_findings"])


def test_find_opencode_sessions_reads_project_storage_metadata(tmp_path):
    opencode_dir = tmp_path / "opencode"
    storage_dir = opencode_dir / "project" / "agent-blackbox" / "storage"
    session_dir = storage_dir / "session" / "agent-blackbox"
    message_dir = storage_dir / "message" / "ses_local"
    session_dir.mkdir(parents=True)
    message_dir.mkdir(parents=True)
    session_file = session_dir / "ses_local.json"
    session_file.write_text(
        json.dumps(
            {
                "id": "ses_local",
                "title": "Local OpenCode run",
                "directory": "C:/Users/alice/agent-blackbox",
                "time_updated": 1783152000000,
            }
        ),
        encoding="utf-8",
    )
    (message_dir / "msg_1.json").write_text(json.dumps({"role": "user", "content": "hello"}), encoding="utf-8")

    from agent_blackbox.opencode import find_opencode_sessions

    sessions = find_opencode_sessions(opencode_dir)
    assert len(sessions) == 1
    assert sessions[0]["id"] == "ses_local"
    assert sessions[0]["project"] == "agent-blackbox"
    assert sessions[0]["message_count"] == 1


def test_export_html_report(tmp_path):
    hermes_home = tmp_path / "hermes"
    logs_dir = hermes_home / "logs"
    logs_dir.mkdir(parents=True)

    import datetime as dt
    now = dt.datetime.now()
    t1 = (now - dt.timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S,000")
    t2 = (now - dt.timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S,000")
    t3 = (now - dt.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S,000")

    # Write a mock gateway log
    gateway_log = logs_dir / "gateway.log"
    gateway_log.write_text(
        f"{t1} INFO hermes_plugins.discord_platform.adapter: [Discord] Connected as AgentRosh#9616\n"
        f"{t2} INFO gateway.run: inbound message: platform=discord user=Dhiraj chat=1 msg='What happened?' reply_to_id=None\n"
        f"{t3} INFO hermes_plugins.discord_platform.adapter: [Discord] Flushing text batch agent:main:discord:dm:1 (88 chars)\n",
        encoding="utf-8"
    )

    # Let's test with the `export-html` subcommand
    output_html = tmp_path / "report.html"
    args = build_parser().parse_args([
        "--hermes-home", str(hermes_home),
        "export-html",
        "--platform", "discord",
        "--minutes", "1440",
        "--out", str(output_html)
    ])

    assert cmd_export_html(args) == 0
    assert output_html.exists()

    html_content = output_html.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in html_content
    assert "AI Flight Recorder Postmortem" in html_content
    assert "response_flushed" in html_content
    assert "inbound message: platform=discord" in html_content
    assert "Flushing text batch" in html_content
    assert "What happened?" in html_content

    # Now let's test with the `visual` alias
    output_html_2 = tmp_path / "report2.html"
    args_alias = build_parser().parse_args([
        "--hermes-home", str(hermes_home),
        "visual",
        "--platform", "discord",
        "--minutes", "1440",
        "--out", str(output_html_2)
    ])
    assert args_alias.func == cmd_export_html
    assert cmd_export_html(args_alias) == 0
    assert output_html_2.exists()


def test_html_report_escapes_log_content():
    lines = [
        "2026-06-20 21:57:57,932 INFO gateway.run: inbound message: platform=discord user=Dhiraj chat=1 msg='<script>alert(1)</script>' reply_to_id=None reply_to_text=''",
    ]
    events = [parse_log_line(line) for line in lines]
    diagnosis = diagnose_no_response(events, "discord")

    html = html_postmortem(events, diagnosis, "discord")

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html


def test_message_correlation_does_not_reuse_one_flush_for_multiple_messages():
    lines = [
        "2026-06-20 21:57:00,000 INFO gateway.run: inbound message: platform=discord user=Dhiraj chat=1 msg='first'",
        "2026-06-20 21:57:01,000 INFO gateway.run: inbound message: platform=discord user=Dhiraj chat=1 msg='second'",
        "2026-06-20 21:57:02,000 INFO hermes_plugins.discord_platform.adapter: [Discord] Flushing text batch agent:main:discord:dm:1 (88 chars)",
    ]
    trace = correlate_inbound_flushes(
        [parse_log_line(line) for line in lines[:2]],
        [parse_log_line(lines[2])],
    )
    assert [item["responded"] for item in trace] == [True, False]


def test_record_raw_retention_requires_explicit_opt_in(tmp_path):
    args = build_parser().parse_args(
        [
            "record",
            "--store-raw",
            "--out-dir",
            str(tmp_path),
            "--",
            sys.executable,
            "-c",
            "print('token=raw-secret-value')",
        ]
    )
    assert cmd_record(args) == 0
    record = json.loads(next(tmp_path.glob("run_*.json")).read_text(encoding="utf-8"))
    assert record["privacy"]["raw_output_stored"] is True
    assert "raw-secret-value" in record["raw_output"]
    assert "raw-secret-value" not in record["sanitized_output"]


def test_record_caps_capture_and_times_out(tmp_path):
    args = build_parser().parse_args(
        [
            "record",
            "--out-dir",
            str(tmp_path),
            "--max-capture-bytes",
            "32",
            "--timeout-seconds",
            "0.1",
            "--",
            sys.executable,
            "-c",
            "import time; print('x' * 200, flush=True); time.sleep(2)",
        ]
    )
    assert cmd_record(args) == 124
    record = json.loads(next(tmp_path.glob("run_*.json")).read_text(encoding="utf-8"))
    assert record["capture"]["truncated"] is True
    assert record["capture"]["captured_bytes"] <= 32
    assert "timed out" in record["sanitized_output"]


def test_sanitize_redacts_url_credentials_and_cli_flags():
    text = "https://alice:secret-pass@example.test --token top-secret-value"
    redacted = sanitize(text)
    assert "secret-pass" not in redacted
    assert "top-secret-value" not in redacted
    assert redacted.count("[REDACTED_SECRET]") == 2


def test_jsonl_error_reports_line_number(tmp_path):
    from agent_blackbox.blackbox import load_blackbox_events

    path = tmp_path / "events.jsonl"
    path.write_text('{"event_type":"user_prompt"}\nnot-json\n', encoding="utf-8")
    try:
        load_blackbox_events(path)
    except ValueError as exc:
        assert "line 2" in str(exc)
    else:
        raise AssertionError("malformed JSONL should fail")


def test_tool_call_ids_are_scoped_by_session():
    from agent_blackbox.blackbox import BlackboxEvent, analyze_blackbox_events

    events = [
        BlackboxEvent("2026-01-01T00:00:00Z", "custom", "session-a", "tool_call_started", tool_call_id="call-1", tool_name="shell"),
        BlackboxEvent("2026-01-01T00:00:01Z", "custom", "session-b", "tool_call_started", tool_call_id="call-1", tool_name="shell"),
        BlackboxEvent("2026-01-01T00:00:02Z", "custom", "session-a", "tool_call_completed", tool_call_id="call-1", tool_name="shell"),
    ]
    analysis = analyze_blackbox_events(events)
    assert analysis["schema_version"] == "1.0"
    assert len(analysis["completed_tool_calls"]) == 1
    assert analysis["completed_tool_calls"][0]["session_id"] == "session-a"
    assert len(analysis["missed_tool_calls"]) == 1
    assert analysis["missed_tool_calls"][0]["session_id"] == "session-b"


def test_analyze_events_json_stdout_and_ci_gate(tmp_path, capsys):
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "event_type": "tool_call_started",
                "tool_call_id": "call-1",
                "tool_name": "shell",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    args = build_parser().parse_args(
        ["analyze-events", str(events_path), "--format", "json", "--fail-on-incident"]
    )
    assert cmd_analyze_events(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "1.0"
    assert payload["status"] == "hung_or_missing_tool_result"


def test_native_adapters_surface_malformed_records(tmp_path):
    from agent_blackbox.claude import summarize_claude_session
    from agent_blackbox.codex import summarize_codex_session

    claude_path = tmp_path / "session-broken.jsonl"
    codex_path = tmp_path / "rollout-broken.jsonl"
    content = "not-json\n"
    claude_path.write_text(content, encoding="utf-8")
    codex_path.write_text(content, encoding="utf-8")

    for summary in (summarize_claude_session(claude_path), summarize_codex_session(codex_path)):
        assert summary["status"] == "invalid_or_unsupported_input"
        assert summary["errors_count"] == 1


def test_imported_sanitized_output_is_redacted_again():
    record = {
        "schema_version": "1.0",
        "id": "untrusted",
        "command": ["tool"],
        "exit_code": 1,
        "sanitized_output": '{"api_key": "nested-secret-value"}\n<script>alert(1)</script>',
    }
    report = recorded_run_postmortem(record)
    assert "nested-secret-value" not in report
    assert "[REDACTED_SECRET]" in report
    assert "<script>" not in report


def test_cron_postmortem_resists_fence_and_markdown_injection():
    from agent_blackbox.diagnosis import cron_postmortem

    report = cron_postmortem(
        {
            "name": "job [click](https://evil.test)",
            "job_id": "id`break",
            "status": "cron_error",
            "root_cause": "unknown",
            "last_status": "error",
            "last_run_at": None,
            "next_run_at": None,
            "script": "probe.py",
            "no_agent": False,
            "last_error": "<img src=https://evil.test/pixel>",
            "latest_output_path": "out.md",
            "latest_output_excerpt": "```\n<img src=https://evil.test/pixel>",
            "recommendations": ["review [link](https://evil.test)"],
        }
    )
    assert "<img" not in report
    assert "[click](https://evil.test)" not in report
    assert "````text" in report


def test_sanitize_redacts_structured_and_provider_secret_shapes():
    text = (
        '{"api_key": "json-secret-value"} '
        "--token cli-secret-value "
        "npm_abcdefghijklmnopqrstuvwxyz123456 "
        "AIzaabcdefghijklmnopqrstuvwxyz123456789"
    )
    redacted = sanitize(text)
    for secret in ("json-secret-value", "cli-secret-value", "npm_abcdefghijklmnopqrstuvwxyz123456"):
        assert secret not in redacted
    assert redacted.count("[REDACTED_SECRET]") >= 4


def test_claude_nested_message_shape_and_mixed_timestamps(tmp_path):
    from agent_blackbox.claude import summarize_claude_session

    path = tmp_path / "session-nested.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "message": {"role": "user", "content": "go"}},
        {
            "timestamp": "2026-01-01T00:00:01",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call-1", "name": "shell", "input": {"cmd": "pytest"}}],
            },
        },
        {
            "timestamp": "2026-01-01T00:00:02+00:00",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "1 passed"}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    summary = summarize_claude_session(path)
    assert summary["user_prompts"] == 1
    assert summary["assistant_turns"] == 1
    assert summary["tool_calls"] == 1
    assert summary["status"] == "completed"
    assert summary["duration_seconds"] == 2


def test_codex_zero_failed_is_not_tool_failure(tmp_path):
    from agent_blackbox.codex import summarize_codex_session

    path = tmp_path / "rollout-zero-failed.jsonl"
    rows = [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "response_item",
            "payload": {"type": "function_call", "name": "shell", "call_id": "call-1", "arguments": "pytest"},
        },
        {
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "call-1", "output": "10 passed, 0 failed"},
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    summary = summarize_codex_session(path)
    assert summary["status"] == "completed"
    assert summary["tool_failures"] == []


def test_direct_session_paths_work_without_discovery_roots(tmp_path):
    claude_path = tmp_path / "session-direct.jsonl"
    claude_path.write_text(
        json.dumps({"timestamp": "2026-01-01T00:00:00Z", "role": "user", "content": "hello"}) + "\n",
        encoding="utf-8",
    )
    codex_path = tmp_path / "rollout-direct.jsonl"
    codex_path.write_text(
        json.dumps({"timestamp": "2026-01-01T00:00:00Z", "type": "event_msg", "payload": {"type": "user_message"}}) + "\n",
        encoding="utf-8",
    )
    empty_root = tmp_path / "empty"
    assert build_parser().parse_args(["claude", "--claude-dir", str(empty_root), "--session", str(claude_path)]).func(
        build_parser().parse_args(["claude", "--claude-dir", str(empty_root), "--session", str(claude_path)])
    ) == 0
    codex_args = build_parser().parse_args(["codex", "--codex-dir", str(empty_root), "--session", str(codex_path)])
    assert codex_args.func(codex_args) == 0


def test_unknown_event_schema_and_type_fail_closed(tmp_path):
    from agent_blackbox.blackbox import load_blackbox_events

    bad_schema = tmp_path / "schema.json"
    bad_schema.write_text(json.dumps({"schema_version": "999", "events": []}), encoding="utf-8")
    bad_type = tmp_path / "type.jsonl"
    bad_type.write_text(json.dumps({"event_type": "future_magic"}) + "\n", encoding="utf-8")
    for path in (bad_schema, bad_type):
        try:
            load_blackbox_events(path)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{path.name} should fail closed")


def test_latest_recording_skips_newer_partial_file(tmp_path):
    from agent_blackbox.diagnosis import diagnose_latest_recording

    valid = write_run_record(tmp_path, id="valid", name="valid older")
    partial = tmp_path / "run_partial.json"
    partial.write_text("{", encoding="utf-8")
    os.utime(valid, (1, 1))
    os.utime(partial, (2, 2))
    path, record, diagnosis = diagnose_latest_recording(tmp_path)
    assert path == valid
    assert record["name"] == "valid older"
    assert diagnosis["invalid_newer_recordings_skipped"] == 1


def test_doctor_returns_nonzero_when_gateway_status_is_unhealthy(monkeypatch, tmp_path):
    from agent_blackbox import cli

    monkeypatch.setattr(cli, "run_gateway_status", lambda: (127, "Hermes executable missing"))
    args = build_parser().parse_args(["--hermes-home", str(tmp_path), "doctor"])
    assert cli.cmd_doctor(args) == 1
