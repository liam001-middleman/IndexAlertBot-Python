#!/usr/bin/env python3
"""IndexAlertBot 主程式。

流程：載入設定 → 依 TTL 更新總體快照 → 抓取行情 → 計算指標 → 判斷警報
      （技術面 + 總體）→ 比對狀態（只留新觸發）→ DeepSeek 生成中文報告
      → Telegram 發送 → 儲存狀態。

總體快照（macro_snapshot.json）由「擁有快照的市場」負責更新；其餘 workflow
一律加 --macro-readonly 只讀既有快照，避免重複抓取與重複通知。

用法：
    python main.py                 # 正式執行（全部市場，僅建議本機測試）
    python main.py --market us,macro     # US workflow：行情 + 總體（更新快照）
    python main.py --market tw --macro-readonly    # TW workflow：只讀總體快照
    python main.py --dry-run       # 預覽：抓資料與警報，但不發送、不更新狀態
"""
import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# 強制使用 UTF-8 輸出，避免在非 UTF-8 主控台（如 Windows Big5/cp950）
# 且 stdout 被 pipe 時，中文 / emoji 輸出拋出 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass

from src.alerts import get_new_alerts
from src.config import load_config
from src.correlations import compute_correlations, detect_divergences, format_correlation_lines
from src.fetcher import FetchError, get_market_data
from src.indicators import compute_ma, compute_ma_deviation, compute_rsi
from src.macro import build_macro_lines, load_snapshot, refresh_snapshot, save_snapshot
from src.macro_alerts import format_macro_alert_lines, get_new_macro_alerts
from src.models import Quote
from src.notifier import NotifyError, send_telegram_message
from src.regime import format_regime_line, regime_from_snapshot
from src.reporter import ReporterError, build_fallback_report, generate_report
from src.risk_score import compute_risk_score
from src.state import StateStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")

ASSET_HISTORY_DAYS = 120   # 保存給相關性計算的日數（需大於最大相關窗口）
RISK_LINE_LIMIT = 6        # 報告中最多列出的風險分數行數


def build_quote(asset, md, cfg) -> Quote:
    """把抓取結果 + 技術指標組合成 Quote。"""
    close = md.df["Close"]
    rsi = compute_rsi(close, cfg.rsi_period)
    ma = compute_ma(close, cfg.ma_periods)
    deviation = compute_ma_deviation(md.price, ma)
    return Quote(
        symbol=asset.symbol,
        name=asset.name,
        market=asset.market,
        price=md.price,
        previous_close=md.previous_close,
        open_price=md.open_price,
        change_pct=md.change_pct,
        rsi=rsi,
        ma=ma,
        ma_deviation_pct=deviation,
        timestamp=md.timestamp,
    )


def history_from_frame(df, days: int = ASSET_HISTORY_DAYS) -> dict:
    """把日線 DataFrame 轉成 {dates, closes}（日期格式對齊總體快照的 YYYY-MM-DD）。"""
    if df is None or df.empty:
        return {}
    closes = [float(v) for v in df["Close"].tolist()][-days:]
    dates = [ts.strftime("%Y-%m-%d") for ts in df.index][-days:]
    if not dates or len(dates) != len(closes):
        return {}
    return {"dates": dates, "closes": closes}


def build_risk_lines(quotes, snapshot, weights, symbols=None, limit: int = RISK_LINE_LIMIT) -> list:
    """組出風險分數文字行：先列有警報的標的，其餘依分數高到低補滿 limit 筆。"""
    wanted = set(symbols or [])
    selected = [q for q in quotes if q.symbol in wanted] or list(quotes)
    scored = [(compute_risk_score(q, snapshot, weights), q) for q in selected]
    scored.sort(key=lambda pair: -(pair[0].total if pair[0].total is not None else -1.0))
    return [risk.to_line(q.name) for risk, q in scored[:limit]]


def snapshot_changed(existing, snapshot) -> bool:
    """比較快照內容（忽略 fetched_at），避免每次執行都改寫檔案造成無意義 commit。"""

    def payload(snap):
        if snap is None:
            return None
        data = snap.to_dict()
        data.pop("fetched_at", None)
        return data

    return payload(existing) != payload(snapshot)


