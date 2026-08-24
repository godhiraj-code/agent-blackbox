from __future__ import annotations

import html
from typing import Any, Iterable

from .parser import sanitize

AUTOPSY_SCHEMA_VERSION = "1.0"
EVIDENCE_DISCLAIMER = (
    "This score compares captured execution evidence only. It does not measure task correctness, "
    "model intelligence, safety, universal model quality, or evidence that was not recorded."
)


def score_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic, explainable 0..100 evidence score."""
    failed = len(_list(analysis.get("failed_tool_calls")))
    missing = len(_list(analysis.get("missed_tool_calls")))
    errors = len(_list(analysis.get("errors")))
    risks = _list(analysis.get("risk_findings"))
    skipped = sum(1 for item in risks if _text(item.get("code")) == "missed_verification")
    high = sum(1 for item in risks if _text(item.get("severity")).lower() == "high")
    medium = sum(
        1
        for item in risks
        if _text(item.get("severity")).lower() == "medium"
        and _text(item.get("code")) != "missed_verification"
    )

    penalty_specs = (
        ("missing_tool_results", missing, 20, 40, "tool calls with no captured result"),
        ("failed_tool_calls", failed, 15, 30, "captured failed tool calls"),
        ("errors", errors, 10, 20, "captured error events"),
        ("high_risk_findings", high, 10, 30, "high-severity risk findings"),
        ("medium_risk_findings", medium, 5, 15, "medium-severity risk findings"),
        ("skipped_verification", skipped, 10, 20, "explicit evidence that verification was skipped"),
    )
    penalties = []
    total = 0
    for code, count, points_each, cap, explanation in penalty_specs:
        deduction = min(count * points_each, cap)
        if deduction:
            penalties.append(
                {
                    "code": code,
                    "count": count,
                    "points_each": points_each,
                    "cap": cap,
                    "deduction": deduction,
                    "explanation": explanation,
                }
            )
            total += deduction

    score = max(0, min(100, 100 - total))
    if score >= 90:
        verdict = "strong captured run"
    elif score >= 70:
        verdict = "review recommended"
    elif score >= 40:
        verdict = "incident evidence found"
    else:
        verdict = "critical captured run"

    verification = _verification_evidence(analysis)
    confidence = "limited"
    if analysis.get("timeline") and analysis.get("tool_calls", 0):
        confidence = "moderate"
    if verification and analysis.get("timeline"):
        confidence = "higher"

    raw_recommendations = analysis.get("recommendations") if isinstance(analysis.get("recommendations"), list) else []
    recommendations = [str(item) for item in raw_recommendations if item]
    if not recommendations:
        if missing:
            recommendations.append("Resolve missing tool results before trusting the run.")
        if failed:
            recommendations.append("Reproduce the failed tool call and inspect its first error.")
        if high:
            recommendations.append("Review high-severity actions and narrow permissions before rerunning.")
        if not recommendations:
            recommendations.append("Confirm task correctness separately; this score covers captured evidence only.")

    quality = analysis.get("quality") if isinstance(analysis.get("quality"), dict) else {}

    return {
        "schema_version": AUTOPSY_SCHEMA_VERSION,
        "score": score,
        "verdict": verdict,
        "status": _text(analysis.get("status") or "unknown"),
        "total_deduction": total,
        "penalties": penalties,
        "evidence_confidence": confidence,
        "verification_evidence": verification,
        "recommendations": list(dict.fromkeys(recommendations))[:6],
        "disclaimer": EVIDENCE_DISCLAIMER,
        "metrics": {
            "user_prompts": _integer(analysis.get("user_prompts")),
            "assistant_turns": _integer(analysis.get("assistant_turns")),
            "tool_calls": _integer(analysis.get("tool_calls")),
            "completed_tool_calls": len(_list(analysis.get("completed_tool_calls"))),
            "failed_tool_calls": failed,
            "missing_tool_results": missing,
            "errors": errors,
            "high_risk_findings": high,
            "medium_risk_findings": medium + skipped,
            "evidence_gaps": len(_list(quality.get("evidence_gaps"))),
        },
    }


def build_autopsy(analysis: dict[str, Any], name: str = "Captured run") -> dict[str, Any]:
    return {
        "schema_version": AUTOPSY_SCHEMA_VERSION,
        "report_type": "agent_autopsy",
        "name": sanitize(_text(name)),
        "scorecard": score_analysis(analysis),
        "analysis": _sanitize_value(analysis),
    }


def rank_fight(entries: Iterable[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    runs = [build_autopsy(analysis, name) for name, analysis in entries]
    names = [run["name"] for run in runs]
    if len(runs) < 2:
        raise ValueError("fight requires at least two NAME=PATH entries")
    if len(set(names)) != len(names):
        raise ValueError("fight entry names must be unique")
    runs.sort(key=lambda run: (-run["scorecard"]["score"], run["name"].casefold(), run["name"]))
    for rank, run in enumerate(runs, start=1):
        run["rank"] = rank
    return {
        "schema_version": AUTOPSY_SCHEMA_VERSION,
        "report_type": "agent_fight",
        "comparison_scope": EVIDENCE_DISCLAIMER,
        "runs": runs,
        "winner": runs[0]["name"],
    }


def render_autopsy_html(report: dict[str, Any]) -> str:
    scorecard = report["scorecard"]
    analysis = report["analysis"]
    metrics = scorecard["metrics"]
    penalties = scorecard["penalties"]
    risks = _list(analysis.get("risk_findings"))
    tools = analysis.get("tools_used") if isinstance(analysis.get("tools_used"), dict) else {}
    timeline = _list(analysis.get("timeline"))
    quality = analysis.get("quality") if isinstance(analysis.get("quality"), dict) else {}

    penalty_rows = "".join(
        f"<tr><td>{_e(item['code'])}</td><td>{item['count']}</td><td>-{item['deduction']}</td><td>{_e(item['explanation'])}</td></tr>"
        for item in penalties
    ) or '<tr><td colspan="4">No score deductions from captured evidence.</td></tr>'
    risk_items = "".join(
        f"<li><strong>{_e(item.get('severity'))}: {_e(item.get('code'))}</strong> — {_e(item.get('label') or item.get('snippet'))}</li>"
        for item in risks
    ) or "<li>No risk findings in captured evidence.</li>"
    tool_items = "".join(f"<li>{_e(name)}: {_integer(count)}</li>" for name, count in sorted(tools.items(), key=lambda x: str(x[0]))) or "<li>No tool calls captured.</li>"
    timeline_rows = "".join(
        "<tr>"
        f"<td>{_e(item.get('timestamp') or 'unknown')}</td>"
        f"<td>{_e(item.get('event_type') or item.get('type') or 'unknown')}</td>"
        f"<td>{_e(item.get('tool_name') or '')}</td>"
        f"<td>{_e(item.get('summary') or '')}</td>"
        "</tr>"
        for item in timeline
    ) or '<tr><td colspan="4">No timeline events captured.</td></tr>'
    recommendation_items = "".join(f"<li>{_e(item)}</li>" for item in scorecard.get("recommendations", [])) or "<li>No next steps generated.</li>"
    gap_items = "".join(f"<li>{_e(item)}</li>" for item in quality.get("evidence_gaps", [])) or "<li>No evidence gaps recorded.</li>"
    metric_cards = "".join(
        f'<div class="card"><span>{_e(label.replace("_", " "))}</span><b>{value}</b></div>'
        for label, value in metrics.items()
    )
    return _page(
        f"Agent Autopsy — {_e(report['name'])}",
        f"""
