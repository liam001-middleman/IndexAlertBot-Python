"""fetcher 的 MAX K 線解析測試（無網路，使用固定 fixture）。"""
import pandas as pd
import pytest

from src.fetcher import FetchError, _to_market_data, parse_max_klines


def sample_payload():
    return [
        [1700000000, 100.0, 110.0, 90.0, 105.0, 100.0],
        [1700000000, 100.0, 110.0, 90.0, 999.0, 100.0],  # 重複時間戳，應被去重
        [1700086400, 105.0, 115.0, 100.0, 110.0, 120.0],
        [1700172800, 110.0, 120.0, 105.0, 115.0, 130.0],
    ]


def test_parse_max_klines_columns_and_order():
    df = parse_max_klines(sample_payload())
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert len(df) == 3  # 重複時間戳被去重
    assert df.index.is_monotonic_increasing
    assert df["Close"].iloc[-1] == 115.0


def test_parse_max_klines_handles_descending_input():
    payload = list(reversed(sample_payload()))
    df = parse_max_klines(payload)
    assert df.index.is_monotonic_increasing
    assert df["Close"].iloc[-1] == 115.0


def test_parse_max_klines_empty_raises():
    with pytest.raises(FetchError):
        parse_max_klines([])


def test_parse_max_klines_bad_format_raises():
    with pytest.raises(FetchError):
        parse_max_klines([["bad", "data"]])


def test_to_market_data():
    df = pd.DataFrame(
        {"Open": [1.0, 2.0], "Close": [1.5, 2.5]},
        index=pd.to_datetime(["2026-01-01", "2026-01-02"]),
    )
    md = _to_market_data(df, "TEST")
    assert md.price == 2.5
    assert md.previous_close == 1.5
    assert md.open_price == 2.0
    assert md.change_pct == pytest.approx(66.6667, abs=0.1)
    assert md.timestamp.year == 2026
