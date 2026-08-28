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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class StateStore:
    """警報狀態儲存。path 為 None 時只存在記憶體（供 dry-run 使用）。"""

    def __init__(self, path: Optional[Any] = None):
        self.path = Path(path) if path else None
        self._data: dict = {}

    def load(self) -> None:
        if self.path is not None and self.path.exists():
            with open(self.path, "r", encoding="utf-8") as fh:
                self._data = json.load(fh)
        else:
            self._data = {}

    @property
    def data(self) -> dict:
        return self._data

    def is_active(self, symbol: str, alert_type: str) -> bool:
        return self._data.get(symbol, {}).get(alert_type, {}).get("status") == "active"

    def get(self, symbol: str, alert_type: str) -> dict:
        return self._data.get(symbol, {}).get(alert_type, {}) or {}

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
        """以「先寫暫存檔再替換」的方式寫入，避免中斷造成檔案損毀。"""
        if self.path is None:
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=2)
        tmp.replace(self.path)
