"""透過 Telegram Bot API 發送通知。"""
import html as _html
import logging
from typing import List

import requests

logger = logging.getLogger(__name__)

TELEGRAM_MSG_LIMIT = 4096
_CHUNK_LIMIT = 4000  # 保留緩衝


class NotifyError(Exception):
    """通知發送失敗。"""


def split_message(text: str, limit: int = _CHUNK_LIMIT) -> List[str]:
    """將過長訊息依換行切成多段（Telegram 單則上限 4096 字元）。"""
    if len(text) <= limit:
        return [text]
    parts: List[str] = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            if current:
                parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    parse_mode: str = "",
) -> None:
    """發送訊息到指定 Telegram chat。parse_mode 支援 HTML / MarkdownV2 / 留空。"""
    if not bot_token or not chat_id:
        raise NotifyError("缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    for chunk in split_message(text):
        payload_text = chunk
        if parse_mode and parse_mode.upper() == "HTML":
            payload_text = _html.escape(chunk)
        payload = {
            "chat_id": chat_id,
            "text": payload_text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            resp = requests.post(url, json=payload, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            raise NotifyError(f"Telegram 發送失敗: {exc}") from exc
