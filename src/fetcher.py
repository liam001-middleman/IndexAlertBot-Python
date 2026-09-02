"""行情資料抓取。

支援兩種資料源：
- yahoo：透過 yfinance 抓取 Yahoo Finance（美股 AAPL、台股 2330.TW、加密貨幣 BTC-USD）
- max  ：透過 MAX 台灣交易所公開 API（加密貨幣台幣報價，如 btctwd / ethtwd）
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests
import yfinance as yf

logger = logging.getLogger(__name__)

MAX_API_BASE = "https://max-api.maicoin.com/api/v2"
MAX_KLINE_LIMIT = 1000  # 日線根數（約 2.7 年，足以計算 MA200 / RSI）
FETCH_TIMEOUT = 30


class FetchError(Exception):
    """抓取失敗。"""


@dataclass
class MarketData:
    df: pd.DataFrame  # 日線歷史（含 Open / Close 等欄位）
    price: float  # 最新價
    previous_close: float  # 前一個交易日收盤價
    open_price: Optional[float]  # 最新一根 K 棒的開盤價
    change_pct: float  # 對比前收的漲跌幅（%）
    timestamp: datetime  # 最新一根 K 棒的時間


# ---------- 共用 ----------

def _to_market_data(df: pd.DataFrame, symbol: str) -> MarketData:
    """把含 Open/Close 欄位的日線 DataFrame 整理成 MarketData。"""
    if df is None or df.empty:
        raise FetchError(f"{symbol} 沒有回傳任何資料")
    price = float(df["Close"].iloc[-1])
    previous_close = float(df["Close"].iloc[-2]) if len(df) >= 2 else price
    open_price = float(df["Open"].iloc[-1])
    change_pct = (price - previous_close) / previous_close * 100.0 if previous_close else 0.0
    timestamp = df.index[-1].to_pydatetime()
    return MarketData(
        df=df,
        price=price,
        previous_close=previous_close,
        open_price=open_price,
        change_pct=change_pct,
        timestamp=timestamp,
    )


# ---------- yahoo ----------

def _fetch_yahoo_df(symbol: str, period: str, interval: str) -> pd.DataFrame:
    """抓 Yahoo 日線並清理（去重、去除缺 Close/Open 的列）。"""
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=interval, auto_adjust=True)
    except Exception as exc:
        raise FetchError(f"yfinance 抓取 {symbol} 失敗: {exc}") from exc
    if df is None or df.empty:
        raise FetchError(f"{symbol} 沒有回傳任何資料")
    df = df[~df.index.duplicated(keep="last")].copy()
    df = df.dropna(subset=["Close", "Open"])
    if df.empty:
        raise FetchError(f"{symbol} 缺少 Close/Open 欄位")
    return df


def _fetch_yahoo_market_data(symbol: str, period: str, interval: str) -> MarketData:
    return _to_market_data(_fetch_yahoo_df(symbol, period, interval), symbol)


# ---------- max（台灣交易所） ----------

def parse_max_klines(payload) -> pd.DataFrame:
    """把 MAX kline API 回傳的原始資料轉成日線 DataFrame。

    MAX 回傳格式：[時間戳(秒), open, high, low, close, volume]，一列一筆。
    純函式、不依賴網路，可直接用 fixture 測試。
    """
    if not isinstance(payload, list) or not payload:
        raise FetchError("MAX 沒有回傳任何 K 線資料")
    rows = []
    for item in payload:
        if not isinstance(item, (list, tuple)) or len(item) < 6:
            continue
        ts = int(item[0])
        rows.append(
            [
                datetime.fromtimestamp(ts, tz=timezone.utc),
                float(item[1]),
                float(item[2]),
                float(item[3]),
                float(item[4]),
                float(item[5]),
            ]
        )
    if not rows:
        raise FetchError("MAX K 線資料格式無法解析")
    df = pd.DataFrame(rows, columns=["Datetime", "Open", "High", "Low", "Close", "Volume"])
    df = df.set_index("Datetime")
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df = df.dropna(subset=["Close", "Open"])
    if df.empty:
        raise FetchError("MAX K 線資料缺少 Close/Open 欄位")
    return df


def _fetch_max_market_data(symbol: str, limit: int = MAX_KLINE_LIMIT) -> MarketData:
    url = f"{MAX_API_BASE}/k"
    params = {"market": symbol, "interval": "1d", "limit": limit}
    try:
        resp = requests.get(url, params=params, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        raise FetchError(f"MAX 抓取 {symbol} 失敗: {exc}") from exc
    return _to_market_data(parse_max_klines(payload), symbol)


# ---------- USD→TWD 換匯（Yahoo 參考匯率） ----------

USD_TWD_SYMBOL = "TWD=X"  # Yahoo 的 USD/TWD 即期匯率代號


def _fetch_usd_twd_converted_df(usd_symbol: str, period: str, interval: str) -> pd.DataFrame:
    """抓 USD 計價標的的日線，乘 USD/TWD 匯率換算成台幣。
    匯率週末無報價，以最後一個有資料的工作日匯率往前填補（ffill）。
    """
    usd_df = _fetch_yahoo_df(usd_symbol, period, interval)
    fx_df = _fetch_yahoo_df(USD_TWD_SYMBOL, period, interval)
    fx = fx_df["Close"].reindex(usd_df.index).ffill().bfill()
    if fx.isna().any():
        raise FetchError(f"USD/TWD 匯率資料不足，無法換算 {usd_symbol}")
    df = usd_df.copy()
    df["Open"] = usd_df["Open"] * fx
    df["Close"] = usd_df["Close"] * fx
    return df


# ---------- 入口 ----------

def get_market_data(symbol: str, period: str = "2y", interval: str = "1d",
                    provider: str = "yahoo",
                    source_symbol: Optional[str] = None,
                    convert_to_twd: bool = False) -> MarketData:
    """依 provider 抓取單一標的的日線歷史，並整理出最新價、前收、日內漲跌幅。
    - convert_to_twd=True：抓 source_symbol（USD 計價）的日線，
      再乘 USD/TWD（TWD=X）匯率換算成台幣，供台幣報價使用。
    - provider == "max"：保留 MAX 交易所路徑（其 kline API 目前只回傳分鐘線）。
    """
    if convert_to_twd:
        df = _fetch_usd_twd_converted_df(source_symbol or symbol, period, interval)
        return _to_market_data(df, symbol)
    if provider == "max":
        return _fetch_max_market_data(symbol)
    return _fetch_yahoo_market_data(symbol, period, interval)
