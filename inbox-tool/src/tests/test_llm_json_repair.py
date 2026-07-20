"""Anna sampling JSON repair fallback 的最小测试。"""

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

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        text = self.responses.pop(0) if self.responses else ""
        return {
            "content": {"type": "text", "text": text},
            "model": "test-model",
            "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        }


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


async def test_truncated_json_is_rejected_without_local_completion() -> None:
    """截断邮件回答不能靠补引号和括号伪装为成功。"""
    from mail_agent.llm_runtime.service import TruncatedJsonResponse, parse_json_response

    try:
        parse_json_response('{"title":"T","summary":"S","sections":[{"heading":"H","items":[{"subject":"x"')
    except TruncatedJsonResponse:
        pass
    else:
        raise AssertionError("truncated JSON must not be accepted")


async def test_truncated_json_retries_original_request() -> None:
    """截断时应重试完整任务，而不是将不完整内容交给 JSON repair。"""
    from mail_agent.llm_runtime.service import call_llm_json

    stub = SamplingStub(['{"markdown":"Participants: Kate (', '{"markdown":"## Complete answer"}'])
    result = await call_llm_json(
        stub,
        system_prompt="Return JSON only.",
        user_message="Summarize the thread.",
        max_tokens=64,
        max_attempts=2,
        allow_sampling_provider_fallback=False,
    )

    assert result["payload"] == {"markdown": "## Complete answer"}
    assert result["json_repair_used"] is False
    assert len(stub.calls) == 2
    retry_prompt = stub.calls[1]["messages"][0]["content"]["text"]
    assert "truncated before its JSON object was complete" in retry_prompt
    assert "Participants: Kate" not in retry_prompt


async def test_invalid_json_triggers_one_sampling_repair() -> None:
    from mail_agent.llm_runtime.service import call_llm_json

    stub = SamplingStub(['{"a": "b", broken}', '{"a": "b"}'])
    result = await call_llm_json(
        stub,
        system_prompt="Return JSON only.",
        user_message='## Output format\n{"a": "string"}\n\n## Emails\nSECRET EMAIL BODY',
        max_tokens=4096,
        max_attempts=1,
        metadata={"tool": "unit_test"},
        allow_sampling_provider_fallback=False,
    )

    assert result["payload"] == {"a": "b"}
    assert result["json_repair_used"] is True
    assert len(stub.calls) == 2
    repair_call = stub.calls[1]
    assert repair_call["metadata"]["tool"] == "json_repair"
    assert repair_call["metadata"]["repair_for"] == "unit_test"
    assert repair_call["max_tokens"] == 512
    repair_prompt = repair_call["messages"][0]["content"]["text"]
    assert "SECRET EMAIL BODY" not in repair_prompt
    assert "## Output format" in repair_prompt


async def test_repair_failure_uses_safe_fallback() -> None:
    from mail_agent.llm_runtime.service import call_llm_json_safe

    # 带 `{` 的残缺文本才会进入 json_repair；无骨架散文走任务重试而非 repair。
    stub = SamplingStub(['{"a": "b", broken}', "still not json"])
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
    assert len(stub.calls) == 2


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


async def test_prose_without_json_skips_repair_and_retries_task() -> None:
    """无 JSON 骨架的散文/推理正文不得走 json_repair，应直接重试原任务。"""
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
    assert "first non-whitespace character MUST be '{'" in retry_prompt


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
    await test_local_json_repair_mail_links_missing_commas()
    await test_truncated_json_is_rejected_without_local_completion()
    await test_truncated_json_retries_original_request()
    await test_invalid_json_triggers_one_sampling_repair()
    await test_repair_failure_uses_safe_fallback()
    await test_empty_sampling_response_does_not_trigger_repair()
    await test_prose_without_json_skips_repair_and_retries_task()
    await test_fullwidth_braces_are_accepted()
    await test_extract_prefers_text_block_over_thinking()
    await test_sampling_prompt_escapes_non_ascii_for_windows_host()
    print("PASS llm json repair tests")


if __name__ == "__main__":
    asyncio.run(main())
