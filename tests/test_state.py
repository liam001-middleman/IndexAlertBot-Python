"""alert_state.json 讀寫與狀態轉換測試。"""
from src.state import StateStore


def test_roundtrip(tmp_path):
    path = tmp_path / "alert_state.json"
    store = StateStore(path)
    store.load()
    store.mark_active("AAPL", "rsi_overbought", 75.5)
    store.save()

    store2 = StateStore(path)
    store2.load()
    assert store2.is_active("AAPL", "rsi_overbought")
    assert store2.get("AAPL", "rsi_overbought")["last_value"] == 75.5
    assert "triggered_at" in store2.get("AAPL", "rsi_overbought")


def test_mark_clear(tmp_path):
    store = StateStore(tmp_path / "alert_state.json")
    store.load()
    store.mark_active("BTC-USD", "intraday_surge", 12.3)
    store.mark_clear("BTC-USD", "intraday_surge", 0.5)
    assert not store.is_active("BTC-USD", "intraday_surge")
    assert store.get("BTC-USD", "intraday_surge")["status"] == "clear"
    assert store.get("BTC-USD", "intraday_surge")["last_value"] == 0.5


def test_missing_file_loads_empty(tmp_path):
    store = StateStore(tmp_path / "nope.json")
    store.load()
    assert store.data == {}


def test_in_memory_store_no_save_error():
    store = StateStore(None)
    store.load()
    store.mark_active("TSLA", "rsi_oversold", 20.0)
    assert store.is_active("TSLA", "rsi_oversold")
    store.save()  # path=None 時不應拋錯