def main() -> int:
    parser = argparse.ArgumentParser(description="IndexAlertBot 定期行情警報")
    parser.add_argument("--config", default=str(ROOT_DIR / "config.yaml"), help="設定檔路徑")
    parser.add_argument("--state", default=str(ROOT_DIR / "alert_state.json"), help="狀態檔路徑")
    parser.add_argument("--market", default="",
                        help="只處理指定市場（逗號分隔：us,tw,crypto），留空 = 全部")
    parser.add_argument("--dry-run", action="store_true", help="預覽：不發送通知、不更新狀態")
    parser.add_argument("--macro-file", default="",
                        help="總體快照檔路徑（預設 = config.yaml 的 macro.cache_file）")
    parser.add_argument("--macro-readonly", action="store_true",
                        help="只讀既有總體快照：不連網、不更新（TW / Crypto workflow 使用）")
    parser.add_argument("--macro-force", action="store_true",
                        help="忽略 TTL，強制重抓所有總體資料區塊")
    args = parser.parse_args()

    cfg = load_config(args.config)
    assets = cfg.filter_assets(args.market)
    if not assets:
        logger.error("沒有符合市場篩選的 assets（--market=%s）", args.market or "全部")
        return 1

    state = StateStore(None if args.dry_run else args.state)
    state.load()

    # 0. 總體快照：依 TTL 只重抓過期區塊；readonly 模式完全不連網也不寫檔
    macro_path = Path(args.macro_file) if args.macro_file else ROOT_DIR / cfg.macro.cache_file
    existing_snapshot = load_snapshot(macro_path)
    # 快照要抓的價格標的以 config.yaml 的 market: macro 資產為準（讀不到時用模組預設清單）
    macro_symbols = [a.symbol for a in cfg.assets if a.market == "macro"]
    snapshot = refresh_snapshot(cfg.macro, existing_snapshot, readonly=args.macro_readonly,
                                force=args.macro_force, price_symbols=macro_symbols)
    if args.macro_readonly and existing_snapshot is None:
        logger.warning("找不到總體快照 %s：本回合略過總體脈絡（需先由 US workflow 產生）", macro_path)
    for macro_error in snapshot.errors:
        logger.warning("總體資料：%s", macro_error)
    if (cfg.macro.enabled and not args.macro_readonly and not args.dry_run
            and snapshot_changed(existing_snapshot, snapshot)):
        save_snapshot(snapshot, macro_path)
        logger.info("總體快照已更新：%s", macro_path)

    # 1. 抓取 + 計算
    quotes = []
    fetch_errors = []
    asset_history = {}   # {symbol: {dates, closes}} 供資產 × 總體因子相關性計算
    for asset in assets:
        try:
            md = get_market_data(
                asset.symbol,
                cfg.history_period,
                cfg.history_interval,
                asset.provider,
                source_symbol=asset.source_symbol,
                convert_to_twd=asset.convert_to_twd,
            )
            quote = build_quote(asset, md, cfg.alert_config_for(asset.market))
            quotes.append(quote)
            if cfg.macro.enabled and asset.market != "macro":
                history = history_from_frame(md.df)
                if history:
                    asset_history[asset.symbol] = history
            rsi_txt = f"{quote.rsi:.1f}" if quote.rsi is not None else "N/A"
            logger.info(
                "OK  %-10s %-6s price=%-10.2f chg=%+.2f%% RSI=%s",
                asset.symbol, asset.name, quote.price, quote.change_pct, rsi_txt,
            )
        except FetchError as exc:
            fetch_errors.append(str(exc))
            logger.warning("SKIP %s: %s", asset.symbol, exc)

    if not quotes:
        logger.error("所有標的皆抓取失敗，中止執行")
        for err in fetch_errors:
            logger.error("  - %s", err)
        return 1

    # 2. 判斷警報（只回傳新觸發，並同步更新狀態）
    new_alerts = get_new_alerts(quotes, cfg.alert_config_for, state)
    # 總體警報只有「負責更新快照的市場」emit，避免 TW / Crypto 重複通知同一件事
    new_macro_alerts = []
    if cfg.macro.enabled and not args.macro_readonly:
        new_macro_alerts = get_new_macro_alerts(snapshot, cfg.macro, state)
    logger.info("本回合 %d 個標的成功，新觸發警報 技術面 %d 則／總體 %d 則",
                len(quotes), len(new_alerts), len(new_macro_alerts))

    if new_alerts or new_macro_alerts:
        # 3. 總體脈絡（總體警報、風險分數、相關性、環境背景）：全部由程式產生
        macro_alert_lines = format_macro_alert_lines(new_macro_alerts)
        risk_lines = build_risk_lines(quotes, snapshot, cfg.macro.risk_weights,
                                      symbols=[a.symbol for a in new_alerts])
        min_abs = cfg.macro.threshold("correlation_min_abs", 0.3)
        factor_history = dict(snapshot.history or {})
        if factor_history and asset_history:
            correlations = compute_correlations(asset_history, factor_history,
                                                cfg.macro.correlation_windows, min_abs)
            divergences = detect_divergences(asset_history, factor_history,
                                             cfg.macro.correlation_windows, min_abs)
            correlation_lines = format_correlation_lines(correlations, divergences)
        else:
            correlation_lines = []
        macro_lines = []
        regime_line = format_regime_line(regime_from_snapshot(snapshot))
        if regime_line:
            macro_lines.append(regime_line)
        macro_lines.extend(build_macro_lines(snapshot, cfg.macro))
        if not macro_lines:
            logger.warning("本回合沒有可用的總體脈絡（快照為空或全部區塊失敗）")

        # 4. DeepSeek 中文報告（失敗時退回原始清單）
        report = None
        if cfg.deepseek.api_key:
            try:
                report = generate_report(
                    new_alerts, cfg.deepseek.api_key, cfg.deepseek.base_url, cfg.deepseek.model,
                    macro_alert_lines=macro_alert_lines, risk_lines=risk_lines,
                    correlation_lines=correlation_lines, macro_lines=macro_lines,
                )
                logger.info("DeepSeek 報告生成成功")
            except ReporterError as exc:
                logger.warning("DeepSeek 報告失敗，改用原始清單: %s", exc)
        if report is None:
            report = build_fallback_report(
                new_alerts, macro_alert_lines=macro_alert_lines, risk_lines=risk_lines,
                correlation_lines=correlation_lines, macro_lines=macro_lines,
            )

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        title = "行情警報" if new_alerts else "總體警報"
        message = f"{title} {now_str}\n\n{report}"
        if fetch_errors:
            message += "\n\n本次部分標的抓取失敗：" + "；".join(fetch_errors)

        # 5. Telegram 發送
        if args.dry_run:
            print(message)
            logger.info("[dry-run] 預覽完成，未發送通知、未更新狀態")
        else:
            try:
                send_telegram_message(
                    cfg.telegram.bot_token, cfg.telegram.chat_id, message, cfg.telegram.parse_mode
                )
                logger.info("Telegram 通知已送出（技術面 %d 則／總體 %d 則）",
                            len(new_alerts), len(new_macro_alerts))
            except NotifyError as exc:
                # 不儲存狀態 → 下回合會重送，確保警報不遺漏
                logger.error("Telegram 發送失敗（狀態未更新，下回合重試）: %s", exc)
                return 1

        # 6. 記錄「上次出報告」快照（供下次報告顯示「上次報告價格」；
        #    只在成功送出後更新，失敗重送時仍顯示上一次成功報告的價格）
        if not args.dry_run:
            report_time = datetime.now(timezone.utc)
            for alert in new_alerts:
                state.set_last_report(alert.symbol, alert.price, report_time)
            logger.info("已更新 %d 個標的的上次報告價格快照", len(new_alerts))

    # 7. 儲存狀態（供 GitHub Actions commit 回 repo）
    if not args.dry_run:
        state.save()
        logger.info("狀態已儲存至 %s", state.path)

    if fetch_errors:
        logger.warning("部分標的抓取失敗（%d）：%s", len(fetch_errors), "; ".join(fetch_errors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
