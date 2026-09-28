"""宏觀情境（四象限）判定測試。"""
import pytest

from src.macro import MacroSnapshot
from src.regime import (classify_regime, inflation_change_from_snapshot,
                        rate_change_from_snapshot, regime_from_snapshot)


def make_point(key, yoy, prev_yoy):
    from src.macro import MacroPoint
    return MacroPoint(key=key, label=key, yoy=yoy, prev_yoy=prev_yoy)


def test_four_quadrants():
    assert classify_regime(+0.4, +50.0).key == "stagflation"
    assert classify_regime(+0.4, -50.0).key == "reflation"
    assert classify_regime(-0.4, +50.0).key == "cooling"
    assert classify_regime(-0.4, -50.0).key == "recovery"


def test_flat_or_missing_inputs_fall_back_to_neutral():
    assert classify_regime(0.05, -50.0).key == "neutral"      # 通膨變化小於 0.1pp
    assert classify_regime(+0.4, 5.0).key == "neutral"        # 殖利率變化小於 10bp
    assert classify_regime(None, +50.0).key == "neutral"
    assert classify_regime(+0.4, None).key == "neutral"
    assert classify_regime(None, None).key == "neutral"


def test_regime_line_mentions_numbers_and_description():
    regime = classify_regime(+0.5, +60.0)
    line = regime.to_line()
    assert line.startswith("宏觀情境：停滯性通膨壓力")
    assert "CPI 年增較前月 +0.50pp" in line
    assert "10Y 殖利率近 20 交易日 +60bp" in line
    assert "通膨往上、利率也往上" in line


def test_inflation_change_prefers_cpi():
    snapshot = MacroSnapshot(monthly={"cpi": make_point("cpi", 3.4, 3.1),
                                      "pce": make_point("pce", 2.6, 2.8)})
    assert inflation_change_from_snapshot(snapshot) == pytest.approx(0.3)
    only_pce = MacroSnapshot(monthly={"pce": make_point("pce", 2.6, 2.8)})
    assert inflation_change_from_snapshot(only_pce) == pytest.approx(-0.2)
    assert inflation_change_from_snapshot(MacroSnapshot()) is None


def test_rate_change_prefers_fred_then_falls_back_to_tnx_history():
    fred = MacroSnapshot(rates={"dgs10_bp_20d": 42.0})
    assert rate_change_from_snapshot(fred) == pytest.approx(42.0)

    prices_only = MacroSnapshot(prices={"^TNX": 4.2},
                                history={"^TNX": {"closes": [4.0] * 20 + [4.2]}})
    assert rate_change_from_snapshot(prices_only) == pytest.approx(20.0)

    assert rate_change_from_snapshot(MacroSnapshot(prices={"^TNX": 4.2})) is None


def test_regime_from_snapshot_end_to_end():
    snapshot = MacroSnapshot(
        monthly={"cpi": make_point("cpi", 3.4, 3.1)},
        rates={"dgs10_bp_20d": 55.0},
    )
    regime = regime_from_snapshot(snapshot)
    assert regime.key == "stagflation"
    assert regime.inflation_change == pytest.approx(0.3)
    assert regime.rate_change_bp == pytest.approx(55.0)
    assert regime_from_snapshot(None).key == "neutral"
