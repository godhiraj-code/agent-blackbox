import datetime as dt

from agent_doctor import diagnose_no_response, parse_log_line, sanitize


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
    assert any("delay/busy-turn" in note for note in diagnosis["notes"])


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
