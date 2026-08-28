"""DeepSeek 報告 prompt 與回退報告的單元測試。"""
from datetime import datetime, timezone

from src.models import Alert
from src.notifier import split_message
from src.reporter import build_fallback_report, build_user_prompt


def make_alert(severity="warning", alert_name="RSI 超買", detail="測試詳細內容"):
    return Alert(
        symbol="AAPL",
        name="蘋果",
        market="us",
        alert_type="rsi_overbought",
        alert_name=alert_name,
        severity=severity,
        message="測試摘要",
        detail=detail,
        value=75.0,
        threshold=70.0,
        triggered_at=datetime.now(timezone.utc),
    )


def test_build_user_prompt_contains_alert_info():
    prompt = build_user_prompt([make_alert()])
    assert "蘋果" in prompt
    assert "AAPL" in prompt
    assert "RSI 超買" in prompt
    assert "測試詳細內容" in prompt


def test_build_fallback_report():
    report = build_fallback_report([make_alert(severity="critical")])
    assert "測試詳細內容" in report
    assert "🔴" in report
    assert "僅供參考" in report
    assert "不構成投資建議" in report


def test_split_message_long_text():
    text = "\n".join(f"第 {i} 行：這是一段測試內容，用來確認長訊息會被正確切分。" for i in range(200))
    parts = split_message(text)
    assert len(parts) > 1
    assert all(len(p) <= 4000 for p in parts)
    # 內容不可因切分而遺失
    assert "第 0 行" in parts[0]
    assert "第 199 行" in parts[-1]
