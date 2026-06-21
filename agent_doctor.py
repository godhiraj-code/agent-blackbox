from __future__ import annotations

import sys

# Backward-compatible wrapper delegating to modularized package structure
from agent_blackbox.cli import main, build_parser, cmd_demo, cmd_record
from agent_blackbox.parser import parse_log_line, sanitize, find_cron_job
from agent_blackbox.diagnosis import (
    diagnose_cron_job,
    diagnose_no_response,
    correlate_inbound_flushes,
)

if __name__ == "__main__":
    raise SystemExit(main())
