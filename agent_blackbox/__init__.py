from __future__ import annotations

from .version import __version__

from .parser import (
    Event,
    parse_timestamp,
    parse_log_line,
    sanitize,
    log_path,
    cron_jobs_path,
    cron_output_dir,
    read_events,
    read_cron_jobs,
    find_cron_job,
    latest_cron_output,
)
from .diagnosis import (
    diagnose_cron_job,
    cron_postmortem,
    diagnose_no_response,
    diagnose_recorded_run,
    diagnose_latest_recording,
    correlate_inbound_flushes,
    build_notes,
    postmortem,
    recorded_run_postmortem,
)
from .blackbox import (
    BlackboxEvent,
    analyze_blackbox_events,
    detect_risk_findings,
    event_from_dict,
    find_latest_recording,
    load_blackbox_events,
)
from .autopsy import build_autopsy, rank_fight, render_autopsy_html, render_fight_html, score_analysis
from .cli import build_parser, cmd_demo, cmd_record, cmd_diagnose_run, cmd_run_doctor, cmd_analyze_events, cmd_autopsy, cmd_fight, cmd_opencode, main
