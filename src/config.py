"""讀取 config.yaml 與環境變數，集中管理所有設定。"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent

# ---------- 總體經濟指標預設值（可被 config.yaml 的 macro 區塊逐項覆寫） ----------

# BLS 公開 API 序列代號（免金鑰）
DEFAULT_BLS_SERIES = {
    "cpi": "CUUR0000SA0",                     # CPI-U 全項目（未季調）
    "core_cpi": "CUUR0000SA0L1E",             # 核心 CPI（排除食物與能源）
    "nfp": "CES0000000001",                   # 非農就業總人數（季調）
    "unemployment_rate": "LNS14000000",       # 失業率（季調）
    "avg_hourly_earnings": "CES0500000003",   # 平均時薪（季調）
}

# FRED 序列代號（需 FRED_API_KEY，未設定時整組略過）
DEFAULT_FRED_SERIES = {
    "pce": "PCEPI",
    "core_pce": "PCEPILFE",
    "fed_funds_upper": "DFEDTARU",
    "fed_funds_lower": "DFEDTARL",
    "dgs10": "DGS10",
    "inflation_expect_10y": "T10YIE",
    "inflation_expect_1y": "EXPINF1YR",
}

# 巨集警報門檻
DEFAULT_MACRO_THRESHOLDS = {
    "cpi_yoy_high": 3.0,        # CPI 年增率（%）
    "core_cpi_yoy_high": 3.0,   # 核心 CPI 年增率（%）
    "cpi_mom_accel_pp": 0.2,    # 月增率較前月加速（百分點）
    "pce_yoy_high": 2.5,        # PCE 年增率（%）
    "us10y_bp_20d": 40.0,       # 10Y 殖利率 20 日變動（bp）
    "dxy_pct_20d": 2.0,         # 美元指數 20 日漲跌幅（%）
    "wti_pct_20d": 10.0,        # WTI 原油 20 日漲跌幅（%）
    "unrate_jump_pp": 0.3,      # 失業率 3 個月內上升（百分點）
    "wage_yoy_high": 4.0,       # 平均時薪年增率（%）
    "correlation_min_abs": 0.3,  # |r| 低於此值不得聲稱背離
}

# 風險分數權重（技術面 : 總體面）
DEFAULT_RISK_WEIGHTS = {"technical": 0.6, "macro": 0.4}

# 宏觀新聞關鍵字（用於過濾 yfinance .news）
DEFAULT_NEWS_KEYWORDS = [
    "Fed", "FOMC", "inflation", "CPI", "PCE", "rate cut", "rate hike",
    "tariff", "recession", "利率", "通膨", "關稅", "降息", "升息",
]



@dataclass
class AssetConfig:
    symbol: str
    name: str
    market: str  # us / tw / crypto（總體資產不放這裡，見 MacroConfig.price_symbols）
    provider: str = "yahoo"  # yahoo（Yahoo Finance） / max（MAX 交易所台幣報價）
    source_symbol: Optional[str] = None  # 實際抓取代號（如 BTC-USD）；None = 直接用 symbol
    convert_to_twd: bool = False  # True：抓 USD 日線並乘 USD/TWD 匯率換算成台幣


@dataclass
class AlertConfig:
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    intraday_change_pct: float = 5.0
    ma_deviation_pct: float = 5.0
    ma_periods: list = field(default_factory=lambda: [20, 60, 200])
    ma_cross_alerts: bool = False  # 站上／跌破均線警報（預設關閉，需逐市場開啟）
    ma_cross_threshold: float = 0.0  # 站上／跌破的死區（%）：乖離需超過此值才觸發

    def merged(self, override: Optional[dict]) -> "AlertConfig":
        """以 override 覆寫目前數值，回傳新的 AlertConfig。"""
        data = {
            "rsi_period": self.rsi_period,
            "rsi_overbought": self.rsi_overbought,
            "rsi_oversold": self.rsi_oversold,
            "intraday_change_pct": self.intraday_change_pct,
            "ma_deviation_pct": self.ma_deviation_pct,
            "ma_periods": list(self.ma_periods),
            "ma_cross_alerts": self.ma_cross_alerts,
            "ma_cross_threshold": self.ma_cross_threshold,
        }
        if override:
            data.update({k: v for k, v in override.items() if v is not None})
        return AlertConfig(**data)


@dataclass
class DeepSeekConfig:
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"


@dataclass
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""
    parse_mode: str = ""  # 留空 = 純文字；可用 HTML


@dataclass
class ReportConfig:
    """報告輸出設定。"""

    style: str = "intuitive"  # intuitive（白話三段式） / technical（指標報表）
    include_raw_appendix: bool = True  # 報告末端附上程式產生的原始數據區塊
    max_chars: int = 1800  # 寫進 prompt 的長度指引


@dataclass
class MacroConfig:
    """總體經濟指標（BLS / FRED / Fed RSS）設定。"""

    enabled: bool = True
    cache_file: str = "macro_snapshot.json"
    monthly_ttl_hours: int = 24
    market_ttl_hours: int = 1
    price_symbols: list = field(default_factory=list)  # 總體價格代號（空 = 用 src/macro.py 的預設清單）
    fred_api_key: str = ""  # 環境變數 FRED_API_KEY（未設定時 FRED 系列自動略過）
    bls_series: dict = field(default_factory=lambda: dict(DEFAULT_BLS_SERIES))
    fred_series: dict = field(default_factory=lambda: dict(DEFAULT_FRED_SERIES))
    thresholds: dict = field(default_factory=lambda: dict(DEFAULT_MACRO_THRESHOLDS))
    correlation_windows: list = field(default_factory=lambda: [20, 60])
    news_keywords: list = field(default_factory=lambda: list(DEFAULT_NEWS_KEYWORDS))
    risk_weights: dict = field(default_factory=lambda: dict(DEFAULT_RISK_WEIGHTS))

    @property
    def has_fred(self) -> bool:
        """是否已設定 FRED API Key（決定 PCE／政策利率／通膨預期能否取得）。"""
        return bool(self.fred_api_key)

    def threshold(self, key: str, default: float = 0.0) -> float:
        """取得指定門檻值，缺少時回傳 default。"""
        value = self.thresholds.get(key)
        return default if value is None else float(value)


@dataclass
class Config:
    assets: list
    alerts_defaults: AlertConfig
    alerts_overrides: dict
    history_period: str = "2y"
    history_interval: str = "1d"
    deepseek: DeepSeekConfig = field(default_factory=DeepSeekConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    macro: MacroConfig = field(default_factory=MacroConfig)

    def alert_config_for(self, market: str) -> AlertConfig:
        """取得某市場的有效警報設定（預設值 + 市場覆寫）。"""
        return self.alerts_defaults.merged(self.alerts_overrides.get(market))

    def filter_assets(self, markets: str) -> list:
        """依市場篩選 assets。markets 為逗號分隔（如 us,tw,crypto），留空回傳全部。"""
        wanted = {m.strip().lower() for m in (markets or "").split(",") if m.strip()}
        if not wanted:
            return self.assets
        return [a for a in self.assets if a.market in wanted]


def load_config(path: Optional[str] = None) -> Config:
    """從 YAML 檔案 + 環境變數載入設定。"""
    if path is None:
        path = ROOT_DIR / "config.yaml"
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    assets = [
        AssetConfig(
            symbol=str(a["symbol"]),
            name=str(a.get("name", a["symbol"])),
            market=str(a.get("market", "us")),
            provider=str(a.get("provider", "yahoo")),
            source_symbol=(a.get("source_symbol") or None),
            convert_to_twd=bool(a.get("convert_to_twd", False)),
        )
        for a in raw.get("assets", [])
    ]

    alert_raw = raw.get("alerts", {}) or {}
    defaults = alert_raw.get("defaults", {}) or {}
    alerts_defaults = AlertConfig(
        rsi_period=int(defaults.get("rsi_period", 14)),
        rsi_overbought=float(defaults.get("rsi_overbought", 70)),
        rsi_oversold=float(defaults.get("rsi_oversold", 30)),
        intraday_change_pct=float(defaults.get("intraday_change_pct", 5.0)),
        ma_deviation_pct=float(defaults.get("ma_deviation_pct", 5.0)),
        ma_periods=[int(p) for p in defaults.get("ma_periods", [20, 60, 200])],
        ma_cross_alerts=bool(defaults.get("ma_cross_alerts", False)),
        ma_cross_threshold=float(defaults.get("ma_cross_threshold", 0.0)),
    )
    alerts_overrides = alert_raw.get("overrides", {}) or {}

    hist_raw = raw.get("history", {}) or {}
    ds_raw = raw.get("deepseek", {}) or {}
    tg_raw = raw.get("telegram", {}) or {}
    report_raw = raw.get("report", {}) or {}
    macro_raw = raw.get("macro", {}) or {}

    # macro 子設定採「與預設值合併」，方便 config.yaml 只覆寫單一項目
    bls_series = dict(DEFAULT_BLS_SERIES)
    bls_series.update({str(k): str(v) for k, v in (macro_raw.get("bls_series") or {}).items()})
    fred_series = dict(DEFAULT_FRED_SERIES)
    fred_series.update({str(k): str(v) for k, v in (macro_raw.get("fred_series") or {}).items()})
    thresholds = dict(DEFAULT_MACRO_THRESHOLDS)
    for key, value in (macro_raw.get("thresholds") or {}).items():
        if value is not None:
            thresholds[str(key)] = float(value)
    risk_weights = dict(DEFAULT_RISK_WEIGHTS)
    for key, value in (macro_raw.get("risk_weights") or {}).items():
        if value is not None:
            risk_weights[str(key)] = float(value)
    corr_windows = [int(w) for w in (macro_raw.get("correlation_windows") or [20, 60]) if int(w) > 0]
    news_keywords = [str(k) for k in (macro_raw.get("news_keywords") or DEFAULT_NEWS_KEYWORDS)]

    return Config(
        assets=assets,
        alerts_defaults=alerts_defaults,
        alerts_overrides=alerts_overrides,
        history_period=str(hist_raw.get("period", "2y")),
        history_interval=str(hist_raw.get("interval", "1d")),
        deepseek=DeepSeekConfig(
            api_key=os.environ.get("DEEPSEEK_API_KEY", "").strip(),
            base_url=str(ds_raw.get("base_url", "https://api.deepseek.com")).rstrip("/"),
            model=str(ds_raw.get("model", "deepseek-chat")),
        ),
        telegram=TelegramConfig(
            bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
            parse_mode=str(tg_raw.get("parse_mode", "")),
        ),
        report=ReportConfig(
            style=str(report_raw.get("style", "intuitive")).strip().lower() or "intuitive",
            include_raw_appendix=bool(report_raw.get("include_raw_appendix", True)),
            max_chars=int(report_raw.get("max_chars", 1800)),
        ),
        macro=MacroConfig(
            enabled=bool(macro_raw.get("enabled", True)),
            cache_file=str(macro_raw.get("cache_file", "macro_snapshot.json")),
            monthly_ttl_hours=int(macro_raw.get("monthly_ttl_hours", 24)),
            market_ttl_hours=int(macro_raw.get("market_ttl_hours", 1)),
            price_symbols=[str(s).strip() for s in (macro_raw.get("price_symbols") or [])
                           if str(s).strip()],
            fred_api_key=os.environ.get("FRED_API_KEY", "").strip(),
            bls_series=bls_series,
            fred_series=fred_series,
            thresholds=thresholds,
            correlation_windows=corr_windows,
            news_keywords=news_keywords,
            risk_weights=risk_weights,
        ),
    )
