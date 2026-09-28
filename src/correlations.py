"""Layer A：資產與總體因子（殖利率／美元／原油／黃金／VIX）的相關性與背離。

純函式模組：輸入是兩組 {dates, closes} 日線歷史，輸出是相關係數與背離清單，
不連網、不依賴其他專案模組，方便單元測試。

重要原則：|r| 低於門檻（預設 0.3）時一律不聲稱「連動」或「背離」，
避免在雜訊上編故事；AI 端也由 prompt 明令禁止把低相關講成因果。
"""
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

DEFAULT_MIN_ABS = 0.3

FACTOR_LABELS = {
    "^TNX": "美 10 年期殖利率",
    "DX-Y.NYB": "美元指數",
    "CL=F": "WTI 原油",
    "GC=F": "黃金",
    "^VIX": "VIX",
}


@dataclass
class Correlation:
    """某資產與某總體因子在指定窗口的相關係數。"""

    asset: str
    factor: str
    window: int
    r: Optional[float]
    significant: bool = False

    @property
    def factor_label(self) -> str:
        return FACTOR_LABELS.get(self.factor, self.factor)

    @property
    def direction(self) -> str:
        if self.r is None:
            return "無資料"
        if self.r <= -DEFAULT_MIN_ABS:
            return "反向連動"
        if self.r >= DEFAULT_MIN_ABS:
            return "同向連動"
        return "無明顯連動"

    def to_line(self) -> str:
        return f"{self.asset} 與{self.factor_label}（{self.window} 日）r={self.r:+.2f}（{self.direction}）"


@dataclass
class Divergence:
    """價格方向與平時連動方向相反的情況（只在 |r| 足夠大時成立）。

    方向是以「最近 recent_days 日」的漲跌判斷，相關性則用完整的 window 日樣本；
    兩者刻意分開，否則同一段資料算兩次會讓背離幾乎不可能成立。
    """

    asset: str
    factor: str
    window: int
    r: float
    asset_change: float
    factor_change: float
    recent_days: int = 5

    @property
    def factor_label(self) -> str:
        return FACTOR_LABELS.get(self.factor, self.factor)

    def to_line(self) -> str:
        asset_dir = "上漲" if self.asset_change >= 0 else "下跌"
        factor_dir = "走高" if self.factor_change >= 0 else "走低"
        habit = "反向" if self.r < 0 else "同向"
        return (f"{self.asset} 近 {self.recent_days} 日{asset_dir}、{self.factor_label}{factor_dir}，"
                f"但近 {self.window} 日為{habit}連動（r={self.r:+.2f}）")


def align(dates_a: Sequence[str], values_a: Sequence[float],
          dates_b: Sequence[str], values_b: Sequence[float]) -> Tuple[List[float], List[float]]:
    """依日期取交集對齊兩組序列（只保留兩邊都有資料的日期，維持 A 的順序）。"""
    lookup = {}
    for date, value in zip(dates_b or [], values_b or []):
        lookup[str(date)] = value
    xs: List[float] = []
    ys: List[float] = []
    for date, value in zip(dates_a or [], values_a or []):
        other = lookup.get(str(date))
        if other is None:
            continue
        xs.append(float(value))
        ys.append(float(other))
    return xs, ys


def pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """皮爾森相關係數；資料不足或其中一組沒有變異數時回傳 None。"""
    n = min(len(xs or []), len(ys or []))
    if n < 3:
        return None
    mean_x = sum(xs[:n]) / n
    mean_y = sum(ys[:n]) / n
    cov = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    var_x = sum((xs[i] - mean_x) ** 2 for i in range(n))
    var_y = sum((ys[i] - mean_y) ** 2 for i in range(n))
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / math.sqrt(var_x * var_y)


def window_change(values: Sequence[float], window: int) -> Optional[float]:
    """最後 window 期的漲跌幅（%）；資料不足或基準為 0 時回傳 None。"""
    values = list(values or [])
    if window <= 0 or len(values) < window + 1:
        return None
    base = values[-1 - window]
    if not base:
        return None
    return (values[-1] / base - 1.0) * 100.0


