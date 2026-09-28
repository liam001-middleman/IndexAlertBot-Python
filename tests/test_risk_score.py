"""風險分數（技術面 + 總體面）測試。"""
import pytest

from src.macro import MacroSnapshot, MacroPoint
from src.risk_score import (build_risk_score, combine_scores, compute_risk_score,
                            format_risk_line, grade_of, macro_component_dollar,
                            macro_component_inflation, macro_component_rates,
                            macro_component_volatility, macro_score, technical_score)


class FakeQuote:
    """只帶風險分數需要的欄位（模擬 models.Quote）。"""

    def __init__(self, rsi=70.0, ma=None, ma_deviation_pct=None):
        self.rsi = rsi
        self.ma = ma if ma is not None else {"20": 100.0, "60": 95.0, "200": 90.0}
        self.ma_deviation_pct = ma_deviation_pct if ma_deviation_pct is not None \
            else {"20": 5.0, "60": 10.0, "200": 20.0}


def make_snapshot(**kwargs):
    defaults = dict(
        monthly={"cpi": MacroPoint(key="cpi", label="CPI", yoy=3.4)},
        rates={"dgs10": 5.0, "dgs10_bp_20d": 50.0},
        prices={"^VIX": 18.0},
        history={"DX-Y.NYB": {"dates": ["d"] * 21, "closes": [100.0] * 20 + [102.0]}},
    )
    defaults.update(kwargs)
    return MacroSnapshot(**defaults)


def test_technical_score_weights_and_clamping():
    assert technical_score(70.0, {"20": 5.0, "60": 10.0, "200": 20.0}) == pytest.approx(88.0)
    assert technical_score(30.0, {"20": -5.0, "60": -5.0}) == pytest.approx(25.13, abs=0.01)
    assert technical_score(70.0, None) == pytest.approx(70.0)   # 只有 RSI 時等同 RSI
    assert technical_score(None, {}, {}) is None
    assert technical_score(None, {"20": 0.0}) == pytest.approx(
        (50.0 * 0.35 + 0.0 * 0.25) / 0.6)


def test_macro_components():
    snapshot = make_snapshot()
    assert macro_component_rates(snapshot) == pytest.approx(82.0)
    assert macro_component_dollar(snapshot) == pytest.approx(60.0)
    assert macro_component_inflation(snapshot) == pytest.approx(35.0)
    assert macro_component_volatility(snapshot) == pytest.approx(18.0)
    empty = MacroSnapshot()
    assert macro_component_rates(empty) is None
    assert macro_component_dollar(empty) is None
    assert macro_component_inflation(empty) is None
    assert macro_component_volatility(empty) is None


def test_macro_component_rates_falls_back_to_tnx_history():
    snapshot = MacroSnapshot(prices={"^TNX": 4.5},
                             history={"^TNX": {"closes": [4.2] * 20 + [4.5]}})
    # 水位 4.5%（+6 分）＋ 30bp 變化（+12 分）= 68
    assert macro_component_rates(snapshot) == pytest.approx(68.0)


def test_macro_score_renormalises_missing_components():
    total, components = macro_score(make_snapshot(prices={}, history={}))
    assert components["dollar"] is None and components["volatility"] is None
    # 只剩利率（0.35）與通膨（0.25）
    assert total == pytest.approx((82.0 * 0.35 + 35.0 * 0.25) / 0.6)
    assert macro_score(MacroSnapshot()) == (None, {"rates": None, "dollar": None,
                                                   "inflation": None, "volatility": None})


def test_combine_scores_renormalises_and_flags_partial():
    total, partial = combine_scores(80.0, 40.0, {"technical": 0.6, "macro": 0.4})
    assert total == pytest.approx(64.0)
    assert partial is False

    total, partial = combine_scores(None, 60.0, {"technical": 0.6, "macro": 0.4})
    assert total == pytest.approx(60.0)
    assert partial is True

    total, partial = combine_scores(50.0, 50.0, {"technical": 0.0, "macro": 0.0})
    assert total == pytest.approx(50.0)   # 權重設定無效時退回等權

    assert combine_scores(None, None) == (None, False)


def test_grade_thresholds():
    assert grade_of(34.9) == "低"
    assert grade_of(35.0) == "中"
    assert grade_of(60.0) == "中"
    assert grade_of(60.1) == "高"
    assert grade_of(None) == "無法計算"


def test_build_and_format_risk_line():
    risk = build_risk_score(88.0, 55.0, {"technical": 0.6, "macro": 0.4})
    assert risk.total == pytest.approx(74.8)
    assert risk.grade == "高"
    line = risk.to_line("NVDA")
    assert line.startswith("NVDA 風險分數 74.8／100（高）")
    assert "技術面 88.0" in line and "總體面 55.0" in line
    assert "僅部分面向可計算" not in line

    partial = build_risk_score(88.0, None)
    assert "僅部分面向可計算" in partial.to_line()

    assert "資料不足" in format_risk_line("NVDA", None)
    assert "資料不足" in build_risk_score(None, None).to_line("NVDA")


def test_compute_risk_score_from_quote_and_snapshot():
    risk = compute_risk_score(FakeQuote(), make_snapshot(), {"technical": 0.6, "macro": 0.4})
    assert risk.technical == pytest.approx(88.0)
    assert risk.macro == pytest.approx(55.15, abs=0.01)
    assert risk.total is not None and risk.grade in {"低", "中", "高"}
    assert set(risk.components) == {"rates", "dollar", "inflation", "volatility"}

    only_tech = compute_risk_score(FakeQuote(), None)
    assert only_tech.macro is None and only_tech.partial is True
    assert compute_risk_score(FakeQuote(rsi=None, ma={}, ma_deviation_pct={}), None).total is None
