"""總體經濟模組測試：解析純函式、快照序列化／快取讀寫、報告文字。

所有測試都不連網：只測 parse_* / build_point / build_macro_lines 等純函式，
以及快照的序列化與降級邏輯（失敗的抓取用 monkeypatch 模擬）。
"""
import json

import pytest

import src.macro as macro_module
from src.config import MacroConfig
from src.macro import (MacroPoint, MacroSnapshot, apply_tnx_fallback, build_macro_lines,
                       build_point, load_snapshot, parse_bls_payload, parse_fed_rss,
                       parse_fred_observations, parse_yahoo_news, refresh_snapshot,
                       save_snapshot)


def make_point(key="cpi", value=300.0, period="2026-08", **kwargs):
    label, unit = macro_module.MONTHLY_LABELS.get(key, (key, ""))
    data = {"key": key, "label": label, "unit": unit, "period": period, "value": value}
    data.update(kwargs)
    return MacroPoint(**data)


def test_parse_bls_payload_skips_m13_and_missing_values():
    payload = {
        "Results": {"series": [{
            "seriesID": "CUUR0000SA0",
            "data": [
                {"year": "2026", "period": "M08", "value": "300.1"},
                {"year": "2026", "period": "M13", "value": "299.0"},
                {"year": "2026", "period": "M07", "value": "-"},
                {"year": "2025", "period": "M12", "value": "295.0"},
            ],
        }]}
    }
    result = parse_bls_payload(payload)
    assert result == {"CUUR0000SA0": [("2025-12", 295.0), ("2026-08", 300.1)]}


def test_parse_fred_observations_sorted_and_skips_dot():
    payload = {"observations": [
        {"date": "2026-09-25", "value": "4.12"},
        {"date": "2026-09-24", "value": "4.10"},
        {"date": "2026-09-23", "value": "."},
    ]}
    assert parse_fred_observations(payload) == [("2026-09-24", 4.10), ("2026-09-25", 4.12)]


def test_parse_fed_rss_extracts_title_and_source_date():
    xml = (
        "<rss><channel>"
        "<item><title>FOMC statement</title>"
        "<pubDate>Mon, 21 Sep 2026 18:00:00 GMT</pubDate></item>"
        "<item><title>Speech by Governor</title>"
        "<pubDate>Tue, 15 Sep 2026 12:00:00 GMT</pubDate></item>"
        "</channel></rss>"
    )
    events = parse_fed_rss(xml, limit=5)
    assert [e["title"] for e in events] == ["FOMC statement", "Speech by Governor"]
    assert events[0]["date"] == "2026-09-21"
    assert events[1]["url"] == ""


def test_parse_yahoo_news_filters_keywords_and_dedups():
    items = [
        {"content": {"title": "Fed signals rate cut",
                     "provider": {"displayName": "Reuters"},
                     "pubDate": "2026-09-26T10:00:00Z"}},
        {"content": {"title": "Fed signals rate cut", "provider": {"displayName": "Reuters"}}},
        {"content": {"title": "Local team wins trophy", "provider": {"displayName": "Sports"}}},
        {"title": "Tariff talks resume", "publisher": "AP"},
        {"title": "Inflation cools again", "providerPublishTime": 1790000000},
    ]
    news = parse_yahoo_news(items, ["fed", "關稅", "tariff", "inflation"], limit=5)
    assert [n["title"] for n in news] == ["Fed signals rate cut", "Tariff talks resume",
                                          "Inflation cools again"]
    assert news[0]["publisher"] == "Reuters"
    assert news[0]["date"] == "2026-09-26"
    assert news[1]["publisher"] == "AP"
    assert news[2]["date"].startswith("2026-")   # providerPublishTime 秒級時間戳
    assert len(parse_yahoo_news(items, ["fed"], limit=1)) == 1


def test_build_point_yoy_mom_and_change():
    rows = [(f"2025-0{m}", 100.0 + m) for m in range(1, 10)] + [("2025-10", 115.0)]
    rows += [(f"2026-0{m}", 120.0 + m) for m in range(1, 9)]
    point = build_point("cpi", rows)
    assert point.period == "2026-08"
    assert point.value == 128.0
    assert point.change == pytest.approx(1.0)
    assert point.yoy is not None and point.yoy > 0
    assert point.change_3m == pytest.approx(3.0)


def test_build_point_requires_two_rows():
    assert build_point("cpi", []) is None
    assert build_point("cpi", [("2026-08", 1.0)]) is None


