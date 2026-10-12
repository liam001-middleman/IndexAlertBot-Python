"""外部觸發器健康檢查（healthcheck.py）的單元測試。"""
from datetime import datetime, timezone

import pytest

import healthcheck
from healthcheck import (
    WORKFLOWS,
    build_alert_message,
    build_silent_message,
    check_started,
    evaluate_silence,
    is_within_window,
    latest_dispatch_age_minutes,
    latest_dispatch_at,
    main,
    parse_hhmm,
    parse_timestamp,
    select_workflows,
    should_alert,
    window_text,
)

NOW = datetime(2026, 10, 7, 15, 30, 0, tzinfo=timezone.utc)
US = next(wf for wf in WORKFLOWS if wf["file"] == "alerts-us.yml")
TW = next(wf for wf in WORKFLOWS if wf["file"] == "alerts-tw.yml")
CRYPTO = next(wf for wf in WORKFLOWS if wf["file"] == "alerts-crypto.yml")

# 模擬 2026-09-29 之後 US / TW 靜默斷線、只有 crypto 正常的情境
US_LAST_RUN = "2026-09-29T22:24:00Z"
CRYPTO_FRESH = "2026-10-07T15:25:00Z"


def _runs(created_at):
    return {"workflow_runs": [{"created_at": created_at}]}


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


def test_parse_hhmm_converts_and_rejects_bad_input():
    assert parse_hhmm("13:00") == 13 * 60
    assert parse_hhmm(" 05:55 ") == 5 * 60 + 55
    with pytest.raises(ValueError):
        parse_hhmm("1360")
    with pytest.raises(ValueError):
        parse_hhmm("25:00")


def test_window_text_formats_weekday_and_daily_ranges():
    assert window_text(US["weekdays"], US["window"]) == "週一~週五 13:00–20:55（UTC）"
    assert window_text(CRYPTO["weekdays"], CRYPTO["window"]) == "每天 00:00–23:59（UTC）"


def test_is_within_window_respects_weekday_and_edges():
    assert is_within_window(NOW, US["weekdays"], US["window"]) is True
    assert is_within_window(NOW, TW["weekdays"], TW["window"]) is False
    # 週六（2026-10-10）美股不該被檢查
    saturday = datetime(2026, 10, 10, 15, 30, tzinfo=timezone.utc)
    assert is_within_window(saturday, US["weekdays"], US["window"]) is False
    # 加密貨幣 24/7
    assert is_within_window(saturday, CRYPTO["weekdays"], CRYPTO["window"]) is True
    # 時段邊界：13:00 含、20:55 含、20:56 不含
    assert is_within_window(datetime(2026, 10, 7, 13, 0, tzinfo=timezone.utc), US["weekdays"], US["window"])
    assert is_within_window(datetime(2026, 10, 7, 20, 55, tzinfo=timezone.utc), US["weekdays"], US["window"])
    assert not is_within_window(datetime(2026, 10, 7, 20, 56, tzinfo=timezone.utc), US["weekdays"], US["window"])


def test_check_started_applies_grace_period():
    weekdays, window = US["weekdays"], US["window"]
    # 時段剛開始 13:10 → 仍在 30 分鐘寬容期內，不檢查
    assert check_started(datetime(2026, 10, 7, 13, 10, tzinfo=timezone.utc), weekdays, window, 30) is False
    assert check_started(datetime(2026, 10, 7, 13, 30, tzinfo=timezone.utc), weekdays, window, 30) is True
    # 週末一律不算已開始
    assert check_started(datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc), weekdays, window, 30) is False


def test_latest_dispatch_at_picks_newest_and_handles_missing():
    assert latest_dispatch_at(_runs(US_LAST_RUN)) == datetime(2026, 9, 29, 22, 24, tzinfo=timezone.utc)
    assert latest_dispatch_at({"workflow_runs": []}) is None
    assert latest_dispatch_at({}) is None
    assert latest_dispatch_at({"workflow_runs": [{"created_at": None}]}) is None