<header><p class="eyebrow">AGENT AUTOPSY</p><h1>{_e(report['name'])}</h1><p class="verdict">{_e(scorecard['verdict'])}</p></header>
<section class="hero"><div class="score">{scorecard['score']}<small>/100</small></div><div><h2>{_e(scorecard['status'])}</h2><p>{_e(scorecard['disclaimer'])}</p><span class="pill">Evidence confidence: {_e(scorecard['evidence_confidence'])}</span></div></section>
<h2>Captured metrics</h2><div class="grid">{metric_cards}</div>
<h2>Explainable deductions</h2><table><thead><tr><th>Penalty</th><th>Count</th><th>Deduction</th><th>Why</th></tr></thead><tbody>{penalty_rows}</tbody></table>
<div class="columns"><section><h2>Risk findings</h2><ul>{risk_items}</ul></section><section><h2>Tool usage</h2><ul>{tool_items}</ul></section></div>
<div class="columns"><section><h2>Recommended next steps</h2><ul>{recommendation_items}</ul></section><section><h2>Evidence gaps</h2><ul>{gap_items}</ul></section></div>
<h2>Sanitized timeline</h2><table><thead><tr><th>Time</th><th>Event</th><th>Tool</th><th>Summary</th></tr></thead><tbody>{timeline_rows}</tbody></table>
""",
    )


def render_fight_html(report: dict[str, Any]) -> str:
    rows = "".join(
        "<tr>"
        f"<td>#{run['rank']}</td><td>{_e(run['name'])}</td>"
        f"<td><strong>{run['scorecard']['score']}</strong></td>"
        f"<td>{_e(run['scorecard']['verdict'])}</td>"
        f"<td>{_e(run['scorecard']['status'])}</td>"
        f"<td>{run['scorecard']['total_deduction']}</td>"
        "</tr>"
        for run in report["runs"]
    )
    cards = "".join(
        f'<div class="card rank"><span>#{run["rank"]} {_e(run["name"])}</span><b>{run["scorecard"]["score"]}</b><small>{_e(run["scorecard"]["verdict"])}</small></div>'
        for run in report["runs"]
    )
    return _page(
        "Agent Fight — captured evidence comparison",
        f"""
