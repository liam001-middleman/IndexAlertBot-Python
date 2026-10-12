#!/usr/bin/env python3
"""cron-job.org 外部觸發器的本機驗證工具。

背景：三個 `Alerts - *` workflow 靠 cron-job.org 以外部 HTTP 呼叫 `workflow_dispatch`
驅動。當 PAT 過期、權限不足、URL/body 打錯時，cron-job.org 只會顯示
`Failed (HTTP error)` 並把 job 停用（Inactive），GitHub 端則完全沒有錯誤訊息。
本工具讓你在改動 cron-job.org 之前／之後，用同一顆 PAT 直接打 dispatch 端點，
立刻知道是 204 / 401 / 403 / 404 / 422，不必等到下一次排程才發現。

環境變數：
    GITHUB_PAT             fine-grained PAT（Permissions → Actions: Read and write）
                           （依序 fallback：GH_TOKEN、GITHUB_TOKEN）
    GITHUB_REPOSITORY      預設 liam001-middleman/IndexAlertBot-Python
    GITHUB_API_URL         預設 https://api.github.com

用法：
    python dispatch_check.py                      # 檢查三個 alerts workflow
    python dispatch_check.py --workflow alerts-us.yml
    python dispatch_check.py --ref master
    python dispatch_check.py --dry-run            # 只印出將送出的請求（不呼叫 API）
    python dispatch_check.py --pat-env MY_PAT     # 改讀其他環境變數

回傳碼：
    0  全部成功（HTTP 204）
    1  有 workflow 觸發失敗（已印出狀態碼與處置建議）
    2  執行環境問題（缺少 PAT、repo 格式錯誤）

安全性：PAT 只從環境變數讀取、永不寫入輸出（只顯示遮蔽後的長度提示），
        `--dry-run` 的 Authorization header 也會被遮蔽。
"""
import argparse
import logging
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

import requests

# 強制使用 UTF-8 輸出，避免在非 UTF-8 主控台（如 Windows Big5/cp950）
# 且 stdout 被 pipe 時，中文輸出拋出 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dispatch-check")

DEFAULT_REPO = "liam001-middleman/IndexAlertBot-Python"
DEFAULT_API_URL = "https://api.github.com"
DEFAULT_REF = "master"
GITHUB_API_VERSION = "2022-11-28"
GITHUB_ACCEPT = "application/vnd.github+json"
DEFAULT_WORKFLOWS: Tuple[str, ...] = (
    "alerts-us.yml",
    "alerts-tw.yml",
    "alerts-crypto.yml",
)
PAT_ENV_CANDIDATES: Tuple[str, ...] = ("GITHUB_PAT", "GH_TOKEN", "GITHUB_TOKEN")
TOKEN_MASK = "***"

# 狀態碼 → 中文處置建議（與 README「外部觸發（cron-job.org）」的表格一致）
STATUS_HELP: Dict[int, str] = {
    204: "成功：GitHub 已接受，會建立一筆 workflow_dispatch run",
    401: "PAT 已過期或被撤銷（最常見）→ 產生新的 fine-grained PAT 並更新 cron-job.org 的 Authorization header",
    403: "PAT 權限不足（缺 Actions: Read and write）或觸發速率限制",
    404: "repo／workflow 檔名錯誤，或 PAT 沒有此 repo 權限（Repository access 要勾本 repo）",
    422: "workflow 未啟用，或 body 的 ref 分支不存在（本 repo 只有 master）",
}


def resolve_pat(
    env: Optional[Dict[str, str]] = None,
    candidates: Sequence[str] = PAT_ENV_CANDIDATES,
) -> Tuple[str, str]:
    """依序尋找可用的 PAT，回傳 (token, 來源環境變數名稱)；找不到時回傳 ("", "")。"""
    env = os.environ if env is None else env
    for name in candidates:
        value = (env.get(name) or "").strip()
        if value:
            return value, name
    return "", ""


def mask_secret(secret: str) -> str:
    """把祕密轉成不含內容的提示字串（保留長度，方便確認是否貼到正確的 token）。"""
    if not secret:
        return "（未設定）"
    return f"{TOKEN_MASK}（長度 {len(secret)}，內容不顯示）"


