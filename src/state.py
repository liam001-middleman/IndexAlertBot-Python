"""alert_state.json 的讀取 / 更新 / 儲存。

狀態結構：
{
  "AAPL": {
    "rsi_overbought": {
      "status": "active" | "clear",
      "last_value": 75.5,
      "triggered_at": "2026-08-28T10:00:00+00:00"
    }
  }
}
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

REPORT_KEY = "__last_report__"  # 保留鍵：記錄該標的「上次出報告時」的價格快照


class StateStore:
    """警報狀態儲存。path 為 None 時只存在記憶體（供 dry-run 使用）。"""

    def __init__(self, path: Optional[Any] = None):
        self.path = Path(path) if path else None
        self._data: dict = {}

    def load(self) -> None:
        """從檔案讀取狀態。

        使用 utf-8-sig 解碼以容忍檔頭 BOM（有些編輯器 / PowerShell 會寫入）。
        檔案不存在或內容損毀時，備份壞檔後以空狀態繼續，避免整個 job 崩潰。
        """
        if self.path is None or not self.path.exists():
            self._data = {}
            return
        try:
            with open(self.path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError(f"狀態檔格式錯誤，預期 dict，實際為 {type(data).__name__}")
            self._data = data
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.warning("狀態檔讀取失敗，備份後以空狀態繼續: %s", exc)
            self._backup_corrupt_file()
            self._data = {}

    def _backup_corrupt_file(self) -> None:
        """把損毀的狀態檔備份成 alert_state.json.bak-<時間戳>。"""
        if self.path is None or not self.path.exists():
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = self.path.with_name(f"{self.path.name}.bak-{stamp}")
        try:
            backup.write_bytes(self.path.read_bytes())
            logger.warning("已備份損毀狀態檔至 %s", backup)
        except OSError as exc:
            logger.warning("備份損毀狀態檔失敗（忽略）: %s", exc)

    @property
    def data(self) -> dict:
        return self._data

    def is_active(self, symbol: str, alert_type: str) -> bool:
        return self._data.get(symbol, {}).get(alert_type, {}).get("status") == "active"

    def get(self, symbol: str, alert_type: str) -> dict:
        return self._data.get(symbol, {}).get(alert_type, {}) or {}

    def get_last_report(self, symbol: str) -> Optional[dict]:
        """回傳該標的「上次出報告時」的快照（{"price", "at"}），無則回傳 None。"""
        entry = self._data.get(symbol, {}).get(REPORT_KEY)
        return entry if isinstance(entry, dict) else None

    def set_last_report(self, symbol: str, price: float,
                        reported_at: Optional[datetime] = None) -> None:
        """記錄該標的本次出報告時的價格快照，供下次報告顯示「上次報告價格」。"""
        self._data.setdefault(symbol, {})[REPORT_KEY] = {
            "price": float(price),
            "at": (reported_at or datetime.now(timezone.utc)).isoformat(),
        }

    def mark_active(self, symbol: str, alert_type: str, value: float,
                    triggered_at: Optional[datetime] = None) -> None:
        self._data.setdefault(symbol, {})[alert_type] = {
            "status": "active",
            "last_value": value,
            "triggered_at": (triggered_at or datetime.now(timezone.utc)).isoformat(),
        }

    def mark_clear(self, symbol: str, alert_type: str, value: Optional[float] = None) -> None:
        entry = self._data.setdefault(symbol, {}).setdefault(alert_type, {})
        entry["status"] = "clear"
        if value is not None:
            entry["last_value"] = value

    def save(self) -> None:
        """以「先寫暫存檔再替換」的方式寫入，避免中斷造成檔案損毀。

        Python json.dump 不會產生 BOM，寫出的檔案為乾淨的 UTF-8。
        """
        if self.path is None:
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=2)
        tmp.replace(self.path)
