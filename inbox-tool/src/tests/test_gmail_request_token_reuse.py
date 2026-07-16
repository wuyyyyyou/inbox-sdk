"""首页 Gmail 摘要拉取的凭据复用回归测试。

运行：uv --directory inbox-tool/src run python tests/test_gmail_request_token_reuse.py
"""

from __future__ import annotations

import sys
from contextvars import copy_context
from pathlib import Path
from unittest.mock import patch


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_scoped_gmail_requests_resolve_platform_token_once() -> None:
    """复制给摘要 worker 的调用上下文必须复用同一个 token。"""
    from mail_agent.mail_providers.gmail import adapter

    token_calls: list[str] = []

    def fake_get_token(mailbox: str) -> str:
        token_calls.append(mailbox)
        return "short-lived-token"

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> bool:
            return False

        def read(self) -> bytes:
            return b"{}"

    with (
        patch.object(adapter, "get_access_token", side_effect=fake_get_token),
        patch("urllib.request.urlopen", return_value=FakeResponse()),
    ):
        with adapter._gmail_request_token_scope():
            adapter.gmail_request("user@example.com", "/users/me/messages")
            worker_context = copy_context()
        worker_context.run(adapter.gmail_request, "user@example.com", "/users/me/messages/message-1")

    assert token_calls == ["user@example.com"]


if __name__ == "__main__":
    test_scoped_gmail_requests_resolve_platform_token_once()
    print("Gmail request token reuse: OK")