def normalize_workflow(workflow: str) -> str:
    """把 `alerts-us.yml` 或 `.github/workflows/alerts-us.yml` 統一成後者。"""
    name = (workflow or "").strip().replace("\\", "/").lstrip("/")
    if not name:
        raise ValueError("workflow 不可為空")
    if not name.startswith(".github/workflows/"):
        name = f".github/workflows/{name}"
    return name


def workflow_file_name(workflow: str) -> str:
    """取出 GitHub API 要的 workflow 檔名（端點只接受檔名或數字 ID，不接受完整路徑）。"""
    return os.path.basename(normalize_workflow(workflow))


def dispatch_url(api_url: str, repo: str, workflow: str) -> str:
    """組出 workflow_dispatch 端點 URL（repo 必須是 owner/repo 格式）。

    注意：`{workflow}` 段必須是**檔名**（例如 `alerts-us.yml`），
    不能是 `.github/workflows/alerts-us.yml`——用完整路徑會拿到 404。
    """
    owner_repo = (repo or "").strip().strip("/")
    parts = owner_repo.split("/")
    if len(parts) != 2 or not all(part.strip() for part in parts):
        raise ValueError(f"repo 格式必須是 owner/repo，收到：{repo!r}")
    base = (api_url or DEFAULT_API_URL).rstrip("/")
    return f"{base}/repos/{owner_repo}/actions/workflows/{workflow_file_name(workflow)}/dispatches"


def build_headers(token: str = "") -> Dict[str, str]:
    """組出 dispatch 請求的標頭（token 為空時不帶 Authorization，用於 --dry-run 檢查）。"""
    headers = {
        "Accept": GITHUB_ACCEPT,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "IndexAlertBot-dispatch-check",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def explain_status(status: Optional[int]) -> str:
    """把 HTTP 狀態碼轉成中文處置建議。"""
    if status is None:
        return "連線失敗（沒有取得 HTTP 回應）：請檢查網路、proxy 或 api.github.com 是否可連"
    help_text = STATUS_HELP.get(status)
    if help_text:
        return help_text
    return f"非預期狀態碼 {status}：請閱讀下方回應內容判斷（常見為 5xx 或 GitHub 端暫時性錯誤）"


def trigger_dispatch(
    repo: str,
    workflow: str,
    ref: str = DEFAULT_REF,
    token: str = "",
    api_url: str = DEFAULT_API_URL,
    timeout: int = 30,
    post=None,
) -> Dict[str, object]:
    """對單一 workflow 發送 workflow_dispatch，回傳結果 dict（永不拋出例外）。

    回傳：{"workflow", "url", "ok", "status", "message"}
    """
    result: Dict[str, object] = {
        "workflow": str(workflow or "").strip(),
        "url": "",
        "ok": False,
        "status": None,
        "message": "",
    }
    try:
        result["workflow"] = workflow_file_name(workflow)
        url = dispatch_url(api_url, repo, workflow)
    except ValueError as exc:
        result["message"] = f"參數錯誤：{exc}"
        return result
    result["url"] = url

    sender = post or requests.post
    payload = {"ref": (ref or DEFAULT_REF).strip() or DEFAULT_REF}
    try:
        response = sender(
            url,
            headers=build_headers(token),
            json=payload,
            timeout=timeout,
        )
    except requests.exceptions.RequestException as exc:
        result["message"] = f"連線失敗：{exc}"
        return result

    status = getattr(response, "status_code", None)
    result["status"] = status
    result["ok"] = status == 204
    result["message"] = explain_status(status)
    if not result["ok"]:
        snippet = (getattr(response, "text", "") or "").strip().replace("\n", " ")
        if snippet:
            result["message"] = f"{result['message']}；回應內容：{snippet[:200]}"
    return result


def check_workflows(
    repo: str = DEFAULT_REPO,
    workflows: Sequence[str] = DEFAULT_WORKFLOWS,
    ref: str = DEFAULT_REF,
    token: str = "",
    api_url: str = DEFAULT_API_URL,
    timeout: int = 30,
    post=None,
) -> List[Dict[str, object]]:
    """逐一觸發所有 workflow，回傳每個 workflow 的結果清單。"""
    return [
        trigger_dispatch(
            repo=repo,
            workflow=workflow,
            ref=ref,
            token=token,
            api_url=api_url,
            timeout=timeout,
            post=post,
        )
        for workflow in workflows
    ]


def print_dry_run(
    repo: str,
    workflows: Sequence[str],
    ref: str,
    api_url: str,
    token_source: str,
) -> None:
    """印出即將送出的請求（Authorization 一律遮蔽），不呼叫 API。"""
    headers = build_headers("PREVIEW-TOKEN")
    headers["Authorization"] = TOKEN_MASK
    print(f"[dry-run] repo={repo} ref={ref} PAT 來源={token_source or '（未設定）'}")
    for workflow in workflows:
        try:
            url = dispatch_url(api_url, repo, workflow)
        except ValueError as exc:
            print(f"[dry-run] {workflow} → 參數錯誤：{exc}")
            continue
        print(f"[dry-run] POST {url}")
        print(f"[dry-run]   headers: {headers}")
        print(f"[dry-run]   body: {{'ref': '{ref}'}}")
    print("[dry-run] 未實際呼叫 API（不會建立任何 run）")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="驗證 cron-job.org 的 workflow_dispatch 設定（PAT／URL／body）是否正確",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO,
        help=f"目標 repo（owner/repo），預設 {DEFAULT_REPO}",
    )
    parser.add_argument(
        "--workflow",
        action="append",
        default=None,
        help="要檢查的 workflow 檔名，可重複或逗號分隔（預設三個 alerts workflow）",
    )
    parser.add_argument("--ref", default=DEFAULT_REF, help=f"要觸發的分支，預設 {DEFAULT_REF}")
    parser.add_argument(
        "--api-url",
        default=os.environ.get("GITHUB_API_URL") or DEFAULT_API_URL,
        help=f"GitHub API 根網址，預設 {DEFAULT_API_URL}",
    )
    parser.add_argument(
        "--pat-env",
        default=PAT_ENV_CANDIDATES[0],
        help="PAT 所在的環境變數名稱，預設 "
        + PAT_ENV_CANDIDATES[0]
        + "（找不到時仍會依序嘗試 "
        + "、".join(PAT_ENV_CANDIDATES[1:])
        + "）",
    )
    parser.add_argument("--timeout", type=int, default=30, help="單一請求逾時秒數，預設 30")
    parser.add_argument("--dry-run", action="store_true", help="只印出將送出的請求，不呼叫 API")
    return parser


