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
