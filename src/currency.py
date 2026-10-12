"""價格顯示幣別：虛擬幣與美股一律以「美金（台幣）」呈現。

分析用的幣別維持不變（加密貨幣是台幣報價、美股是美金、台股是台幣），
這裡只負責把數字換成「顯示幣別」：

- us     ：原值就是美金 → 「314.58 美金（約 9877.81 台幣）」
- crypto ：分析基準是台幣（config 的 convert_to_twd）→ 「82001.21 美金（約 2574838.00 台幣）」
- tw     ：維持台幣

匯率（USD/TWD）不可得時，加密貨幣自動退回台幣顯示，
避免出現「數字其實是台幣、標籤卻寫美金」的不一致。
"""

USD_MARKETS = ("us", "crypto")  # 以美金為顯示主幣別的市場


def _has_rate(usd_twd) -> bool:
    """匯率是否可用（None / 0 / 非數字一律視為不可用）。"""
    try:
        return float(usd_twd) > 0
    except (TypeError, ValueError):
        return False


def display_currency(market: str, usd_twd=None) -> str:
    """顯示主幣別：us 一律美金；crypto 需匯率可得；其餘（含無匯率的 crypto）為台幣。"""
    if market == "us":
        return "美金"
    if market == "crypto" and _has_rate(usd_twd):
        return "美金"
    return "台幣"


def convert_value(value, market: str, usd_twd=None):
    """把「分析幣別」的數值換成「顯示幣別」的數值（None 原樣回傳）。"""
    if value is None:
        return None
    if market == "crypto" and _has_rate(usd_twd):
        return float(value) / float(usd_twd)
    return float(value)


def twd_equivalent(value, market: str, usd_twd=None):
    """回傳台幣等值（「約 X 台幣」用）；無法計算時回 None。"""
    if value is None:
        return None
    if market == "us" and _has_rate(usd_twd):
        return float(value) * float(usd_twd)
    if market == "crypto" and _has_rate(usd_twd):
        return float(value)
    return None


def format_price(value, market: str, usd_twd=None) -> str:
    """單一價格字串（含顯示幣別）：`82001.21 美金` / `1080.00 台幣`。"""
    if value is None:
        return "N/A"
    return f"{convert_value(value, market, usd_twd):.2f} {display_currency(market, usd_twd)}"


def format_price_dual(value, market: str, usd_twd=None) -> str:
    """雙幣別字串：`82001.21 美金（約 2574838.00 台幣）`。

    台股或匯率不可得時退回 format_price（不憑空生出美金）。
    """
    if value is None:
        return "N/A"
    twd = twd_equivalent(value, market, usd_twd)
    if twd is None:
        return format_price(value, market, usd_twd)
    return f"{convert_value(value, market, usd_twd):.2f} 美金（約 {twd:.2f} 台幣）"
