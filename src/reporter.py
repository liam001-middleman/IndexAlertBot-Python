"""呼叫 DeepSeek API，將新觸發的警報整理成繁體中文報告。"""
import logging
from typing import List

import requests

from .models import Alert

logger = logging.getLogger(__name__)

SEVERITY_ICON = {"critical": "🔴", "warning": "🟠", "info": "🔵"}

SYSTEM_PROMPT = (
    "你是一個專業的市場警報分析助理。使用者會提供一組「剛觸發」的技術面警報"
    "（RSI 超買/超賣、日內急漲急跌、乖離均線）。請用繁體中文輸出簡潔的市場報告：\n"
    "1. 開頭用一句話總結本次警報總覽（幾檔標的、幾則警報、最大風險類別）。\n"
    "2. 每個標的用一小段說明，依嚴重程度（critical > warning > info）排序，"
    "列出標的、現價、觸發原因與指標數值，並用 1~2 句話補充可能的市場情境與風險提示。\n"
    "3. 不要給具體買賣建議，最後加一行風險聲明。\n"
    "4. 使用 Markdown 排版，總長度控制在 1200 字以內。\n"
)


class ReporterError(Exception):
    """報告生成失敗。"""


def build_user_prompt(alerts: List[Alert]) -> str:
    lines = [f"共 {len(alerts)} 則新觸發警報：\n"]
    for a in alerts:
        icon = SEVERITY_ICON.get(a.severity, "⚪")
        lines.append(
            f"- {icon} [{a.severity.upper()}] {a.alert_name} | {a.name}（{a.symbol}，{a.market}）| {a.detail}"
        )
    return "\n".join(lines)


def generate_report(
    alerts: List[Alert],
    api_key: str,
    base_url: str = "https://api.deepseek.com",
    model: str = "deepseek-chat",
) -> str:
    """呼叫 DeepSeek chat completions（OpenAI 相容 API）。"""
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(alerts)},
        ],
        "temperature": 0.7,
        "max_tokens": 2000,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        raise ReporterError(f"DeepSeek API 呼叫失敗: {exc}") from exc
    if not content:
        raise ReporterError("DeepSeek 回傳內容為空")
    return content


def build_fallback_report(alerts: List[Alert]) -> str:
    """當 DeepSeek 不可用時，以警報原始清單拼出簡易報告。"""
    lines = [f"⚠️ 共 {len(alerts)} 則新觸發警報（DeepSeek 摘要暫時不可用，以下為原始清單）：\n"]
    for a in alerts:
        icon = SEVERITY_ICON.get(a.severity, "⚪")
        lines.append(f"• {icon} {a.detail}")
    lines.append("\n本報告由系統自動產生，僅供參考，不構成投資建議。")
    return "\n".join(lines)
