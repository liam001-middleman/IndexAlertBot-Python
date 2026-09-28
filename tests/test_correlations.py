"""相關性與背離判定測試（純函式，不需要網路）。"""
import pytest

from src.correlations import (Correlation, Divergence, align, compute_correlations,
                              detect_divergences, format_correlation_lines, pearson,
                              rolling_correlation, window_change)


def make_history(closes, start_day=1):
    dates = [f"2026-09-{start_day + i:02d}" for i in range(len(closes))]
    return {"dates": dates, "closes": [float(c) for c in closes]}


def test_pearson_perfect_positive_and_negative():
    assert pearson([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)
    assert pearson([1, 2, 3, 4], [8, 6, 4, 2]) == pytest.approx(-1.0)
    assert pearson([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)


def test_pearson_returns_none_for_short_or_flat_series():
    assert pearson([1, 2], [1, 2]) is None           # 樣本不足
    assert pearson([1, 1, 1, 1], [1, 2, 3, 4]) is None  # 沒有變異數
    assert pearson([], []) is None


def test_align_uses_date_intersection():
    xs, ys = align(["d1", "d2", "d3"], [1, 2, 3], ["d3", "d2"], [30, 20])
    assert xs == [2.0, 3.0]     # 依 A 的順序，只留兩邊都有的日期
    assert ys == [20.0, 30.0]


def test_window_change():
    assert window_change([100, 101, 110], 2) == pytest.approx(10.0)
    assert window_change([100, 101], 2) is None
    assert window_change([0, 1, 2], 2) is None


def test_rolling_correlation_aligns_dates():
    asset = make_history([100 + i for i in range(25)])
    factor = {"dates": asset["dates"][2:], "closes": [200 + 2 * i for i in range(23)]}
    r = rolling_correlation(asset, factor, 20)
    assert r == pytest.approx(1.0, abs=1e-9)   # 完全線性 → r = 1


def test_compute_correlations_marks_significance():
    asset = make_history([100 + i for i in range(30)])
    factor = make_history([200 + 2 * i for i in range(30)])
    noise = make_history([10, 12, 9, 11, 13, 8, 12, 10, 11, 9] * 3)
    result = compute_correlations({"NVDA": asset}, {"^TNX": factor, "DX-Y.NYB": noise}, [20, 60])
    by_factor = {(c.factor, c.window): c for c in result}
    assert by_factor[("^TNX", 20)].r == pytest.approx(1.0, abs=1e-9)
    assert by_factor[("^TNX", 20)].significant is True
    assert by_factor[("^TNX", 60)].r is None            # 樣本不足
    assert by_factor[("^TNX", 60)].significant is False
    assert by_factor[("DX-Y.NYB", 20)].significant is False


def test_correlation_line_text():
    corr = Correlation(asset="NVDA", factor="DX-Y.NYB", window=20, r=-0.51, significant=True)
    assert corr.factor_label == "美元指數"
    assert "反向連動" in corr.to_line()
    weak = Correlation(asset="NVDA", factor="^VIX", window=20, r=0.05, significant=False)
    assert weak.direction == "無明顯連動"


def test_rolling_correlation_needs_full_window():
    asset = make_history([100 + i for i in range(25)])
    factor = make_history([200 + 2 * i for i in range(25)])
    assert rolling_correlation(asset, factor, 20) == pytest.approx(1.0, abs=1e-9)
    # 樣本不足 window 筆時不硬算（否則「60 日」實際上只有 25 筆樣本）
    assert rolling_correlation(asset, factor, 60) is None
    assert rolling_correlation({}, {}, 20) is None


def test_detect_divergences_finds_break_in_positive_habit():
    # 平時同向（r = +0.93）：指標一路走高，但標的最近 5 日開始下跌 → 背離
    factor = make_history([100 + i for i in range(20)])
    divergent = make_history([100 + i for i in range(15)] + [120, 118, 116, 114, 112])
    found = detect_divergences({"NVDA": divergent}, {"^TNX": factor}, [20], min_abs=0.3)
    assert len(found) == 1
    assert found[0].r > 0.9 and found[0].recent_days == 5
    assert found[0].asset_change < 0 < found[0].factor_change
    line = found[0].to_line()
    assert "NVDA 近 5 日下跌" in line and "10 年期殖利率走高" in line
    assert "同向連動" in line

    # 最近方向一致 → 沒有背離
    aligned = make_history([100 + i for i in range(20)])
    assert detect_divergences({"NVDA": aligned}, {"^TNX": factor}, [20], min_abs=0.3) == []


def test_detect_divergences_finds_break_in_negative_habit():
    # 平時反向（r = -0.70）：最近卻一起走低 → 背離
    factor = make_history([100 - i for i in range(20)])
    divergent = make_history([100 + i for i in range(15)] + [110, 109, 108, 107, 106])
    found = detect_divergences({"NVDA": divergent}, {"^TNX": factor}, [20], min_abs=0.3)
    assert len(found) == 1
    assert found[0].r < 0
    assert found[0].asset_change < 0 and found[0].factor_change < 0
    assert "反向連動" in found[0].to_line()

    # 平時反向且最近仍反向 → 正常，不算背離
    consistent = make_history([100 + i for i in range(20)])
    assert detect_divergences({"NVDA": consistent}, {"^TNX": factor}, [20], min_abs=0.3) == []


def test_detect_divergences_uses_custom_recent_days():
    factor = make_history([100 + i for i in range(20)])
    asset = make_history([100 + i for i in range(17)] + [130, 126, 120])
    assert detect_divergences({"NVDA": asset}, {"^TNX": factor}, [20], min_abs=0.3) == []
    found = detect_divergences({"NVDA": asset}, {"^TNX": factor}, [20], min_abs=0.3, recent_days=2)
    assert len(found) == 1 and found[0].recent_days == 2


def test_detect_divergences_respects_min_abs():
    factor = make_history([100 + i for i in range(20)])
    divergent = make_history([100 + i for i in range(15)] + [120, 118, 116, 114, 112])
    assert detect_divergences({"NVDA": divergent}, {"^TNX": factor}, [20], min_abs=1.01) == []


def test_format_correlation_lines_empty_when_nothing_significant():
    weak = [Correlation(asset="NVDA", factor="^VIX", window=20, r=0.1, significant=False)]
    assert format_correlation_lines(weak, []) == []
    strong = [Correlation(asset="NVDA", factor="^VIX", window=20, r=0.9, significant=True)]
    lines = format_correlation_lines(strong, [])
    assert len(lines) == 1 and "r=+0.90" in lines[0]

    div = Divergence(asset="NVDA", factor="DX-Y.NYB", window=20, r=-0.51,
                     asset_change=5.0, factor_change=1.2)
    lines = format_correlation_lines(strong, [div])
    assert len(lines) == 2 and lines[1].startswith("總體背離")