def rolling_correlation(asset_history: Optional[dict], factor_history: Optional[dict],
                        window: int) -> Optional[float]:
    """對齊後取最後 window 筆收盤價計算相關係數。

    對齊後的樣本數必須至少 window 筆，否則回傳 None：
    資料不足時硬算會讓「60 日相關性」實際上只有 20 筆樣本，屬於誤導。
    """
    asset_dates = (asset_history or {}).get("dates") or []
    asset_closes = (asset_history or {}).get("closes") or []
    factor_dates = (factor_history or {}).get("dates") or []
    factor_closes = (factor_history or {}).get("closes") or []
    xs, ys = align(asset_dates, asset_closes, factor_dates, factor_closes)
    if window > 0:
        if len(xs) < window:
            return None
        xs, ys = xs[-window:], ys[-window:]
    return pearson(xs, ys)


def compute_correlations(asset_history: Dict[str, dict], factor_history: Dict[str, dict],
                         windows: Sequence[int],
                         min_abs: float = DEFAULT_MIN_ABS) -> List[Correlation]:
    """計算所有資產 × 總體因子 × 窗口的相關係數。"""
    result: List[Correlation] = []
    for asset, history in (asset_history or {}).items():
        for factor, factor_data in (factor_history or {}).items():
            if asset == factor:
                continue
            for window in windows or []:
                r = rolling_correlation(history, factor_data, int(window))
                result.append(Correlation(
                    asset=asset,
                    factor=factor,
                    window=int(window),
                    r=r,
                    significant=r is not None and abs(r) >= min_abs,
                ))
    return result


def detect_divergences(asset_history: Dict[str, dict], factor_history: Dict[str, dict],
                       windows: Sequence[int], min_abs: float = DEFAULT_MIN_ABS,
                       asset_names: Optional[Dict[str, str]] = None,
                       recent_days: int = 5) -> List[Divergence]:
    """找出「最近的價格方向與平時連動方向相反」的組合。

    判定：近期方向（最近 recent_days 日漲跌）與習慣方向相反，且 |r| >= min_abs
    - r <= -min_abs（平時反向）：最近卻同向 → 背離
    - r >=  min_abs（平時同向）：最近卻反向 → 背離
    """
    found: List[Divergence] = []
    for asset, history in (asset_history or {}).items():
        for factor, factor_data in (factor_history or {}).items():
            if asset == factor:
                continue
            for window in windows or []:
                window = int(window)
                r = rolling_correlation(history, factor_data, window)
                if r is None or abs(r) < min_abs:
                    continue
                asset_change = window_change((history or {}).get("closes") or [], recent_days)
                factor_change = window_change((factor_data or {}).get("closes") or [], recent_days)
                if asset_change is None or factor_change is None:
                    continue
                same_direction = (asset_change >= 0) == (factor_change >= 0)
                diverged = same_direction if r < 0 else not same_direction
                if not diverged:
                    continue
                found.append(Divergence(
                    asset=(asset_names or {}).get(asset, asset),
                    factor=factor,
                    window=window,
                    r=round(r, 3),
                    asset_change=round(asset_change, 2),
                    factor_change=round(factor_change, 2),
                    recent_days=int(recent_days),
                ))
    found.sort(key=lambda d: -abs(d.r))
    return found


def format_correlation_lines(correlations: Sequence[Correlation],
                             divergences: Sequence[Divergence], limit: int = 3) -> List[str]:
    """組出給報告用的相關性／背離文字行（沒有顯著結果時回傳空清單）。"""
    lines: List[str] = []
    strong = [c for c in (correlations or []) if c.significant and c.r is not None]
    if strong:
        strong.sort(key=lambda c: -abs(c.r))
        bits = [c.to_line() for c in strong[:limit]]
        lines.append("資產與總體因子相關性：" + "；".join(bits))
    if divergences:
        bits = [d.to_line() for d in divergences[:limit]]
        lines.append("總體背離（價格方向與平時連動方向相反）：" + "；".join(bits))
    return lines

