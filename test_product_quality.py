import datetime as dt
import json
from pathlib import Path

import pytest

from agent_blackbox.blackbox import BlackboxEvent, analyze_blackbox_events
from agent_blackbox.cli import build_parser, cmd_autopsy, cmd_why
from agent_blackbox.parser import find_cron_job, parse_timestamp


def test_timestamp_parser_accepts_iso_and_normalizes_offsets():
    assert parse_timestamp("2026-06-20T21:56:48.739Z INFO") == dt.datetime(2026, 6, 20, 21, 56, 48)
    assert parse_timestamp("2026-06-21T03:26:48+05:30 INFO") == dt.datetime(2026, 6, 20, 21, 56, 48)


def test_ambiguous_cron_name_does_not_silently_choose_a_job(tmp_path: Path):
    home = tmp_path / "hermes"
    (home / "cron").mkdir(parents=True)
    (home / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "one", "name": "Mail watcher"}, {"id": "two", "name": "Mail watcher backup"}]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ambiguous"):
        find_cron_job("mail", home)


def test_analysis_exposes_evidence_quality_and_next_steps():
    analysis = analyze_blackbox_events(
        [
            BlackboxEvent(
                timestamp="not-a-timestamp",
                source="test",
                session_id="s1",
                event_type="tool_call_started",
                tool_call_id="call-1",
                tool_name="shell",
                metadata={"arguments": "pytest"},
            )
        ]
    )

    assert analysis["status"] == "hung_or_missing_tool_result"
    assert analysis["incident"] is True
    assert analysis["quality"]["invalid_timestamp_count"] == 1
    assert analysis["quality"]["evidence_gaps"]
    assert analysis["recommendations"]


def test_why_no_response_is_a_nonzero_diagnostic_gate(tmp_path: Path):
    home = tmp_path / "hermes"
    log = home / "logs" / "gateway.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "2099-06-20 21:57:57,932 INFO gateway.run: inbound message: platform=discord user=test chat=1 msg='hello'\n",
        encoding="utf-8",
    )

    args = build_parser().parse_args(["--hermes-home", str(home), "why-no-response", "--minutes", "180"])
    assert cmd_why(args) == 1


def test_autopsy_can_gate_incidents(tmp_path: Path):
    trace = tmp_path / "incident.jsonl"
    trace.write_text(
        json.dumps(
            {
                "timestamp": "2026-06-20T21:57:57Z",
                "source": "test",
                "session_id": "s1",
                "event_type": "tool_call_started",
                "tool_call_id": "call-1",
                "tool_name": "shell",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    args = build_parser().parse_args(
        ["autopsy", str(trace), "--out", str(tmp_path / "report.html"), "--fail-on-incident"]
    )
    assert cmd_autopsy(args) == 1
