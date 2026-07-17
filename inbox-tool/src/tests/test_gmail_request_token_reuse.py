"""首页 Gmail 摘要拉取的凭据复用回归测试。

运行：uv --directory inbox-tool/src run python tests/test_gmail_request_token_reuse.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
from contextvars import copy_context
from pathlib import Path
from unittest.mock import patch


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class FakeResponse:
    def __init__(self, body: bytes = b"{}"):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def test_scoped_gmail_requests_resolve_platform_token_once() -> None:
    """复制给摘要 worker 的调用上下文必须复用同一个 token。"""
    from mail_agent.mail_providers.gmail import adapter

    token_calls: list[str] = []

    def fake_get_token(mailbox: str, **_kwargs) -> str:
        token_calls.append(mailbox)
        return "short-lived-token"

    with (
        patch.object(adapter, "get_access_token", side_effect=fake_get_token),
        patch("urllib.request.urlopen", return_value=FakeResponse()),
    ):
        with adapter._gmail_request_token_scope(mailbox="user@example.com"):
            adapter.gmail_request("user@example.com", "/users/me/messages")
            worker_context = copy_context()
        worker_context.run(adapter.gmail_request, "user@example.com", "/users/me/messages/message-1")

    assert token_calls == ["user@example.com"]


def test_ask_full_message_workers_inherit_request_token() -> None:
    """Ask 的全文缓存 worker 必须复用搜索请求的短期 token。"""
    from mail_agent.mail_providers.gmail import adapter

    observed_tokens: list[str | None] = []
    token_calls: list[str] = []

    def fake_get_token(mailbox: str, **_kwargs) -> str:
        token_calls.append(mailbox)
        return "short-lived-token"

    def fake_search(mailbox: str, _query: str, _limit: int) -> list[str]:
        # 真实路径：search 内的 gmail_request 会把 token 写入活跃 scope。
        adapter.gmail_request(mailbox, "/users/me/messages")
        return ["message-1", "message-2"]

    def fake_fetch(_mailbox: str, message_id: str) -> dict:
        scoped = adapter._gmail_request_token.get()
        observed_tokens.append(scoped[1] if scoped else None)
        return {"id": message_id, "internal_date": "1"}

    with (
        patch.object(adapter, "get_access_token", side_effect=fake_get_token),
        patch("urllib.request.urlopen", return_value=FakeResponse()),
        patch.object(adapter, "search_gmail", side_effect=fake_search),
        patch.object(adapter, "read_cache", return_value={"messages": []}),
        patch.object(adapter, "fetch_and_cache_message", side_effect=fake_fetch),
        patch.object(adapter, "message_summary", side_effect=lambda item: item),
        patch.object(adapter, "write_index"),
    ):
        returned = adapter.live_search_and_cache("user@example.com", "newer_than:30d", 2)

    assert returned == ["message-1", "message-2"]
    assert observed_tokens == ["short-lived-token", "short-lived-token"]
    assert token_calls == ["user@example.com"]


def test_gmail_request_retries_once_after_401_with_force_refresh() -> None:
    """Gmail 401 后必须 force_refresh 换票并只重试一次。"""
    from mail_agent.mail_providers.gmail import adapter

    token_kwargs: list[dict] = []
    auth_headers: list[str] = []

    def fake_get_token(_mailbox: str, **kwargs) -> str:
        token_kwargs.append(dict(kwargs))
        if kwargs.get("force_refresh"):
            return "fresh-token"
        return "stale-token"

    def fake_urlopen(req, timeout=60):
        auth_headers.append(req.get_header("Authorization") or "")
        if "stale-token" in auth_headers[-1]:
            raise urllib.error.HTTPError(
                req.full_url,
                401,
                "Unauthorized",
                hdrs=None,
                fp=__import__("io").BytesIO(
                    json.dumps({"error": {"code": 401, "status": "UNAUTHENTICATED"}}).encode()
                ),
            )
        return FakeResponse(b'{"emailAddress":"user@example.com"}')

    with (
        patch.object(adapter, "get_access_token", side_effect=fake_get_token),
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        with adapter._gmail_request_token_scope(mailbox="user@example.com"):
            payload = adapter.gmail_request("user@example.com", "/users/me/profile")
            assert adapter._gmail_request_token.get() == ("user@example.com", "fresh-token")

    assert payload == {"emailAddress": "user@example.com"}
    assert token_kwargs == [{"force_refresh": False}, {"force_refresh": True}]
    assert auth_headers == ["Bearer stale-token", "Bearer fresh-token"]


def test_gmail_request_does_not_retry_when_access_token_is_explicit() -> None:
    """调用方显式传入 access_token 时，401 不应自动 force_refresh。"""
    from mail_agent.mail_providers.gmail import adapter

    token_calls = 0

    def fake_get_token(*_args, **_kwargs) -> str:
        nonlocal token_calls
        token_calls += 1
        return "should-not-be-used"

    def fake_urlopen(req, timeout=60):
        raise urllib.error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            hdrs=None,
            fp=__import__("io").BytesIO(b'{"error":{"code":401}}'),
        )

    with (
        patch.object(adapter, "get_access_token", side_effect=fake_get_token),
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        try:
            adapter.gmail_request(
                "user@example.com",
                "/users/me/profile",
                access_token="caller-owned-token",
            )
            raise AssertionError("expected GmailApiError")
        except adapter.GmailApiError as exc:
            assert exc.status_code == 401

    assert token_calls == 0


def test_force_refresh_ignores_expires_at_for_multi_token() -> None:
    """force_refresh 必须在 expires_at 仍未到期时强制换 multi-token。"""
    from mail_agent.mail_providers.gmail import adapter
    import time

    adapter._multi_token_map.clear()
    adapter.set_platform_accounts([])
    adapter.configure_platform_accounts(None, None)
    adapter.set_multi_tokens([{
        "email": "force@gmail.com",
        "access_token": "still-listed-as-valid",
        "refresh_token": "r",
        "client_id": "c",
        "client_secret": "s",
        # 故意设为远期，模拟时钟上未过期但 Gmail 已拒收的 token
        "expires_at": time.time() + 3600,
    }])

    mock_response = FakeResponse(json.dumps({"access_token": "forced-fresh", "expires_in": 3600}).encode())
    with patch("urllib.request.urlopen", return_value=mock_response):
        token = adapter.get_access_token("force@gmail.com", force_refresh=True)

    assert token == "forced-fresh"
    assert adapter._multi_token_map["force@gmail.com"]["access_token"] == "forced-fresh"
    adapter._multi_token_map.clear()


def test_scoped_token_does_not_leak_across_mailboxes() -> None:
    """同 scope 内不同邮箱不得复用 token；scope 外不得读残留 token。"""
    from mail_agent.mail_providers.gmail import adapter

    token_calls: list[str] = []
    auth_headers: list[str] = []

    def fake_get_token(mailbox: str, **_kwargs) -> str:
        token_calls.append(mailbox)
        return f"token-for-{mailbox}"

    def fake_urlopen(req, timeout=60):
        auth_headers.append(req.get_header("Authorization") or "")
        return FakeResponse(b"{}")

    with (
        patch.object(adapter, "get_access_token", side_effect=fake_get_token),
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        with adapter._gmail_request_token_scope(mailbox="a@example.com"):
            adapter.gmail_request("a@example.com", "/users/me/profile")
            adapter.gmail_request("b@example.com", "/users/me/profile")

        # scope 退出后即使 ContextVar 被错误残留，scope_active=False 也不得复用。
        adapter._gmail_request_token.set(("a@example.com", "leaked-token"))
        adapter.gmail_request("b@example.com", "/users/me/profile")

    assert token_calls == ["a@example.com", "b@example.com", "b@example.com"]
    assert auth_headers == [
        "Bearer token-for-a@example.com",
        "Bearer token-for-b@example.com",
        "Bearer token-for-b@example.com",
    ]


def test_platform_force_refresh_waits_and_reissues_token() -> None:
    """平台 force_refresh 必须冷却后再次 getToken，避免连打同一张废票。"""
    from mail_agent.mail_providers.gmail import adapter

    calls: list[str] = []

    def resolve(account_id: str, _timeout: float) -> str:
        calls.append(account_id)
        return f"token-{len(calls)}"

    adapter.set_platform_accounts([
        {"account_id": "acct-a", "email": "a@example.com", "status": "active"},
    ])
    adapter.configure_platform_accounts(lambda: None, resolve)
    try:
        with patch("mail_agent.mail_providers.gmail.adapter.time.sleep") as sleep_mock:
            token = adapter.get_access_token("a@example.com", force_refresh=True, refresh_platform_accounts=False)
        assert token == "token-2"
        assert calls == ["acct-a", "acct-a"]
        assert sleep_mock.call_count >= 2
    finally:
        adapter.set_platform_accounts([])
        adapter.configure_platform_accounts(None, None)


def test_stale_scoped_token_without_scope_is_ignored() -> None:
    """模拟 ThreadPool worker 残留：无活跃 scope 时不得复用上一邮箱 token。"""
    from mail_agent.mail_providers.gmail import adapter

    token_calls: list[str] = []
    auth_headers: list[str] = []

    def fake_get_token(mailbox: str, **_kwargs) -> str:
        token_calls.append(mailbox)
        return f"fresh-{mailbox}"

    def fake_urlopen(req, timeout=60):
        auth_headers.append(req.get_header("Authorization") or "")
        return FakeResponse(b'{"emailAddress":"riazm4777@gmail.com"}')

    with (
        patch.object(adapter, "get_access_token", side_effect=fake_get_token),
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        # 上一 invoke 在 worker 线程留下的错误残留（修复前的形态）
        adapter._gmail_request_token.set(("kateq@anna.partners", "token-for-kateq"))
        adapter._gmail_request_token_scope_active.set(False)
        adapter.gmail_request("riazm4777@gmail.com", "/users/me/profile")

    assert token_calls == ["riazm4777@gmail.com"]
    assert auth_headers == ["Bearer fresh-riazm4777@gmail.com"]


if __name__ == "__main__":
    test_scoped_gmail_requests_resolve_platform_token_once()
    test_ask_full_message_workers_inherit_request_token()
    test_gmail_request_retries_once_after_401_with_force_refresh()
    test_gmail_request_does_not_retry_when_access_token_is_explicit()
    test_force_refresh_ignores_expires_at_for_multi_token()
    test_scoped_token_does_not_leak_across_mailboxes()
    test_platform_force_refresh_waits_and_reissues_token()
    test_stale_scoped_token_without_scope_is_ignored()
    print("Gmail request token reuse: OK")
