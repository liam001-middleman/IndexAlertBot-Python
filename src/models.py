"""資料模型（DataClasses）。"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Quote:
    """單一標的在某次執行的行情快照與技術指標。"""

    symbol: str
    name: str
    market: str
    price: float
    previous_close: float
    open_price: Optional[float]
    change_pct: float  # 對比前收的漲跌幅（%）
    rsi: Optional[float]
    ma: dict = field(default_factory=dict)  # {"20": 值, "60": 值, "200": 值}
    ma_deviation_pct: dict = field(default_factory=dict)  # 乖離率（%）
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Alert:
    """一則「新觸發」的警報事件。"""

    symbol: str
    name: str
    market: str
    alert_type: str  # 例如 rsi_overbought / intraday_surge / ma_deviation_pos_20
    alert_name: str  # 人類可讀名稱，例如「RSI 超買」
    severity: str  # critical / warning / info
    message: str  # 單行摘要
    detail: str  # 詳細說明（含數值）
    value: float
    threshold: float
    price: float  # 該標的現價（報告用）
    rsi: Optional[float] = None  # RSI 值（報告背景資訊）
    ma: dict = field(default_factory=dict)  # 均線數值，{"20": 值, "60": 值, "200": 值}
    ma_deviation_pct: dict = field(default_factory=dict)  # 對各 MA 的乖離率（%）
    last_report_price: Optional[float] = None  # 上次出報告時的價格（報告比較用）
    last_report_at: Optional[str] = None  # 上次出報告時間（ISO 字串）
    triggered_at: datetime = field(default_factory=datetime.utcnow)
