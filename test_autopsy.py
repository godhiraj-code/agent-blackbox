import json
from pathlib import Path

import pytest

from agent_blackbox.autopsy import build_autopsy, rank_fight, render_autopsy_html, render_fight_html, score_analysis
from agent_blackbox.blackbox import analyze_blackbox_events, load_blackbox_events
from agent_blackbox.cli import build_parser, cmd_autopsy, cmd_fight

FIXTURES = Path(__file__).parent / "examples" / "agent-autopsy"


def analysis(name):
    return analyze_blackbox_events(load_blackbox_events(FIXTURES / name))


def test_scoring_is_deterministic_bounded_and_explainable():
    clean = score_analysis(analysis("clean-verified.jsonl"))
    risky = score_analysis(analysis("risky-false-success.jsonl"))
    hung = score_analysis(analysis("hung-authentication.jsonl"))
    assert clean == score_analysis(analysis("clean-verified.jsonl"))
    assert clean["score"] == 100
    assert 0 <= hung["score"] < risky["score"] < clean["score"] <= 100
    assert {item["code"] for item in risky["penalties"]} >= {
        "failed_tool_calls", "errors", "high_risk_findings", "skipped_verification"
    }
    assert clean["verification_evidence"]
    assert "does not measure task correctness" in clean["disclaimer"]


def test_autopsy_html_escapes_and_redacts_all_untrusted_values():
    secret = "sk-" + "x" * 40
    report = build_autopsy(
        {
            "status": '<img src=x onerror="alert(1)">',
            "tool_calls": 1,
            "timeline": [{"event_type": "assistant_message", "summary": f"<script>alert(1)</script> {secret}"}],
            "risk_findings": [{"severity": "high", "code": "x<script>", "label": secret}],
            "tools_used": {'shell"><script>': 1},
        },
        'run"><script>alert(1)</script>',
    )
    rendered = render_autopsy_html(report)
    assert "<script>alert(1)</script>" not in rendered
    assert "<img src=x" not in rendered
    assert secret not in rendered
    assert "&lt;script&gt;" in rendered
    assert "[REDACTED_SECRET]" in rendered
    assert "http://" not in rendered and "https://" not in rendered


def test_fight_ranking_is_deterministic_and_names_are_escaped():
    clean = analysis("clean-verified.jsonl")
    risky = analysis("risky-false-success.jsonl")
    report = rank_fight([("Zulu", clean), ("Alpha", clean), ('<script>x</script>', risky)])
    assert [run["name"] for run in report["runs"]] == ["Alpha", "Zulu", "<script>x</script>"]
    rendered = render_fight_html(report)
    assert "<script>x</script>" not in rendered
    assert "&lt;script&gt;x&lt;/script&gt;" in rendered
    with pytest.raises(ValueError, match="unique"):
        rank_fight([("same", clean), ("same", risky)])


def test_autopsy_cli_writes_html_and_json(tmp_path, capsys):
    html_out = tmp_path / "report.html"
    json_out = tmp_path / "score.json"
    args = build_parser().parse_args([
        "autopsy", str(FIXTURES / "clean-verified.jsonl"), "--out", str(html_out), "--json-out", str(json_out)
    ])
    assert cmd_autopsy(args) == 0
    assert html_out.read_text(encoding="utf-8").startswith("<!doctype html>")
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["scorecard"]["score"] == 100
    assert "verdict:" in capsys.readouterr().out


def test_fight_cli_writes_ranked_outputs(tmp_path):
    html_out = tmp_path / "fight.html"
    json_out = tmp_path / "fight.json"
    args = build_parser().parse_args([
        "fight",
        f"Clean={FIXTURES / 'clean-verified.jsonl'}",
        f"Risky={FIXTURES / 'risky-false-success.jsonl'}",
        "--out", str(html_out), "--json-out", str(json_out),
    ])
    assert cmd_fight(args) == 0
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["winner"] == "Clean"
    assert [run["rank"] for run in payload["runs"]] == [1, 2]
    assert "universal model quality" in html_out.read_text(encoding="utf-8")


@pytest.mark.parametrize("argv, expected", [
    (["fight", f"Only={FIXTURES / 'clean-verified.jsonl'}", "--out", "ignored.html"], "at least two"),
    (["fight", f"Same={FIXTURES / 'clean-verified.jsonl'}", f"Same={FIXTURES / 'risky-false-success.jsonl'}", "--out", "ignored.html"], "unique"),
    (["fight", "not-an-entry", f"Two={FIXTURES / 'clean-verified.jsonl'}", "--out", "ignored.html"], "NAME=PATH"),
])
def test_fight_cli_validation(argv, expected, capsys):
    args = build_parser().parse_args(argv)
    assert cmd_fight(args) == 2
    assert expected in capsys.readouterr().err


def test_autopsy_rejects_empty_and_malformed_input(tmp_path, capsys):
    for filename, content in (("empty.jsonl", ""), ("broken.jsonl", "not-json\n")):
        path = tmp_path / filename
        path.write_text(content, encoding="utf-8")
        args = build_parser().parse_args(["autopsy", str(path), "--out", str(tmp_path / f"{filename}.html")])
        assert cmd_autopsy(args) == 2
    stderr = capsys.readouterr().err
    assert "no events" in stderr
    assert "line 1" in stderr
