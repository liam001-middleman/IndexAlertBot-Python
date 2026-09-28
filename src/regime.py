"""Layer B：宏觀情境（四象限）判定。

以兩個方向變數做分類：
- 通膨方向：CPI 年增率的變化（百分點，最新月 vs 前月）
- 利率方向：10 年期公債殖利率近 20 交易日的變化（bp）

任一項缺資料，或變化量落在「持平區間」內，就回傳 neutral，不硬套象限；
資料不足時給出明確的「方向不明」而不是猜一個情境。
"""
from dataclasses import dataclass
from typing import Optional

FLAT_CPI_PP = 0.1     # CPI 年增率變化小於 0.1pp 視為持平
FLAT_RATE_BP = 10.0   # 10Y 殖利率變化小於 10bp 視為持平

INFLATION_FALLBACK_KEYS = ("cpi", "core_cpi", "pce", "core_pce")

QUADRANTS = {
    ("up", "up"): (
        "stagflation",
        "停滯性通膨壓力",
        "通膨往上、利率也往上：成本壓力與資金成本同時升高，市場容易對任何通膨或就業數據過度反應。",
    ),
    ("up", "down"): (
        "reflation",
        "再通膨",
        "通膨往上但利率往下：景氣與獲利預期偏樂觀，但要注意實質利率偏低的後座力。",
    ),
    ("down", "up"): (
        "cooling",
        "降溫但利率偏高",
        "通膨往下、利率仍往上：實質利率持續收緊，評價修復不易，成長股與高負債族群壓力較大。",
    ),
    ("down", "down"): (
        "recovery",
        "寬鬆復甦",
        "通膨往下、利率也往下：資金環境轉鬆，對風險資產相對友善，但要分辨是景氣降溫還是衰退式降息。",
    ),
}


@dataclass
class Regime:
    """當前宏觀情境。"""

    key: str
    label: str
    description: str
    inflation_direction: str = "unknown"   # up / down / flat / unknown
    rate_direction: str = "unknown"
    inflation_change: Optional[float] = None   # CPI 年增率變化（pp）
    rate_change_bp: Optional[float] = None     # 10Y 殖利率 20 日變化（bp）

    def to_line(self) -> str:
        detail = []
        if self.inflation_change is not None:
            detail.append(f"CPI 年增較前月 {self.inflation_change:+.2f}pp")
        if self.rate_change_bp is not None:
            detail.append(f"10Y 殖利率近 20 交易日 {self.rate_change_bp:+.0f}bp")
        suffix = f"（{'；'.join(detail)}）" if detail else ""
        return f"宏觀情境：{self.label}{suffix}。{self.description}"


def _direction(change: Optional[float], flat_threshold: float) -> str:
    """把變化量轉成 up / down / flat / unknown。"""
    if change is None:
        return "unknown"
    if abs(change) < flat_threshold:
        return "flat"
    return "up" if change > 0 else "down"


def classify_regime(inflation_change: Optional[float],
                    rate_change_bp: Optional[float]) -> Regime:
    """依「通膨方向 × 利率方向」判定情境；任一方向不明或持平時回傳 neutral。"""
    inflation_direction = _direction(inflation_change, FLAT_CPI_PP)
    rate_direction = _direction(rate_change_bp, FLAT_RATE_BP)
    if "unknown" in (inflation_direction, rate_direction) or "flat" in (inflation_direction, rate_direction):
        return Regime(
            key="neutral",
            label="方向不明（總體沒有明確趨勢）",
            description="通膨與利率的變化都不明顯，總體環境暫時不是主導因素。",
            inflation_direction=inflation_direction,
            rate_direction=rate_direction,
            inflation_change=inflation_change,
            rate_change_bp=rate_change_bp,
        )
    key, label, description = QUADRANTS[(inflation_direction, rate_direction)]
    return Regime(
        key=key,
        label=label,
        description=description,
        inflation_direction=inflation_direction,
        rate_direction=rate_direction,
        inflation_change=inflation_change,
        rate_change_bp=rate_change_bp,
    )


def inflation_change_from_snapshot(snapshot) -> Optional[float]:
    """取 CPI（其次核心 CPI／PCE）年增率相對前一期的變化（pp）。"""
    monthly = getattr(snapshot, "monthly", {}) or {}
    for key in INFLATION_FALLBACK_KEYS:
        point = monthly.get(key)
        if point is None or point.yoy is None or point.prev_yoy is None:
            continue
        return point.yoy - point.prev_yoy
    return None


def rate_change_from_snapshot(snapshot) -> Optional[float]:
    """取 10Y 殖利率 20 日變化（bp）：優先用 FRED，其次用 Yahoo ^TNX 日線。"""
    rates = getattr(snapshot, "rates", {}) or {}
    change = rates.get("dgs10_bp_20d")
    if change is not None:
        return change
    history = (getattr(snapshot, "history", {}) or {}).get("^TNX") or {}
    closes = history.get("closes") or []
    if len(closes) >= 21:
        return (closes[-1] - closes[-21]) * 100.0
    return None


def regime_from_snapshot(snapshot) -> Regime:
    """由 macro 快照直接判定情境（缺值時走 neutral）。"""
    if snapshot is None:
        return classify_regime(None, None)
    return classify_regime(inflation_change_from_snapshot(snapshot),
                           rate_change_from_snapshot(snapshot))


def format_regime_line(regime: Optional[Regime]) -> Optional[str]:
    """給報告用的情境文字；None 時回傳 None。"""
    return regime.to_line() if regime is not None else None
