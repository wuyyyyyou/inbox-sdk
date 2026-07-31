"""Focused tests for bounded Inbox thread-page responses."""

from __future__ import annotations

import asyncio
import base64
from urllib.request import urlopen
from unittest.mock import patch


def _message(index: int, **overrides: object) -> dict[str, object]:
    message: dict[str, object] = {
        "id": f"m{index}",
        "thread_id": "thread-1",
        "internal_date": str(index),
        "from": "sender@example.com",
        "to": "user@example.com",
        "subject": "Subject",
        "label_ids": ["INBOX"],
        "attachments": [],
        "body_text": "Body",
    }
    message.update(overrides)
    return message


async def test_thread_assist_refuses_raw_mail_fallback() -> None:
    """线程概览缺少模型总结时必须失败，不能用主题或正文片段代替。"""
    import anna_inbox_executa.v2_tools as tools

    async def empty_summary_sampling(**_kwargs: object) -> dict[str, object]:
        return {"content": {"type": "text", "text": "{}"}}

    with patch.object(tools, "_load_thread_messages", return_value=[_message(1, snippet="Raw mail snippet")]):
        try:
            await tools._generate_thread_assist_result(
                "user@example.com",
                "thread-1",
                "m1",
                "m1",
                empty_summary_sampling,
            )
        except RuntimeError as exc:
            assert str(exc) == "analysis_unavailable"
        else:
            raise AssertionError("模型空总结不能退化为原邮件内容")
    print("[PASS] test_thread_assist_refuses_raw_mail_fallback")


