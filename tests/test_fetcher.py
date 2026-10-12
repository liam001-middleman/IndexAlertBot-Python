"""fetcher 的 MAX K 線解析測試（無網路，使用固定 fixture）。"""
import pandas as pd
import pytest

import src.fetcher as fetcher
from src.fetcher import (FetchError, _to_market_data, clear_usd_twd_cache,
                         get_usd_twd_rate, parse_max_klines)


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
    assert md.usd_twd is None  # 非換匯路徑不帶匯率


def fake_history(close):
    """依收盤價序列造出 Yahoo 風格的 DataFrame。"""
    return pd.DataFrame(
        {"Open": close, "Close": close},
        index=pd.to_datetime(["2026-01-01", "2026-01-02"][:len(close)]),
    )


def test_get_market_data_convert_to_twd_reports_fx_rate(monkeypatch):
    """convert_to_twd 路徑要把使用的 USD/TWD 匯率帶回（報告雙幣別顯示用）。"""
    clear_usd_twd_cache()
    usd_df = fake_history([100.0, 110.0])
    fx_df = fake_history([31.0, 31.5])

    def fake_fetch(symbol, period, interval):
        return fx_df if symbol == fetcher.USD_TWD_SYMBOL else usd_df

    monkeypatch.setattr(fetcher, "_fetch_yahoo_df", fake_fetch)
    md = fetcher.get_market_data("btctwd", provider="yahoo", source_symbol="BTC-USD",
                                 convert_to_twd=True)
    assert md.usd_twd == pytest.approx(31.5)
    assert md.price == pytest.approx(110.0 * 31.5)
    # 換匯時順手填快取 → 同一回合的美股標的不必重抓匯率
    assert get_usd_twd_rate() == pytest.approx(31.5)
    clear_usd_twd_cache()


def test_get_usd_twd_rate_caches_single_fetch(monkeypatch):
    """同一次執行只抓一次匯率。"""
    clear_usd_twd_cache()
    calls = []

    def fake_fetch(symbol, period, interval):
        calls.append(symbol)
        return fake_history([32.0])

    monkeypatch.setattr(fetcher, "_fetch_yahoo_df", fake_fetch)
    assert get_usd_twd_rate() == pytest.approx(32.0)
    assert get_usd_twd_rate() == pytest.approx(32.0)
    assert calls == [fetcher.USD_TWD_SYMBOL]
    clear_usd_twd_cache()


def test_get_usd_twd_rate_failure_returns_none(monkeypatch):
    """抓不到匯率時回 None（報告端再退回單一幣別），且不寫入快取。"""
    clear_usd_twd_cache()

    def boom(symbol, period, interval):
        raise FetchError("no network")

    monkeypatch.setattr(fetcher, "_fetch_yahoo_df", boom)
    assert get_usd_twd_rate() is None
    assert get_usd_twd_rate() is None
    clear_usd_twd_cache()


def test_get_usd_twd_rate_rejects_bad_value(monkeypatch):
    """匯率為 0 或非數字時視為抓取失敗。"""
    clear_usd_twd_cache()
    monkeypatch.setattr(fetcher, "_fetch_yahoo_df", lambda s, p, i: fake_history([0.0]))
    assert get_usd_twd_rate() is None
    clear_usd_twd_cache()
