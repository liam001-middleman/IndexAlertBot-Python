"""Layer C：風險分數（0~100）。

R = 技術面權重 × 技術面分數 + 總體面權重 × 總體面分數（預設 0.6／0.4，可由 config 調整）

語意：**分數越高 = 短線過熱、追高的風險越大**（技術面看 RSI／正乖離／價格站在均線
上方的比例；總體面看利率水位與變化、美元走勢、通膨水位、VIX）。分級：< 35 低、
35~60 中、> 60 高。反過來說，低分只代表「不熱」，並不代表沒有下跌風險。

⚠️ 所有換算常數與權重都**沒有經過回測**，只是把可觀察的壓力來源量化成一個
可比較的相對指標，不代表勝率或機率。
"""
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from .correlations import window_change

# 技術面分項權重
TECH_WEIGHTS = {"rsi": 0.4, "deviation": 0.35, "trend": 0.25}
# 總體面分項權重
MACRO_WEIGHTS = {"rates": 0.35, "dollar": 0.25, "inflation": 0.25, "volatility": 0.15}

# 總體面換算常數（經驗值，未回測）
LONG_RATE_NEUTRAL = 4.0      # 10 年期殖利率中性水位（%）
RATE_LEVEL_WEIGHT = 12.0     # 每 1% 殖利率水位對應的分數
RATE_CHANGE_WEIGHT = 0.4     # 每 1bp 殖利率變化對應的分數
DXY_CHANGE_WEIGHT = 5.0      # 美元指數每 1% 變化對應的分數
CPI_TARGET = 2.0             # 通膨目標（%）
CPI_WEIGHT = 25.0            # 每高於目標 1% 對應的分數
VIX_BASE = 12.0              # VIX 基準水位
VIX_WEIGHT = 3.0             # VIX 每 1 點對應的分數

# 技術面換算常數
NEUTRAL_SCORE = 50.0         # 零乖離／零變化時的分數
DEVIATION_WEIGHT = 2.5       # 每 1% 正乖離對應的分數

GRADE_LOW = 35.0
GRADE_HIGH = 60.0


@dataclass
class RiskScore:
    """一次風險分數計算結果。"""

    total: Optional[float] = None
    technical: Optional[float] = None
    macro: Optional[float] = None
    grade: str = "無法計算"
    components: Dict[str, Optional[float]] = field(default_factory=dict)
    partial: bool = False   # True = 只有部分面向算得出來（缺資料時會揭露）

    def to_line(self, label: str = "") -> str:
        prefix = f"{label} " if label else ""
        if self.total is None:
            return f"{prefix}風險分數：資料不足，無法計算"
        parts = []
        if self.technical is not None:
            parts.append(f"技術面 {self.technical:.1f}")
        if self.macro is not None:
            parts.append(f"總體面 {self.macro:.1f}")
        detail = f"（{'、'.join(parts)}）" if parts else ""
        note = "，僅部分面向可計算" if self.partial else ""
        return f"{prefix}風險分數 {self.total:.1f}／100（{self.grade}{note}）{detail}"


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def technical_score(rsi: Optional[float], ma_deviation: Optional[dict],
                    ma_values: Optional[dict] = None) -> Optional[float]:
    """技術面分數（0~100）：RSI／正乖離／站在均線上方的比例。

    - RSI 直接當成 0~100 的熱度分數。
    - 乖離：以「最大的均線乖離率」換算，50 分為零乖離，每 +1% 加 2.5 分。
    - 排列：價格在越多條均線之上 → 越偏多頭延伸 → 分數越高。
    資料全部缺少時回傳 None（呼叫端據此揭露缺值）。
    """
    parts: Dict[str, float] = {}
    if rsi is not None:
        parts["rsi"] = _clamp(float(rsi))

    deviations = [float(v) for v in (ma_deviation or {}).values() if v is not None]
    if deviations:
        parts["deviation"] = _clamp(NEUTRAL_SCORE + max(deviations) * DEVIATION_WEIGHT)
        above = len([v for v in deviations if v > 0])
        parts["trend"] = _clamp(above / len(deviations) * 100.0)

    if not parts:
        return None
    weight_sum = sum(TECH_WEIGHTS[key] for key in parts)
    return sum(parts[key] * TECH_WEIGHTS[key] for key in parts) / weight_sum


def _rate_level_and_change(snapshot) -> Tuple[Optional[float], Optional[float]]:
    """取 10Y 殖利率的水位（%）與 20 日變化（bp）：FRED 優先，其次 Yahoo ^TNX。"""
    rates = getattr(snapshot, "rates", {}) or {}
    level = rates.get("dgs10")
    if level is None:
        level = (getattr(snapshot, "prices", {}) or {}).get("^TNX")
    change = rates.get("dgs10_bp_20d")
    if change is None:
        closes = ((getattr(snapshot, "history", {}) or {}).get("^TNX") or {}).get("closes") or []
        if len(closes) >= 21:
            change = (closes[-1] - closes[-21]) * 100.0
    return level, change


