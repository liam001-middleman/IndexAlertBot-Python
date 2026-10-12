"""價格顯示幣別（美金／台幣）的單元測試。"""
import pytest

from src.currency import (USD_MARKETS, convert_value, display_currency, format_price,
                          format_price_dual, twd_equivalent)

USD_TWD = 31.4  # 測試用匯率


def test_us_market_shows_usd_with_twd_equivalent():
    """美股：原值就是美金，括號補上台幣等值。"""
    assert format_price_dual(314.58, "us", USD_TWD) == "314.58 美金（約 9877.81 台幣）"
    assert display_currency("us", USD_TWD) == "美金"


def test_crypto_market_converts_twd_price_to_usd():
    """加密貨幣：分析基準是台幣，顯示時換算回美金，括號保留原本的台幣價。"""
    assert format_price_dual(2574838.0, "crypto", USD_TWD) == "82001.21 美金（約 2574838.00 台幣）"
    assert format_price(2574838.0, "crypto", USD_TWD) == "82001.21 美金"


def test_tw_market_stays_in_twd_without_parentheses():
    """台股維持台幣，不會出現「約 X 台幣」。"""
    assert format_price(1080.0, "tw", USD_TWD) == "1080.00 台幣"
    assert format_price_dual(1080.0, "tw", USD_TWD) == "1080.00 台幣"
    assert display_currency("tw", USD_TWD) == "台幣"


def test_missing_rate_falls_back_to_original_currency():
    """沒有匯率時不得憑空生出美金：crypto 的數字其實是台幣。"""
    assert format_price_dual(2574838.0, "crypto", None) == "2574838.00 台幣"
    assert format_price(2574838.0, "crypto", None) == "2574838.00 台幣"
    assert display_currency("crypto", None) == "台幣"


@pytest.mark.parametrize("bad_rate", [0, -1, "abc", ""])
def test_invalid_rate_is_treated_as_unavailable(bad_rate):
    """0 / 負數 / 非數字一律視為沒有匯率。"""
    assert display_currency("crypto", bad_rate) == "台幣"
    assert convert_value(100.0, "crypto", bad_rate) == 100.0
    assert format_price_dual(100.0, "crypto", bad_rate) == "100.00 台幣"


def test_us_without_rate_keeps_usd_label_without_parentheses():
    """美股抓不到匯率時仍標美金（數字本來就是美金），只是沒有台幣等值。"""
    assert format_price(314.58, "us", None) == "314.58 美金"
    assert format_price_dual(314.58, "us", None) == "314.58 美金"


def test_convert_value_and_twd_equivalent():
    assert convert_value(314.58, "crypto", USD_TWD) == pytest.approx(314.58 / USD_TWD)
    assert convert_value(314.58, "us", USD_TWD) == 314.58
    assert convert_value(1080.0, "tw", USD_TWD) == 1080.0
    assert twd_equivalent(314.58, "us", USD_TWD) == pytest.approx(314.58 * USD_TWD)
    assert twd_equivalent(2574838.0, "crypto", USD_TWD) == 2574838.0
    assert twd_equivalent(1080.0, "tw", USD_TWD) is None


def test_none_value_renders_na():
    assert format_price(None, "us", USD_TWD) == "N/A"
    assert format_price_dual(None, "crypto", USD_TWD) == "N/A"
    assert convert_value(None, "us", USD_TWD) is None
    assert twd_equivalent(None, "us", USD_TWD) is None


def test_usd_markets_constant():
    assert USD_MARKETS == ("us", "crypto")