<header><p class="eyebrow">AGENT FIGHT</p><h1>Captured evidence comparison</h1><p class="verdict">Winner: {_e(report['winner'])}</p><p>{_e(report['comparison_scope'])}</p></header>
<div class="grid">{cards}</div>
<table><thead><tr><th>Rank</th><th>Run</th><th>Score</th><th>Verdict</th><th>Status</th><th>Deductions</th></tr></thead><tbody>{rows}</tbody></table>
""",
    )


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
:root{{--bg:#080b12;--panel:#111827;--line:#263244;--ink:#f8fafc;--muted:#9ca3af;--hot:#f97316;--good:#22c55e}}*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at top right,#28150d,var(--bg) 38%);color:var(--ink);font:15px/1.55 system-ui,sans-serif}}main{{max-width:1100px;margin:auto;padding:48px 22px 80px}}header{{border-bottom:1px solid var(--line);margin-bottom:30px}}h1{{font-size:clamp(2.2rem,6vw,4.8rem);line-height:.95;margin:.2em 0}}h2{{margin-top:34px}}.eyebrow{{letter-spacing:.28em;color:var(--hot);font-weight:800}}.verdict{{font-size:1.3rem;color:#fdba74}}.hero{{display:grid;grid-template-columns:auto 1fr;gap:28px;align-items:center;background:linear-gradient(120deg,#1f2937,#111827);border:1px solid var(--line);border-radius:18px;padding:28px}}.score{{font-size:5rem;font-weight:900;color:var(--hot)}}.score small{{font-size:1rem;color:var(--muted)}}.pill{{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:5px 10px;color:var(--muted)}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;display:flex;flex-direction:column}}.card span,.card small{{color:var(--muted)}}.card b{{font-size:2rem}}.rank b{{color:var(--hot)}}.columns{{display:grid;grid-template-columns:1fr 1fr;gap:22px}}table{{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line)}}th,td{{text-align:left;vertical-align:top;padding:11px;border-bottom:1px solid var(--line);overflow-wrap:anywhere}}th{{color:#fdba74}}li{{margin:.45em 0}}@media(max-width:650px){{.hero,.columns{{grid-template-columns:1fr}}table{{display:block;overflow-x:auto}}}}
</style></head><body><main>{body}<footer><p>Generated locally by Agent Blackbox. No external assets or network requests.</p></footer></main></body></html>"""


def _verification_evidence(analysis: dict[str, Any]) -> list[str]:
    evidence = []
    markers = ("pytest", "unittest", " test", "tests", "verify", "verification", "lint", "typecheck", "build")
    for event in _list(analysis.get("timeline")):
        haystack = " ".join(
            _text(value).lower()
            for value in (
                event.get("tool_name"), event.get("summary"), (event.get("metadata") or {}).get("arguments") if isinstance(event.get("metadata"), dict) else ""
            )
        )
        if any(marker in haystack for marker in markers):
            evidence.append(sanitize(haystack[:160]))
    return list(dict.fromkeys(evidence))[:5]


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {sanitize(_text(key)): _sanitize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str):
        return sanitize(value)
    return value


def _list(value: Any) -> list[dict[str, Any]]:
    return value if isinstance(value, list) else []


def _integer(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _e(value: Any) -> str:
    return html.escape(sanitize(_text(value)), quote=True)
