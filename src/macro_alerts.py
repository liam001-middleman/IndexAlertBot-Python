"""總體經濟警報規則與去重。

與技術面警報（src/alerts.py）刻意分開，因為：
- 來源是 macro 快照（月度統計／利率／商品價格），不是單一行情標的；
- 每一則警報是「國家級」事件（例如 CPI 年增偏高），不屬於任何一檔股票，
  因此在 alert_state 中以虛擬標的 MACRO_STATE_SYMBOL 保存狀態。

規則（門檻皆來自 config.yaml 的 macro.thresholds，可調）：
1. cpi_yoy_high          CPI 年增率 >= cpi_yoy_high
2. core_cpi_yoy_high     核心 CPI 年增率 >= core_cpi_yoy_high
3. cpi_mom_acceleration  CPI 月增率較前月加速 >= cpi_mom_accel_pp
4. pce_yoy_high          PCE 年增率 >= pce_yoy_high
5. inflation_expect_up   10 年期通膨預期 20 日上升（bp）
6. unrate_jump           失業率 3 個月內上升 >= unrate_jump_pp
7. nfp_negative          非農就業月增為負
8. wage_yoy_high         平均時薪年增率 >= wage_yoy_high
9. us10y_surge           10Y 殖利率 20 日上升 >= us10y_bp_20d
10. us10y_drop           10Y 殖利率 20 日下降 >= us10y_bp_20d
11. fed_rate_change      Fed 政策利率與上次不同（升息／降息）
12. dxy_surge            美元指數 20 日漲幅 >= dxy_pct_20d
13. wti_surge            WTI 原油 20 日漲幅 >= wti_pct_20d

限制：免費資料源沒有「市場預期（consensus）」數值，因此不會出現
「高於／低於預期」這類判斷，只有實際數值與門檻的比較。
"""
from datetime import datetime, timezone
from typing import List, Optional

from .config import MacroConfig
from .correlations import window_change
from .models import MacroAlert
from .state import StateStore

MACRO_STATE_SYMBOL = "MACRO"  # alert_state 中代表「總體經濟」的虛擬標的
SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


def _fmt(value: Optional[float], digits: int = 2, suffix: str = "") -> str:
    """數值格式化（None 顯示 N/A）。刻意在本模組自帶一份，不跨模組存取私有函式。"""
    if value is None:
        return "N/A"
    return f"{value:.{digits}f}{suffix}"


def _condition(rule_type: str, name: str, triggered: bool, value: Optional[float],
               threshold: float, severity: str, detail: str) -> dict:
    """組出與 alerts.evaluate_conditions 相同結構的條件 dict。"""
    return {
        "type": rule_type,
        "alert_name": name,
        "triggered": bool(triggered),
        "value": value,
        "threshold": threshold,
        "severity": severity,
        "message": f"{name}：{detail}",
        "detail": detail,
    }


def _price_conditions(snapshot, cfg: MacroConfig) -> List[dict]:
    """物價類規則（CPI／核心 CPI／PCE／通膨預期）。"""
    conditions: List[dict] = []

    cpi = snapshot.monthly.get("cpi")
    if cpi and cpi.yoy is not None:
        limit = cfg.threshold("cpi_yoy_high", 3.0)
        conditions.append(_condition(
            "cpi_yoy_high", "CPI 年增偏高", cpi.yoy >= limit, cpi.yoy, limit, "warning",
            f"CPI 年增率 {_fmt(cpi.yoy, 2, '%')}（前值 {_fmt(cpi.prev_yoy, 2, '%')}），"
            f"資料期別 {cpi.period}，門檻 {_fmt(limit, 2, '%')}",
        ))

    core = snapshot.monthly.get("core_cpi")
    if core and core.yoy is not None:
        limit = cfg.threshold("core_cpi_yoy_high", 3.0)
        conditions.append(_condition(
            "core_cpi_yoy_high", "核心 CPI 年增偏高", core.yoy >= limit, core.yoy, limit, "warning",
            f"核心 CPI 年增率 {_fmt(core.yoy, 2, '%')}（前值 {_fmt(core.prev_yoy, 2, '%')}），"
            f"資料期別 {core.period}，門檻 {_fmt(limit, 2, '%')}",
        ))

    if cpi and cpi.mom is not None and cpi.prev_mom is not None:
        limit = cfg.threshold("cpi_mom_accel_pp", 0.2)
        accel = cpi.mom - cpi.prev_mom
        conditions.append(_condition(
            "cpi_mom_acceleration", "CPI 月增率加速", accel >= limit, accel, limit, "info",
            f"CPI 月增率 {_fmt(cpi.mom, 2, '%')}，較前月加速 {accel:+.2f}pp"
            f"（前月月增 {_fmt(cpi.prev_mom, 2, '%')}），門檻 {_fmt(limit, 2)}pp",
        ))

    pce = snapshot.monthly.get("pce")
    if pce and pce.yoy is not None:
        limit = cfg.threshold("pce_yoy_high", 2.5)
        conditions.append(_condition(
            "pce_yoy_high", "PCE 年增偏高", pce.yoy >= limit, pce.yoy, limit, "warning",
            f"PCE 年增率 {_fmt(pce.yoy, 2, '%')}（前值 {_fmt(pce.prev_yoy, 2, '%')}），"
            f"資料期別 {pce.period}，門檻 {_fmt(limit, 2, '%')}",
        ))

    bp_change = snapshot.rates.get("inflation_expect_10y_bp_20d")
    if bp_change is not None:
        limit = 20.0
        conditions.append(_condition(
            "inflation_expect_up", "通膨預期升溫", bp_change >= limit, bp_change, limit, "info",
            f"10 年期通膨預期 {_fmt(snapshot.rates.get('inflation_expect_10y'), 2, '%')}，"
            f"近 20 交易日 {bp_change:+.0f}bp，門檻 {limit:.0f}bp",
        ))
    return conditions