def macro_component_rates(snapshot) -> Optional[float]:
    """利率分項：水位偏高＋短期彈升 → 分數高（對長天期資產不利）。"""
    level, change = _rate_level_and_change(snapshot)
    if level is None:
        return None
    value = NEUTRAL_SCORE + (float(level) - LONG_RATE_NEUTRAL) * RATE_LEVEL_WEIGHT
    if change is not None:
        value += float(change) * RATE_CHANGE_WEIGHT
    return _clamp(value)


def macro_component_dollar(snapshot) -> Optional[float]:
    """美元分項：美元指數 20 日走強 → 分數高（對非美資產與出口不利）。"""
    closes = ((getattr(snapshot, "history", {}) or {}).get("DX-Y.NYB") or {}).get("closes") or []
    change = window_change(closes, 20)
    if change is None:
        return None
    return _clamp(NEUTRAL_SCORE + change * DXY_CHANGE_WEIGHT)


def macro_component_inflation(snapshot) -> Optional[float]:
    """通膨分項：CPI（其次核心 CPI／PCE）年增率高於目標 → 分數高。"""
    monthly = getattr(snapshot, "monthly", {}) or {}
    for key in ("cpi", "core_cpi", "pce", "core_pce"):
        point = monthly.get(key)
        if point is None or point.yoy is None:
            continue
        return _clamp((point.yoy - CPI_TARGET) * CPI_WEIGHT)
    return None


def macro_component_volatility(snapshot) -> Optional[float]:
    """波動分項：VIX 越高 → 分數高。"""
    vix = (getattr(snapshot, "prices", {}) or {}).get("^VIX")
    if vix is None:
        return None
    return _clamp((float(vix) - VIX_BASE) * VIX_WEIGHT)


def macro_score(snapshot) -> Tuple[Optional[float], Dict[str, Optional[float]]]:
    """總體面分數（0~100）與各分項；可算的分項不足時回傳 (None, components)。"""
    components = {
        "rates": macro_component_rates(snapshot),
        "dollar": macro_component_dollar(snapshot),
        "inflation": macro_component_inflation(snapshot),
        "volatility": macro_component_volatility(snapshot),
    }
    available = {k: v for k, v in components.items() if v is not None}
    if not available:
        return None, components
    weight_sum = sum(MACRO_WEIGHTS[k] for k in available)
    score = sum(available[k] * MACRO_WEIGHTS[k] for k in available) / weight_sum
    return score, components


def combine_scores(technical: Optional[float], macro: Optional[float],
                   weights: Optional[dict] = None) -> Tuple[Optional[float], bool]:
    """加權合併技術面與總體面分數，回傳（總分, 是否只算到部分面向）。

    缺其中一項時以剩下的權重重新歸一化（並在報告中揭露「僅部分面向可計算」）。
    """
    weights = weights or {"technical": 0.6, "macro": 0.4}
    available = {k: v for k, v in (("technical", technical), ("macro", macro)) if v is not None}
    if not available:
        return None, False
    weight_sum = sum(max(0.0, float(weights.get(k, 0.0))) for k in available)
    if weight_sum <= 0:
        total = sum(available.values()) / len(available)
    else:
        total = sum(available[k] * max(0.0, float(weights.get(k, 0.0))) for k in available) / weight_sum
    return total, len(available) < 2


def grade_of(total: Optional[float]) -> str:
    """分數分級：< 35 低、35~60 中、> 60 高。"""
    if total is None:
        return "無法計算"
    if total < GRADE_LOW:
        return "低"
    if total <= GRADE_HIGH:
        return "中"
    return "高"


def build_risk_score(technical: Optional[float], macro: Optional[float],
                     weights: Optional[dict] = None,
                     components: Optional[Dict[str, Optional[float]]] = None) -> RiskScore:
    """合併出完整 RiskScore（含分級與分項）。"""
    total, partial = combine_scores(technical, macro, weights)
    return RiskScore(
        total=total,
        technical=technical,
        macro=macro,
        grade=grade_of(total),
        components=dict(components or {}),
        partial=partial,
    )


def compute_risk_score(quote, snapshot, weights: Optional[dict] = None) -> RiskScore:
    """由 Quote（技術面）與 macro 快照（總體面）一次算出風險分數。"""
    technical = technical_score(getattr(quote, "rsi", None),
                                getattr(quote, "ma_deviation_pct", None),
                                getattr(quote, "ma", None))
    macro, components = macro_score(snapshot) if snapshot is not None else (None, {})
    return build_risk_score(technical, macro, weights, components)


def format_risk_line(label: str, risk: Optional[RiskScore]) -> str:
    """給報告用的風險分數文字行。"""
    if risk is None:
        return f"{label} 風險分數：資料不足，無法計算"
    return risk.to_line(label)