def test_evaluate_silence_flags_only_silent_workflows_in_window():
    runs = {
        "alerts-us.yml": _runs(US_LAST_RUN),      # 斷線：在活躍時段內但已靜默數千分鐘
        "alerts-tw.yml": _runs("2026-09-30T06:13:00Z"),  # 現在不在 TW 活躍時段 → 不判定
        "alerts-crypto.yml": _runs(CRYPTO_FRESH),  # 5 分鐘前才跑過 → 正常
    }
    silent = evaluate_silence(NOW, runs, 30)
    assert len(silent) == 1
    item = silent[0]
    assert item["workflow"] == "alerts-us.yml"
    assert item["name"] == "Alerts - US Stocks"
    assert item["age"] > 30
    assert item["last_run_text"] == "2026-09-29 22:24 UTC"
    assert item["window_text"] == "週一~週五 13:00–20:55（UTC）"


def test_evaluate_silence_ignores_workflows_without_query_data():
    # 只有 crypto 有查詢結果且正常 → 不該因為查不到 US/TW 就誤報
    silent = evaluate_silence(NOW, {"alerts-crypto.yml": _runs(CRYPTO_FRESH)}, 30)
    assert silent == []


def test_evaluate_silence_flags_missing_records():
    silent = evaluate_silence(NOW, {"alerts-us.yml": {"workflow_runs": []}}, 30)
    assert len(silent) == 1
    assert silent[0]["age"] is None
    assert silent[0]["last_run_text"] == "查不到紀錄"


def test_evaluate_silence_weekend_skips_weekday_workflows():
    saturday = datetime(2026, 10, 10, 15, 30, tzinfo=timezone.utc)
    runs = {
        "alerts-us.yml": _runs(US_LAST_RUN),
        "alerts-tw.yml": _runs(US_LAST_RUN),
        "alerts-crypto.yml": _runs("2026-10-10T15:25:00Z"),
    }
    # 週末：US / TW 不判定（即使已靜默多日）；24/7 的 crypto 仍正常
    assert evaluate_silence(saturday, runs, 30) == []
    # 對照組：同一組資料在平日就會抓到 US / TW 斷線
    weekday = datetime(2026, 10, 9, 15, 30, tzinfo=timezone.utc)  # 週五
    assert [item["workflow"] for item in evaluate_silence(weekday, runs, 30)] == ["alerts-us.yml"]


def test_build_silent_message_names_workflow_and_recovery_steps():
    silent = [
        {
            "name": "Alerts - US Stocks",
            "workflow": "alerts-us.yml",
            "age": 13000.0,
            "last_run_text": "2026-09-29 22:24 UTC",
            "window_text": "週一~週五 13:00–20:55（UTC）",
            "actions_url": "https://github.com/owner/repo/actions/workflows/alerts-us.yml",
        }
    ]
    msg = build_silent_message(silent, 30, "owner/repo")
    assert "Alerts - US Stocks" in msg
    assert "alerts-us.yml" in msg
    assert "13000.0" in msg
    assert "2026-09-29 22:24 UTC" in msg
    assert "Inactive" in msg
    assert "dispatch_check.py" in msg
    assert "cron-job.org" in msg
    assert "owner/repo" in msg


def test_build_alert_message_accepts_workflow_name():
    msg = build_alert_message(42.5, 30, "owner/repo", workflow_name="Alerts - TW Stocks")
    assert "Alerts - TW Stocks" in msg
    assert "42.5" in msg


def test_select_workflows_spec_handles_names_and_unknown():
    assert [wf["file"] for wf in select_workflows(None)] == [
        "alerts-us.yml",
        "alerts-tw.yml",
        "alerts-crypto.yml",
    ]
    assert [wf["file"] for wf in select_workflows("alerts-us.yml")] == ["alerts-us.yml"]
    assert [wf["file"] for wf in select_workflows(".github/workflows/alerts-tw.yml")] == ["alerts-tw.yml"]
    assert [wf["file"] for wf in select_workflows("alerts-us.yml,alerts-us.yml")] == ["alerts-us.yml"]
    # 未知名稱全被忽略時，回退成全部（避免哨兵被設定錯誤癱瘓）
    assert len(select_workflows("nope.yml")) == len(WORKFLOWS)


