"""總體經濟指標抓取與快取（BLS / FRED / Fed RSS / Yahoo）。

設計原則：
1. 解析邏輯一律寫成「純函式」（parse_*），不碰網路，方便用 fixture 單元測試。
2. 任何一個資料源失敗都只影響該區塊（降級），不會讓整個 job 崩潰；
   失敗原因記在 snapshot.errors 與 sections[*].error，並在報告末端揭露。
3. FRED 的 api_key 是放在 URL query string，因此錯誤訊息「刻意不含例外內容與 URL」，
   避免金鑰被寫進 GitHub Actions 日誌或 Telegram 訊息。
4. 快取寫入 macro_snapshot.json，只有 US workflow 會寫；TW / Crypto 以唯讀模式讀取。
"""
import json
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
import yfinance as yf

from .config import MacroConfig

logger = logging.getLogger(__name__)

BLS_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"
FED_FEEDS = {
    "monetary": "https://www.federalreserve.gov/feeds/press_monetary.xml",
    "speeches": "https://www.federalreserve.gov/feeds/speeches.xml",
}
HTTP_TIMEOUT = 30
FRED_OBS_LIMIT = 400       # FRED 日頻序列回看筆數（約 1.5 年）
BLS_YEARS_BACK = 2         # BLS 回看年數（計算 YoY 至少需 13 期）
SNAPSHOT_VERSION = 1
HISTORY_DAYS = 90          # 快照保存的 macro 價格歷史天數（供相關性計算）
NEWS_LIMIT = 4             # 新聞最多保留筆數

# 預設的 macro 資產代號（config.yaml 的 assets 有 market: macro 時以該清單為準）
DEFAULT_MACRO_SYMBOLS = ["^TNX", "DX-Y.NYB", "CL=F", "GC=F", "^VIX"]

# 報告文字用的標籤
MONTHLY_LABELS = {
    "cpi": ("CPI", "%"),
    "core_cpi": ("核心 CPI", "%"),
    "pce": ("PCE 物價", "%"),
    "core_pce": ("核心 PCE", "%"),
    "nfp": ("非農就業", "千人"),
    "unemployment_rate": ("失業率", "%"),
    "avg_hourly_earnings": ("平均時薪", "%"),
    "inflation_expect_1y": ("1 年期通膨預期", "%"),
    "inflation_expect_10y": ("10 年期通膨預期", "%"),
}
SECTION_LABELS = {
    "bls": "BLS（CPI／就業）",
    "fred": "FRED（PCE／政策利率／通膨預期）",
    "fed_rss": "Fed RSS（FOMC 與官員談話）",
    "prices": "Yahoo（macro 資產價格）",
    "news": "新聞",
}


class MacroFetchError(Exception):
    """總體資料抓取或解析失敗。"""


# ---------- 共用小工具 ----------

