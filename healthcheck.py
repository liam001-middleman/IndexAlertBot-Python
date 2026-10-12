#!/usr/bin/env python3
"""外部觸發器（cron-job.org）健康檢查。

背景：GitHub Actions 的原生 schedule 極不穩定（`*/5` 的 cron 名目上一整天該跑
288 次，實測卻常只有 7~11 次），因此三個 alerts workflow 實際上都是靠
cron-job.org 以 `workflow_dispatch` 方式**外部觸發**。若 cron-job.org 的 GitHub
PAT 過期或被撤銷，dispatch 會**靜默停止**——狀態檔不再更新、Telegram 不再收到
警報，但 GitHub 不會有任何錯誤訊息。本程式就是防堵這種「靜默斷線」的哨兵。

原理：**逐一**向 GitHub API 查詢每個 alerts workflow 最近的 `workflow_dispatch`
執行紀錄（`GET /repos/{owner}/{repo}/actions/workflows/{file}/runs`），而不是查
全 repo 的總表——因為加密貨幣 24/7 每 5 分鐘的流量會蓋掉 US／TW 的斷線。

檢查只在各 workflow 的**活躍時段**內進行，且時段開始後的寬容期（grace）內不告警，
避免週末或時段剛開始時拿昨日的舊紀錄誤報：

    Alerts - US Stocks    週一~週五 15:20–21:55（UTC；判定期間，見 WORKFLOWS 註解）
    Alerts - TW Stocks    週一~週五 01:00–05:55（UTC）
    Alerts - Crypto       每天 00:00–23:59（UTC，24/7）

若某 workflow 在活躍時段內「最近一次距現在已超過 `CRONJOB_MAX_SILENCE_MINUTES`
分鐘」或「完全查不到紀錄」，即透過 Telegram 發出**指名該 workflow** 的告警，
並以非零狀態結束（讓 workflow 一起變紅，多一層提醒）。

環境變數：
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID  用於告警（沿用主程式的 Secrets）
    GITHUB_REPOSITORY                      Actions 自動提供（owner/repo）
    GITHUB_TOKEN                           Actions 自動提供（讀取 Actions 執行紀錄）
    GITHUB_API_URL                         預設 https://api.github.com
    CRONJOB_MAX_SILENCE_MINUTES            預設 30（加密貨幣 24/7 每 5 分鐘一次，
                                           30 分鐘無聲即視為異常）
    CRONJOB_GRACE_MINUTES                  預設等於上面的門檻（活躍時段開始後的寬容期）
    HEALTHCHECK_WORKFLOWS                  選用，逗號分隔的 workflow 檔名；未設定=三個都檢查

用法：
    python healthcheck.py                # 檢查並在異常時發 Telegram
    python healthcheck.py --no-notify    # 只印出結果，不發送（本機測試用）

回傳碼：
    0  正常（活躍時段內各 workflow 都有即時的外部觸發紀錄）
    1  偵測到斷線（已嘗試發送 Telegram 告警，訊息會指名是哪個 workflow）
    2  執行環境問題（缺少 GITHUB_REPOSITORY，或所有查詢都失敗）
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

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

WEEKDAY_NAMES = "一二三四五六日"
WEEKDAYS_WEEKDAY = (0, 1, 2, 3, 4)
WEEKDAYS_ALL = (0, 1, 2, 3, 4, 5, 6)

# 三個 alerts workflow 的活躍時段（UTC）。哨兵只在各自時段內檢查，
# 週末與時段外一律不檢查，避免拿昨日的舊紀錄誤報。
#
# US 的時段刻意比 dispatcher 的實際範圍（cron-job.org 實測 13:00–21:55）晚開：
#   - 起點實測有 13:00（09-29）、13:40（09-01）、14:20（09-28）三種，
#     且若 cron-job.org 排程跟隨美東時區，冬令（EST）還會再晚 1 小時。
#   - 哨兵誤報一次就會失去信任，因此取「任何時區／DST／排程設定都不會誤報」的
#     交集 15:20 起算（+30 分寬容 → 15:50 起才判定）。
#   - 代價：dispatcher 若在開盤（13:00）就掛掉，最晚 15:50 才會告警；
#     但仍遠優於過去「靜默 12 天」無人發現。
#   - 結束時間 21:55 為實測最後一筆 dispatch 的時間（09-01、09-29 皆相同）。
WORKFLOWS: List[Dict[str, object]] = [
    {
        "file": "alerts-us.yml",
        "name": "Alerts - US Stocks",
        "weekdays": WEEKDAYS_WEEKDAY,
        "window": ("15:20", "21:55"),
    },
    {
        "file": "alerts-tw.yml",
        "name": "Alerts - TW Stocks",
        "weekdays": WEEKDAYS_WEEKDAY,
        "window": ("01:00", "05:55"),
    },
    {
        "file": "alerts-crypto.yml",
        "name": "Alerts - Crypto",
        "weekdays": WEEKDAYS_ALL,
        "window": ("00:00", "23:59"),
    },
]


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


def parse_hhmm(value: str) -> int:
    """把 `HH:MM` 轉成「當日第幾分鐘」（用於活躍時段比較）。"""
    text = str(value).strip()
    try:
        hours, minutes = text.split(":")
        total = int(hours) * 60 + int(minutes)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"時段格式必須是 HH:MM，收到：{value!r}") from exc
    if not 0 <= total <= 24 * 60:
        raise ValueError(f"時段超出範圍：{value!r}")
    return total


def window_text(weekdays: Sequence[int], window: Sequence[str]) -> str:
    """把活躍時段轉成人看得懂的文字，例如「週一~週五 15:20–21:55（UTC）」。"""
    days = sorted(int(day) for day in weekdays)
    if len(days) == 7:
        day_text = "每天"
    elif days == list(WEEKDAYS_WEEKDAY):
        day_text = "週一~週五"
    else:
        day_text = "週" + "、".join(WEEKDAY_NAMES[day] for day in days)
    return f"{day_text} {window[0]}–{window[1]}（UTC）"


def is_within_window(now: datetime, weekdays: Sequence[int], window: Sequence[str]) -> bool:
    """現在是否落在該 workflow 的活躍時段內（假設時段不跨日）。"""
    if now.weekday() not in tuple(int(day) for day in weekdays):
        return False
    minutes = now.hour * 60 + now.minute
    return parse_hhmm(window[0]) <= minutes <= parse_hhmm(window[1])


def check_started(
    now: datetime,
    weekdays: Sequence[int],
    window: Sequence[str],
    grace_minutes: int,
) -> bool:
    """活躍時段是否已開始且過了寬容期（避免時段一開始就拿昨天的舊紀錄誤報）。

    自帶 `is_within_window` 檢查，因此時段開始前與結束後都回 False；
    `evaluate_silence` 仍會先檢查視窗（兩者是 AND 關係）。
    """
    if now.weekday() not in tuple(int(day) for day in weekdays):
        return False
    if not is_within_window(now, weekdays, window):
        return False
    minutes = now.hour * 60 + now.minute
    return minutes >= parse_hhmm(window[0]) + max(0, int(grace_minutes))


def latest_dispatch_at(runs) -> Optional[datetime]:
    """從 API 回應取出最近一次 workflow_dispatch 的建立時間；查無資料回 None。"""
    items = (runs or {}).get("workflow_runs") or []
    stamps = [parse_timestamp(item.get("created_at")) for item in items]
    stamps = [ts for ts in stamps if ts is not None]
    return max(stamps) if stamps else None


ALERT_HEADER = "[警示] 外部觸發器疑似斷線（cron-job.org workflow_dispatch）"


def workflow_actions_url(repo: str, workflow_file: str) -> str:
    """該 workflow 在 GitHub Actions 頁面的網址（方便點進去看歷史）。"""
    return f"https://github.com/{repo}/actions/workflows/{workflow_file}"


def _silence_line(item: Dict[str, object], max_silence_minutes: int) -> str:
    """單一 workflow 的斷線摘要（一行）。"""
    age = item.get("age")
    if age is None:
        detail = "查不到任何 workflow_dispatch 執行紀錄（可能從未成功觸發）"
    else:
        detail = (
            f"最近一次 workflow_dispatch 距今約 {float(age):.1f} 分鐘"
            f"（門檻 {max_silence_minutes} 分鐘）"
        )
    label = str(item.get("name") or "外部觸發器")
    workflow = str(item.get("workflow") or "")
    return f"{label}（{workflow}）：{detail}" if workflow else f"{label}：{detail}"


def _remediation_lines(repo: str) -> List[str]:
    """共用的排錯步驟（純文字，不使用 emoji / Markdown）。"""
    return [
        "可能原因：cron-job.org 的 GitHub PAT 已過期／被撤銷、權限不足，"
        "或 job 已被自動停用（Inactive）。",
        "處理步驟：",
        "1. 登入 cron-job.org → 該 job 的 History 查看失敗的 HTTP 狀態碼"
        "（401=PAT 過期／403=權限不足／404=URL 或 repo 權限範圍錯誤／"
        "422=workflow 未啟用或 ref 分支不存在）",
        "2. 把三個 job 的 Method（POST）、URL（.../actions/workflows/<檔名>/dispatches）、"
        "Request body（{\"ref\":\"master\"}）、Authorization header 逐欄比對"
        "（以加密貨幣 job 為正常樣板）",
        "3. 若為 401／403，產生新的 fine-grained PAT"
        "（Permissions → Actions: Read and write；Repository access 勾選本 repo）",
        "4. 修正後按 Test run now，確認回應 HTTP 204；被停用的 job 記得重新 Enable"
        "（並開啟 Notify on failure）",
        "5. 本機可先執行 `python dispatch_check.py`，用同一組 PAT／URL 確認會拿到 204",
        f"6. 到 GitHub Actions 確認三個 workflow 都建立了新的 workflow_dispatch run"
        f"（repo：{repo}）",
        "詳見 README「外部觸發（cron-job.org）」。",
    ]


def build_silent_message(
    silent: Sequence[Dict[str, object]],
    max_silence_minutes: int,
    repo: str,
) -> str:
    """組出斷線告警文字：逐一指名哪個 workflow 在活躍時段內沒有觸發紀錄。"""
    lines = [ALERT_HEADER, ""]
    lines.append(f"偵測到 {len(silent)} 個 workflow 在活躍時段內沒有即時的外部觸發紀錄：")
    for item in silent:
        lines.append(f"- {_silence_line(item, max_silence_minutes)}")
        if item.get("window_text"):
            lines.append(f"  活躍時段：{item['window_text']}")
        if item.get("last_run_text"):
            lines.append(f"  最近一次紀錄：{item['last_run_text']}")
        if item.get("actions_url"):
            lines.append(f"  Actions：{item['actions_url']}")
    lines.append(f"repo：{repo}")
    lines.append("")
    lines.extend(_remediation_lines(repo))
    return "\n".join(lines)


def build_alert_message(
    age_minutes: Optional[float],
    max_silence_minutes: int,
    repo: str,
    workflow_name: Optional[str] = None,
) -> str:
    """單一 workflow 的斷線告警文字（保留原本簽章，新增選用的 workflow 名稱）。"""
    item: Dict[str, object] = {
        "name": workflow_name or "外部觸發器",
        "workflow": "",
        "age": age_minutes,
    }
    return build_silent_message([item], max_silence_minutes, repo)


def evaluate_silence(
    now: datetime,
    runs_by_workflow: Dict[str, dict],
    max_silence_minutes: int,
    grace_minutes: Optional[int] = None,
    workflows: Sequence[Dict[str, object]] = tuple(WORKFLOWS),
) -> List[Dict[str, object]]:
    """找出「在活躍時段內卻沒有即時觸發紀錄」的 workflow（純函式，方便測試）。

    只檢查 runs_by_workflow 內有提供資料的 workflow——查詢失敗的不要誤判成斷線。
    """
    grace = max_silence_minutes if grace_minutes is None else grace_minutes
    silent: List[Dict[str, object]] = []
    for wf in workflows:
        file_name = str(wf["file"])
        if file_name not in runs_by_workflow:
            continue
        weekdays = wf["weekdays"]
        window = wf["window"]
        if not is_within_window(now, weekdays, window):
            continue
        if not check_started(now, weekdays, window, grace):
            logger.debug("%s 活躍時段剛開始（寬容期 %s 分鐘），暫不判定", wf["name"], grace)
            continue
        runs = runs_by_workflow[file_name]
        age = latest_dispatch_age_minutes(runs, now=now)
        if not should_alert(age, max_silence_minutes):
            continue
        last_at = latest_dispatch_at(runs)
        silent.append(
            {
                "name": wf["name"],
                "workflow": file_name,
                "age": age,
                "last_run_text": last_at.strftime("%Y-%m-%d %H:%M UTC") if last_at else "查不到紀錄",
                "window_text": window_text(weekdays, window),
            }
        )
    return silent


def select_workflows(
    spec: Optional[str] = None,
    workflows: Sequence[Dict[str, object]] = tuple(WORKFLOWS),
) -> List[Dict[str, object]]:
    """依 `HEALTHCHECK_WORKFLOWS`（逗號分隔檔名）挑選要檢查的 workflow；空值＝全部。"""
    names = [part.strip() for part in (spec or "").split(",") if part.strip()]
    if not names:
        return list(workflows)
    by_basename = {os.path.basename(str(wf["file"])): wf for wf in workflows}
    selected: List[Dict[str, object]] = []
    for name in names:
        match = by_basename.get(os.path.basename(name.replace("\\", "/")))
        if match is None:
            logger.warning("HEALTHCHECK_WORKFLOWS 指定的 %s 不在已知清單中，已忽略", name)
            continue
        if match not in selected:
            selected.append(match)
    return selected or list(workflows)


def fetch_workflow_dispatch_runs(
    repo: str,
    token: str,
    api_url: str,
    workflow_file: str,
    per_page: int = 1,
) -> dict:
    """取得**單一 workflow** 最近的 workflow_dispatch 執行紀錄。

    GET /repos/{owner}/{repo}/actions/workflows/{workflow_file}/runs
    """
    file_name = os.path.basename(str(workflow_file).strip().replace("\\", "/"))
    url = f"{api_url.rstrip('/')}/repos/{repo}/actions/workflows/{file_name}/runs"
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


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="外部觸發器（cron-job.org workflow_dispatch）健康檢查",
    )
    parser.add_argument(
        "--no-notify",
        "--dry-run",
        dest="notify",
        action="store_false",
        help="只印出檢查結果，不發送 Telegram 告警（本機測試用）",
    )
    parser.set_defaults(notify=True)
    return parser


def main(argv: Optional[Sequence[str]] = None, now: Optional[datetime] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    api_url = os.environ.get("GITHUB_API_URL", DEFAULT_GITHUB_API_URL).strip() or DEFAULT_GITHUB_API_URL
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    max_silence = _env_int("CRONJOB_MAX_SILENCE_MINUTES", DEFAULT_MAX_SILENCE_MINUTES)
    grace = _env_int("CRONJOB_GRACE_MINUTES", max_silence)
    workflows = select_workflows(os.environ.get("HEALTHCHECK_WORKFLOWS"))

    if not repo:
        logger.error(
            "缺少 GITHUB_REPOSITORY（Actions 會自動提供；本機測試請自行設定，"
            "例如 liam001-middleman/IndexAlertBot-Python）"
        )
        return 2

    now = now or datetime.now(timezone.utc)
    logger.info("檢查時間 %s（活躍時段外或寬容期內不判定）", now.strftime("%Y-%m-%d %H:%M UTC"))

    runs_by_workflow: Dict[str, dict] = {}
    failed_queries: List[str] = []
    for wf in workflows:
        file_name = str(wf["file"])
        try:
            runs_by_workflow[file_name] = fetch_workflow_dispatch_runs(repo, token, api_url, file_name)
        except Exception as exc:  # noqa: BLE001 - 查詢失敗不該誤判成「斷線」
            failed_queries.append(file_name)
            logger.error("查詢 %s 的執行紀錄失敗（略過該 workflow，不判定為斷線）: %s", file_name, exc)

    if not runs_by_workflow:
        logger.error("所有 workflow 的執行紀錄查詢都失敗（為避免誤報，不發出告警）")
        return 2

    for wf in workflows:
        runs = runs_by_workflow.get(str(wf["file"]))
        if runs is None:
            continue
        age = latest_dispatch_age_minutes(runs, now=now)
        age_text = f"{age:.1f} 分鐘" if age is not None else "查不到紀錄"
        logger.info(
            "%s：最近一次 workflow_dispatch 距今 %s（門檻 %d 分鐘；活躍時段 %s）",
            wf["name"],
            age_text,
            max_silence,
            window_text(wf["weekdays"], wf["window"]),
        )

    silent = evaluate_silence(
        now,
        runs_by_workflow,
        max_silence,
        grace_minutes=grace,
        workflows=workflows,
    )
    if not silent:
        logger.info("外部觸發器正常：活躍時段內的 workflow 都有即時的外部觸發紀錄。")
        if failed_queries:
            logger.warning("注意：%s 的查詢失敗，本次未納入判定。", "、".join(failed_queries))
        return 0

    for item in silent:
        item["actions_url"] = workflow_actions_url(repo, str(item["workflow"]))

    message = build_silent_message(silent, max_silence, repo)
    logger.warning("%s", message)
    if not args.notify:
        logger.info("（--no-notify）略過 Telegram 告警發送")
        return 1
    try:
        send_telegram_message(bot_token, chat_id, message)
        logger.info("已發出 Telegram 斷線告警")
    except NotifyError as exc:
        logger.error("Telegram 告警發送失敗: %s", exc)
    return 1


if __name__ == "__main__":
    sys.exit(main())
