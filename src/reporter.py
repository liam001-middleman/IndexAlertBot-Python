"""呼叫 DeepSeek API，將新觸發的警報整理成繁體中文報告。"""
import logging
from typing import List

import requests

from .models import Alert

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是一個專業的市場警報分析助理。使用者會提供一組「剛觸發」的技術面警報"
    "（RSI 超買/超賣、日內急漲急跌、乖離均線）。請用繁體中文輸出簡潔的純文字報告。\n"
    "格式規則：\n"
    "1. 開頭一行總覽：幾檔標的、幾則警報、最大風險類別。\n"
    "2. 每個標的獨立一個區塊，依嚴重度排序（critical > warning > info）：\n"
    "   第一行：標的名稱（代號，代號請用可讀格式，例如 ethtwd 寫成 ETH/TWD）\n"
    "   第二行：現價（必列，任何警報都要有現價）\n"
    "   之後：每一則警報一行（警報名稱＋指標數值＋門檻）\n"
    "   最後：1~2 句簡短風險說明\n"
    "3. 不要使用任何 Markdown 符號（如 **、##、-、*）、不要使用 emoji、不要畫表格與分隔線。\n"
    "4. 不要給具體買賣建議；結尾加一行風險聲明。\n"
    "5. 總長度控制在 1000 字以內。\n"
)


class ReporterError(Exception):
    """報告生成失敗。"""


def build_user_prompt(alerts: List[Alert]) -> str:
    """把新觸發警報轉成結構化資料，確保 DeepSeek 一定有標的與現價可引用。"""
    lines = [f"共 {len(alerts)} 則新觸發警報："]
    for a in alerts:
        lines.append("")
        lines.append(f"標的：{a.name}（{a.symbol}，{a.market}）")
        lines.append(f"嚴重度：{a.severity}")
        lines.append(f"現價：{a.price:.2f}")
        lines.append(f"警報：{a.alert_name}")
        lines.append(f"內容：{a.detail}")
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
    lines = [f"共 {len(alerts)} 則新觸發警報（DeepSeek 摘要暫時不可用，以下為原始清單）：\n"]
    for a in alerts:
        lines.append(a.detail)
    lines.append("\n本報告由系統自動產生，僅供參考，不構成投資建議。")
    return "\n".join(lines)
