"""外部觸發器健康檢查（healthcheck.py）的單元測試。"""
from datetime import datetime, timezone

from healthcheck import (
    build_alert_message,
    latest_dispatch_age_minutes,
    parse_timestamp,
    should_alert,
)

NOW = datetime(2026, 10, 7, 15, 30, 0, tzinfo=timezone.utc)


def test_parse_timestamp_with_z_suffix():
    assert parse_timestamp("2026-10-07T15:03:09Z") == datetime(
        2026, 10, 7, 15, 3, 9, tzinfo=timezone.utc
    )


def test_parse_timestamp_invalid_returns_none():
    assert parse_timestamp("") is None
    assert parse_timestamp(None) is None
    assert parse_timestamp("not-a-date") is None


def test_latest_dispatch_age_minutes_picks_newest():
    payload = {
        "workflow_runs": [
            {"created_at": "2026-10-07T15:00:00Z"},
            {"created_at": "2026-10-07T15:25:00Z"},
            {"created_at": "2026-10-07T14:55:00Z"},
        ]
    }
    assert latest_dispatch_age_minutes(payload, now=NOW) == 5.0


def test_latest_dispatch_age_minutes_no_runs():
    assert latest_dispatch_age_minutes({"workflow_runs": []}, now=NOW) is None
    assert latest_dispatch_age_minutes({}, now=NOW) is None


def test_should_alert_thresholds():
    assert should_alert(45.0, 30) is True
    assert should_alert(30.0, 30) is False   # 剛好等於門檻不告警
    assert should_alert(None, 30) is True     # 完全沒有紀錄視為異常


def test_build_alert_message_mentions_minutes_and_repo():
    msg = build_alert_message(42.5, 30, "owner/repo")
    assert "42.5" in msg
    assert "30" in msg
    assert "owner/repo" in msg
    assert "cron-job.org" in msg


def test_build_alert_message_without_runs():
    msg = build_alert_message(None, 30, "owner/repo")
    assert "查不到" in msg
    assert "owner/repo" in msg
