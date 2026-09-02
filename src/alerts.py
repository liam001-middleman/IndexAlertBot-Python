"""警報規則引擎。

流程：
1. evaluate_conditions() 評估所有條件（RSI 超買/超賣、日內急漲/急跌、MA 乖離）
2. get_new_alerts() 比對 alert_state.json，只回傳「新觸發」的警報，
   並同步更新狀態（觸發→active，解除→clear）。
"""
from datetime import datetime, timezone
from typing import Callable, List

from .config import AlertConfig
from .models import Alert, Quote
from .state import StateStore

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


def _ma_alert_type(period: int, positive: bool) -> str:
    return f"ma_deviation_pos_{period}" if positive else f"ma_deviation_neg_{period}"


def _ma_alert_name(period: int, positive: bool) -> str:
    return f"MA{period} 正乖離" if positive else f"MA{period} 負乖離"


def _fmt(value: float) -> str:
    return f"{value:.2f}"


def evaluate_conditions(quote: Quote, cfg: AlertConfig) -> List[dict]:
    """評估所有警報條件，回傳條件結果清單（不做狀態比對）。

    每個元素包含 type / alert_name / triggered / value / threshold /
    severity / message / detail。
    """
    conditions: List[dict] = []

    if quote.rsi is not None:
        conditions.append(
            {
                "type": "rsi_overbought",
                "alert_name": "RSI 超買",
                "triggered": quote.rsi >= cfg.rsi_overbought,
                "value": quote.rsi,
                "threshold": cfg.rsi_overbought,
                "severity": "warning",
                "message": f"{quote.name}（{quote.symbol}）現價 {_fmt(quote.price)}，RSI({cfg.rsi_period}) {_fmt(quote.rsi)}，超買",
                "detail": (
                    f"{quote.name}（{quote.symbol}）現價 {_fmt(quote.price)}，"
                    f"RSI({cfg.rsi_period}) 為 {_fmt(quote.rsi)}，"
                    f"已達超買門檻 {_fmt(cfg.rsi_overbought)}"
                ),
            }
        )
        conditions.append(
            {
                "type": "rsi_oversold",
                "alert_name": "RSI 超賣",
                "triggered": quote.rsi <= cfg.rsi_oversold,
                "value": quote.rsi,
                "threshold": cfg.rsi_oversold,
                "severity": "warning",
                "message": f"{quote.name}（{quote.symbol}）現價 {_fmt(quote.price)}，RSI({cfg.rsi_period}) {_fmt(quote.rsi)}，超賣",
                "detail": (
                    f"{quote.name}（{quote.symbol}）現價 {_fmt(quote.price)}，"
                    f"RSI({cfg.rsi_period}) 為 {_fmt(quote.rsi)}，"
                    f"已達超賣門檻 {_fmt(cfg.rsi_oversold)}"
                ),
            }
        )

    conditions.append(
        {
            "type": "intraday_surge",
            "alert_name": "日內急漲",
            "triggered": quote.change_pct >= cfg.intraday_change_pct,
            "value": quote.change_pct,
            "threshold": cfg.intraday_change_pct,
            "severity": "critical",
            "message": f"{quote.name}（{quote.symbol}）日內漲幅 {quote.change_pct:+.1f}%",
            "detail": (
                f"{quote.name}（{quote.symbol}）現價 {_fmt(quote.price)}，"
                f"對比前收 {_fmt(quote.previous_close)}，漲幅 {quote.change_pct:+.1f}%，"
                f"超過 {_fmt(cfg.intraday_change_pct)}% 門檻"
            ),
        }
    )
    conditions.append(
        {
            "type": "intraday_drop",
            "alert_name": "日內急跌",
            "triggered": quote.change_pct <= -cfg.intraday_change_pct,
            "value": quote.change_pct,
            "threshold": -cfg.intraday_change_pct,
            "severity": "critical",
            "message": f"{quote.name}（{quote.symbol}）日內跌幅 {quote.change_pct:+.1f}%",
            "detail": (
                f"{quote.name}（{quote.symbol}）現價 {_fmt(quote.price)}，"
                f"對比前收 {_fmt(quote.previous_close)}，跌幅 {quote.change_pct:+.1f}%，"
                f"超過 {_fmt(cfg.intraday_change_pct)}% 門檻"
            ),
        }
    )

    for period in cfg.ma_periods:
        window = int(period)
        dev = quote.ma_deviation_pct.get(str(window))
        if dev is None:
            continue
        for positive in (True, False):
            triggered = (dev >= cfg.ma_deviation_pct) if positive else (dev <= -cfg.ma_deviation_pct)
            ma_value = quote.ma.get(str(window))
            conditions.append(
                {
                    "type": _ma_alert_type(window, positive),
                    "alert_name": _ma_alert_name(window, positive),
                    "triggered": triggered,
                    "value": dev,
                    "threshold": cfg.ma_deviation_pct,
                    "severity": "info",
                    "message": f"{quote.name}（{quote.symbol}）MA{window} "
                               f"{'正' if positive else '負'}乖離 {dev:+.1f}%",
                    "detail": (
                        f"{quote.name}（{quote.symbol}）現價 {_fmt(quote.price)} 對比 "
                        f"MA{window} {_fmt(ma_value) if ma_value else 'N/A'}，乖離率 {dev:+.1f}%"
                        + (f"，已達{'正' if positive else '負'}乖離門檻 {_fmt(cfg.ma_deviation_pct)}%"
                           if triggered else "")
                    ),
                }
            )

    return conditions


def get_new_alerts(
    quotes: List[Quote],
    cfg_resolver: Callable[[str], AlertConfig],
    state: StateStore,
) -> List[Alert]:
    """比對狀態，只回傳「新觸發」的警報，並同步更新狀態。

    - 條件成立且狀態為 clear（或無記錄）→ 新觸發，回傳並標記 active
    - 條件成立且狀態已 active → 重複，不回傳
    - 條件不成立且狀態為 active → 標記 clear（之後再觸發會重新通知）
    """
    now = datetime.now(timezone.utc)
    new_alerts: List[Alert] = []

    for quote in quotes:
        cfg = cfg_resolver(quote.market)
        last_report = state.get_last_report(quote.symbol) or {}
        for cond in evaluate_conditions(quote, cfg):
            key = cond["type"]
            if cond["triggered"]:
                if not state.is_active(quote.symbol, key):
                    new_alerts.append(
                        Alert(
                            symbol=quote.symbol,
                            name=quote.name,
                            market=quote.market,
                            alert_type=key,
                            alert_name=cond["alert_name"],
                            severity=cond["severity"],
                            message=cond["message"],
                            detail=cond["detail"],
                            value=float(cond["value"]),
                            threshold=float(cond["threshold"]),
                            price=quote.price,
                            rsi=quote.rsi,
                            ma=quote.ma,
                            ma_deviation_pct=quote.ma_deviation_pct,
                            last_report_price=last_report.get("price"),
                            last_report_at=last_report.get("at"),
                            triggered_at=now,
                        )
                    )
                    state.mark_active(quote.symbol, key, float(cond["value"]), now)
            else:
                if state.is_active(quote.symbol, key):
                    state.mark_clear(quote.symbol, key, cond.get("value"))

    new_alerts.sort(
        key=lambda a: (-SEVERITY_ORDER.get(a.severity, 0), a.market, a.symbol, a.alert_type)
    )
    return new_alerts