def _collect_workflows(raw: Optional[Sequence[str]]) -> List[str]:
    """把 --workflow（可重複、可逗號分隔）攤平成去重後清單。"""
    if not raw:
        return list(DEFAULT_WORKFLOWS)
    items: List[str] = []
    for entry in raw:
        for part in str(entry).split(","):
            name = part.strip()
            if name and name not in items:
                items.append(name)
    return items or list(DEFAULT_WORKFLOWS)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    workflows = _collect_workflows(args.workflow)

    candidates = (args.pat_env,) + tuple(
        name for name in PAT_ENV_CANDIDATES if name != args.pat_env
    )
    token, source = resolve_pat(candidates=candidates)
    logger.info("PAT 來源：%s → %s", source or "（找不到）", mask_secret(token))
    logger.info("repo=%s ref=%s workflow=%s", args.repo, args.ref, ", ".join(workflows))

    if args.dry_run:
        print_dry_run(args.repo, workflows, args.ref, args.api_url, source)
        return 0

    if not token:
        logger.error(
            "找不到 PAT：請先設定環境變數（%s）再執行。例如（PowerShell）："
            "$env:GITHUB_PAT=\"github_pat_...\" ; python dispatch_check.py",
            " 或 ".join(PAT_ENV_CANDIDATES),
        )
        return 2

    results = check_workflows(
        repo=args.repo,
        workflows=workflows,
        ref=args.ref,
        token=token,
        api_url=args.api_url,
        timeout=args.timeout,
    )

    failures = [item for item in results if not item["ok"]]
    for item in results:
        state = "PASS" if item["ok"] else "FAIL"
        status = item["status"] if item["status"] is not None else "無回應"
        logger.info("[%s] %s → %s：%s", state, item["workflow"], status, item["message"])

    if failures:
        logger.error(
            "%d/%d 個 workflow 觸發失敗，請依上方狀態碼處置；修正後重新執行本工具確認 204。",
            len(failures),
            len(results),
        )
        return 1

    logger.info(
        "全部 %d 個 workflow 觸發成功（HTTP 204），cron-job.org 的設定可以照此套用。",
        len(results),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
