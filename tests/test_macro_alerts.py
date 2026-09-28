"""總體警報規則與去重測試。"""
import pytest

from src.config import MacroConfig
from src.macro import MacroPoint, MacroSnapshot, load_snapshot, save_snapshot
from src.macro_alerts import (MACRO_STATE_SYMBOL, evaluate_macro_conditions,
                              format_macro_alert_lines, get_new_macro_alerts)
from src.state import StateStore


def make_cfg(**thresholds):
    cfg = MacroConfig()
    cfg.thresholds.update(thresholds)
    return cfg


def make_snapshot(**kwargs):
    defaults = dict(monthly={}, rates={}, prices={}, history={})
    defaults.update(kwargs)
    return MacroSnapshot(**defaults)


def find(conditions, rule_type):
    for cond in conditions:
        if cond["type"] == rule_type:
            return cond
    raise AssertionError(f"找不到規則 {rule_type}（實際：{[c['type'] for c in conditions]}）")


def test_evaluate_returns_empty_for_missing_snapshot():
    assert evaluate_macro_conditions(None, make_cfg()) == []


def test_cpi_and_core_cpi_rules():
    snapshot = make_snapshot(monthly={
        "cpi": MacroPoint(key="cpi", label="CPI", period="2026-08", yoy=3.4, prev_yoy=3.1,
                          mom=0.4, prev_mom=0.1),
        "core_cpi": MacroPoint(key="core_cpi", label="核心 CPI", yoy=2.9, prev_yoy=2.8),
    })
    conditions = evaluate_macro_conditions(snapshot, make_cfg())

    cpi = find(conditions, "cpi_yoy_high")
    assert cpi["triggered"] is True
    assert cpi["value"] == pytest.approx(3.4)
    assert "CPI 年增率 3.40%" in cpi["detail"] and "門檻 3.00%" in cpi["detail"]

    accel = find(conditions, "cpi_mom_acceleration")
    assert accel["triggered"] is True and accel["value"] == pytest.approx(0.3, abs=1e-9)
    assert accel["severity"] == "info"

    assert find(conditions, "core_cpi_yoy_high")["triggered"] is False


def test_thresholds_can_be_overridden():
    snapshot = make_snapshot(monthly={"cpi": MacroPoint(key="cpi", label="CPI", yoy=2.9)})
    conditions = evaluate_macro_conditions(snapshot, make_cfg(cpi_yoy_high=2.5))
    assert find(conditions, "cpi_yoy_high")["triggered"] is True


def test_labor_rules():
    snapshot = make_snapshot(monthly={
        "unemployment_rate": MacroPoint(key="unemployment_rate", label="失業率", period="2026-08",
                                        value=4.6, change_3m=0.4),
        "nfp": MacroPoint(key="nfp", label="非農就業", period="2026-08", value=100.0,
                          change=-25.0, prev_change=80.0),
        "avg_hourly_earnings": MacroPoint(key="avg_hourly_earnings", label="平均時薪",
                                          period="2026-08", yoy=4.3),
    })
    conditions = evaluate_macro_conditions(snapshot, make_cfg())
    jump = find(conditions, "unrate_jump")
    assert jump["triggered"] is True and jump["severity"] == "critical"
    assert "較 3 個月前 +0.40pp" in jump["detail"]

    nfp = find(conditions, "nfp_negative")
    assert nfp["triggered"] is True and nfp["severity"] == "warning"
    assert "非農就業月增 -25 千人" in nfp["detail"]

    assert find(conditions, "wage_yoy_high")["triggered"] is True


def test_rate_rules_include_surge_drop_and_fed_change():
    snapshot = make_snapshot(rates={
        "dgs10": 4.55, "dgs10_bp_20d": 55.0,
        "fed_funds_upper": 4.0, "fed_funds_lower": 3.75,
        "fed_funds_prev": 4.25, "fed_funds_changed_at": "2026-09-17",
    })
    conditions = evaluate_macro_conditions(snapshot, make_cfg())
    surge = find(conditions, "us10y_surge")
    assert surge["triggered"] is True and surge["severity"] == "critical"
    assert "近 20 交易日 +55bp" in surge["detail"]
    assert find(conditions, "us10y_drop")["triggered"] is False

    fed = find(conditions, "fed_rate_change")
    assert fed["triggered"] is True
    assert "降息 1 碼" in fed["detail"] and "2026-09-17" in fed["detail"]

    # 沒有變化時不算觸發
    flat = make_snapshot(rates={"fed_funds_upper": 4.0, "fed_funds_prev": 4.0})
    assert find(evaluate_macro_conditions(flat, make_cfg()), "fed_rate_change")["triggered"] is False

    # 殖利率急降
    falling = make_snapshot(rates={"dgs10": 3.5, "dgs10_bp_20d": -55.0})
    conditions = evaluate_macro_conditions(falling, make_cfg())
    assert find(conditions, "us10y_drop")["triggered"] is True
    assert find(conditions, "us10y_surge")["triggered"] is False


