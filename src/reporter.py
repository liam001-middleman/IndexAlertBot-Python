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
    "   第三行：RSI（若使用者有提供該資料：RSI(14)：xxx；無則省略此行）\n"
    "   第四行：上次報告價格（若使用者有提供該資料：上次報告價格：xxx（較上次 +x.xx%）；無則省略此行）\n"
    "   第五行：均線（若使用者有提供該資料：均線：MA20=xxx（乖離 +x.xx%）、MA60=xxx（乖離 +x.xx%）、MA200=xxx（乖離 +x.xx%））\n"
    "   之後：每一則警報一行（警報名稱＋指標數值＋門檻）\n"
    "   最後：1~2 句簡短風險說明\n"
    "3. 不要使用任何 Markdown 符號（如 **、##、-、*）、不要使用 emoji、不要畫表格與分隔線。\n"
    "4. 給具體買賣建議。\n"
    "5. 總長度控制在 1200 字以內。\n"
)


class ReporterError(Exception):
    """報告生成失敗。"""


def _format_ma(a: Alert) -> str:
    """把 Alert 的均線資料格式化成一行：MA20=xxx（乖離 +x.xx%）、MA60=...。"""
    parts = []
    for period in sorted(a.ma, key=int):
        ma_val = a.ma[period]
        if ma_val is None:
            continue
        dev = a.ma_deviation_pct.get(period)
        dev_txt = f"{dev:+.2f}%" if dev is not None else "N/A"
        parts.append(f"MA{period}={ma_val:.2f}（乖離 {dev_txt}）")
    return "、".join(parts)


def _format_last_report(a: Alert) -> str:
    """把上次報告價格格式化成一行：上次報告價格：xxx（較上次 +x.xx%）。"""
    if a.last_report_price is None:
        return ""
    prev = float(a.last_report_price)
    change = (a.price - prev) / prev * 100.0 if prev else 0.0
    return f"上次報告價格：{prev:.2f}（較上次 {change:+.2f}%）"


def build_user_prompt(alerts: List[Alert]) -> str:
    """把新觸發警報轉成結構化資料，確保 DeepSeek 一定有標的、現價、均線與上次報告價可引用。"""
    lines = [f"共 {len(alerts)} 則新觸發警報："]
    for a in alerts:
        lines.append("")
        lines.append(f"標的：{a.name}（{a.symbol}，{a.market}）")
        lines.append(f"嚴重度：{a.severity}")
        lines.append(f"現價：{a.price:.2f}")
        if a.rsi is not None:
            lines.append(f"RSI：{a.rsi:.1f}")
        if a.last_report_price is not None:
            lines.append(f"上次報告價格：{a.last_report_price:.2f}")
        ma_txt = _format_ma(a)
        if ma_txt:
            lines.append(f"均線：{ma_txt}")
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
        if a.rsi is not None:
            lines.append(f"RSI：{a.rsi:.1f}")
        ma_txt = _format_ma(a)
        if ma_txt:
            lines.append(f"均線：{ma_txt}")
        last_txt = _format_last_report(a)
        if last_txt:
            lines.append(last_txt)
        lines.append("")
    lines.append("本報告由系統自動產生，僅供參考，不構成投資建議。")
    return "\n".join(lines)
