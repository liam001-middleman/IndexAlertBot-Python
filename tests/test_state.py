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


def test_load_tolerates_bom(tmp_path):
    """檔頭帶 UTF-8 BOM（PowerShell/部分編輯器會寫入）時不應崩潰。"""
    path = tmp_path / "alert_state.json"
    body = b'{"AAPL": {"rsi_overbought": {"status": "active", "last_value": 75.5}}}'
    path.write_bytes(b"\xef\xbb\xbf" + body)
    store = StateStore(path)
    store.load()
    assert store.is_active("AAPL", "rsi_overbought")
    assert store.get("AAPL", "rsi_overbought")["last_value"] == 75.5


def test_load_corrupt_file_backs_up_and_resets(tmp_path):
    """內容損毀時應備份壞檔並以空狀態繼續，而不是拋錯。"""
    path = tmp_path / "alert_state.json"
    path.write_text("{ broken json !!!", encoding="utf-8")
    store = StateStore(path)
    store.load()
    assert store.data == {}
    backups = list(tmp_path.glob("alert_state.json.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{ broken json !!!"


def test_in_memory_store_no_save_error():
    store = StateStore(None)
    store.load()
    store.mark_active("TSLA", "rsi_oversold", 20.0)
    assert store.is_active("TSLA", "rsi_oversold")
    store.save()  # path=None 時不應拋錯
