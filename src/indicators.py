"""技術指標計算（純函式，方便單元測試）。

- RSI：採用 Wilder 平滑法（先以簡單平均做種子，再遞迴平滑），
  與 TA-Lib / 多數看盤軟體一致。
- MA：簡單移動平均。
- 乖離率：(現價 - MA) / MA * 100。
"""
from typing import Optional

import pandas as pd


def compute_rsi(close: pd.Series, period: int = 14) -> Optional[float]:
    """計算 RSI(period)，資料不足或無法定義時回傳 None。"""
    close = close.dropna()
    if len(close) < period + 1:
        return None

    delta = close.diff().iloc[1:]
    gain = delta.clip(lower=0.0).to_numpy()
    loss = (-delta).clip(lower=0.0).to_numpy()

    # Wilder 平滑：以第一個 period 個差值的簡單平均為種子
    avg_gain = float(gain[:period].mean())
    avg_loss = float(loss[:period].mean())
    for i in range(period, len(gain)):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else None
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def compute_ma(close: pd.Series, periods) -> dict:
    """計算各週期的移動平均，回傳 {period: 值 或 None}。"""
    result = {}
    for p in periods:
        window = int(p)
        ma = close.rolling(window=window, min_periods=window).mean()
        value = ma.iloc[-1]
        result[str(window)] = None if pd.isna(value) else float(value)
    return result


def compute_ma_deviation(price: float, ma_values: dict) -> dict:
    """計算現價對各 MA 的乖離率（%），回傳 {period: 值 或 None}。"""
    result = {}
    for key, ma in ma_values.items():
        result[key] = (price - ma) / ma * 100.0 if ma else None
    return result
