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


async def test_invalid_json_triggers_one_sampling_repair() -> None:
    from mail_agent.llm_runtime.service import call_llm_json

    stub = SamplingStub(['{"a": "b", broken}', '{"a": "b"}'])
    result = await call_llm_json(
        stub,
        system_prompt="Return JSON only.",
        user_message='## Output format\n{"a": "string"}\n\n## Emails\nSECRET EMAIL BODY',
        max_tokens=64,
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
    repair_prompt = repair_call["messages"][0]["content"]["text"]
    assert "SECRET EMAIL BODY" not in repair_prompt
    assert "## Output format" in repair_prompt


async def test_repair_failure_uses_safe_fallback() -> None:
    from mail_agent.llm_runtime.service import call_llm_json_safe

    stub = SamplingStub(["not json", "still not json"])
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
    await test_local_json_repair_does_not_trigger_llm_repair()
    await test_invalid_json_triggers_one_sampling_repair()
    await test_repair_failure_uses_safe_fallback()
    await test_empty_sampling_response_does_not_trigger_repair()
    await test_sampling_prompt_escapes_non_ascii_for_windows_host()
    print("PASS llm json repair tests")


if __name__ == "__main__":
    asyncio.run(main())
