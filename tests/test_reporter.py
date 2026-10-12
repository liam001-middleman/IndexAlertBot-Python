"""DeepSeek 報告 prompt 與回退報告的單元測試。"""
from datetime import datetime, timezone

from src.models import Alert
from src.notifier import split_message
from src.reporter import build_fallback_report, build_user_prompt


def make_alert(severity="warning", alert_name="RSI 超買", detail="測試詳細內容",
               market="us", price=314.58, usd_twd=None):
    return Alert(
        symbol="AAPL",
        name="蘋果",
        market=market,
        alert_type="rsi_overbought",
        alert_name=alert_name,
        severity=severity,
        message="測試摘要",
        detail=detail,
        value=75.0,
        threshold=70.0,
        price=price,
        rsi=64.2,
        ma={"20": 300.0, "60": 290.0, "200": 280.0},
        ma_deviation_pct={"20": 4.86, "60": 8.48, "200": 12.35},
        last_report_price=305.0,
        last_report_at="2026-08-29T09:00:00+00:00",
        usd_twd=usd_twd,
        triggered_at=datetime.now(timezone.utc),
    )


def test_build_user_prompt_contains_alert_info():
    prompt = build_user_prompt([make_alert()])
    assert "蘋果" in prompt
    assert "AAPL" in prompt
    assert "RSI 超買" in prompt
    assert "測試詳細內容" in prompt
    assert "314.58" in prompt  # 現價


def test_build_user_prompt_contains_ma_and_last_report():
    prompt = build_user_prompt([make_alert()])
    assert "均線" in prompt
    assert "MA20=300.00" in prompt
    assert "乖離" in prompt
    assert "上次報告價格" in prompt
    assert "305.00" in prompt  # 上次報告價格
    assert "RSI：64.2" in prompt  # RSI 背景資訊


def test_build_fallback_report():
    detail = "蘋果（AAPL）現價 314.58，RSI(14) 為 75.00，已達超買門檻 70.00"
    report = build_fallback_report([make_alert(severity="critical", detail=detail)])
    assert detail in report
    assert "現價 314.58" in report
    assert "均線" in report
    assert "MA20=300.00" in report
    assert "RSI：64.2" in report
    assert "上次報告價格" in report
    assert "僅供參考" in report
    assert "不構成投資建議" in report
    assert "🔴" not in report  # 不使用 emoji 符號


def test_prompt_shows_usd_with_twd_for_us_market():
    """美股：現價以美金為主、台幣放括號；均線標示美金。"""
    prompt = build_user_prompt([make_alert(usd_twd=31.4)])
    assert "現價：314.58 美金（約 9877.81 台幣）" in prompt
    assert "均線（美金）：MA20=300.00（乖離 +4.86%）" in prompt
    assert "上次報告價格：305.00 美金（較上次 +3.14%）" in prompt


def test_prompt_converts_crypto_price_to_usd():
    """加密貨幣：分析基準是台幣，呈現時換成美金（括號保留台幣）。"""
    alert = make_alert(market="crypto", price=2574838.0, usd_twd=31.4)
    alert.ma = {"20": 2669000.0, "60": 2500000.0, "200": None}
    alert.ma_deviation_pct = {"20": -3.53, "60": 2.99, "200": None}
    alert.last_report_price = 2612000.0
    prompt = build_user_prompt([alert])
    assert "現價：82001.21 美金（約 2574838.00 台幣）" in prompt
    assert "均線（美金）：MA20=85000.00（乖離 -3.53%）、MA60=79617.83（乖離 +2.99%）" in prompt
    assert "MA200" not in prompt  # 缺值不列出
    assert "上次報告價格：83184.71 美金（較上次 -1.42%）" in prompt


def test_prompt_keeps_twd_for_tw_market():
    """台股維持台幣，不會出現「約 X 台幣」。"""
    prompt = build_user_prompt([make_alert(market="tw", price=1080.0, usd_twd=31.4)])
    assert "現價：1080.00 台幣" in prompt
    assert "約 " not in prompt
    assert "均線（台幣）：MA20=300.00" in prompt


def test_prompt_without_rate_keeps_single_currency():
    """沒有匯率時只顯示原幣別，數字仍要正確。"""
    prompt = build_user_prompt([make_alert()])
    assert "現價：314.58 美金" in prompt
    assert "美金（約" not in prompt
    assert "均線（美金）：MA20=300.00" in prompt


def test_fallback_report_converts_crypto_ma_to_usd():
    """回退報告的均線也要跟著顯示幣別換算。"""
    alert = make_alert(market="crypto", price=2574838.0, usd_twd=31.4,
                       detail="比特幣（btctwd）現價 82001.21 美金（約 2574838.00 台幣），"
                              "RSI(14) 為 75.00，已達超買門檻 70.00")
    alert.last_report_price = 2612000.0
    report = build_fallback_report([alert])
    assert "現價 82001.21 美金（約 2574838.00 台幣）" in report
    assert "均線（美金）：MA20=9.55（乖離 +4.86%）" in report
    assert "上次報告價格：83184.71 美金（較上次 -1.42%）" in report


def test_system_prompt_forbids_fx_recalculation():
    from src.reporter import SYSTEM_PROMPT
    assert "美金（約" in SYSTEM_PROMPT
    assert "不得自行換算匯率" in SYSTEM_PROMPT


def test_split_message_long_text():
    text = "\n".join(f"第 {i} 行：這是一段測試內容，用來確認長訊息會被正確切分。" for i in range(200))
    parts = split_message(text)
    assert len(parts) > 1
    assert all(len(p) <= 4000 for p in parts)
    # 內容不可因切分而遺失
    assert "第 0 行" in parts[0]
    assert "第 199 行" in parts[-1]
