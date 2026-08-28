"""讀取 config.yaml 與環境變數，集中管理所有設定。"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent


@dataclass
class AssetConfig:
    symbol: str
    name: str
    market: str  # us / tw / crypto


@dataclass
class AlertConfig:
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    intraday_change_pct: float = 5.0
    ma_deviation_pct: float = 5.0
    ma_periods: list = field(default_factory=lambda: [20, 60, 200])

    def merged(self, override: Optional[dict]) -> "AlertConfig":
        """以 override 覆寫目前數值，回傳新的 AlertConfig。"""
        data = {
            "rsi_period": self.rsi_period,
            "rsi_overbought": self.rsi_overbought,
            "rsi_oversold": self.rsi_oversold,
            "intraday_change_pct": self.intraday_change_pct,
            "ma_deviation_pct": self.ma_deviation_pct,
            "ma_periods": list(self.ma_periods),
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
class Config:
    assets: list
    alerts_defaults: AlertConfig
    alerts_overrides: dict
    history_period: str = "2y"
    history_interval: str = "1d"
    deepseek: DeepSeekConfig = field(default_factory=DeepSeekConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)

    def alert_config_for(self, market: str) -> AlertConfig:
        """取得某市場的有效警報設定（預設值 + 市場覆寫）。"""
        return self.alerts_defaults.merged(self.alerts_overrides.get(market))


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
    )
    alerts_overrides = alert_raw.get("overrides", {}) or {}

    hist_raw = raw.get("history", {}) or {}
    ds_raw = raw.get("deepseek", {}) or {}
    tg_raw = raw.get("telegram", {}) or {}

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
    )
