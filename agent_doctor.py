from __future__ import annotations

import sys

# Backward-compatible wrapper delegating to modularized package structure
from agent_blackbox.cli import (
    main,
    build_parser,
    cmd_demo,
    cmd_record,
    cmd_diagnose_run,
    cmd_run_doctor,
    cmd_analyze_events,
    cmd_autopsy,
    cmd_fight,
    cmd_export_html,
    cmd_claude,
    cmd_codex,
    cmd_opencode,
)
from agent_blackbox.parser import parse_log_line, sanitize, find_cron_job
from agent_blackbox.diagnosis import (
    diagnose_cron_job,
    diagnose_no_response,
    diagnose_recorded_run,
    diagnose_latest_recording,
    correlate_inbound_flushes,
    html_postmortem,
    recorded_run_postmortem,
)
from agent_blackbox.blackbox import (
    BlackboxEvent,
    analyze_blackbox_events,
    detect_risk_findings,
    event_from_dict,
    find_latest_recording,
    load_blackbox_events,
)
from agent_blackbox.autopsy import build_autopsy, rank_fight, render_autopsy_html, render_fight_html, score_analysis

if __name__ == "__main__":
    raise SystemExit(main())