def _labor_conditions(snapshot, cfg: MacroConfig) -> List[dict]:
    """就業類規則（失業率跳升／非農轉負／薪資年增偏高）。"""
    conditions: List[dict] = []

    unrate = snapshot.monthly.get("unemployment_rate")
    if unrate and unrate.change_3m is not None:
        limit = cfg.threshold("unrate_jump_pp", 0.3)
        conditions.append(_condition(
            "unrate_jump", "失業率三個月內跳升", unrate.change_3m >= limit,
            unrate.change_3m, limit, "critical",
            f"失業率 {_fmt(unrate.value, 2, '%')}（資料期別 {unrate.period}），"
            f"較 3 個月前 {unrate.change_3m:+.2f}pp，門檻 {_fmt(limit, 2)}pp",
        ))

    nfp = snapshot.monthly.get("nfp")
    if nfp and nfp.change is not None:
        limit = 0.0
        conditions.append(_condition(
            "nfp_negative", "非農就業轉負", nfp.change < limit, nfp.change, limit, "warning",
            f"非農就業月增 {nfp.change:+.0f} 千人（資料期別 {nfp.period}，"
            f"前月 {_fmt(nfp.prev_change, 0)} 千人），門檻 0 千人",
        ))

    wage = snapshot.monthly.get("avg_hourly_earnings")
    if wage and wage.yoy is not None:
        limit = cfg.threshold("wage_yoy_high", 4.0)
        conditions.append(_condition(
            "wage_yoy_high", "薪資年增偏高", wage.yoy >= limit, wage.yoy, limit, "info",
            f"平均時薪年增率 {_fmt(wage.yoy, 2, '%')}（資料期別 {wage.period}），"
            f"門檻 {_fmt(limit, 2, '%')}",
        ))
    return conditions


def _rate_conditions(snapshot, cfg: MacroConfig) -> List[dict]:
    """利率類規則（10Y 殖利率急升／急降、Fed 政策利率調整）。"""
    conditions: List[dict] = []

    bp_change = snapshot.rates.get("dgs10_bp_20d")
    if bp_change is not None:
        limit = cfg.threshold("us10y_bp_20d", 40.0)
        level = _fmt(snapshot.rates.get("dgs10"), 3, "%")
        conditions.append(_condition(
            "us10y_surge", "10Y 殖利率急升", bp_change >= limit, bp_change, limit, "critical",
            f"10 年期公債殖利率 {level}，近 20 交易日 {bp_change:+.0f}bp，門檻 {limit:.0f}bp",
        ))
        conditions.append(_condition(
            "us10y_drop", "10Y 殖利率急降", bp_change <= -limit, bp_change, -limit, "warning",
            f"10 年期公債殖利率 {level}，近 20 交易日 {bp_change:+.0f}bp，門檻 {-limit:.0f}bp",
        ))

    upper = snapshot.rates.get("fed_funds_upper")
    previous = snapshot.rates.get("fed_funds_prev")
    if upper is not None and previous is not None:
        changed = abs(float(upper) - float(previous)) > 1e-9
        detail = (f"Fed 政策利率 {_fmt(snapshot.rates.get('fed_funds_lower'), 2)}~"
                  f"{_fmt(upper, 2, '%')}，上次為 {_fmt(previous, 2, '%')}"
                  f"（調整日 {snapshot.rates.get('fed_funds_changed_at') or '未標示'}）")
        if changed:
            diff = float(upper) - float(previous)
            action = "降息" if diff < 0 else "升息"
            detail += f"，{action} {abs(diff) / 0.25:g} 碼"
        conditions.append(_condition(
            "fed_rate_change", "Fed 政策利率調整", changed, upper, previous, "critical", detail,
        ))
    return conditions


