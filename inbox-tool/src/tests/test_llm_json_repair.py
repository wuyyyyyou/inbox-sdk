"""Anna sampling JSON 解析与本地 salvage 的最小测试（已移除二次 sampling repair）。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class SamplingStub:
    """返回预设文本的最小异步 sampling stub。"""

    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        response = self.responses.pop(0) if self.responses else ""
        text = response.get("text", "") if isinstance(response, dict) else response
        result = {
            "content": {"type": "text", "text": text},
            "model": "test-model",
            "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        }
        if isinstance(response, dict):
            result.update({key: value for key, value in response.items() if key != "text"})
        return result


async def test_valid_json_does_not_trigger_repair() -> None:
    from mail_agent.llm_runtime.service import call_llm_json

    stub = SamplingStub(['{"ok": true}'])
    result = await call_llm_json(
        stub,
        system_prompt="Return JSON.",
        user_message="Return {\"ok\": true}.",
        max_tokens=64,
        max_attempts=1,
        allow_sampling_provider_fallback=False,
    )

    assert result["payload"] == {"ok": True}
    assert result["json_repair_used"] is False
    assert len(stub.calls) == 1


async def test_json_mode_is_forwarded_to_sampling() -> None:
    """Ask 等结构化调用必须把 Host JSON mode 透传到底层 Sampling。"""
    from mail_agent.llm_runtime.service import call_llm_json

    stub = SamplingStub(['{"ok": true}'])
    await call_llm_json(
        stub,
        system_prompt="Return JSON.",
        user_message="Return an object.",
        max_tokens=64,
        response_format={"type": "json_object"},
        on_unsupported="text",
        max_attempts=1,
        allow_sampling_provider_fallback=False,
    )

    assert stub.calls[0]["response_format"] == {"type": "json_object"}
    assert stub.calls[0]["on_unsupported"] == "text"


async def test_local_json_repair_does_not_trigger_llm_repair() -> None:
    from mail_agent.llm_runtime.service import call_llm_json

    stub = SamplingStub(['{"a":"b"\n"c":1}'])
    result = await call_llm_json(
        stub,
        system_prompt="Return JSON.",
        user_message="Return an object.",
        max_tokens=64,
        max_attempts=1,
        allow_sampling_provider_fallback=False,
    )

    assert result["payload"] == {"a": "b", "c": 1}
    assert result["json_repair_used"] is False
    assert len(stub.calls) == 1


async def test_local_json_repair_unescaped_quotes_in_query_string() -> None:
    """QueryPlan 常见：query 值内嵌 Gmail 短语双引号未转义。"""
    from mail_agent.llm_runtime.service import parse_json_response

    broken = (
        '{"intent": "find", "query": "urgent OR "asap" OR "emergency" OR "important"", '
        '"order": "newest", "answer_mode": "llm", "needs": []}'
    )
    payload = parse_json_response(broken)
    assert payload["intent"] == "find"
    assert payload["query"] == 'urgent OR "asap" OR "emergency" OR "important"'
    assert payload["order"] == "newest"
    assert payload["needs"] == []


async def test_local_json_repair_missing_colon_between_key_value() -> None:
    """local agent final 常见：{"action" "final"} 漏冒号。"""
    from mail_agent.llm_runtime.service import parse_json_response

    payload = parse_json_response('{"action" "final"}')
    assert payload == {"action": "final"}


async def test_local_json_repair_invalid_and_truncated_unicode_escapes() -> None:
    """截断/非法 \\uXXXX：尾部残缺丢弃，中段非法转义字面化后仍可解析。"""
    from mail_agent.llm_runtime.service import parse_json_response

    # max_tokens 截断在 \\u 中间：残缺转义丢弃后闭合
    assert parse_json_response('{"a":"hello \\u') == {"a": "hello "}
    assert parse_json_response('{"a":"hello\\u4e8') == {"a": "hello"}
    # 非法四位（非 hex）→ 反斜杠再转义，保留字面
    assert parse_json_response('{"a":"\\u606v ok"}') == {"a": "\\u606v ok"}
    # 真实 route 场景：中间非法 \\u + 尾部截断 \\u，须 salvage 出 tool_call
    broken = (
        '{"action":"tool_call","tool":"query_mail_evidence",'
        '"arguments":{"user_text":"find","recent":"\\u4fe1\\u606v\\uff0c\\u'
    )
    payload = parse_json_response(broken)
    assert payload["action"] == "tool_call"
    assert payload["tool"] == "query_mail_evidence"
    assert payload["arguments"]["user_text"] == "find"
    assert "\\u606v" in payload["arguments"]["recent"]


async def test_local_json_repair_mail_links_missing_commas() -> None:
    """Ask answer 常见：mail_links 对象字段/相邻对象漏逗号，须本地修好。"""
    from mail_agent.llm_runtime.service import parse_json_response

    broken = """
    {
      "title": "Partners",
      "summary": "ok",
      "sections": [{
        "heading": "Items",
        "items": [{
          "subject": "partners",
          "mail_links": [
            {
              "label": "partners"
              "thread_id": "19f5e73b7c72c7b7"
              "message_id": "19f5e73b7c72c7b7"
            }
            {
              "label": "second",
              "thread_id": "abc",
              "message_id": "abc"
            }
          ]
        }]
      }]
    }
    """
    payload = parse_json_response(broken)
    assert payload["title"] == "Partners"
    links = payload["sections"][0]["items"][0]["mail_links"]
    assert links[0]["thread_id"] == "19f5e73b7c72c7b7"
    assert links[1]["label"] == "second"


async def test_truncated_json_is_salvaged_by_closing_brackets() -> None:
    """截断 JSON 通过本地闭合恢复已写出的字段，不再整单失败。"""
    from mail_agent.llm_runtime.service import parse_json_response, salvage_truncated_json

    payload = parse_json_response(
        '{"title":"T","summary":"S","items":[{"subject":"x","from":"a","message_id":"m1"'
    )
    assert payload["title"] == "T"
    assert payload["items"][0]["subject"] == "x"
    assert payload["items"][0]["message_id"] == "m1"
    assert salvage_truncated_json("no brace") is None


async def test_length_stop_reason_retries_even_for_valid_json() -> None:
    """Host 报告达到长度上限时，即使表面合法也必须重试。"""
    from mail_agent.llm_runtime.service import call_llm_json

    stub = SamplingStub([
        {"text": '{"markdown":"short"}', "stopReason": "length"},
        '{"markdown":"complete"}',
    ])
    result = await call_llm_json(
        stub,
        system_prompt="Return JSON only.",
        user_message="Summarize the thread.",
        max_tokens=64,
        max_attempts=2,
        allow_sampling_provider_fallback=False,
    )

    assert result["payload"] == {"markdown": "complete"}
    assert result["json_repair_used"] is False
    assert len(stub.calls) == 2


async def test_truncation_salvage_is_discarded_and_retried() -> None:
    """仅靠闭合半截 JSON 恢复的结果不得作为 AI 回答返回。"""
    from mail_agent.llm_runtime.service import call_llm_json

    stub = SamplingStub([
        '{"markdown":"Participants: Kate (',
        '{"markdown":"complete"}',
    ])
    result = await call_llm_json(
        stub,
        system_prompt="Return JSON only.",
        user_message="Summarize the thread.",
        max_tokens=64,
        max_attempts=2,
        allow_sampling_provider_fallback=False,
    )

    assert result["payload"] == {"markdown": "complete"}
    assert len(stub.calls) == 2


async def test_truncation_fails_without_returning_salvage() -> None:
    """重试额度耗尽时抛错，不能返回任一半截恢复结果。"""
    from mail_agent.llm_runtime.service import call_llm_json

    stub = SamplingStub(['{"markdown":"first', '{"markdown":"second'])
    try:
        await call_llm_json(
            stub,
            system_prompt="Return JSON only.",
            user_message="Summarize the thread.",
            max_tokens=64,
            max_attempts=2,
            allow_sampling_provider_fallback=False,
        )
    except RuntimeError as exc:
        assert "failed after 2 attempts" in str(exc)
    else:
        raise AssertionError("truncated payload must not be returned")
    assert len(stub.calls) == 2


async def test_invalid_json_does_not_trigger_sampling_repair() -> None:
    """含 { 但无法本地闭合的损坏 JSON：不再二次 sampling repair，直接失败。"""
    from mail_agent.llm_runtime.service import call_llm_json

    stub = SamplingStub(['{"a": true true, "b": [1,2,}', '{"a": "b"}'])
    raised = False
    try:
        await call_llm_json(
            stub,
            system_prompt="Return JSON only.",
            user_message='## Output format\n{"a": "string"}\n\n## Emails\nSECRET EMAIL BODY',
            max_tokens=4096,
            max_attempts=1,
            metadata={"tool": "unit_test"},
            allow_sampling_provider_fallback=False,
        )
    except RuntimeError:
        raised = True
    assert raised
    # 只打主任务一次；第二段合法 JSON 不会被当作 repair 调用。
    assert len(stub.calls) == 1
    assert stub.calls[0]["metadata"]["tool"] == "unit_test"
    assert stub.calls[0]["metadata"]["sampling_stage"] == "primary"


async def test_invalid_json_uses_safe_fallback_without_repair() -> None:
    """无法本地闭合时直接走 safe fallback，不再多打一次 repair sampling。"""
    from mail_agent.llm_runtime.service import call_llm_json_safe

    stub = SamplingStub(['{"a": true true, "b": [1,2,}', "still not json"])
    result = await call_llm_json_safe(
        stub,
        system_prompt="Return JSON only.",
        user_message='## Output format\n{"a": "string"}',
        fallback={"fallback": True},
        max_tokens=64,
        max_attempts=1,
        allow_sampling_provider_fallback=False,
    )

    assert result["payload"] == {"fallback": True}
    assert result["fallback_used"] is True
    assert len(stub.calls) == 1


async def test_empty_sampling_response_does_not_trigger_repair() -> None:
    from mail_agent.llm_runtime.service import call_llm_json_safe

    stub = SamplingStub([""])
    result = await call_llm_json_safe(
        stub,
        system_prompt="Return JSON only.",
        user_message='## Output format\n{"a": "string"}',
        fallback={"fallback": True},
        max_tokens=64,
        max_attempts=1,
        allow_sampling_provider_fallback=False,
    )

    assert result["payload"] == {"fallback": True}
    assert result["fallback_used"] is True
    assert len(stub.calls) == 1


async def test_prose_without_json_retries_task() -> None:
    """无 JSON 骨架的散文/推理正文应直接重试原任务，不走 json_repair。"""
    from mail_agent.llm_runtime.service import call_llm_json

    stub = SamplingStub([
        "Here is my analysis of the inbox without any structured payload.",
        '{"ok": true, "summary": "done"}',
    ])
    result = await call_llm_json(
        stub,
        system_prompt="Return JSON only.",
        user_message="Summarize the inbox.",
        max_tokens=64,
        max_attempts=2,
        allow_sampling_provider_fallback=False,
    )

    assert result["payload"] == {"ok": True, "summary": "done"}
    assert result["json_repair_used"] is False
    assert len(stub.calls) == 2
    retry_prompt = stub.calls[1]["messages"][0]["content"]["text"]
    assert "not parseable as a JSON object" in retry_prompt
    assert "JSON only" in retry_prompt or "First char" in retry_prompt


async def test_fullwidth_braces_are_accepted() -> None:
    from mail_agent.llm_runtime.service import parse_json_response

    payload = parse_json_response("\uff5b\"title\": \"T\", \"summary\": \"S\"\uff5d")
    assert payload == {"title": "T", "summary": "S"}


async def test_extract_prefers_text_block_over_thinking() -> None:
    from mail_agent.llm_runtime.service import extract_sampling_text, parse_json_response

    text = extract_sampling_text({
        "content": [
            {"type": "thinking", "text": "long reasoning without braces"},
            {"type": "text", "text": '{"title":"T","summary":"S"}'},
        ]
    })
    assert parse_json_response(text) == {"title": "T", "summary": "S"}


async def test_sampling_prompt_escapes_non_ascii_for_windows_host() -> None:
    from mail_agent.llm_runtime.service import call_llm_json

    stub = SamplingStub(['{"ok": true}'])
    await call_llm_json(
        stub,
        system_prompt="Return JSON only. 版权 ©",
        user_message="Evaluate attachment: 报价单 © Пример.pdf",
        max_tokens=64,
        max_attempts=1,
        metadata={"tool": "unit_test", "filename": "报价单 ©.pdf"},
        allow_sampling_provider_fallback=False,
    )

    call = stub.calls[0]
    sent_text = call["messages"][0]["content"]["text"]
    sent_system = call["system_prompt"]
    sent_metadata = call["metadata"]
    assert all(ord(ch) < 128 for ch in sent_text)
    assert all(ord(ch) < 128 for ch in sent_system)
    assert all(all(ord(ch) < 128 for ch in str(value)) for value in sent_metadata.values())
    assert "\\u62a5" in sent_text
    assert "\\xa9" in sent_text


async def main() -> None:
    # 中文注释：确保测试不会因为本地 DashScope 环境变量而走 provider fallback。
    os.environ.pop("DASHSCOPE_API_KEY", None)
    await test_valid_json_does_not_trigger_repair()
    await test_json_mode_is_forwarded_to_sampling()
    await test_local_json_repair_does_not_trigger_llm_repair()
    await test_local_json_repair_unescaped_quotes_in_query_string()
    await test_local_json_repair_missing_colon_between_key_value()
    await test_local_json_repair_invalid_and_truncated_unicode_escapes()
    await test_local_json_repair_mail_links_missing_commas()
    await test_truncated_json_is_salvaged_by_closing_brackets()
    await test_length_stop_reason_retries_even_for_valid_json()
    await test_truncation_salvage_is_discarded_and_retried()
    await test_truncation_fails_without_returning_salvage()
    await test_invalid_json_does_not_trigger_sampling_repair()
    await test_invalid_json_uses_safe_fallback_without_repair()
    await test_empty_sampling_response_does_not_trigger_repair()
    await test_prose_without_json_retries_task()
    await test_fullwidth_braces_are_accepted()
    await test_extract_prefers_text_block_over_thinking()
    await test_sampling_prompt_escapes_non_ascii_for_windows_host()
    print("PASS llm json parse tests")


if __name__ == "__main__":
    asyncio.run(main())
