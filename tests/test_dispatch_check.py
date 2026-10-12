"""cron-job.org 外部觸發驗證工具（dispatch_check.py）的單元測試。"""
import logging

import pytest
import requests

import dispatch_check
from dispatch_check import (
    build_headers,
    check_workflows,
    dispatch_url,
    explain_status,
    main,
    mask_secret,
    normalize_workflow,
    resolve_pat,
    trigger_dispatch,
)

REPO = "owner/repo"
TOKEN = "github_pat_0123456789abcdefghijklmnop"
PAT_ENVS = ("GITHUB_PAT", "GH_TOKEN", "GITHUB_TOKEN")


class FakeResponse:
    """模擬 requests.Response 的最小介面。"""

    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


@pytest.fixture(autouse=True)
def _clear_pat_env(monkeypatch):
    """避免本機真實 PAT 汙染測試（也確保不會被寫進輸出）。"""
    for name in PAT_ENVS:
        monkeypatch.delenv(name, raising=False)


def test_resolve_pat_priority_order():
    env = {"GITHUB_PAT": "pat-primary", "GH_TOKEN": "pat-secondary"}
    assert resolve_pat(env) == ("pat-primary", "GITHUB_PAT")
    assert resolve_pat({"GH_TOKEN": "pat-secondary"}) == ("pat-secondary", "GH_TOKEN")
    assert resolve_pat({"GITHUB_TOKEN": " pat-third "}) == ("pat-third", "GITHUB_TOKEN")


def test_resolve_pat_missing_returns_empty_tuple():
    assert resolve_pat({}) == ("", "")
    assert resolve_pat({"GITHUB_PAT": "   "}) == ("", "")


def test_mask_secret_never_contains_content():
    masked = mask_secret(TOKEN)
    assert TOKEN not in masked
    assert "長度" in masked
    assert mask_secret("") == "（未設定）"


def test_normalize_workflow_accepts_short_and_full_path():
    expected = ".github/workflows/alerts-us.yml"
    assert normalize_workflow("alerts-us.yml") == expected
    assert normalize_workflow(".github/workflows/alerts-us.yml") == expected
    assert normalize_workflow("/alerts-us.yml") == expected
    with pytest.raises(ValueError):
        normalize_workflow("  ")


def test_dispatch_url_uses_file_name_not_full_path():
    url = dispatch_url("https://api.github.com", REPO, "alerts-tw.yml")
    assert url == (
        "https://api.github.com/repos/owner/repo/actions/workflows/"
        "alerts-tw.yml/dispatches"
    )
    # 端點只接受檔名：傳完整路徑時必須自動收斂成檔名（否則會 404）
    assert dispatch_url("https://api.github.com", REPO, ".github/workflows/alerts-tw.yml") == url
    assert dispatch_url("https://api.github.com", REPO, "/alerts-tw.yml") == url
    # 尾端斜線不影響結果
    assert dispatch_url("https://api.github.com/", REPO, "alerts-tw.yml") == url


@pytest.mark.parametrize("bad_repo", ["owner", "owner/repo/extra", "", "owner/"])
def test_dispatch_url_rejects_bad_repo(bad_repo):
    with pytest.raises(ValueError):
        dispatch_url("https://api.github.com", bad_repo, "alerts-us.yml")


def test_build_headers_omits_authorization_without_token():
    headers = build_headers("")
    assert headers["Accept"] == "application/vnd.github+json"
    assert "Authorization" not in headers
    assert build_headers(TOKEN)["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    "status,keyword",
    [
        (204, "成功"),
        (401, "過期"),
        (403, "權限不足"),
        (404, "檔名"),
        (422, "ref"),
    ],
)
def test_explain_status_known_codes(status, keyword):
    assert keyword in explain_status(status)


def test_explain_status_unknown_and_missing_response():
    assert "500" in explain_status(500)
    assert "連線失敗" in explain_status(None)


def _fake_post(response=None, calls=None, error=None):
    def _post(url, headers=None, json=None, timeout=None):
        if calls is not None:
            calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if error is not None:
            raise error
        return response

    return _post


