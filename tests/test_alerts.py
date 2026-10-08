"""警報引擎「新觸發 / 重複抑制 / 解除再觸發」邏輯測試。"""
from datetime import datetime, timezone

import pytest

from src.alerts import get_new_alerts
from src.config import AlertConfig
from src.models import Quote
from src.state import StateStore


def make_quote(symbol="AAPL", name="蘋果", market="us", price=100.0,
               previous_close=95.0, change_pct=5.0, rsi=75.0, ma_dev=None):
    return Quote(
        symbol=symbol,
        name=name,
        market=market,
        price=price,
        previous_close=previous_close,
        open_price=99.0,
        change_pct=change_pct,
        rsi=rsi,
        ma={"20": 95.0, "60": 90.0, "200": 80.0},
        ma_deviation_pct=ma_dev or {"20": 5.26, "60": 11.11, "200": 25.0},
        timestamp=datetime.now(timezone.utc),
    )


def make_cfg():
    # 高門檻，讓測試只專注在 RSI 條件上
    return AlertConfig(
        rsi_period=14,
        rsi_overbought=70.0,
        rsi_oversold=30.0,
        intraday_change_pct=100.0,
        ma_deviation_pct=100.0,
    )


def test_new_trigger_only_once(tmp_path):
    state = StateStore(tmp_path / "alert_state.json")
    state.load()
    cfg = make_cfg()

    alerts1 = get_new_alerts([make_quote()], lambda m: cfg, state)
    assert "rsi_overbought" in {a.alert_type for a in alerts1}
    assert state.is_active("AAPL", "rsi_overbought")

    # 相同條件再次執行 → 重複觸發，不通知
    alerts2 = get_new_alerts([make_quote()], lambda m: cfg, state)
    assert alerts2 == []


def test_clear_then_retrigger(tmp_path):
    state = StateStore(tmp_path / "alert_state.json")
    state.load()
    cfg = make_cfg()

    assert len(get_new_alerts([make_quote(rsi=75)], lambda m: cfg, state)) == 1

    # RSI 回到中性區 → 解除
    assert get_new_alerts([make_quote(rsi=50)], lambda m: cfg, state) == []
    assert not state.is_active("AAPL", "rsi_overbought")

    # 再次超買 → 重新觸發
    alerts = get_new_alerts([make_quote(rsi=80)], lambda m: cfg, state)
    assert len(alerts) == 1
    assert alerts[0].alert_type == "rsi_overbought"


def test_oversold_for_multiple_markets(tmp_path):
    state = StateStore(tmp_path / "alert_state.json")
    state.load()
    cfg = make_cfg()

    q1 = make_quote(symbol="2330.TW", name="台積電", market="tw", rsi=25.0)
    q2 = make_quote(symbol="BTC-USD", name="比特幣", market="crypto", rsi=25.0)
    alerts = get_new_alerts([q1, q2], lambda m: cfg, state)
    assert len(alerts) == 2
    for a in alerts:
        assert a.alert_type == "rsi_oversold"


def test_intraday_drop_trigger(tmp_path):
    state = StateStore(tmp_path / "alert_state.json")
    state.load()
    cfg = AlertConfig(
        rsi_overbought=100, rsi_oversold=0,
        intraday_change_pct=5.0, ma_deviation_pct=100.0,
    )
    quote = make_quote(price=90.0, previous_close=100.0, change_pct=-10.0, rsi=40.0)
    alerts = get_new_alerts([quote], lambda m: cfg, state)
    types = {a.alert_type for a in alerts}
    assert "intraday_drop" in types


def test_alert_carries_ma_and_last_report_price(tmp_path):
    """Alert 應帶入均線資料與「上次出報告」的價格快照，供報告端引用。"""
    state = StateStore(tmp_path / "alert_state.json")
    state.load()
    state.set_last_report("AAPL", 90.0, datetime.now(timezone.utc))
    cfg = make_cfg()

    alerts = get_new_alerts([make_quote()], lambda m: cfg, state)
    assert len(alerts) == 1
    a = alerts[0]
    assert a.ma["20"] == 95.0
    assert a.rsi == 75.0  # 帶入 Quote 的 RSI
    assert a.ma_deviation_pct["20"] == 5.26
    assert a.last_report_price == 90.0
    assert a.last_report_at is not None


# ---- 「站上／跌破均線」（ma_cross_up_* / ma_cross_down_*）----


def make_cross_cfg(**kwargs):
    """只開啟「站上／跌破均線」，其餘門檻調到不可能觸發。"""
    base = dict(
        rsi_overbought=100.0,
        rsi_oversold=0.0,
        intraday_change_pct=100.0,
        ma_deviation_pct=100.0,
        ma_cross_alerts=True,
        ma_cross_threshold=0.0,
    )
    base.update(kwargs)
    return AlertConfig(**base)


def only_ma_dev(dev_20, dev_60=None, dev_200=None):
    """只給定要測的均線乖離率，其餘留 None（資料不足，不產生條件）。"""
    return {"20": dev_20, "60": dev_60, "200": dev_200}


def test_ma_cross_up_triggers_when_price_above_ma(tmp_path):
    state = StateStore(tmp_path / "alert_state.json")
    state.load()
    cfg = make_cross_cfg()

    alerts = get_new_alerts([make_quote(ma_dev=only_ma_dev(2.0))], lambda m: cfg, state)
    assert {a.alert_type for a in alerts} == {"ma_cross_up_20"}
    assert alerts[0].alert_name == "MA20 站上"
    assert alerts[0].severity == "info"
    assert "站上 MA20" in alerts[0].message
    assert "乖離率" in alerts[0].detail
    assert state.is_active("AAPL", "ma_cross_up_20")


