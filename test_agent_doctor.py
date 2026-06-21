import datetime as dt
import json
import sys
from pathlib import Path

from agent_doctor import build_parser, cmd_demo, cmd_record, correlate_inbound_flushes, diagnose_cron_job, diagnose_no_response, find_cron_job, parse_log_line, sanitize


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


def test_cmd_record(tmp_path):
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
    records = list(tmp_path.glob("run_*.json"))
    assert len(records) == 1

    data = json.loads(records[0].read_text(encoding="utf-8"))
    assert data["id"]
    assert data["name"] == "test recording"
    assert data["command"] == [sys.executable, "-c", "print('hello world'); print('token=supersecret')"]
    assert data["exit_code"] == 0
    assert data["duration_seconds"] >= 0
    assert dt.datetime.fromisoformat(data["start_time"]) <= dt.datetime.fromisoformat(data["end_time"])
    assert "hello world" in data["raw_output"]
    assert "token=supersecret" in data["raw_output"]
    assert "supersecret" not in data["sanitized_output"]
    assert "[REDACTED_SECRET]" in data["sanitized_output"]