def _market_conditions(snapshot, cfg: MacroConfig) -> List[dict]:
    """市場類規則（美元指數急升、原油急漲）。"""
    conditions: List[dict] = []

    dxy_change = window_change((snapshot.history.get("DX-Y.NYB") or {}).get("closes") or [], 20)
    if dxy_change is not None:
        limit = cfg.threshold("dxy_pct_20d", 2.0)
        conditions.append(_condition(
            "dxy_surge", "美元指數急升", dxy_change >= limit, dxy_change, limit, "warning",
            f"美元指數 {_fmt(snapshot.prices.get('DX-Y.NYB'), 2)}，"
            f"近 20 交易日 {dxy_change:+.2f}%，門檻 {_fmt(limit, 2, '%')}",
        ))

    wti_change = window_change((snapshot.history.get("CL=F") or {}).get("closes") or [], 20)
    if wti_change is not None:
        limit = cfg.threshold("wti_pct_20d", 10.0)
        conditions.append(_condition(
            "wti_surge", "原油急漲", wti_change >= limit, wti_change, limit, "warning",
            f"WTI 原油 {_fmt(snapshot.prices.get('CL=F'), 2)}，"
            f"近 20 交易日 {wti_change:+.2f}%，門檻 {_fmt(limit, 2, '%')}",
        ))
    return conditions


def evaluate_macro_conditions(snapshot, cfg: MacroConfig) -> List[dict]:
    """評估所有總體警報條件，回傳條件結果清單（不做狀態比對）。

    每個元素包含 type / alert_name / triggered / value / threshold /
    severity / message / detail，與 alerts.evaluate_conditions 結構一致。
    """
    if snapshot is None:
        return []
    conditions: List[dict] = []
    conditions.extend(_price_conditions(snapshot, cfg))
    conditions.extend(_labor_conditions(snapshot, cfg))
    conditions.extend(_rate_conditions(snapshot, cfg))
    conditions.extend(_market_conditions(snapshot, cfg))
    return conditions


def get_new_macro_alerts(snapshot, cfg: MacroConfig, state: StateStore,
                         symbol: str = MACRO_STATE_SYMBOL) -> List[MacroAlert]:
    """比對 alert_state，只回傳「新觸發」的總體警報，並同步更新狀態。

    去重邏輯與技術面警報一致：觸發且原本為 clear → 新警報；恢復正常 → 標記 clear。
    """
    now = datetime.now(timezone.utc)
    new_alerts: List[MacroAlert] = []
    for cond in evaluate_macro_conditions(snapshot, cfg):
        key = cond["type"]
        if cond["triggered"]:
            if not state.is_active(symbol, key):
                value = cond["value"]
                numeric = float(value) if value is not None else 0.0
                new_alerts.append(MacroAlert(
                    key=key,
                    alert_name=cond["alert_name"],
                    severity=cond["severity"],
                    message=cond["message"],
                    detail=cond["detail"],
                    value=numeric,
                    threshold=float(cond["threshold"]),
                    triggered_at=now,
                ))
                state.mark_active(symbol, key, numeric, now)
        else:
            if state.is_active(symbol, key):
                state.mark_clear(symbol, key, cond.get("value"))
    new_alerts.sort(key=lambda a: (-SEVERITY_ORDER.get(a.severity, 0), a.key))
    return new_alerts


def format_macro_alert_lines(alerts: List[MacroAlert]) -> List[str]:
    """把新觸發的總體警報轉成給 AI／通知用的文字行。"""
    return [f"[{alert.severity}] {alert.alert_name}：{alert.detail}" for alert in alerts or []]