def test_snapshot_roundtrip_keeps_sections_and_history():
    snapshot = MacroSnapshot(
        sections={"bls": {"ok": True, "at": "2026-09-27T00:00:00+00:00", "error": None}},
        monthly={"cpi": make_point(yoy=3.2)},
        rates={"dgs10": 4.12},
        prices={"^VIX": 15.5},
        history={"^VIX": {"dates": ["2026-09-25"], "closes": [15.5]}},
        events=[{"title": "FOMC", "date": "2026-09-21", "source": "monetary"}],
        news=[{"title": "Fed", "publisher": "Reuters", "date": "2026-09-26"}],
        errors=["FRED：未設定金鑰"],
    )
    restored = MacroSnapshot.from_dict(json.loads(json.dumps(snapshot.to_dict())))
    assert restored.monthly["cpi"].yoy == 3.2
    assert restored.rates["dgs10"] == 4.12
    assert restored.history["^VIX"]["closes"] == [15.5]
    assert restored.events[0]["title"] == "FOMC"
    assert restored.errors == ["FRED：未設定金鑰"]
    assert restored.section_ok("bls") is True


def test_load_snapshot_missing_and_corrupt(tmp_path):
    assert load_snapshot(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_snapshot(bad) is None
    assert load_snapshot(None) is None


def test_save_snapshot_sets_fetched_at_and_is_readable(tmp_path):
    path = tmp_path / "macro_snapshot.json"
    snapshot = MacroSnapshot(prices={"^VIX": 14.2})
    save_snapshot(snapshot, path)
    restored = load_snapshot(path)
    assert restored is not None
    assert restored.prices["^VIX"] == 14.2
    assert restored.fetched_at  # 寫檔時自動蓋上時間戳
    assert not (tmp_path / "macro_snapshot.json.tmp").exists()  # 暫存檔已置換


def test_is_fresh_respects_ttl_and_missing_section():
    snapshot = MacroSnapshot(sections={"bls": {"ok": True, "at": macro_module._now_iso()}})
    assert snapshot.is_fresh("bls", ttl_hours=24) is True
    assert snapshot.is_fresh("fred", ttl_hours=24) is False
    snapshot.sections["bls"] = {"ok": False, "at": macro_module._now_iso(), "error": "x"}
    assert snapshot.is_fresh("bls", ttl_hours=24) is False


def test_data_date_uses_latest_successful_section_only():
    snapshot = MacroSnapshot(sections={
        "bls": {"ok": True, "at": "2026-09-20T00:00:00+00:00"},
        "fred": {"ok": False, "at": "2026-09-28T00:00:00+00:00", "error": "boom"},
    })
    assert snapshot.data_date() == "2026-09-20"
    assert MacroSnapshot().data_date() == "無資料"
    assert snapshot.unavailable() == [macro_module.SECTION_LABELS["fred"]]


def test_refresh_snapshot_readonly_returns_copy_without_network(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("唯讀模式不應該連網")

    monkeypatch.setattr(macro_module, "fetch_bls_series", boom)
    monkeypatch.setattr(macro_module, "fetch_macro_prices", boom)
    cfg = MacroConfig()
    existing = MacroSnapshot(prices={"^VIX": 14.2},
                             sections={"prices": {"ok": True, "at": macro_module._now_iso()}})
    result = refresh_snapshot(cfg, existing, readonly=True)
    assert result.prices == {"^VIX": 14.2}
    assert result.version == macro_module.SNAPSHOT_VERSION
    assert existing.version == macro_module.SNAPSHOT_VERSION  # 不改動傳入物件


def test_refresh_snapshot_marks_failures_and_keeps_old_data(monkeypatch):
    def boom(*args, **kwargs):
        raise macro_module.MacroFetchError("模擬失敗")

    for name in ("fetch_bls_series", "fetch_fred_series", "fetch_macro_prices",
                 "fetch_fed_events", "fetch_macro_news"):
        monkeypatch.setattr(macro_module, name, boom)

    cfg = MacroConfig(fred_api_key="dummy-key")
    existing = MacroSnapshot(
        monthly={"cpi": make_point(yoy=3.1)},
        prices={"^VIX": 14.2},
        sections={"bls": {"ok": True, "at": "2026-09-01T00:00:00+00:00", "error": None},
                  "prices": {"ok": True, "at": "2026-09-01T00:00:00+00:00", "error": None}},
    )
    result = refresh_snapshot(cfg, existing, force=True)
    # 舊資料保留（不會因為抓取失敗而被清空）
    assert result.monthly["cpi"].yoy == 3.1
    assert result.prices["^VIX"] == 14.2
    assert result.section_ok("bls") is False
    assert result.sections["bls"]["at"] == "2026-09-01T00:00:00+00:00"  # 上次成功時間保留
    assert len(result.errors) == len(macro_module.SECTION_LABELS)
    assert any("模擬失敗" in err for err in result.errors)


def test_refresh_snapshot_disabled_reports_reason():
    result = refresh_snapshot(MacroConfig(enabled=False), MacroSnapshot(), force=True)
    assert result.errors == ["macro.enabled=false：已停用總體經濟資料"]


def test_refresh_snapshot_skips_fresh_sections(monkeypatch):
    calls = []

    def spy_bls(series_ids, **kwargs):
        calls.append(series_ids)
        return {}

    monkeypatch.setattr(macro_module, "fetch_bls_series", spy_bls)
    monkeypatch.setattr(macro_module, "fetch_macro_prices", lambda symbols, **kw: ({}, {}))
    monkeypatch.setattr(macro_module, "fetch_fed_events", lambda **kw: [])
    monkeypatch.setattr(macro_module, "fetch_macro_news", lambda keywords, **kw: [])
    monkeypatch.setattr(macro_module, "fetch_fred_series", lambda ids, key, **kw: {})

    now = macro_module._now_iso()
    cfg = MacroConfig()
    existing = MacroSnapshot(sections={
        "bls": {"ok": True, "at": now},
        "fred": {"ok": True, "at": now},
        "prices": {"ok": True, "at": now},
        "fed_rss": {"ok": True, "at": now},
        "news": {"ok": True, "at": now},
    })
    result = refresh_snapshot(cfg, existing)
    assert calls == []          # 全部都在 TTL 內，不重抓
    assert result.errors == []
    assert result.section_ok("bls") is True


def test_refresh_snapshot_price_symbols_fall_back_to_defaults(monkeypatch):
    """macro.price_symbols 留空（或未設定）時，價格區塊用 src/macro.py 的預設清單。"""
    captured = []

    def spy_prices(symbols, **kwargs):
        captured.append(list(symbols))
        return {}, {}

    monkeypatch.setattr(macro_module, "fetch_macro_prices", spy_prices)
    monkeypatch.setattr(macro_module, "fetch_bls_series", lambda series_ids, **kw: {})
    monkeypatch.setattr(macro_module, "fetch_fred_series", lambda ids, key, **kw: {})
    monkeypatch.setattr(macro_module, "fetch_fed_events", lambda **kw: [])
    monkeypatch.setattr(macro_module, "fetch_macro_news", lambda keywords, **kw: [])

    refresh_snapshot(MacroConfig(), MacroSnapshot(), force=True)                    # 未提供
    refresh_snapshot(MacroConfig(), MacroSnapshot(), force=True, price_symbols=[])  # 空清單
    refresh_snapshot(MacroConfig(), MacroSnapshot(), force=True, price_symbols=["^VIX"])

    assert captured[0] == macro_module.DEFAULT_MACRO_SYMBOLS
    assert captured[1] == macro_module.DEFAULT_MACRO_SYMBOLS
    assert captured[2] == ["^VIX"]        # 有設定時以設定為準


def test_snapshot_from_dict_ignores_bad_numeric_values():
    restored = MacroSnapshot.from_dict({"rates": {"dgs10": "abc", "dxy": 100.5}})
    assert restored.rates == {"dxy": 100.5}


def test_history_change_uses_window():
    history = {"closes": [100.0] * 20 + [110.0]}
    assert macro_module._history_change(history, 20) == pytest.approx(10.0)
    assert macro_module._history_change(history, 25) is None
    assert macro_module._history_change(None, 20) is None


def test_apply_tnx_fallback_fills_dgs10_when_fred_missing():
    """未設定 FRED 時，用 ^TNX 日線補上 10Y 殖利率水位與 20 日變化（bp）。"""
    snapshot = MacroSnapshot(prices={"^TNX": 5.18},
                             history={"^TNX": {"dates": ["2026-09-26"] * 21,
                                               "closes": [4.67] * 20 + [5.18]}})
    result = apply_tnx_fallback(snapshot)
    assert result.rates["dgs10"] == pytest.approx(5.18)
    assert result.rates["dgs10_20d_ago"] == pytest.approx(4.67)
    assert result.rates["dgs10_bp_20d"] == pytest.approx(51.0)


def test_apply_tnx_fallback_never_overwrites_fred_and_needs_samples():
    fred = MacroSnapshot(rates={"dgs10": 4.123, "dgs10_bp_20d": 42.0},
                         history={"^TNX": {"closes": [4.0] * 20 + [5.0]}})
    result = apply_tnx_fallback(fred)
    assert result.rates["dgs10"] == 4.123        # FRED 數值優先
    assert result.rates["dgs10_bp_20d"] == 42.0
    assert result.rates.get("dgs10_20d_ago") is None  # 沒有動到其他欄位

    # 樣本不足：不推估（單筆或不足 21 筆都不補 20 日變化）
    assert apply_tnx_fallback(MacroSnapshot(history={"^TNX": {"closes": [4.0]}})).rates == {}
    partial = apply_tnx_fallback(MacroSnapshot(history={"^TNX": {"closes": [4.0] * 10 + [4.5]}}))
    assert partial.rates == {"dgs10": 4.5}
    assert apply_tnx_fallback(MacroSnapshot()).rates == {}


def test_refresh_snapshot_readonly_still_applies_tnx_fallback(monkeypatch):
    """唯讀模式不連網，但仍可用快照內的 ^TNX 補欄位，且不改動傳入物件。"""
    def boom(*args, **kwargs):
        raise AssertionError("唯讀模式不應該連網")

    monkeypatch.setattr(macro_module, "fetch_macro_prices", boom)
    existing = MacroSnapshot(prices={"^TNX": 4.5},
                             history={"^TNX": {"closes": [4.2] * 20 + [4.5]}},
                             sections={"prices": {"ok": True, "at": macro_module._now_iso()}})
    result = refresh_snapshot(MacroConfig(), existing, readonly=True)
    assert result.rates["dgs10_bp_20d"] == pytest.approx(30.0)
    assert existing.rates == {}                  # 傳入物件不被修改


def test_market_bits_skip_tnx_when_rates_already_show_dgs10():
    """10Y 殖利率已由利率段落呈現時，市場段落不再重複列出 ^TNX。"""
    with_rates = MacroSnapshot(rates={"dgs10": 4.5, "dgs10_bp_20d": 30.0},
                               prices={"^TNX": 4.5, "^VIX": 15.0},
                               history={"^TNX": {"closes": [4.2] * 20 + [4.5]}})
    text = "\n".join(build_macro_lines(with_rates))
    assert "10 年期公債殖利率 4.500%（近 20 交易日 +30bp）" in text
    assert "美 10 年期殖利率" not in text          # 沒有重複條目
    assert "VIX 15.00" in text

    no_rates = MacroSnapshot(prices={"^TNX": 4.5},
                             history={"^TNX": {"closes": [4.2] * 20 + [4.5]}})
    assert "美 10 年期殖利率（Yahoo 即時） 4.50" in "\n".join(build_macro_lines(no_rates))


def test_build_macro_lines_with_full_snapshot():
    snapshot = MacroSnapshot(
        sections={"bls": {"ok": True, "at": "2026-09-27T02:00:00+00:00", "error": None}},
        monthly={
            "cpi": make_point(yoy=3.4, prev_yoy=3.1, mom=0.3),
            "nfp": make_point(key="nfp", value=120.0, change=120.0, prev_change=200.0),
        },
        rates={"fed_funds_upper": 4.0, "fed_funds_lower": 3.75, "fed_funds_prev": 4.25,
               "fed_funds_changed_at": "2026-09-17", "dgs10": 4.123, "dgs10_bp_20d": 42.0},
        prices={"^VIX": 18.4},
        history={"^VIX": {"dates": ["2026-09-01", "2026-09-26"], "closes": [15.0, 18.4]}},
        events=[{"title": "FOMC statement", "date": "2026-09-17", "source": "monetary"}],
        news=[{"title": "Fed holds rates", "publisher": "Reuters", "date": "2026-09-17"}],
    )
    lines = build_macro_lines(snapshot)
    text = "\n".join(lines)
    assert lines[0].startswith("當前總體環境（資料時間 2026-09-27")
    assert "CPI 年增 3.40%" in text
    assert "非農就業月增 +120 千人" in text
    assert "Fed 政策利率 3.75~4.00%（2026-09-17 降息 1 碼）" in text
    assert "10 年期公債殖利率 4.123%（近 20 交易日 +42bp）" in text
    assert "VIX 18.40" in text
    assert "FOMC statement" in text
    assert "Fed holds rates" in text
    assert not any("本回合無法取得" in line for line in lines)
    assert lines[-1].startswith("提醒：")


def test_build_macro_lines_reports_missing_sections_and_empty_snapshot():
    empty_lines = build_macro_lines(MacroSnapshot())
    assert empty_lines == []
    assert build_macro_lines(None) == []

    snapshot = MacroSnapshot(
        sections={"fred": {"ok": False, "at": None, "error": "未設定 FRED_API_KEY"}},
        prices={"^VIX": 14.0},
    )
    lines = build_macro_lines(snapshot)
    assert any("本回合無法取得" in line and "FRED" in line for line in lines)


def test_build_macro_lines_omits_missing_fields():
    snapshot = MacroSnapshot(sections={"bls": {"ok": True, "at": "2026-09-27T02:00:00+00:00"}},
                             monthly={"cpi": make_point(yoy=3.4)})  # 沒有 prev_yoy / mom
    text = "\n".join(build_macro_lines(snapshot))
    assert "CPI 年增 3.40%" in text
    assert "前值" not in text
    assert "月增" not in text


