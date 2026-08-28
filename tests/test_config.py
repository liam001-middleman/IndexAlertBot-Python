"""config 的市場篩選測試。"""
from src.config import AlertConfig, AssetConfig, Config


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
