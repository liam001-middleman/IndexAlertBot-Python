#!/usr/bin/env python3
"""外部觸發器（cron-job.org）健康檢查。

背景：GitHub Actions 的原生 schedule 極不穩定（`*/5` 的 cron 名目上一整天該跑
288 次，實測卻常只有 7~11 次），因此三個 alerts workflow 實際上都是靠
cron-job.org 以 `workflow_dispatch` 方式**外部觸發**。若 cron-job.org 的 GitHub
PAT 過期或被撤銷，dispatch 會**靜默停止**——狀態檔不再更新、Telegram 不再收到
警報，但 GitHub 不會有任何錯誤訊息。本程式就是防堵這種「靜默斷線」的哨兵。

原理：向 GitHub API 查詢本 repo 最近的 `workflow_dispatch` 執行紀錄，若「最近一次」
距現在已超過 `CRONJOB_MAX_SILENCE_MINUTES` 分鐘（或完全查不到紀錄），即透過
Telegram 發出告警，並以非零狀態結束（讓 workflow 一起變紅，多一層提醒）。

環境變數：
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID  用於告警（沿用主程式的 Secrets）
    GITHUB_REPOSITORY                      Actions 自動提供（owner/repo）
    GITHUB_TOKEN                           Actions 自動提供（讀取 Actions 執行紀錄）
    GITHUB_API_URL                         預設 https://api.github.com
    CRONJOB_MAX_SILENCE_MINUTES            預設 30（加密貨幣 24/7 每 5 分鐘一次，
                                           30 分鐘無聲即視為異常）

用法：
    python healthcheck.py

回傳碼：
    0  正常（最近仍有 workflow_dispatch）
    1  偵測到斷線（已嘗試發送 Telegram 告警）
    2  執行環境問題（缺少 GITHUB_REPOSITORY，或查詢 API 失敗）
"""
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional

import requests

# 強制使用 UTF-8 輸出，避免在非 UTF-8 主控台（如 Windows Big5/cp950）
# 且 stdout 被 pipe 時，中文輸出拋出 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass

from src.notifier import NotifyError, send_telegram_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("healthcheck")

DEFAULT_GITHUB_API_URL = "https://api.github.com"
DEFAULT_MAX_SILENCE_MINUTES = 30
GITHUB_API_VERSION = "2022-11-28"


def parse_timestamp(value) -> Optional[datetime]:
    """把 GitHub API 的 ISO8601 時間字串轉成 timezone-aware datetime（無法解析回 None）。"""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def latest_dispatch_age_minutes(runs, now: Optional[datetime] = None) -> Optional[float]:
    """從 API 回應取出最近一次 workflow_dispatch 距今幾分鐘；查無資料回 None。"""
    now = now or datetime.now(timezone.utc)
    items = (runs or {}).get("workflow_runs") or []
    stamps = [parse_timestamp(item.get("created_at")) for item in items]
    stamps = [ts for ts in stamps if ts is not None]
    if not stamps:
        return None
    return (now - max(stamps)).total_seconds() / 60.0


def should_alert(age_minutes: Optional[float], max_silence_minutes: int) -> bool:
    """距今時間超過門檻、或完全查不到紀錄，就該告警。"""
    if age_minutes is None:
        return True
    return age_minutes > max_silence_minutes


def build_alert_message(age_minutes: Optional[float], max_silence_minutes: int, repo: str) -> str:
    """組出斷線告警文字（純文字，不使用 emoji / Markdown）。"""
    lines = ["[警示] 外部觸發器疑似斷線（cron-job.org workflow_dispatch）", ""]
    if age_minutes is None:
        lines.append("查不到任何 workflow_dispatch 執行紀錄（可能從未成功觸發）。")
    else:
        lines.append(
            f"最近一次 workflow_dispatch 距今約 {age_minutes:.1f} 分鐘"
            f"（門檻 {max_silence_minutes} 分鐘）。"
        )
    lines.append(f"repo：{repo}")
    lines.append("")
    lines.append("可能原因：cron-job.org 的 GitHub PAT 已過期／被撤銷，或 job 被停用。")
    lines.append("處理步驟：")
    lines.append("1. 登入 cron-job.org，逐一檢查三個 job 的 Authorization token")
    lines.append("2. 更新為有效的 fine-grained PAT（權限：Actions: Read and write）")
    lines.append("3. 各 job 按 Test run now，確認回應為 HTTP 204")
    lines.append("4. 到 GitHub Actions 確認已建立新的 workflow_dispatch run")
    lines.append("詳見 README「外部觸發（cron-job.org）」。")
    return "\n".join(lines)


def fetch_dispatch_runs(repo: str, token: str, api_url: str, per_page: int = 1) -> dict:
    """取得本 repo 最近的 workflow_dispatch 執行紀錄（GET /repos/{owner}/{repo}/actions/runs）。"""
    url = f"{api_url.rstrip('/')}/repos/{repo}/actions/runs"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "IndexAlertBot-healthcheck",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    params = {"event": "workflow_dispatch", "per_page": per_page}
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _env_int(name: str, default: int) -> int:
    """讀取整數環境變數，缺漏或格式錯誤時回傳預設值。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("環境變數 %s=%r 不是整數，改用預設值 %d", name, raw, default)
        return default


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    api_url = os.environ.get("GITHUB_API_URL", DEFAULT_GITHUB_API_URL).strip() or DEFAULT_GITHUB_API_URL
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    max_silence = _env_int("CRONJOB_MAX_SILENCE_MINUTES", DEFAULT_MAX_SILENCE_MINUTES)

    if not repo:
        logger.error("缺少 GITHUB_REPOSITORY（Actions 會自動提供；本機測試請自行設定，例如 liam001-middleman/IndexAlertBot-Python）")
        return 2

    try:
        runs = fetch_dispatch_runs(repo, token, api_url)
    except Exception as exc:  # noqa: BLE001 - 任何查詢失敗都不該誤判成「斷線」
        logger.error("查詢 GitHub Actions 執行紀錄失敗（為避免誤報，不發出告警）: %s", exc)
        return 2

    age_minutes = latest_dispatch_age_minutes(runs)
    if not should_alert(age_minutes, max_silence):
        logger.info(
            "外部觸發器正常：最近一次 workflow_dispatch 距今 %.1f 分鐘（門檻 %d 分鐘）",
            age_minutes, max_silence,
        )
        return 0

    message = build_alert_message(age_minutes, max_silence, repo)
    logger.warning("%s", message)
    try:
        send_telegram_message(bot_token, chat_id, message)
        logger.info("已發出 Telegram 斷線告警")
    except NotifyError as exc:
        logger.error("Telegram 告警發送失敗: %s", exc)
    return 1


if __name__ == "__main__":
    sys.exit(main())