@pytest.fixture
def env(monkeypatch):
    """乾淨的哨兵環境：只設定 repo，其他變數回到預設值。"""
    for name in (
        "CRONJOB_MAX_SILENCE_MINUTES",
        "CRONJOB_GRACE_MINUTES",
        "HEALTHCHECK_WORKFLOWS",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "GITHUB_TOKEN",
        "GH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    return monkeypatch


def _patch_fetch(monkeypatch, mapping, failures=()):
    """把 per-workflow 的執行紀錄查詢換成假資料；failures 內的 workflow 會丟錯。"""

    def _fetch(repo, token, api_url, workflow_file, per_page=1):
        if workflow_file in failures:
            raise RuntimeError("GitHub API 500")
        return mapping[workflow_file]

    monkeypatch.setattr(healthcheck, "fetch_workflow_dispatch_runs", _fetch)


def _spy_notifier(monkeypatch):
    sent = []
    monkeypatch.setattr(
        healthcheck,
        "send_telegram_message",
        lambda token, chat_id, text: sent.append(text),
    )
    return sent


def test_main_returns_0_when_every_workflow_is_healthy(monkeypatch, env):
    _patch_fetch(
        monkeypatch,
        {
            "alerts-us.yml": _runs("2026-10-07T15:25:00Z"),
            "alerts-tw.yml": _runs("2026-10-07T15:25:00Z"),
            "alerts-crypto.yml": _runs(CRYPTO_FRESH),
        },
    )
    sent = _spy_notifier(monkeypatch)
    assert main([], now=NOW) == 0
    assert sent == []


def test_main_alerts_and_names_only_the_dead_workflow(monkeypatch, env):
    _patch_fetch(
        monkeypatch,
        {
            "alerts-us.yml": _runs(US_LAST_RUN),
            "alerts-tw.yml": _runs(US_LAST_RUN),
            "alerts-crypto.yml": _runs(CRYPTO_FRESH),
        },
    )
    sent = _spy_notifier(monkeypatch)
    assert main([], now=NOW) == 1
    assert len(sent) == 1
    message = sent[0]
    assert "Alerts - US Stocks" in message          # 指名掛掉的 workflow
    assert "Alerts - TW Stocks" not in message      # 非活躍時段（15:30 UTC）不判定
    assert "Alerts - Crypto" not in message         # 正常的不點名
    assert "actions/workflows/alerts-us.yml" in message


def test_main_no_notify_skips_telegram(monkeypatch, env):
    monkeypatch.setenv("HEALTHCHECK_WORKFLOWS", "alerts-us.yml")
    _patch_fetch(monkeypatch, {"alerts-us.yml": _runs(US_LAST_RUN)})
    sent = _spy_notifier(monkeypatch)
    assert main(["--no-notify"], now=NOW) == 1
    assert sent == []


def test_main_returns_2_when_all_queries_fail(monkeypatch, env):
    _patch_fetch(
        monkeypatch,
        {},
        failures=("alerts-us.yml", "alerts-tw.yml", "alerts-crypto.yml"),
    )
    sent = _spy_notifier(monkeypatch)
    assert main([], now=NOW) == 2
    assert sent == []


def test_main_treats_query_failure_as_unknown_not_dead(monkeypatch, env):
    monkeypatch.setenv("HEALTHCHECK_WORKFLOWS", "alerts-us.yml,alerts-crypto.yml")
    _patch_fetch(
        monkeypatch,
        {"alerts-crypto.yml": _runs(CRYPTO_FRESH)},
        failures=("alerts-us.yml",),
    )
    sent = _spy_notifier(monkeypatch)
    assert main([], now=NOW) == 0
    assert sent == []


def test_main_returns_2_without_repository(monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    assert main([], now=NOW) == 2


def test_main_respects_silence_threshold_env(monkeypatch, env):
    monkeypatch.setenv("HEALTHCHECK_WORKFLOWS", "alerts-crypto.yml")
    monkeypatch.setenv("CRONJOB_MAX_SILENCE_MINUTES", "60")
    _patch_fetch(monkeypatch, {"alerts-crypto.yml": _runs("2026-10-07T14:00:00Z")})
    sent = _spy_notifier(monkeypatch)
    # 90 分鐘前觸發 > 60 分鐘門檻 → 告警
    assert main([], now=NOW) == 1
    assert len(sent) == 1
    assert "Alerts - Crypto" in sent[0]
