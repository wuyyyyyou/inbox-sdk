"""连通性检测的回归测试。

覆盖反向 RPC 响应直通、Gmail 总预算传递，以及 manifest 工具声明。
运行：uv --directory inbox-tool/src run python tests/test_connectivity_status.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch

_SRC = str(Path(__file__).resolve().parents[1])
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


class _GmailProfileResponse:
    """提供最小的 urlopen 上下文响应，避免测试访问真实 Gmail。"""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return b'{"emailAddress":"checked@example.com"}'


def test_reverse_response_bypasses_request_handler() -> None:
    """反向响应必须立即路由，不能进入会被业务 invoke 占满的处理器。"""
    from anna_inbox_executa import main

    frame = json.dumps({"jsonrpc": "2.0", "id": "sampling-response", "result": {}})
    with (
        patch.object(main.common.sampling, "dispatch_response", return_value=True) as dispatch,
        patch.object(main, "handle_request") as handle_request,
    ):
        main.handle_line(frame)

    dispatch.assert_called_once()
    handle_request.assert_not_called()


def test_gmail_check_uses_one_total_budget() -> None:
    """账号刷新、取 token 与 Gmail HTTP 必须共享一次检测的剩余时间。"""
    from anna_inbox_executa import gmail_tools

    with (
        patch("mail_agent.mail_providers.gmail.adapter.get_platform_account", return_value={}),
        patch.object(gmail_tools, "refresh_platform_google_accounts", return_value=[]) as refresh,
        patch("mail_agent.mail_providers.gmail.adapter.get_access_token", return_value="short-lived-token") as get_token,
        patch("urllib.request.urlopen", return_value=_GmailProfileResponse()) as urlopen,
    ):
        result = gmail_tools._check_gmail_api_status("checked@example.com", timeout_seconds=2.0)

    assert result["ok"] is True
    assert result["mailbox"] == "checked@example.com"
    assert refresh.call_args.kwargs["timeout_seconds"] <= 2.0
    assert get_token.call_args.kwargs["platform_token_timeout_seconds"] <= 2.0
    assert get_token.call_args.kwargs["token_refresh_timeout_seconds"] <= 2.0
    assert get_token.call_args.kwargs["refresh_platform_accounts"] is False
    assert urlopen.call_args.kwargs["timeout"] <= 2.0


def test_gmail_check_exposes_http_error_code() -> None:
    """Gmail HTTP 错误必须返回状态码，供前端展示对应的处理提示。"""
    from anna_inbox_executa import gmail_tools

    error = urllib.error.HTTPError(
        "https://gmail.googleapis.com",
        400,
        "Bad Request",
        Message(),
        None,
    )
    with (
        patch("mail_agent.mail_providers.gmail.adapter.get_platform_account", return_value={}),
        patch.object(gmail_tools, "refresh_platform_google_accounts", return_value=[]),
        patch("mail_agent.mail_providers.gmail.adapter.get_access_token", return_value="short-lived-token"),
        patch("urllib.request.urlopen", side_effect=error),
    ):
        result = gmail_tools._check_gmail_api_status("checked@example.com", timeout_seconds=2.0)

    assert result["error_code"] == "400"


def test_manifest_declares_both_connectivity_tools() -> None:
    """前端调用的两项检测都必须在发布 manifest 中声明。"""
    manifest_path = Path(__file__).resolve().parents[2] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tools = {str(tool.get("name")) for tool in manifest.get("tools", [])}
    assert {"check_sampling_status", "check_gmail_api_status"} <= tools


if __name__ == "__main__":
    test_reverse_response_bypasses_request_handler()
    test_gmail_check_uses_one_total_budget()
    test_gmail_check_exposes_http_error_code()
    test_manifest_declares_both_connectivity_tools()
    print("Connectivity status: OK")
