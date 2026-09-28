"""config 的市場篩選與 macro.price_symbols 測試。"""
from src.config import AlertConfig, AssetConfig, Config, MacroConfig, load_config


def make_config():
    return Config(
        assets=[
            AssetConfig(symbol="AAPL", name="蘋果", market="us"),
            AssetConfig(symbol="2330.TW", name="台積電", market="tw"),
            AssetConfig(symbol="btctwd", name="比特幣", market="crypto", provider="max"),
        ],
        alerts_defaults=AlertConfig(),
        alerts_overrides={},
    )


def test_filter_assets_empty_returns_all():
    cfg = make_config()
    symbols = [a.symbol for a in cfg.filter_assets("")]
    assert symbols == ["AAPL", "2330.TW", "btctwd"]


def test_filter_assets_single_market():
    cfg = make_config()
    assert [a.symbol for a in cfg.filter_assets("us")] == ["AAPL"]
    assert [a.symbol for a in cfg.filter_assets("tw")] == ["2330.TW"]
    assert [a.symbol for a in cfg.filter_assets("crypto")] == ["btctwd"]


def test_filter_assets_multiple_markets():
    cfg = make_config()
    assert [a.symbol for a in cfg.filter_assets("us,crypto")] == ["AAPL", "btctwd"]
    assert [a.symbol for a in cfg.filter_assets("us, tw")] == ["AAPL", "2330.TW"]


def test_macro_config_price_symbols_default_empty():
    """總體價格代號預設留空：由 src/macro.py 的預設清單決定（避免 config→macro 循環匯入）。"""
    assert MacroConfig().price_symbols == []


def test_load_config_reads_macro_price_symbols_and_macro_is_not_a_market(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "assets:\n"
        "  - {symbol: AAPL, name: 蘋果, market: us}\n"
        "  - {symbol: 2330.TW, name: 台積電, market: tw}\n"
        "  - {symbol: btctwd, name: 比特幣, market: crypto, provider: max}\n"
        "macro:\n"
        "  price_symbols: ['^TNX', 'DX-Y.NYB', '^VIX']\n",
        encoding="utf-8",
    )

    cfg = load_config(str(path))
    assert cfg.macro.price_symbols == ["^TNX", "DX-Y.NYB", "^VIX"]
    # 總體資產不是追蹤標的：即使誤用 --market us,macro 也只會回傳 us 資產
    assert [a.symbol for a in cfg.filter_assets("us,macro")] == ["AAPL"]
    assert cfg.filter_assets("macro") == []