def _to_float(value) -> Optional[float]:
    """寬鬆轉 float；無法轉換（含 BLS/FRED 的缺值符號）時回傳 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text in {".", "-", "N/A", "null", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _pct_change(value: Optional[float], base: Optional[float]) -> Optional[float]:
    """（value - base）/ base * 100；base 為 0 或 None 時回傳 None。"""
    if value is None or base in (None, 0):
        return None
    return (value - base) / base * 100.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(text) -> Optional[datetime]:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt(value: Optional[float], digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "N/A"
    return f"{value:.{digits}f}{suffix}"


# ---------- 資料模型 ----------

@dataclass
class MacroPoint:
    """單一月度統計序列的最新值與變化。"""

    key: str
    label: str
    unit: str = ""
    period: str = ""                                  # 最新期別（如 "2026-08"）
    value: Optional[float] = None                     # 最新值（原始單位）
    prev_value: Optional[float] = None                # 前一期值
    yoy: Optional[float] = None                       # 年增率（%）
    prev_yoy: Optional[float] = None                  # 前一期年增率（%）
    mom: Optional[float] = None                       # 月增率（%）
    prev_mom: Optional[float] = None                  # 前一期的月增率（%）
    change: Optional[float] = None                    # 對前一期水準差（原始單位）
    prev_change: Optional[float] = None               # 前一期的水準差
    change_3m: Optional[float] = None                 # 對 3 期前的水準差

    def to_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "unit": self.unit, "period": self.period,
            "value": self.value, "prev_value": self.prev_value,
            "yoy": self.yoy, "prev_yoy": self.prev_yoy,
            "mom": self.mom, "prev_mom": self.prev_mom,
            "change": self.change, "prev_change": self.prev_change,
            "change_3m": self.change_3m,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "MacroPoint":
        raw = raw or {}
        return cls(
            key=str(raw.get("key", "")),
            label=str(raw.get("label", "")),
            unit=str(raw.get("unit", "")),
            period=str(raw.get("period", "")),
            value=_to_float(raw.get("value")),
            prev_value=_to_float(raw.get("prev_value")),
            yoy=_to_float(raw.get("yoy")),
            prev_yoy=_to_float(raw.get("prev_yoy")),
            mom=_to_float(raw.get("mom")),
            prev_mom=_to_float(raw.get("prev_mom")),
            change=_to_float(raw.get("change")),
            prev_change=_to_float(raw.get("prev_change")),
            change_3m=_to_float(raw.get("change_3m")),
        )


@dataclass
class MacroSnapshot:
    """一次總體環境快照（可序列化成 macro_snapshot.json）。"""

    version: int = SNAPSHOT_VERSION
    fetched_at: str = ""
    sections: Dict[str, dict] = field(default_factory=dict)   # {區塊: {ok, at, error}}
    monthly: Dict[str, MacroPoint] = field(default_factory=dict)
    rates: Dict[str, float] = field(default_factory=dict)
    prices: Dict[str, float] = field(default_factory=dict)
    history: Dict[str, dict] = field(default_factory=dict)    # {symbol: {dates, closes}}
    events: List[dict] = field(default_factory=list)
    news: List[dict] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    # ---- 查詢 ----

    def section_ok(self, name: str) -> bool:
        return bool((self.sections.get(name) or {}).get("ok"))

    def section_at(self, name: str) -> Optional[str]:
        return (self.sections.get(name) or {}).get("at")

    def is_fresh(self, name: str, ttl_hours: float) -> bool:
        """該區塊是否仍在 TTL 內（未成功過一律視為需要重抓）。"""
        info = self.sections.get(name) or {}
        if not info.get("ok"):
            return False
        at = _parse_iso(info.get("at"))
        if at is None:
            return False
        return datetime.now(timezone.utc) - at < timedelta(hours=ttl_hours)

    def data_date(self) -> str:
        """資料時間（取最新「成功」區塊的時間，僅顯示到日期）。"""
        stamps = [
            _parse_iso(info.get("at"))
            for info in self.sections.values()
            if isinstance(info, dict) and info.get("ok")
        ]
        valid = [s for s in stamps if s is not None]
        if not valid:
            return "無資料"
        return max(valid).astimezone(timezone.utc).strftime("%Y-%m-%d")

    def unavailable(self) -> List[str]:
        """列出「本回合無法取得」的資料區塊標籤（供報告末端揭露，避免靜默降級）。"""
        missing = []
        for name, label in SECTION_LABELS.items():
            if not self.sections.get(name):
                continue
            if not self.section_ok(name):
                missing.append(label)
        return missing

    def copy(self) -> "MacroSnapshot":
        return MacroSnapshot.from_dict(self.to_dict())

    # ---- 序列化 ----

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "fetched_at": self.fetched_at,
            "sections": self.sections,
            "monthly": {k: v.to_dict() for k, v in self.monthly.items()},
            "rates": self.rates,
            "prices": self.prices,
            "history": self.history,
            "events": self.events,
            "news": self.news,
            "errors": self.errors,
        }

    @classmethod
    def from_dict(cls, raw) -> "MacroSnapshot":
        raw = raw if isinstance(raw, dict) else {}
        monthly = {}
        for key, value in (raw.get("monthly") or {}).items():
            point = MacroPoint.from_dict(value)
            if not point.key:
                point.key = str(key)
            monthly[str(key)] = point
        return cls(
            version=int(raw.get("version", SNAPSHOT_VERSION) or SNAPSHOT_VERSION),
            fetched_at=str(raw.get("fetched_at", "")),
            sections=raw.get("sections") or {},
            monthly=monthly,
            rates={k: v for k, v in (raw.get("rates") or {}).items() if _to_float(v) is not None},
            prices={k: v for k, v in (raw.get("prices") or {}).items() if _to_float(v) is not None},
            history=raw.get("history") or {},
            events=raw.get("events") or [],
            news=raw.get("news") or [],
            errors=[str(e) for e in (raw.get("errors") or [])],
        )


# ---------- 純函式：解析（不連網，可直接用 fixture 測試） ----------

def parse_bls_series(series_obj) -> List[Tuple[str, float]]:
    """解析 BLS timeseries/data 的單一 series，回傳 [(期別 "YYYY-MM", 值)] 升冪。

    排除 M13（年度平均）與非數值（BLS 以 "-" 表示缺值）。
    """
    rows: List[Tuple[str, float]] = []
    for item in (series_obj or {}).get("data") or []:
        period = str(item.get("period", ""))
        if not period.startswith("M") or period == "M13":
            continue
        try:
            month = int(period[1:])
            year = int(item.get("year"))
        except (TypeError, ValueError):
            continue
        if not 1 <= month <= 12:
            continue
        value = _to_float(item.get("value"))
        if value is None:
            continue
        rows.append((f"{year:04d}-{month:02d}", value))
    return sorted(rows, key=lambda r: r[0])


def parse_bls_payload(payload) -> Dict[str, List[Tuple[str, float]]]:
    """解析整份 BLS 回應，回傳 {seriesID: [(期別, 值)]}。"""
    result: Dict[str, List[Tuple[str, float]]] = {}
    series = ((payload or {}).get("Results") or {}).get("series") or []
    for item in series:
        sid = str(item.get("seriesID", "")).strip()
        rows = parse_bls_series(item)
        if sid and rows:
            result[sid] = rows
    return result


def parse_fred_observations(payload) -> List[Tuple[str, float]]:
    """解析 FRED observations，回傳 [(日期 "YYYY-MM-DD", 值)] 升冪；"." 視為缺值。"""
    rows: List[Tuple[str, float]] = []
    for item in (payload or {}).get("observations") or []:
        date = str(item.get("date", "")).strip()
        value = _to_float(item.get("value"))
        if date and value is not None:
            rows.append((date, value))
    return sorted(rows, key=lambda r: r[0])


def _rss_date(text: str) -> Optional[str]:
    """把 RSS 的 pubDate（RFC 822）轉成 "YYYY-MM-DD"（UTC），失敗回 None。"""
    try:
        return parsedate_to_datetime(text).astimezone(timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def parse_fed_rss(xml_text: str, limit: int = 5) -> List[dict]:
    """解析 Fed 官方 RSS（press_monetary / speeches），回傳最新的 limit 筆。

    使用標準庫 xml.etree（CDATA 會自動展開），並容忍檔頭 BOM。
    """
    if not xml_text:
        return []
    text = str(xml_text).lstrip("\ufeff").strip()
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise MacroFetchError(f"Fed RSS 格式無法解析: {exc}") from None
    items: List[dict] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        items.append({
            "date": _rss_date((item.findtext("pubDate") or "").strip()),
            "title": title,
            "url": (item.findtext("link") or "").strip(),
        })
    items.sort(key=lambda x: x.get("date") or "", reverse=True)
    return items[:limit]


def parse_yahoo_news(items, keywords, limit: int = NEWS_LIMIT) -> List[dict]:
    """過濾 yfinance .news：只保留標題含關鍵字的項目（不分大小寫、標題去重）。

    同時相容新舊兩種回傳格式（content 巢狀 / 扁平欄位）。
    """
    wanted = [str(k).lower() for k in (keywords or []) if str(k).strip()]
    seen = set()
    result: List[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content") if isinstance(item.get("content"), dict) else {}
        title = str(content.get("title") or item.get("title") or "").strip()
        if not title or title in seen:
            continue
        if wanted and not any(k in title.lower() for k in wanted):
            continue
        url = ""
        click = content.get("clickThroughUrl")
        if isinstance(click, dict):
            url = str(click.get("url") or "")
        url = url or str(item.get("link") or "")
        provider = content.get("provider")
        publisher = str(provider.get("displayName") or "") if isinstance(provider, dict) else ""
        publisher = publisher or str(item.get("publisher") or "")
        date = str(content.get("pubDate") or "")[:10]
        if not date:
            stamp = item.get("providerPublishTime")
            if isinstance(stamp, (int, float)):
                date = datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%d")
        seen.add(title)
        result.append({"date": date, "title": title, "publisher": publisher, "url": url})
        if len(result) >= limit:
            break
    return result


def build_point(key: str, rows: List[Tuple[str, float]]) -> Optional[MacroPoint]:
    """由 [(期別, 值)] 建立 MacroPoint（含 YoY / MoM／水準差／3 期變化）。"""
    if not rows or len(rows) < 2:
        return None
    label, unit = MONTHLY_LABELS.get(key, (key, ""))
    values = [v for _, v in rows]
    point = MacroPoint(
        key=key,
        label=label,
        unit=unit,
        period=rows[-1][0],
        value=values[-1],
        prev_value=values[-2],
        mom=_pct_change(values[-1], values[-2]),
        change=values[-1] - values[-2],
    )
    if len(values) >= 13:
        point.yoy = _pct_change(values[-1], values[-13])
        if len(values) >= 14:
            point.prev_yoy = _pct_change(values[-2], values[-14])
    if len(values) >= 3:
        point.prev_mom = _pct_change(values[-2], values[-3])
        point.prev_change = values[-2] - values[-3]
    if len(values) >= 4:
        point.change_3m = values[-1] - values[-4]
    return point


# ---------- 抓取（連網） ----------

def _request_json(method: str, url: str, *, params=None, json_body=None, label: str) -> dict:
    """共用的 JSON 請求。

    錯誤訊息只保留 label 與 HTTP 狀態碼，**刻意不含 URL 與例外內容**：
    FRED 的 api_key 是放在 query string，例外訊息可能帶有完整網址，
    若直接往外拋會讓金鑰出現在 GitHub Actions 日誌或 Telegram 訊息中。
    """
    resp = None
    try:
        resp = requests.request(
            method, url, params=params, json=json_body,
            timeout=HTTP_TIMEOUT, headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        status = getattr(resp, "status_code", "N/A")
        hint = "（請確認 FRED_API_KEY 是否正確）" if status == 400 else ""
        raise MacroFetchError(f"{label} 失敗（HTTP {status}／{type(exc).__name__}）{hint}") from None
    if not isinstance(data, dict):
        raise MacroFetchError(f"{label} 回傳格式非預期")
    return data


def fetch_bls_series(series_ids, years_back: int = BLS_YEARS_BACK) -> Dict[str, List[Tuple[str, float]]]:
    """抓取 BLS 公開 API（免金鑰，可一次多個序列）。"""
    ids = [s for s in series_ids if s]
    if not ids:
        return {}
    end_year = datetime.now(timezone.utc).year
    payload = {
        "seriesid": ids,
        "startyear": str(end_year - years_back),
        "endyear": str(end_year),
    }
    data = _request_json("POST", BLS_API_URL, json_body=payload, label="BLS")
    status = str(data.get("status", ""))
    if status and status != "REQUEST_SUCCEEDED":
        message = "；".join(str(m) for m in (data.get("message") or []))[:160]
        raise MacroFetchError(f"BLS 回報 {status}{('：' + message) if message else ''}")
    return parse_bls_payload(data)


def fetch_fred_series(series_ids, api_key: str,
                      limit: int = FRED_OBS_LIMIT) -> Dict[str, List[Tuple[str, float]]]:
    """抓取 FRED 序列（需免費 API Key）。單一序列失敗只略過該序列。"""
    if not api_key:
        raise MacroFetchError("未設定 FRED_API_KEY，略過 FRED 系列")
    result: Dict[str, List[Tuple[str, float]]] = {}
    errors: List[str] = []
    for sid in [s for s in series_ids if s]:
        params = {
            "series_id": sid, "api_key": api_key, "file_type": "json",
            "sort_order": "desc", "limit": limit,
        }
        try:
            data = _request_json("GET", FRED_API_URL, params=params, label=f"FRED {sid}")
        except MacroFetchError as exc:
            errors.append(str(exc))
            logger.warning("FRED %s 取得失敗（後續以舊資料或略過）", sid)
            continue
        rows = parse_fred_observations(data)
        if rows:
            result[sid] = rows
    if not result and errors:
        raise MacroFetchError("；".join(errors[:2]))
    return result


def fetch_fed_events(limit_per_feed: int = 3, total_limit: int = 6) -> List[dict]:
    """抓取 Fed 官方 RSS（FOMC 聲明／官員談話）；個別 feed 失敗只略過該 feed。"""
    events: List[dict] = []
    errors: List[str] = []
    for name, url in FED_FEEDS.items():
        try:
            resp = requests.get(url, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            # 部分 feed 未帶 charset，直接以 UTF-8 解碼避免中文/特殊字元變亂碼
            items = parse_fed_rss(resp.content.decode("utf-8", errors="replace"), limit=limit_per_feed)
        except Exception as exc:
            errors.append(f"{name}（{type(exc).__name__}）")
            continue
        for item in items:
            item["source"] = name
            events.append(item)
    if not events and errors:
        raise MacroFetchError("Fed RSS 取得失敗：" + "；".join(errors))
    events.sort(key=lambda x: x.get("date") or "", reverse=True)
    return events[:total_limit]


def fetch_macro_prices(symbols, period: str = "6mo") -> Tuple[Dict[str, float], Dict[str, dict]]:
    """用 yfinance 抓 macro 資產日線，回傳（最新收盤價, 近期歷史）。

    歷史只保留最近 HISTORY_DAYS 筆並存進快照，讓 TW / Crypto 唯讀模式不必重抓就有
    足夠資料可以算相關性。單一標的失敗只略過該標的。
    """
    prices: Dict[str, float] = {}
    history: Dict[str, dict] = {}
    for symbol in symbols or []:
        try:
            df = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=True)
        except Exception as exc:
            logger.warning("macro 價格 %s 取得失敗（%s）", symbol, type(exc).__name__)
            continue
        if df is None or df.empty or "Close" not in df:
            logger.warning("macro 價格 %s 沒有回傳資料", symbol)
            continue
        close = df["Close"].dropna()
        if close.empty:
            continue
        dates = [ts.strftime("%Y-%m-%d") for ts in close.index]
        values = [round(float(v), 4) for v in close.tolist()]
        prices[symbol] = values[-1]
        history[symbol] = {"dates": dates[-HISTORY_DAYS:], "closes": values[-HISTORY_DAYS:]}
    return prices, history


def fetch_macro_news(keywords, limit: int = NEWS_LIMIT) -> List[dict]:
    """抓市場新聞並依關鍵字過濾（yfinance 的新聞端點，取大盤 ^GSPC 的新聞）。"""
    try:
        items = yf.Ticker("^GSPC").news or []
    except Exception as exc:
        raise MacroFetchError(f"新聞取得失敗（{type(exc).__name__}）") from None
    return parse_yahoo_news(items, keywords, limit=limit)


# ---------- 快取讀寫 ----------

def load_snapshot(path) -> Optional[MacroSnapshot]:
    """讀取快取快照；檔案不存在或損毀時回傳 None（不丟例外）。"""
    if not path:
        return None
    file_path = Path(path)
    if not file_path.exists():
        return None
    try:
        with open(file_path, "r", encoding="utf-8-sig") as fh:
            raw = json.load(fh)
        return MacroSnapshot.from_dict(raw)
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        logger.warning("macro 快照讀取失敗（視為無快取）: %s", exc)
        return None


def save_snapshot(snapshot: MacroSnapshot, path) -> None:
    """以「先寫暫存檔再替換」寫入快照，避免中斷造成檔案損毀。"""
    if snapshot is None or not path:
        return
    file_path = Path(path)
    snapshot.fetched_at = _now_iso()
    tmp = file_path.with_suffix(file_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(snapshot.to_dict(), fh, ensure_ascii=False, indent=1)
    tmp.replace(file_path)


# ---------- 快照更新（依 TTL 分區塊，個別失敗不影響其他區塊） ----------

def _mark_success(snapshot: MacroSnapshot, name: str) -> MacroSnapshot:
    now = _now_iso()
    snapshot.sections[name] = {"ok": True, "at": now, "attempt_at": now, "error": None}
    return snapshot


def _mark_failure(snapshot: MacroSnapshot, name: str, error: str) -> MacroSnapshot:
    """標記區塊失敗：保留上次成功的資料與時間（供報告顯示資料截止日），下次執行會重試。"""
    previous = snapshot.sections.get(name) or {}
    snapshot.sections[name] = {
        "ok": False,
        "at": previous.get("at"),
        "attempt_at": _now_iso(),
        "error": error,
    }
    return snapshot


def _refresh_bls(snapshot: MacroSnapshot, cfg: MacroConfig) -> MacroSnapshot:
    """更新 BLS 月度序列（CPI／核心 CPI／非農／失業率／時薪）。"""
    try:
        data = fetch_bls_series(list(cfg.bls_series.values()))
    except MacroFetchError as exc:
        return _mark_failure(snapshot, "bls", str(exc))
    for key, sid in cfg.bls_series.items():
        point = build_point(key, data.get(sid) or [])
        if point:
            snapshot.monthly[key] = point
    return _mark_success(snapshot, "bls")


def _refresh_fred(snapshot: MacroSnapshot, cfg: MacroConfig) -> MacroSnapshot:
    """更新 FRED 序列（月度：PCE／通膨預期；日頻：政策利率／10Y 殖利率）。"""
    if not cfg.has_fred:
        return _mark_failure(snapshot, "fred", "未設定 FRED_API_KEY，略過 PCE／政策利率／通膨預期")
    try:
        data = fetch_fred_series(list(cfg.fred_series.values()), cfg.fred_api_key)
    except MacroFetchError as exc:
        return _mark_failure(snapshot, "fred", str(exc))

    monthly_keys = {"pce", "core_pce", "inflation_expect_1y", "inflation_expect_10y"}
    for key, sid in cfg.fred_series.items():
        rows = data.get(sid) or []
        if not rows:
            continue
        if key in monthly_keys:
            point = build_point(key, rows)
            if point:
                snapshot.monthly[key] = point
        else:
            snapshot.rates[key] = rows[-1][1]

    _fill_daily_rates(snapshot, data, cfg)
    return _mark_success(snapshot, "fred")


def _fill_daily_rates(snapshot: MacroSnapshot, data: Dict[str, List[Tuple[str, float]]],
                      cfg: MacroConfig) -> None:
    """計算日頻序列的變化量：10Y 殖利率 20 日 bp、通膨預期 20 日變化、政策利率上次調整。"""
    ids = cfg.fred_series

    def values_of(sid: str) -> List[float]:
        return [v for _, v in (data.get(sid) or [])]

    dgs10 = values_of(ids.get("dgs10", "DGS10"))
    if dgs10:
        snapshot.rates["dgs10"] = dgs10[-1]
        if len(dgs10) >= 21:
            snapshot.rates["dgs10_20d_ago"] = dgs10[-21]
            snapshot.rates["dgs10_bp_20d"] = round((dgs10[-1] - dgs10[-21]) * 100.0, 1)

    t10 = values_of(ids.get("inflation_expect_10y", "T10YIE"))
    if t10:
        snapshot.rates["inflation_expect_10y"] = t10[-1]
        if len(t10) >= 21:
            snapshot.rates["inflation_expect_10y_prev"] = t10[-21]
            snapshot.rates["inflation_expect_10y_bp_20d"] = round((t10[-1] - t10[-21]) * 100.0, 1)

    upper_rows = data.get(ids.get("fed_funds_upper", "DFEDTARU")) or []
    if upper_rows:
        latest = upper_rows[-1][1]
        snapshot.rates["fed_funds_upper"] = latest
        for date, value in reversed(upper_rows[:-1]):
            if abs(value - latest) > 1e-9:
                snapshot.rates["fed_funds_prev"] = value
                snapshot.rates["fed_funds_changed_at"] = date
                break

    lower = values_of(ids.get("fed_funds_lower", "DFEDTARL"))
    if lower:
        snapshot.rates["fed_funds_lower"] = lower[-1]


def _refresh_prices(snapshot: MacroSnapshot, cfg: MacroConfig, symbols: List[str]) -> MacroSnapshot:
    """更新 macro 資產價格與近期日線歷史（供相關性計算）。"""
    try:
        prices, history = fetch_macro_prices(symbols)
    except MacroFetchError as exc:
        return _mark_failure(snapshot, "prices", str(exc))
    if not prices:
        return _mark_failure(snapshot, "prices", "macro 資產價格全數取得失敗")
    snapshot.prices.update(prices)
    snapshot.history.update(history)
    return _mark_success(snapshot, "prices")


def _refresh_fed(snapshot: MacroSnapshot, cfg: MacroConfig) -> MacroSnapshot:
    """更新 Fed RSS 事件（FOMC 聲明／官員談話）。"""
    try:
        events = fetch_fed_events()
    except MacroFetchError as exc:
        return _mark_failure(snapshot, "fed_rss", str(exc))
    snapshot.events = events
    return _mark_success(snapshot, "fed_rss")


def _refresh_news(snapshot: MacroSnapshot, cfg: MacroConfig) -> MacroSnapshot:
    """更新關鍵字過濾後的新聞（失敗不影響其他區塊）。"""
    try:
        news = fetch_macro_news(cfg.news_keywords)
    except MacroFetchError as exc:
        return _mark_failure(snapshot, "news", str(exc))
    snapshot.news = news
    return _mark_success(snapshot, "news")


def apply_tnx_fallback(snapshot: MacroSnapshot) -> MacroSnapshot:
    """未設定 FRED_API_KEY 時，用 Yahoo ^TNX 日線補上 10Y 殖利率水位與 20 日變化（bp）。

    只補「FRED 沒提供」的欄位（不覆寫 FRED 數值），樣本不足 2 筆時不推估；
    這是純本機計算，唯讀模式也會套用，讓 TW / Crypto 報告的利率欄位一致。
    """
    history = (snapshot.history or {}).get("^TNX") or {}
    closes = [c for c in (history.get("closes") or []) if c is not None]
    if len(closes) < 2:
        return snapshot
    if snapshot.rates.get("dgs10") is None:
        snapshot.rates["dgs10"] = round(float(closes[-1]), 3)
    if len(closes) >= 21 and snapshot.rates.get("dgs10_bp_20d") is None:
        snapshot.rates["dgs10_20d_ago"] = round(float(closes[-21]), 3)
        snapshot.rates["dgs10_bp_20d"] = round((closes[-1] - closes[-21]) * 100.0, 1)
    return snapshot


def refresh_snapshot(cfg: MacroConfig, existing: Optional[MacroSnapshot] = None, *,
                     readonly: bool = False, force: bool = False,
                     price_symbols=None) -> MacroSnapshot:
    """依 TTL 更新快照：只重抓過期的區塊，失敗的區塊保留舊資料並記錄原因。

    readonly=True（TW / Crypto workflow）完全不連網，只回傳既有快照，
    避免重複抓取與 macro_snapshot.json 的寫入衝突。
    """
    snapshot = existing.copy() if existing is not None else MacroSnapshot()
    snapshot.version = SNAPSHOT_VERSION

    if readonly:
        # 唯讀模式不連網：只用快照內既有的 ^TNX 日線補欄位後直接回傳
        return apply_tnx_fallback(snapshot)
    if not cfg.enabled:
        snapshot.errors = ["macro.enabled=false：已停用總體經濟資料"]
        return snapshot

    symbols = [s for s in (price_symbols or []) if s] or list(DEFAULT_MACRO_SYMBOLS)

    if force or not snapshot.is_fresh("bls", cfg.monthly_ttl_hours):
        snapshot = _refresh_bls(snapshot, cfg)
    if force or not snapshot.is_fresh("fred", cfg.monthly_ttl_hours):
        snapshot = _refresh_fred(snapshot, cfg)
    if force or not snapshot.is_fresh("prices", cfg.market_ttl_hours):
        snapshot = _refresh_prices(snapshot, cfg, symbols)
    if force or not snapshot.is_fresh("fed_rss", cfg.market_ttl_hours):
        snapshot = _refresh_fed(snapshot, cfg)
    if force or not snapshot.is_fresh("news", cfg.market_ttl_hours):
        snapshot = _refresh_news(snapshot, cfg)

    snapshot.fetched_at = _now_iso()
    # 價格區塊更新後才補 10Y 殖利率（FRED 未設定或失敗時的 ^TNX 回退）
    snapshot = apply_tnx_fallback(snapshot)
    snapshot.errors = [
        f"{SECTION_LABELS.get(name, name)}：{info.get('error')}"
        for name, info in snapshot.sections.items()
        if isinstance(info, dict) and not info.get("ok") and info.get("error")
    ]
    return snapshot


# ---------- 報告文字（純函式） ----------

PRICE_LABELS = {
    "^TNX": "美 10 年期殖利率（Yahoo 即時）",
    "DX-Y.NYB": "美元指數",
    "CL=F": "WTI 原油",
    "GC=F": "黃金",
    "^VIX": "VIX",
}


def _history_change(history: Optional[dict], window: int) -> Optional[float]:
    """由快照保存的日線歷史計算 window 期漲跌幅（%）。"""
    closes = (history or {}).get("closes") or []
    if len(closes) < window + 1:
        return None
    base = closes[-1 - window]
    if not base:
        return None
    return (closes[-1] / base - 1.0) * 100.0


def _monthly_price_bits(snapshot: MacroSnapshot) -> List[str]:
    """物價段落：CPI／核心 CPI／PCE／核心 PCE。"""
    bits = []
    for key in ("cpi", "core_cpi", "pce", "core_pce"):
        point = snapshot.monthly.get(key)
        if not point or point.value is None:
            continue
        bit = f"{point.label} 年增 {_fmt(point.yoy, 2, '%')}"
        if point.prev_yoy is not None:
            bit += f"（前值 {_fmt(point.prev_yoy, 2, '%')}）"
        if point.mom is not None:
            bit += f"、月增 {_fmt(point.mom, 2, '%')}"
        if point.period:
            bit += f"〔{point.period}〕"
        bits.append(bit)
    return bits


def _monthly_labor_bits(snapshot: MacroSnapshot) -> List[str]:
    """就業段落：非農／失業率／平均時薪。"""
    bits = []
    nfp = snapshot.monthly.get("nfp")
    if nfp and nfp.change is not None:
        bit = f"非農就業月增 {nfp.change:+.0f} 千人"
        if nfp.prev_change is not None:
            bit += f"（前月 {nfp.prev_change:+.0f}）"
        bits.append(bit)
    unrate = snapshot.monthly.get("unemployment_rate")
    if unrate and unrate.value is not None:
        bit = f"失業率 {_fmt(unrate.value, 2, '%')}"
        if unrate.change_3m is not None:
            bit += f"（較 3 個月前 {unrate.change_3m:+.2f}pp）"
        bits.append(bit)
    wage = snapshot.monthly.get("avg_hourly_earnings")
    if wage and wage.yoy is not None:
        bits.append(f"平均時薪年增 {_fmt(wage.yoy, 2, '%')}")
    return bits


def _fed_rate_bit(snapshot: MacroSnapshot) -> Optional[str]:
    """Fed 政策利率 + 上次調整方向（升息／降息幾碼）。"""
    upper = snapshot.rates.get("fed_funds_upper")
    if upper is None:
        return None
    lower = snapshot.rates.get("fed_funds_lower")
    band = f"{_fmt(lower, 2)}~{_fmt(upper, 2)}%" if lower is not None else f"{_fmt(upper, 2)}%"
    bit = f"Fed 政策利率 {band}"
    previous = snapshot.rates.get("fed_funds_prev")
    if previous is not None:
        diff = previous - upper
        action = "降息" if diff > 0 else "升息"
        steps = abs(diff) / 0.25
        when = snapshot.rates.get("fed_funds_changed_at") or "日前"
        bit += f"（{when} {action} {steps:g} 碼）"
    return bit


def _rate_bits(snapshot: MacroSnapshot) -> List[str]:
    """利率段落：政策利率／10Y 殖利率／通膨預期。"""
    bits = []
    fed_bit = _fed_rate_bit(snapshot)
    if fed_bit:
        bits.append(fed_bit)
    dgs10 = snapshot.rates.get("dgs10")
    if dgs10 is not None:
        bit = f"10 年期公債殖利率 {_fmt(dgs10, 3, '%')}"
        change = snapshot.rates.get("dgs10_bp_20d")
        if change is not None:
            bit += f"（近 20 交易日 {change:+.0f}bp）"
        bits.append(bit)
    for key, label in (("inflation_expect_10y", "10 年期通膨預期"),
                       ("inflation_expect_1y", "1 年期通膨預期")):
        if key == "inflation_expect_1y":
            point = snapshot.monthly.get(key)
            if not point or point.value is None:
                continue
            bit = f"{label} {_fmt(point.value, 2, '%')}"
            if point.change is not None:
                bit += f"（較前月 {point.change:+.2f}pp）"
            bits.append(bit)
        else:
            value = snapshot.rates.get(key)
            if value is None:
                continue
            bit = f"{label} {_fmt(value, 2, '%')}"
            change = snapshot.rates.get("inflation_expect_10y_bp_20d")
            if change is not None:
                bit += f"（近 20 交易日 {change:+.0f}bp）"
            bits.append(bit)
    return bits


def _market_bits(snapshot: MacroSnapshot) -> List[str]:
    """市場段落：由快照中的 macro 資產價格組出（含 20 日變化）。

    ^TNX 若已由利率段落（dgs10，FRED 或 ^TNX 回退）呈現，就不再重複列出。
    """
    bits = []
    rates_has_10y = snapshot.rates.get("dgs10") is not None
    for symbol in DEFAULT_MACRO_SYMBOLS:
        if symbol == "^TNX" and rates_has_10y:
            continue
        price = snapshot.prices.get(symbol)
        if price is None:
            continue
        label = PRICE_LABELS.get(symbol, symbol)
        bit = f"{label} {_fmt(price, 2)}"
        change = _history_change(snapshot.history.get(symbol), 20)
        if change is not None:
            bit += f"（近 20 交易日 {change:+.2f}%）"
        bits.append(bit)
    return bits


def _event_bits(snapshot: MacroSnapshot, limit: int = 3) -> List[str]:
    """Fed 事件：FOMC 聲明／官員談話（RSS 標題）。"""
    bits = []
    for event in snapshot.events[:limit]:
        title = str(event.get("title", "")).strip()
        if not title:
            continue
        date = str(event.get("date") or "").strip() or "日期未標示"
        source = "FOMC／聲明" if event.get("source") == "monetary" else "官員談話"
        bits.append(f"{date} {title}（{source}）")
    return bits


def _news_bits(snapshot: MacroSnapshot, limit: int = 3) -> List[str]:
    """關鍵字過濾後的市場新聞標題。"""
    bits = []
    for item in snapshot.news[:limit]:
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        meta = "，".join(x for x in (str(item.get("publisher") or "").strip(),
                                    str(item.get("date") or "").strip()) if x)
        bits.append(f"〈{title}〉（{meta}）" if meta else f"〈{title}〉")
    return bits


def build_macro_lines(snapshot: Optional[MacroSnapshot], cfg: Optional[MacroConfig] = None) -> List[str]:
    """把快照整理成給 AI 的「總體環境」文字行（純函式，缺值自動省略）。

    回傳 [] 代表完全沒有總體資料可用，呼叫端應整段略過。
    """
    if snapshot is None:
        return []
    groups = [
        ("物價", _monthly_price_bits(snapshot)),
        ("就業", _monthly_labor_bits(snapshot)),
        ("利率", _rate_bits(snapshot)),
        ("市場", _market_bits(snapshot)),
        ("Fed 動態", _event_bits(snapshot)),
        ("市場新聞", _news_bits(snapshot)),
    ]
    lines = [f"{name}：" + "；".join(bits) for name, bits in groups if bits]
    if not lines:
        return []
    header = f"當前總體環境（資料時間 {snapshot.data_date()}；來源：BLS／FRED／Fed RSS／Yahoo）"
    result = [header] + lines
    missing = snapshot.unavailable()
    if missing:
        result.append("本回合無法取得：" + "、".join(missing))
    result.append("提醒：以上僅為背景脈絡，不得當成短線漲跌的單一歸因；"
                  "沒有列出的數字一律不得推測、補值或聲稱市場預期。")
    return result



