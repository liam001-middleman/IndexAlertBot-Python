"""技術指標計算的單元測試。"""
import pandas as pd
import pytest

from src.indicators import compute_ma, compute_ma_deviation, compute_rsi


def test_rsi_increasing_series_is_100():
    close = pd.Series([float(i) for i in range(1, 40)])
    assert compute_rsi(close, 14) == pytest.approx(100.0)


def test_rsi_decreasing_series_is_0():
    close = pd.Series([float(i) for i in range(40, 1, -1)])
    assert compute_rsi(close, 14) == pytest.approx(0.0)


def test_rsi_flat_series_is_none():
    close = pd.Series([100.0] * 30)
    assert compute_rsi(close, 14) is None


def test_rsi_known_wilder_example():
    # StockCharts 經典範例，RSI(14) ≈ 70.46
    prices = [
        44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10,
        45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
    ]
    close = pd.Series(prices)
    assert compute_rsi(close, 14) == pytest.approx(70.46, abs=0.1)


def test_rsi_insufficient_data_returns_none():
    close = pd.Series([1.0, 2.0, 3.0])
    assert compute_rsi(close, 14) is None


def test_ma():
    close = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    result = compute_ma(close, [3, 5])
    assert result["3"] == pytest.approx(4.0)
    assert result["5"] == pytest.approx(3.0)
    assert compute_ma(close, [10])["10"] is None


def test_ma_deviation():
    result = compute_ma_deviation(110.0, {"20": 100.0, "60": 120.0})
    assert result["20"] == pytest.approx(10.0)
    assert result["60"] == pytest.approx(-8.333333, abs=1e-3)
    assert result.get("200") is None  # 未提供的週期不會出現在結果中