def test_trigger_dispatch_success_returns_204():
    calls = []
    result = trigger_dispatch(REPO, "alerts-us.yml", token=TOKEN, post=_fake_post(FakeResponse(204), calls))
    assert result["ok"] is True
    assert result["status"] == 204
    assert result["workflow"] == "alerts-us.yml"
    assert result["url"].endswith("/actions/workflows/alerts-us.yml/dispatches")
    assert calls[0]["json"] == {"ref": "master"}
    assert calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_trigger_dispatch_unauthorized_is_not_ok_and_includes_body():
    result = trigger_dispatch(
        REPO,
        "alerts-us.yml",
        token=TOKEN,
        post=_fake_post(FakeResponse(401, '{"message":"Bad credentials"}')),
    )
    assert result["ok"] is False
    assert result["status"] == 401
    assert "過期" in result["message"]
    assert "Bad credentials" in result["message"]


def test_trigger_dispatch_network_error_does_not_raise():
    result = trigger_dispatch(
        REPO,
        "alerts-us.yml",
        token=TOKEN,
        post=_fake_post(error=requests.exceptions.ConnectionError("boom")),
    )
    assert result["ok"] is False
    assert result["status"] is None
    assert "連線失敗" in result["message"]


def test_trigger_dispatch_invalid_repo_reports_argument_error():
    result = trigger_dispatch("owner", "alerts-us.yml", token=TOKEN, post=_fake_post(FakeResponse(204)))
    assert result["ok"] is False
    assert "參數錯誤" in result["message"]


def test_check_workflows_returns_one_result_per_workflow():
    responses = iter([FakeResponse(204), FakeResponse(204), FakeResponse(404, "Not Found")])
    results = check_workflows(
        repo=REPO,
        workflows=["alerts-us.yml", "alerts-tw.yml", "alerts-crypto.yml"],
        token=TOKEN,
        post=lambda *a, **kw: next(responses),
    )
    assert [item["ok"] for item in results] == [True, True, False]
    assert results[2]["status"] == 404


def test_main_returns_0_when_all_workflows_pass(monkeypatch):
    monkeypatch.setenv("GITHUB_PAT", TOKEN)
    monkeypatch.setattr(dispatch_check.requests, "post", _fake_post(FakeResponse(204)))
    assert main([]) == 0


def test_main_returns_1_when_any_workflow_fails(monkeypatch):
    monkeypatch.setenv("GITHUB_PAT", TOKEN)
    monkeypatch.setattr(dispatch_check.requests, "post", _fake_post(FakeResponse(403)))
    assert main([]) == 1


def test_main_returns_2_without_pat(monkeypatch):
    monkeypatch.setattr(
        dispatch_check.requests,
        "post",
        lambda *a, **kw: pytest.fail("沒有 PAT 時不應該發出任何請求"),
    )
    assert main([]) == 2


def test_main_dry_run_does_not_call_api(monkeypatch, capsys):
    monkeypatch.setattr(
        dispatch_check.requests,
        "post",
        lambda *a, **kw: pytest.fail("--dry-run 不應該呼叫 API"),
    )
    assert main(["--dry-run", "--workflow", "alerts-us.yml"]) == 0
    out = capsys.readouterr().out
    assert "actions/workflows/alerts-us.yml/dispatches" in out
    assert ".github/workflows" not in out     # 端點只吃檔名，不能把完整路徑送出去
    assert "{'ref': 'master'}" in out
    assert "Authorization" in out


def test_main_never_leaks_token_in_output(monkeypatch, capsys, caplog):
    monkeypatch.setenv("GITHUB_PAT", TOKEN)
    monkeypatch.setattr(dispatch_check.requests, "post", _fake_post(FakeResponse(401, "Bad credentials")))
    with caplog.at_level(logging.INFO, logger="dispatch-check"):
        assert main([]) == 1
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert TOKEN not in logged
    assert "***" in logged
    assert TOKEN not in capsys.readouterr().out