def test_fed_rate_change_detail_keeps_change_date_after_snapshot_reload(tmp_path):
    """回歸：快照存檔→讀檔後，警報仍要帶出調整日（曾被數值過濾吃掉→顯示「未標示」）。"""
    path = tmp_path / "macro_snapshot.json"
    save_snapshot(make_snapshot(rates={
        "fed_funds_lower": 3.75, "fed_funds_upper": 4.0, "fed_funds_prev": 3.75,
        "fed_funds_changed_at": "2026-09-17",
    }), path)
    fed = find(evaluate_macro_conditions(load_snapshot(path), make_cfg()), "fed_rate_change")
    assert fed["triggered"] is True
    assert "調整日 2026-09-17" in fed["detail"]


def test_alert_detail_uses_normalized_period_for_legacy_snapshot():
    """舊快照的期別是 FRED 的 YYYY-MM-01，讀檔後警報文字應顯示 YYYY-MM。"""
    snapshot = MacroSnapshot.from_dict({"monthly": {"pce": {
        "key": "pce", "label": "PCE 物價", "unit": "%", "period": "2026-07-01",
        "value": 131.6, "yoy": 3.9,
    }}})
    pce = find(evaluate_macro_conditions(snapshot, make_cfg()), "pce_yoy_high")
    assert pce["triggered"] is True
    assert "資料期別 2026-07" in pce["detail"]
    assert "2026-07-01" not in pce["detail"]


def test_market_rules_use_saved_history():
    snapshot = make_snapshot(
        prices={"DX-Y.NYB": 104.0, "CL=F": 82.0},
        history={
            "DX-Y.NYB": {"dates": ["d"] * 21, "closes": [100.0] * 20 + [104.0]},
            "CL=F": {"dates": ["d"] * 21, "closes": [70.0] * 20 + [82.0]},
        },
    )
    conditions = evaluate_macro_conditions(snapshot, make_cfg())
    dxy = find(conditions, "dxy_surge")
    assert dxy["triggered"] is True and dxy["value"] == pytest.approx(4.0)
    wti = find(conditions, "wti_surge")
    assert wti["triggered"] is True and wti["value"] == pytest.approx(17.14, abs=0.01)

    # 歷史資料不足時整條規則不出現（不猜）
    short = make_snapshot(prices={"DX-Y.NYB": 104.0}, history={"DX-Y.NYB": {"closes": [104.0]}})
    assert "dxy_surge" not in [c["type"] for c in evaluate_macro_conditions(short, make_cfg())]


def test_inflation_expectation_rule():
    snapshot = make_snapshot(rates={"inflation_expect_10y": 2.5,
                                    "inflation_expect_10y_bp_20d": 25.0})
    conditions = evaluate_macro_conditions(snapshot, make_cfg())
    expect = find(conditions, "inflation_expect_up")
    assert expect["triggered"] is True and expect["severity"] == "info"
    assert "近 20 交易日 +25bp" in expect["detail"]


def test_get_new_macro_alerts_dedups_and_rearms(tmp_path):
    state = StateStore(tmp_path / "alert_state.json")
    cfg = make_cfg()
    hot = make_snapshot(monthly={"cpi": MacroPoint(key="cpi", label="CPI", yoy=3.6)})
    calm = make_snapshot(monthly={"cpi": MacroPoint(key="cpi", label="CPI", yoy=2.1)})

    first = get_new_macro_alerts(hot, cfg, state)
    assert [a.key for a in first] == ["cpi_yoy_high"]
    assert first[0].alert_name == "CPI 年增偏高"
    assert state.is_active(MACRO_STATE_SYMBOL, "cpi_yoy_high") is True

    # 同一組條件再次執行不重複通知
    assert get_new_macro_alerts(hot, cfg, state) == []

    # 恢復正常 → 狀態清除，下次再觸發會重新通知
    assert get_new_macro_alerts(calm, cfg, state) == []
    assert state.is_active(MACRO_STATE_SYMBOL, "cpi_yoy_high") is False
    assert [a.key for a in get_new_macro_alerts(hot, cfg, state)] == ["cpi_yoy_high"]


def test_new_alerts_sorted_by_severity_and_formatted():
    snapshot = make_snapshot(
        monthly={"cpi": MacroPoint(key="cpi", label="CPI", yoy=3.6, mom=0.5, prev_mom=0.1)},
        rates={"dgs10": 4.6, "dgs10_bp_20d": 60.0},
    )
    alerts = get_new_macro_alerts(snapshot, make_cfg(), StateStore(None))
    severities = [a.severity for a in alerts]
    assert severities[0] == "critical"
    order = {"info": 0, "warning": 1, "critical": 2}
    assert severities == sorted(severities, key=lambda s: -order[s])
    lines = format_macro_alert_lines(alerts)
    assert lines[0].startswith("[critical] 10Y 殖利率急升")
    assert format_macro_alert_lines([]) == []
