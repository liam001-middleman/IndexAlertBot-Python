#!/usr/bin/env python3
"""IndexAlertBot 主程式。

流程：載入設定 → 抓取行情 → 計算指標 → 判斷警報 → 比對狀態（只留新觸發）
      → DeepSeek 生成中文報告 → Telegram 發送 → 儲存狀態。

用法：
    python main.py                 # 正式執行
    python main.py --dry-run       # 預覽：抓資料與警報，但不發送、不更新狀態
"""
import argparse
import logging
import sys
from datetime import datetime
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
from src.fetcher import FetchError, get_market_data
from src.indicators import compute_ma, compute_ma_deviation, compute_rsi
from src.models import Quote
from src.notifier import NotifyError, send_telegram_message
from src.reporter import ReporterError, build_fallback_report, generate_report
from src.state import StateStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")


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


def main() -> int:
    parser = argparse.ArgumentParser(description="IndexAlertBot 定期行情警報")
    parser.add_argument("--config", default=str(ROOT_DIR / "config.yaml"), help="設定檔路徑")
    parser.add_argument("--state", default=str(ROOT_DIR / "alert_state.json"), help="狀態檔路徑")
    parser.add_argument("--dry-run", action="store_true", help="預覽：不發送通知、不更新狀態")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if not cfg.assets:
        logger.error("config.yaml 沒有設定任何 assets")
        return 1

    state = StateStore(None if args.dry_run else args.state)
    state.load()

    # 1. 抓取 + 計算
    quotes = []
    fetch_errors = []
    for asset in cfg.assets:
        try:
            md = get_market_data(asset.symbol, cfg.history_period, cfg.history_interval)
            quote = build_quote(asset, md, cfg.alert_config_for(asset.market))
            quotes.append(quote)
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
    logger.info("本回合 %d 個標的成功，新觸發警報 %d 則", len(quotes), len(new_alerts))

    if new_alerts:
        # 3. DeepSeek 中文報告（失敗時退回原始清單）
        report = None
        if cfg.deepseek.api_key:
            try:
                report = generate_report(
                    new_alerts, cfg.deepseek.api_key, cfg.deepseek.base_url, cfg.deepseek.model
                )
                logger.info("DeepSeek 報告生成成功")
            except ReporterError as exc:
                logger.warning("DeepSeek 報告失敗，改用原始清單: %s", exc)
        if report is None:
            report = build_fallback_report(new_alerts)

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        message = f"📊 行情警報 · {now_str}\n\n{report}"
        if fetch_errors:
            message += "\n\n⚠️ 本次部分標的抓取失敗：" + "；".join(fetch_errors)

        # 4. Telegram 發送
        if args.dry_run:
            print(message)
            logger.info("[dry-run] 預覽完成，未發送通知、未更新狀態")
        else:
            try:
                send_telegram_message(
                    cfg.telegram.bot_token, cfg.telegram.chat_id, message, cfg.telegram.parse_mode
                )
                logger.info("Telegram 通知已送出（%d 則警報）", len(new_alerts))
            except NotifyError as exc:
                # 不儲存狀態 → 下回合會重送，確保警報不遺漏
                logger.error("Telegram 發送失敗（狀態未更新，下回合重試）: %s", exc)
                return 1

    # 5. 儲存狀態（供 GitHub Actions commit 回 repo）
    if not args.dry_run:
        state.save()
        logger.info("狀態已儲存至 %s", state.path)

    if fetch_errors:
        logger.warning("部分標的抓取失敗（%d）：%s", len(fetch_errors), "; ".join(fetch_errors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
