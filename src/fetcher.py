"""透過 yfinance 抓取 Yahoo Finance 行情資料。

支援：美股（AAPL）、台股（2330.TW）、加密貨幣（BTC-USD）。
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


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


def _ticker_history(symbol: str, period: str, interval: str) -> pd.DataFrame:
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


def get_market_data(symbol: str, period: str = "2y", interval: str = "1d") -> MarketData:
    """抓取單一標的的日線歷史，並整理出最新價、前收、日內漲跌幅。"""
    df = _ticker_history(symbol, period, interval)

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