async def test_thread_assist_retries_truncated_json_with_larger_budget() -> None:
    """概览首次 JSON 截断时，应以更大输出额度重试而非直接报不可用。"""
    import anna_inbox_executa.v2_tools as tools

    calls: list[dict[str, object]] = []

    async def truncated_then_complete_sampling(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        if len(calls) == 1:
            # 无 JSON 骨架：触发 max_attempts 重试（本地 salvage 无法恢复）
            return {"content": {"type": "text", "text": "not a json object"}}
        return {
            "content": {
                "type": "text",
                "text": (
                    '{"overview":"The sender asks you to confirm the delivery date.",'
                    '"needs_reply":true,'
                    '"no_reply_reason":"",'
                    '"quick_replies":[{"id":"confirm","label":"Confirm date",'
                    '"intent":"Draft a reply confirming the delivery date."},'
                    '{"id":"ask","label":"Ask details",'
                    '"intent":"Draft a reply asking for delivery details."}]}'
                ),
            }
        }

    with patch.object(tools, "_load_thread_messages", return_value=[_message(1, body_text="Please confirm the delivery date.")]):
        result = await tools._generate_thread_assist_result(
            "user@example.com",
            "thread-1",
            "m1",
            "m1",
            truncated_then_complete_sampling,
        )

    assert result["overview"] == "The sender asks you to confirm the delivery date."
    assert result["needs_reply"] is True
    assert result["no_reply_reason"] == ""
    assert len(result["quick_replies"]) == 2
    assert [int(call["max_tokens"]) for call in calls] == [768, 1024]
    assert all(call["response_format"] == {"type": "json_object"} for call in calls)
    print("[PASS] test_thread_assist_retries_truncated_json_with_larger_budget")


async def test_thread_assist_no_reply_clears_quick_replies() -> None:
    """不需要回复时不应产出快捷 draft 提示，而应给出原因。"""
    import anna_inbox_executa.v2_tools as tools

    async def no_reply_sampling(**_kwargs: object) -> dict[str, object]:
        return {
            "content": {
                "type": "text",
                "text": (
                    '{"overview":"Stripe sent a payment receipt for your subscription.",'
                    '"needs_reply":false,'
                    '"no_reply_reason":"Automated receipt with no request for a response.",'
                    '"quick_replies":[{"id":"thanks","label":"Say thanks",'
                    '"intent":"Draft a reply saying thanks."}]}'
                ),
            }
        }

    with patch.object(tools, "_load_thread_messages", return_value=[_message(1, body_text="Your receipt is attached.")]):
        result = await tools._generate_thread_assist_result(
            "user@example.com",
            "thread-1",
            "m1",
            "m1",
            no_reply_sampling,
        )

    assert result["needs_reply"] is False
    assert result["quick_replies"] == []
    assert "receipt" in result["no_reply_reason"].lower()
    print("[PASS] test_thread_assist_no_reply_clears_quick_replies")


def main() -> None:
    import anna_inbox_executa.common as common
    import anna_inbox_executa.v2_tools as tools

    assert tools.INBOX_THREAD_PAGE_SIZE == 5

    with patch(
        "mail_agent.mail_providers.gmail.adapter.decode_body_for_display",
        return_value={"html": "<p>Hello</p>", "text": "Hello"},
    ):
        html_payload = tools._display_body_payload(_message(1), limit=24000)
    assert html_payload["body_html"] == "<p>Hello</p>"
    assert "body_text" not in html_payload

    with patch(
        "mail_agent.mail_providers.gmail.adapter.decode_body_for_display",
        return_value={"html": "<p>Hello</p>", "text": "Hello"},
    ):
        prompt_payload = tools._display_body_payload(_message(1), limit=24000, prefer_html=False)
    assert prompt_payload["body_text"] == "Hello"
    assert "body_html" not in prompt_payload

    # Newsletter HTML larger than the old 30k/24k thresholds must retain its
    # layout when the complete response still fits under the 48 KiB host-safe budget.
    newsletter_html = "<html><body><table>" + ("<tr><td>Cloud update</td></tr>" * 1200) + "</table></body></html>"
    encoded_html = base64.urlsafe_b64encode(newsletter_html.encode("utf-8")).decode("ascii")
    newsletter_message = _message(2, payload={
        "mimeType": "text/html",
        "body": {"data": encoded_html},
    })
    from mail_agent.mail_providers.gmail.adapter import decode_body_for_display
    decoded_newsletter = decode_body_for_display(newsletter_message)
    assert len(decoded_newsletter["html"]) > 30_000
    newsletter_display = tools._build_inbox_message_display_response("user@example.com", newsletter_message)
    assert newsletter_display.get("body_html")
    assert "body_text" not in newsletter_display
    assert tools._inbox_message_display_rpc_frame_size(newsletter_display) <= tools.INBOX_THREAD_RESPONSE_MAX_BYTES

    # JSON 转义后的超大 HTML 必须通过 loopback URL 读取，不能再次塞入 JSON-RPC。
    oversized_html = "<html><body><p>" + ("汉" * 100_000) + "</p></body></html>"
    oversized_message = _message(3, payload={
        "mimeType": "text/html",
        "body": {"data": base64.urlsafe_b64encode(oversized_html.encode("utf-8")).decode("ascii")},
    })
    oversized_display = tools._build_inbox_message_display_response("user@example.com", oversized_message)
    assert oversized_display.get("body_url")
    assert "body_html" not in oversized_display
    assert tools._inbox_message_display_rpc_frame_size(oversized_display) <= tools.INBOX_THREAD_RESPONSE_MAX_BYTES
    with urlopen(str(oversized_display["body_url"])) as response:
        assert "汉" * 100 in response.read().decode("utf-8")

    # 超长纯文本同样必须返回完整正文 URL，不能退化为截断后的 URL 列表预览。
    oversized_text = "纯文本正文\n" + ("汉" * 100_000)
    with patch(
        "mail_agent.mail_providers.gmail.adapter.decode_body_for_display",
        return_value={"html": "", "text": oversized_text},
    ):
        oversized_text_display = tools._build_inbox_message_display_response("user@example.com", _message(31))
    assert oversized_text_display.get("body_url")
    assert oversized_text_display["body_truncated"] is False
    with urlopen(str(oversized_text_display["body_url"])) as response:
        assert "纯文本正文" in response.read().decode("utf-8")

    # CID 图片也必须改为同一短期 loopback 服务的 URL，避免 base64 data URI 膨胀正文。
    cid_html = '<html><body><img src="cid:newsletter-logo"></body></html>'
    cid_message = _message(4, payload={
        "mimeType": "multipart/related",
        "parts": [
            {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(cid_html.encode("utf-8")).decode("ascii")}},
            {
                "mimeType": "image/png",
                "headers": [{"name": "Content-ID", "value": "<newsletter-logo>"}],
                "body": {"data": base64.urlsafe_b64encode(b"png-bytes").decode("ascii")},
            },
        ],
    })
    cid_display = tools._build_inbox_message_display_response("user@example.com", cid_message)
    if cid_display.get("body_url"):
        with urlopen(str(cid_display["body_url"])) as response:
            rendered_cid_html = response.read().decode("utf-8")
        assert "cid:newsletter-logo" not in rendered_cid_html
        cid_url = rendered_cid_html.split('src="', 1)[1].split('"', 1)[0]
        with urlopen(cid_url) as response:
            assert response.read() == b"png-bytes"
    else:
        assert "cid:newsletter-logo" not in str(cid_display.get("body_html") or "")
        assert "data:image/png;base64," in str(cid_display.get("body_html") or "")

    with patch(
        "mail_agent.mail_providers.gmail.adapter.decode_body_for_display",
        return_value={"html": "", "text": "Plain body"},
    ):
        text_payload = tools._display_body_payload(_message(1), limit=24000)
    assert text_payload["body_text"] == "Plain body"
    assert "body_html" not in text_payload

    quoted_only = _message(2, body_text="", payload={"mimeType": "text/html"})
    with patch(
        "mail_agent.mail_providers.gmail.adapter.decode_body_for_display",
        return_value={"html": '<div class="gmail_quote"><p>Forwarded details</p></div>', "text": ""},
    ):
        quoted_only_payload = tools._display_body_payload(quoted_only, limit=24000)
    assert "Forwarded details" in str(quoted_only_payload.get("body_html") or "")
    assert quoted_only_payload["body_truncated"] is False

    with patch(
        "mail_agent.mail_providers.gmail.adapter.decode_body_for_display",
        return_value={"html": '<div class="gmail_quote"><p>Forwarded details</p></div>', "text": ""},
    ):
        quoted_only_display = tools._build_inbox_message_display_response("user@example.com", quoted_only)
    assert "Forwarded details" in str(quoted_only_display.get("body_html") or "")

    messages = [_message(index) for index in range(12)]
    with (
        patch.object(tools, "_load_thread_messages", return_value=messages),
        patch.object(
            tools,
            "_display_body_payload",
            side_effect=lambda _message, *, limit: {
                "body_text": "汉" * limit,
                "body_truncated": True,
            },
        ),
    ):
        bounded = tools._build_inbox_thread_page("user@example.com", "thread-1")
    assert bounded["returned_count"] == 5
    assert [item["id"] for item in bounded["messages"]] == ["m7", "m8", "m9", "m10", "m11"]
    assert tools._inbox_thread_rpc_frame_size(bounded) <= tools.INBOX_THREAD_RESPONSE_MAX_BYTES
    assert all("body_html" not in item for item in bounded["messages"])

    with (
        patch.object(tools, "_load_thread_messages", return_value=messages),
        patch.object(
            tools,
            "_display_body_payload",
            side_effect=lambda _message, *, limit: {
                "body_text": str(_message.get("body_text") or ""),
                "body_truncated": False,
            },
        ),
    ):
        anchored = tools._build_inbox_thread_page(
            "user@example.com",
            "thread-1",
            anchor_message_id="m6",
        )
    assert anchored["returned_count"] == 5
    assert [item["id"] for item in anchored["messages"]] == ["m2", "m3", "m4", "m5", "m6"]
    assert anchored["has_earlier"] is True
    assert anchored["next_before_index"] == 2

    subject_changed_messages = [
        _message(1, subject="Original invite title"),
        _message(2, subject="Changed latest title"),
    ]
    with (
        patch.object(tools, "_load_thread_messages", return_value=subject_changed_messages),
        patch.object(
            tools,
            "_display_body_payload",
            side_effect=lambda message, *, limit: {
                "body_text": str(message.get("body_text") or ""),
                "body_truncated": False,
            },
        ),
    ):
        subject_page = tools._build_inbox_thread_page("user@example.com", "thread-1")
    assert subject_page["subject"] == "Original invite title"
    assert subject_page["latest_subject"] == "Changed latest title"

    quick_replies = tools._normalize_quick_replies([
        {"label": "Reply with timing", "intent": "Draft a reply that asks about timing."},
        {"label": "Reply with timing", "intent": "Duplicate label should be ignored."},
        {"id": "summarize_thread", "label": "Summarize", "intent": "Summarize the thread."},
        {"label": "Archive it", "intent": "Archive this email."},
        {"label": "Add todo", "intent": "Add this to todo."},
        {"label": "", "intent": "Ignore missing label."},
    ])
    assert [item["label"] for item in quick_replies] == ["Reply with timing", "Summarize"]
    assert quick_replies[0]["id"] == "reply_with_timing"
    assert quick_replies[1]["intent"].lower().startswith("draft a reply")
    assert tools._is_valid_thread_assist_cache({
        "format_version": tools.THREAD_ASSIST_CACHE_VERSION,
        "overview": "Sender asks for a meeting time.",
        "needs_reply": True,
        "quick_replies": [{"id": "a", "label": "Propose times", "intent": "Draft a reply with times."}],
        "fallback_used": False,
    })
    assert tools._is_valid_thread_assist_cache({
        "format_version": tools.THREAD_ASSIST_CACHE_VERSION,
        "overview": "Payment receipt arrived.",
        "needs_reply": False,
        "no_reply_reason": "Automated receipt.",
        "quick_replies": [],
        "fallback_used": False,
    })
    assert not tools._is_valid_thread_assist_cache({
        "format_version": tools.THREAD_ASSIST_CACHE_VERSION,
        "overview": "Payment receipt arrived.",
        "needs_reply": False,
        "no_reply_reason": "",
        "quick_replies": [],
        "fallback_used": False,
    })

    complete_overview = "Mitce has suspended your Basic service because an overdue payment remains outstanding."
    assert tools._one_line_overview(complete_overview) == complete_overview

    thread_with_draft = [
        _message(1),
        _message(2),
        _message(3, label_ids=["DRAFT"], body_text="Unsent Gmail draft"),
    ]
    with (
        patch.object(tools, "_load_thread_messages", return_value=thread_with_draft),
        patch.object(
            tools,
            "_display_body_payload",
            side_effect=lambda message, *, limit: {
                "body_text": str(message.get("body_text") or ""),
                "body_truncated": False,
            },
        ),
    ):
        visible_page = tools._build_inbox_thread_page("user@example.com", "thread-1")
    assert visible_page["returned_count"] == 2
    assert [item["id"] for item in visible_page["messages"]] == ["m1", "m2"]
    assert visible_page["latest_message_id"] == "m2"
    assert all("DRAFT" not in item["label_ids"] for item in visible_page["messages"])

    metadata_heavy = [_message(index, **{"from": "x" * 100_000}) for index in range(12)]
    with patch.object(tools, "_load_thread_messages", return_value=metadata_heavy):
        reduced_page = tools._build_inbox_thread_page(
            "user@example.com",
            "thread-1",
            include_display_body=False,
        )
    # 超长元数据会在序列化边界被裁剪；裁剪后仍应尽量保留完整首屏。
    assert 0 < reduced_page["returned_count"] <= 5
    assert reduced_page["messages"][-1]["id"] == "m11"
    assert reduced_page["next_before_index"] == 12 - reduced_page["returned_count"]
    assert tools._inbox_thread_rpc_frame_size(reduced_page) <= tools.INBOX_THREAD_RESPONSE_MAX_BYTES

    frame = {
        "jsonrpc": "2.0",
        "id": "request-id",
        "result": {
            "success": True,
            "tool": "get_inbox_thread_page",
            "data": {
                "mailbox": "user@example.com",
                "thread_id": "thread-1",
                "messages": [
                    {"id": f"m{index}", "body_text": "汉" * 24_000, "body_truncated": False}
                    for index in range(5)
                ],
                "returned_count": 5,
                "has_earlier": False,
                "next_before_index": None,
                "latest_message_id": "m4",
            },
        },
    }
    fitted = common._limit_inbox_thread_response_frame(frame)
    assert common._encoded_frame_size(fitted) <= common.MAX_INBOX_THREAD_RESPONSE_BYTES
    assert all(not item.get("body_html") for item in fitted["result"]["data"]["messages"])

    asyncio.run(test_thread_assist_refuses_raw_mail_fallback())
    asyncio.run(test_thread_assist_retries_truncated_json_with_larger_budget())
    asyncio.run(test_thread_assist_no_reply_clears_quick_replies())

    print("PASS inbox thread response budget tests")


if __name__ == "__main__":
    main()