def test_ma_cross_down_triggers_when_price_below_ma(tmp_path):
    state = StateStore(tmp_path / "alert_state.json")
    state.load()
    cfg = make_cross_cfg()

    alerts = get_new_alerts([make_quote(ma_dev=only_ma_dev(-2.0))], lambda m: cfg, state)
    assert {a.alert_type for a in alerts} == {"ma_cross_down_20"}
    assert alerts[0].alert_name == "MA20 跌破"
    assert state.is_active("AAPL", "ma_cross_down_20")


def test_ma_cross_repeat_suppressed(tmp_path):
    """價格一直在均線上方時只通知一次（不是每回合都發）。"""
    state = StateStore(tmp_path / "alert_state.json")
    state.load()
    cfg = make_cross_cfg()

    first = get_new_alerts([make_quote(ma_dev=only_ma_dev(2.0))], lambda m: cfg, state)
    assert {a.alert_type for a in first} == {"ma_cross_up_20"}
    second = get_new_alerts([make_quote(ma_dev=only_ma_dev(2.0))], lambda m: cfg, state)
    assert second == []


def test_ma_cross_round_trip_refires_on_each_cross(tmp_path):
    """穿越才通知：上去→站上、下來→跌破、再上去→再站上。"""
    state = StateStore(tmp_path / "alert_state.json")
    state.load()
    cfg = make_cross_cfg()

    for dev, expected in ((2.0, "ma_cross_up_20"),
                          (-2.0, "ma_cross_down_20"),
                          (2.0, "ma_cross_up_20")):
        alerts = get_new_alerts([make_quote(ma_dev=only_ma_dev(dev))], lambda m: cfg, state)
        assert {a.alert_type for a in alerts} == {expected}, f"dev={dev}"


def test_ma_cross_disabled_by_default(tmp_path):
    """ma_cross_alerts 預設 False：條件完全不產生，狀態檔也不會多出 ma_cross_* key。"""
    state = StateStore(tmp_path / "alert_state.json")
    state.load()
    cfg = make_cross_cfg(ma_cross_alerts=False)

    alerts = get_new_alerts([make_quote(ma_dev=only_ma_dev(2.0))], lambda m: cfg, state)
    assert alerts == []
    assert not state.is_active("AAPL", "ma_cross_up_20")
    assert not state.is_active("AAPL", "ma_cross_down_20")


def test_ma_cross_exact_zero_triggers_neither(tmp_path):
    """乖離率恰為 0 時，同一條均線不得同時通報「站上」與「跌破」。"""
    state = StateStore(tmp_path / "alert_state.json")
    state.load()
    cfg = make_cross_cfg()

    alerts = get_new_alerts([make_quote(ma_dev=only_ma_dev(0.0))], lambda m: cfg, state)
    assert alerts == []
    assert not state.is_active("AAPL", "ma_cross_up_20")
    assert not state.is_active("AAPL", "ma_cross_down_20")


def test_ma_cross_dead_band_requires_exceeding_threshold(tmp_path):
    """死區：乖離率沒超過 ma_cross_threshold 就不觸發。"""
    state = StateStore(tmp_path / "alert_state.json")
    state.load()
    cfg = make_cross_cfg(ma_cross_threshold=0.5)

    assert get_new_alerts([make_quote(ma_dev=only_ma_dev(0.3))], lambda m: cfg, state) == []
    assert get_new_alerts([make_quote(ma_dev=only_ma_dev(-0.4))], lambda m: cfg, state) == []

    alerts = get_new_alerts([make_quote(ma_dev=only_ma_dev(0.6))], lambda m: cfg, state)
    assert {a.alert_type for a in alerts} == {"ma_cross_up_20"}
    assert alerts[0].threshold == 0.5
    assert "死區" in alerts[0].detail


def test_ma_cross_dead_band_rearms_after_returning_inside(tmp_path):
    """死區只是緩衝帶、不是真遲滯：回到死區內即解除，再突破會再通知。"""
    state = StateStore(tmp_path / "alert_state.json")
    state.load()
    cfg = make_cross_cfg(ma_cross_threshold=0.5)

    first = get_new_alerts([make_quote(ma_dev=only_ma_dev(0.6))], lambda m: cfg, state)
    assert {a.alert_type for a in first} == {"ma_cross_up_20"}

    assert get_new_alerts([make_quote(ma_dev=only_ma_dev(0.3))], lambda m: cfg, state) == []
    assert not state.is_active("AAPL", "ma_cross_up_20")

    third = get_new_alerts([make_quote(ma_dev=only_ma_dev(0.6))], lambda m: cfg, state)
    assert {a.alert_type for a in third} == {"ma_cross_up_20"}


def test_ma_cross_skips_missing_deviation(tmp_path):
    """均線資料不足（乖離率 None）時不產生條件，也不得拋錯。"""
    state = StateStore(tmp_path / "alert_state.json")
    state.load()
    cfg = make_cross_cfg()

    alerts = get_new_alerts([make_quote(ma_dev=only_ma_dev(None))], lambda m: cfg, state)
    assert alerts == []


def test_ma_cross_coexists_with_deviation_alert(tmp_path):
    """「位置」（站上／跌破）與「幅度」（正／負乖離）是兩個獨立信號，可同時出現。"""
    state = StateStore(tmp_path / "alert_state.json")
    state.load()
    cfg = make_cross_cfg(ma_deviation_pct=5.0)

    alerts = get_new_alerts([make_quote(ma_dev=only_ma_dev(6.0))], lambda m: cfg, state)
    assert {a.alert_type for a in alerts} == {"ma_cross_up_20", "ma_deviation_pos_20"}
