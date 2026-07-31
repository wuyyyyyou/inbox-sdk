

"""LLM adapter using DashScope or Anna Executa sampling."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Awaitable

_BEIJING_TZ = timezone(timedelta(hours=8))


def _llm_log_dir() -> Path | None:
    """Return the LLM log directory, or None if running on the Anna platform."""
    if os.environ.get("GMAIL_ACCESS_TOKEN") or os.environ.get("GOOGLE_ACCESS_TOKEN"):
        return None  # Platform — skip logging
    # Use the executa's .data/llm_logs/ directory
    return Path(__file__).resolve().parents[3] / ".data" / "llm_logs"


def _write_llm_log(tool: str, direction: str, content: str, extra: dict[str, str] | None = None) -> None:
    """Append a log entry for one LLM interaction."""
    log_dir = _llm_log_dir()
    if not log_dir:
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_BEIJING_TZ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:23]
    entry = {"ts": ts, "tool": tool, "direction": direction, "content": content}
    if extra:
        entry.update(extra)
    log_file = log_dir / f"llm_{datetime.now(_BEIJING_TZ).strftime('%Y%m%d')}.jsonl"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

# 兼容旧调用签名，pipeline 仍会传入 sampling_create_message 参数。
LlmFunc = Callable[..., Awaitable[dict[str, Any]]]
DASHSCOPE_API_KEY_ENV = "DASHSCOPE_API_KEY"
DASHSCOPE_MODEL_ENV = "DASHSCOPE_MODEL"
DASHSCOPE_DEFAULT_MODEL = "qwen3-max"
DASHSCOPE_CHAT_COMPLETIONS_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MAX_RETRIES = 2
BASE_DELAY = 1.2
# JSON 解析失败时落盘/诊断的原文上限；便于区分格式问题与混入无关内容，同时避免日志爆炸。
JSON_PARSE_ERROR_LOG_LIMIT = 12_000


class TruncatedJsonResponse(ValueError):
    """模型在 JSON 对象完成前停止输出，且本地闭合也无法得到合法 dict。

    调用方应回退到基于真实邮件证据的本地结果，而不是再次 Sampling 重试。
    """


# Surrogate range: U+D800–U+DFFF
_SURROGATE_START = 0xD800
_SURROGATE_END = 0xE000


def _sanitize_str(s: str) -> str:
    """Replace lone surrogate characters that break JSON/UTF-8 encoding."""
    if not isinstance(s, str):
        return str(s) if s is not None else ""
    # Fast path: most strings are clean
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        pass
    # Slow path: replace surrogates character by character
    return "".join(
        c if ord(c) < _SURROGATE_START or ord(c) >= _SURROGATE_END else "�"
        for c in s
    )


def _ascii_escape_for_host_transport(s: str) -> str:
    """Keep Anna Sampling reverse-RPC text ASCII-only for Windows hosts.

    Some Anna host handlers still pass request text through a GBK-encoded
    logging/transport path on Windows. Non-ASCII email bodies or filenames
    such as "报价单 ©.pdf" can crash that handler before the model runs.
    Preserve the information as Python-style escapes instead of dropping it.
    """
    s = _sanitize_str(s)
    try:
        s.encode("ascii")
        return s
    except UnicodeEncodeError:
        return s.encode("ascii", "backslashreplace").decode("ascii")


def _sanitize_value(obj: Any) -> Any:
    """Recursively sanitize surrogate characters from all strings in a nested structure."""
    if isinstance(obj, str):
        return _sanitize_str(obj)
    if isinstance(obj, dict):
        return {_sanitize_str(str(k)): _sanitize_value(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_value(item) for item in obj]
    return obj


def _fix_json_string_escapes(text: str) -> str:
    """修复字符串内非法或截断的 \\u 转义，避免 Invalid \\uXXXX escape。

    常见来源：
    1. max_tokens 截断落在 `\\uXXXX` 中间（尾部 `\\u` / `\\u4e`）；
    2. 模型把中文错误写成非法转义（如 `\\u606v`，息应为 `\\u606f`）。

    策略：合法完整 `\\uXXXX` 原样保留；尾部不完整转义直接丢弃以便闭合；
    中段非法则把反斜杠再转义为 `\\\\`，保留字面内容，不编造码点。
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue
        if ch == '"':
            out.append(ch)
            in_string = False
            i += 1
            continue
        if ch == "\\":
            if i + 1 >= n:
                # 尾部悬挂反斜杠：丢弃，后续由闭合逻辑补引号。
                break
            nxt = text[i + 1]
            if nxt == "u":
                hexpart = text[i + 2 : i + 6]
                if len(hexpart) == 4 and all(c in "0123456789abcdefABCDEF" for c in hexpart):
                    out.append(text[i : i + 6])
                    i += 6
                    continue
                # 截断在输入末尾：丢掉残缺 \\u…，避免闭合后仍 Invalid escape。
                if len(hexpart) < 4 and (i + 2 + len(hexpart)) >= n:
                    break
                # 中段非法（如 \\u606v）：\\ → \\\\，保留后面的 u…
                out.append("\\\\")
                out.append("u")
                i += 2
                continue
            # 其它转义对原样保留（含 \" \\ \/ \n 以及未知双字符转义）。
            out.append(ch)
            out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _escape_raw_quotes_inside_strings(text: str) -> str:
    """把字符串值内部未转义的双引号改成 \\"，不改 key/字段边界上的合法引号。

    QueryPlan 常见坏例：
      {"query": "urgent OR "asap" OR "important""}
    模型把 Gmail 短语引号原样塞进 JSON 字符串，导致 Expecting ',' delimiter。
    判定：字符串内的 `"` 若后面不是合法结束符（, : } ] 或 EOF，允许空白），则视为内容引号。
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue
        # 已在字符串内
        if ch == "\\" and i + 1 < n:
            # 保留已有转义对（含 \"）
            out.append(ch)
            out.append(text[i + 1])
            i += 2
            continue
        if ch == '"':
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            # 合法结束：后接结构分隔符或文本结束（对象/数组/字段边界）
            if j >= n or text[j] in ",:}]":
                out.append('"')
                in_string = False
                i += 1
                continue
            # 内容中的裸引号 → 转义
            out.append('\\"')
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _repair_json(text: str) -> str:
    """Fix common LLM JSON mistakes so json.loads has a better chance."""

    # ── Step 0: 字符串内非法/截断 \\u 转义（须尽早，否则后续扫描会踩 Invalid escape）──
    text = _fix_json_string_escapes(text)

    # ── Step 1: Single-quote JSON → double-quote ──
    # Some models output Python-style {'key': 'value'} which is invalid JSON.
    # Only apply when the text appears to use single quotes as delimiters.
    if text.lstrip().startswith("{") and "'" in text[:200] and '"' not in text[:200]:
        # Single-quoted keys: 'key': → "key":
        text = re.sub(r"'([^']+)'\s*:", r'"\1":', text)
        # Single-quoted top-level string values: : 'value' → : "value"
        text = re.sub(r":\s*'([^']*)'", r': "\1"', text)

    # ── Step 2: Comma / bracket / colon fixes（必须先于裸引号转义）──
    # 否则 `"a":"b"\n"c":1` 会把 b 的结束引号误判为内容引号。
    # Trailing comma before ] or }  (e.g. {"a": 1,} → {"a": 1})
    text = re.sub(r",\s*([}\]])", r"\1", text)
    # 对象字段漏冒号：{"action" "final"} → {"action": "final"}
    # 要求第二个字符串后接 , } ]，且 key 前为 { 或 ,；避免 ["a" "b"] 被改成冒号。
    text = re.sub(
        r'([{\,]\s*)"((?:[^"\\]|\\.)*)"\s+"((?:[^"\\]|\\.)*)"(?=\s*[,}\]])',
        r'\1"\2": "\3"',
        text,
    )
    # Missing comma: "value"\n  "next_key"  →  "value",\n  "next_key"
    text = re.sub(r'"\s*\n\s*"', '",\n"', text)
    # Anna sampling 偶尔会在同一行的下一个 key 前漏逗号。
    text = re.sub(r'(?<=")\s+(?="[^"\r\n]{1,80}"\s*:)', ', ', text)
    # Missing comma: }\n  "next_key"  →  },\n  "next_key"（含缩进空格/制表）
    text = re.sub(r'}\s*\n\s*"', '},\n"', text)
    text = re.sub(r'}\s+"', '}, "', text)
    # 数组里的相邻对象如果少逗号，json.loads 会报 Expecting delimiter。
    text = re.sub(r'}\s*{', '},{', text)
    # Missing comma: ]\n  "next_key"  →  ],\n  "next_key"
    text = re.sub(r']\s*\n\s*"', '],\n"', text)
    text = re.sub(r']\s+"', '], "', text)
    text = re.sub(r'(?<=])\s+(?="[^"\r\n]{1,80}"\s*:)', ', ', text)
    # Missing comma: number\n  "next_key"  →  number,\n  "next_key"
    text = re.sub(r'(\d)\s*\n\s*"', r'\1,\n"', text)
    text = re.sub(r'(?<=\d)\s+(?="[^"\r\n]{1,80}"\s*:)', ', ', text)
    # Missing comma: true\n  "next_key" | false\n  "next_key" | null\n  "next_key"
    text = re.sub(r'(true|false|null)\s*\n\s*"', r'\1,\n"', text)
    text = re.sub(r'\b(true|false|null)\s+(?="[^"\r\n]{1,80}"\s*:)', r'\1, ', text)
    # Ask answer 的 mail_links 数组里对象之间常漏逗号且夹杂换行缩进。
    text = re.sub(r'}\s*\n\s*{', '},\n{', text)

    # ── Step 3: 字符串值内未转义双引号（QueryPlan query 短语引号）──
    # 在补逗号之后执行，避免把“下一字段 key 的引号”误当成内容。
    text = _escape_raw_quotes_inside_strings(text)
    return text


def _normalize_json_delimiters(text: str) -> str:
    """把全角括号等常见 Unicode 分隔符归一成标准 JSON 分隔符。"""
    return (
        text.replace("\uff5b", "{")
        .replace("\uff5d", "}")
        .replace("\uff3b", "[")
        .replace("\uff3d", "]")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )


def _strip_markdown_json_fence(text: str) -> str:
    """提取 markdown fence 中的 JSON，兼容前后夹杂说明文字。"""
    stripped = text.strip()
    whole = re.match(r"^```(?:json)?\s*\n?(.*)\n?```\s*$", stripped, re.DOTALL)
    if whole:
        return whole.group(1).strip()
    embedded = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", stripped, re.DOTALL)
    if embedded:
        return embedded.group(1).strip()
    return stripped


def _close_truncated_json(fragment: str) -> str:
    """闭合半截 JSON：补齐未闭合字符串与括号，不新增业务字段。"""
    # 先清掉尾部残缺 \\u，否则补上引号后仍会 Invalid \\uXXXX escape。
    s = _fix_json_string_escapes(str(fragment or ""))
    in_string = False
    escape = False
    stack: list[str] = []
    for ch in s:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in ("}", "]"):
            if stack and stack[-1] == ch:
                stack.pop()
    # 未闭合字符串内的尾部空格是内容，不能 rstrip；结构外才去掉尾部空白。
    if in_string:
        if s.endswith("\\"):
            s = s[:-1]
        s += '"'
    else:
        s = s.rstrip()
        if s.endswith("\\"):
            s = s[:-1]
        s = re.sub(r",\s*$", "", s)
    while stack:
        s += stack.pop()
    return s


def salvage_truncated_json(text: str) -> dict[str, Any] | None:
    """从被 length 截断的模型输出中尽量恢复合法 JSON dict。

    只闭合语法结构，不编造邮件事实；恢复失败返回 None。
    """
    normalized = _normalize_json_delimiters(_strip_markdown_json_fence(str(text or "")))
    start = normalized.find("{")
    if start < 0:
        return None
    fragment = normalized[start:]
    candidates = [fragment]
    # 若卡在最后一个未写完字段，回退到最近完整逗号处再闭合。
    last_comma = fragment.rfind(",")
    if last_comma > 0:
        candidates.append(fragment[:last_comma])
    last_brace = fragment.rfind("}")
    if last_brace > 0:
        candidates.append(fragment[: last_brace + 1])

    for frag in candidates:
        versions = (
            frag,
            _repair_json(frag),
            _close_truncated_json(frag),
            _close_truncated_json(_repair_json(frag)),
        )
        for version in versions:
            try:
                payload = json.loads(version)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                return payload
    return None


def parse_json_response(text: str) -> dict[str, Any]:
    """Extract JSON object from LLM response text (handles markdown fences and common LLM errors)."""
    payload, _ = _parse_json_response_with_kind(text)
    return payload


def _parse_json_response_with_kind(text: str) -> tuple[dict[str, Any], str]:
    """解析 JSON 并标记 exact、syntax_repair 或 truncation_salvage。"""
    text = _normalize_json_delimiters(_strip_markdown_json_fence(text))
    start = text.find("{")
    end = text.rfind("}")
    if start < 0:
        # 预览仅用于诊断模型是否输出了散文/推理而非 JSON；不记录完整邮件内容。
        preview = text[:160].replace("\n", "\\n")
        raise ValueError(
            f"LLM response did not contain a JSON object; preview={preview!r}"
        )
    # 无闭合 `}`：先本地闭合挽救，避免整次 Ask 因 length 截断失败。
    if end <= start:
        salvaged = salvage_truncated_json(text)
        if salvaged is not None:
            return salvaged, "truncation_salvage"
        raise TruncatedJsonResponse("LLM response ended before its JSON object was complete")
    candidate = text[start : end + 1]
    # 仅修复缺逗号等局部语法问题；失败再尝试闭合截断。
    attempts = [candidate, _repair_json(candidate)]
    last_error: json.JSONDecodeError | None = None
    for attempt in attempts:
        try:
            payload = json.loads(attempt)
            if isinstance(payload, dict):
                return payload, "exact" if attempt == candidate else "syntax_repair"
        except json.JSONDecodeError as exc:
            last_error = exc
    salvaged = salvage_truncated_json(text)
    if salvaged is not None:
        return salvaged, "truncation_salvage"
    assert last_error is not None
    start_excerpt = max(0, last_error.pos - 140)
    end_excerpt = min(len(attempts[-1]), last_error.pos + 140)
    excerpt = attempts[-1][start_excerpt:end_excerpt].replace("\n", "\\n")
    raise ValueError(
        f"{last_error.msg}: line {last_error.lineno} column {last_error.colno} "
        f"(char {last_error.pos}); excerpt={excerpt}"
    ) from last_error


def _sampling_stop_reason_is_length(result: Any) -> bool:
    """兼容 Host 常见的大小写及嵌套 stopReason/finishReason 写法。"""
    if isinstance(result, dict):
        for key, value in result.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if normalized in {"stopreason", "finishreason"}:
                if isinstance(value, str):
                    reason = re.sub(r"[^a-z0-9]", "", value.casefold())
                    if reason in {"length", "maxtokens"}:
                        return True
            if _sampling_stop_reason_is_length(value):
                return True
    elif isinstance(result, list):
        return any(_sampling_stop_reason_is_length(item) for item in result)
    return False


def _build_sampling_json_user_message(system_prompt: str, user_message: str, retry_note: str = "") -> str:
    # system_prompt 由 Host 的 systemPrompt 字段单独下发；此处只追加极短输出约束（省 input tokens）。
    _ = system_prompt
    # 尾部再锁一次，压住“先写说明再写 JSON”的模型习惯。
    prompt = (
        f"{user_message.strip()}\n\n"
        "FINAL: output JSON object only. First char `{{`. Last char `}}`. "
        "No bullets, no restating instructions."
    )
    if retry_note:
        prompt = f"{prompt}\n{retry_note}"
    return prompt


def _classify_json_parse_failure(text: str, exc: BaseException) -> str:
    """把 JSON 解析失败粗分为便于检索的错误类别。"""
    if isinstance(exc, TruncatedJsonResponse):
        return "truncated_json"
    if "{" not in text and "\uff5b" not in text:
        return "no_json_object"
    return "invalid_json"


def _safe_log_tool_slug(tool_name: str) -> str:
    """把 tool 名压成适合文件名的短标识。"""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(tool_name or "unknown")).strip("._-")
    return (slug or "unknown")[:80]


def _write_json_parse_failure_dump(
    tool_name: str,
    content: str,
    *,
    error: str,
    error_kind: str,
    model: str = "",
    shape: str = "",
    attempt: int | None = None,
    content_chars: int = 0,
) -> str:
    """把解析失败的模型原文单独写成可读 txt，返回相对路径或空串。

    目录：`.data/llm_logs/json_parse_failed/`，便于直接打开查看完整生成内容，
    而不必从 jsonl 里再抽 content 字段。
    """
    log_dir = _llm_log_dir()
    if not log_dir:
        return ""
    dump_dir = log_dir / "json_parse_failed"
    try:
        dump_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return ""
    now = datetime.now(_BEIJING_TZ)
    stamp = now.strftime("%Y%m%d_%H%M%S_%f")[:21]
    path = dump_dir / f"{stamp}_{_safe_log_tool_slug(tool_name)}.txt"
    header_lines = [
        f"ts={now.strftime('%Y-%m-%dT%H:%M:%S.%f')[:23]}",
        f"tool={tool_name}",
        f"error={error_kind}",
        f"error_detail={_sanitize_str(error)[:500]}",
        f"content_chars={content_chars}",
        f"model={model}",
        f"shape={shape}",
        f"attempt={attempt if attempt is not None else ''}",
        "----- BEGIN MODEL OUTPUT -----",
    ]
    body = "\n".join(header_lines) + "\n" + content + "\n----- END MODEL OUTPUT -----\n"
    try:
        path.write_text(body, encoding="utf-8")
    except OSError:
        return ""
    # 返回相对 llm_logs 的路径，方便 stderr 与 jsonl 引用。
    try:
        return str(path.relative_to(log_dir)).replace("\\", "/")
    except ValueError:
        return str(path)


def _log_json_parse_failure(
    tool_name: str,
    text: str,
    *,
    error: str,
    error_kind: str,
    model: str = "",
    shape: str = "",
    attempt: int | None = None,
) -> None:
    """JSON 解析失败时落盘模型原文，并在 stderr 打摘要，便于区分格式问题与内容混入。

    不再做二次 sampling repair；失败后由调用方 fallback 或直接抛错。
    完整生成内容会额外写入 `json_parse_failed/*.txt` 独立文件。
    """
    raw_chars = len(text or "")
    content = _sanitize_str(text or "")
    if len(content) > JSON_PARSE_ERROR_LOG_LIMIT:
        content = content[:JSON_PARSE_ERROR_LOG_LIMIT] + "\n...[json parse error text truncated]"
    dump_rel = _write_json_parse_failure_dump(
        tool_name,
        content,
        error=error,
        error_kind=error_kind,
        model=model,
        shape=shape,
        attempt=attempt,
        content_chars=raw_chars,
    )
    extra: dict[str, str] = {
        "error": error_kind,
        "error_detail": _sanitize_str(error)[:500],
        "content_chars": str(raw_chars),
        "has_json_brace": str("{" in (text or "") or "\uff5b" in (text or "")),
    }
    if model:
        extra["model"] = model
    if shape:
        extra["shape"] = shape
    if attempt is not None:
        extra["attempt"] = str(attempt)
    if dump_rel:
        extra["dump_file"] = dump_rel
    # jsonl 仍保留短摘要，完整正文以独立 txt 为准。
    _write_llm_log(tool_name, "output_error", content[:4000], extra)
    # 平台侧只能看 stderr；完整原文在本地 llm_logs/json_parse_failed/。
    preview = content[:240].replace("\n", "\\n")
    try:
        import sys

        dump_part = f" dump={dump_rel}" if dump_rel else ""
        sys.stderr.write(
            f"[llm_runtime] json_parse_failed tool={tool_name} error={error_kind} "
            f"content_chars={raw_chars} has_json_brace={extra['has_json_brace']}"
            f"{dump_part} preview={preview!r}\n"
        )
        sys.stderr.flush()
    except Exception:
        pass


def _coerce_sampling_text_value(value: Any) -> str:
    """把 Host 可能返回的 text/dict/list 统一成可解析字符串。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        # JSON mode 偶发直接回传对象；序列化后交给 parse_json_response。
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return ""
    if isinstance(value, list):
        parts = [_coerce_sampling_text_value(item) for item in value]
        return "\n".join(part for part in parts if part)
    return ""


def extract_sampling_text(result: Any) -> str:
    # 不同 host 版本可能返回 content.text、content 字符串、content 数组或 OpenAI 风格 message.content。
    # thinking/reasoning 块优先忽略：它们常耗尽 max_tokens 且不含最终 JSON。
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        if text is None:
            text = content.get("content")
        if text is None and content.get("type") in {"json", "object"}:
            text = content.get("json") or content.get("data") or content.get("value")
        coerced = _coerce_sampling_text_value(text)
        if coerced:
            return coerced
    if isinstance(content, list):
        preferred: list[str] = []
        fallback: list[str] = []
        for item in content:
            if isinstance(item, str):
                preferred.append(item)
                continue
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").casefold()
            text = item.get("text")
            if text is None:
                text = item.get("content")
            coerced = _coerce_sampling_text_value(text)
            if not coerced:
                continue
            # 最终回答块优先；thinking/reasoning 仅作兜底。
            if item_type in {"thinking", "reasoning", "thought"}:
                fallback.append(coerced)
            else:
                preferred.append(coerced)
        parts = preferred or fallback
        if parts:
            return "\n".join(parts)
    message = result.get("message")
    if isinstance(message, dict):
        nested = extract_sampling_text({"content": message.get("content")})
        if nested:
            return nested
    choices = result.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        nested = extract_sampling_text(first)
        if nested:
            return nested
        nested = extract_sampling_text(first.get("message") if isinstance(first.get("message"), dict) else {})
        if nested:
            return nested
    # 少数 Host 在 json_object 模式下把业务 JSON 摊到 result 顶层。
    if any(key in result for key in ("title", "summary", "sections", "markdown", "assistant_text")):
        try:
            return json.dumps(
                {key: value for key, value in result.items() if key not in {"model", "usage", "stopReason", "role", "content", "message", "choices"}},
                ensure_ascii=False,
            )
        except (TypeError, ValueError):
            pass
    return ""


def _sampling_result_shape(result: Any) -> str:
    # 错误信息只带响应形态，不带正文，避免日志泄漏邮件内容。
    if not isinstance(result, dict):
        return type(result).__name__
    content = result.get("content")
    if isinstance(content, dict):
        content_shape = f"dict:{','.join(sorted(str(k) for k in content.keys()))}"
        text = content.get("text")
        if isinstance(text, str):
            content_shape = f"{content_shape}; text_len={len(text)}"
    elif isinstance(content, list):
        content_shape = f"list:{len(content)}"
        text_len = 0
        for item in content:
            if isinstance(item, str):
                text_len += len(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                text_len += len(item["text"])
        content_shape = f"{content_shape}; text_len={text_len}"
    else:
        content_shape = type(content).__name__
        if isinstance(content, str):
            content_shape = f"{content_shape}; text_len={len(content)}"
    return f"keys={','.join(sorted(str(k) for k in result.keys()))}; content={content_shape}"


def dashscope_available() -> bool:
    return bool(os.environ.get(DASHSCOPE_API_KEY_ENV, "").strip())


def _call_dashscope_json(
    *,
    system_prompt: str,
    user_message: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> dict[str, Any]:
    api_key = os.environ.get(DASHSCOPE_API_KEY_ENV, "").strip()
    if not api_key:
        raise RuntimeError(f"{DASHSCOPE_API_KEY_ENV} is not set")

    model = os.environ.get(DASHSCOPE_MODEL_ENV, "").strip() or DASHSCOPE_DEFAULT_MODEL
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            body = {
                "model": model,
                "messages": [
                    {"role": "system", "content": _sanitize_str(system_prompt)},
                    {"role": "user", "content": _sanitize_str(user_message)},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                "enable_thinking": False,
            }
            request = urllib.request.Request(
                DASHSCOPE_CHAT_COMPLETIONS_URL,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
            result = json.loads(raw)
            choices = result.get("choices") if isinstance(result, dict) else None
            message = ((choices or [{}])[0].get("message") or {}) if isinstance(choices, list) else {}
            text = message.get("content") if isinstance(message, dict) else ""
            if not isinstance(text, str) or not text.strip():
                raise ValueError("empty DashScope response")
            text = _sanitize_str(text)
            return {
                "payload": _sanitize_value(parse_json_response(text)),
                "text": text,
                "model": result.get("model") or model,
                "usage": result.get("usage"),
                "provider": "dashscope",
            }
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code < 500 and exc.code != 429:
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
        if attempt >= MAX_RETRIES:
            break
        time.sleep(min(BASE_DELAY ** (attempt + 1), 6.0))

    raise RuntimeError(f"DashScope call failed after {MAX_RETRIES + 1} attempts: {last_error}")


def call_dashscope_text(
    *,
    system_prompt: str,
    user_message: str,
    temperature: float = 0.2,
    max_tokens: int = 512,
    timeout: float = 60.0,
) -> dict[str, Any]:
    api_key = os.environ.get(DASHSCOPE_API_KEY_ENV, "").strip()
    if not api_key:
        raise RuntimeError(f"{DASHSCOPE_API_KEY_ENV} is not set")

    model = os.environ.get(DASHSCOPE_MODEL_ENV, "").strip() or DASHSCOPE_DEFAULT_MODEL
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _sanitize_str(system_prompt)},
            {"role": "user", "content": _sanitize_str(user_message)},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "enable_thinking": False,
    }
    request = urllib.request.Request(
        DASHSCOPE_CHAT_COMPLETIONS_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    result = json.loads(raw)
    choices = result.get("choices") if isinstance(result, dict) else None
    message = ((choices or [{}])[0].get("message") or {}) if isinstance(choices, list) else {}
    text = message.get("content") if isinstance(message, dict) else ""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty DashScope response")
    return {
        "text": text,
        "model": result.get("model") or model,
        "usage": result.get("usage"),
        "provider": "dashscope",
    }


async def call_llm_json(
    sampling_create_message: Any,
    *,
    system_prompt: str,
    user_message: str,
    temperature: float = 0.1,
    max_tokens: int = 512,
    timeout: float = 60.0,
    metadata: dict[str, str] | None = None,
    allow_sampling_provider_fallback: bool = False,
    stop_sequences: list[str] | None = None,
    response_format: dict[str, Any] | None = None,
    on_unsupported: str | None = None,
    max_attempts: int | None = None,
    retry_max_tokens: int | None = None,
) -> dict[str, Any]:
    """Call the selected LLM and return parsed JSON payload.

    Returns: {"payload": {...}, "text": "...", "model": "...", "usage": {...}}
    解析失败时仅本地 salvage（缺逗号/截断闭合等），不再二次 sampling json_repair。
    """
    tool_name = (metadata or {}).get("tool", "unknown") if metadata else "unknown"

    if sampling_create_message is not None:
        _write_llm_log(tool_name, "input", _sanitize_str(user_message)[:4000],
                       {"system_prompt": _sanitize_str(system_prompt)[:800], "max_tokens": str(max_tokens)})
        last_error: Exception | None = None
        last_text = ""
        last_response_was_truncated = False
        attempts = max(1, max_attempts or (MAX_RETRIES + 1))
        for attempt in range(attempts):
            try:
                # 截断重试可使用调用方为该分支单独预留的额度，避免首次输出
                # 很短时重试仍沿用同一个上限；未传入时保持旧调用的兼容行为。
                request_max_tokens = max_tokens if attempt == 0 else (retry_max_tokens or max_tokens)
                retry_note = ""
                if last_text:
                    retry_note = (
                        "Your previous response was truncated before its JSON object was complete. "
                        "Return a shorter, complete JSON object only."
                        if last_response_was_truncated
                        else (
                            "Your previous response was not parseable as a JSON object. "
                            f"Parser error: {last_error}. "
                            f"Previous response excerpt: {last_text[:800]}. "
                            "Return the corrected JSON object only."
                        )
                    )
                metadata_payload = {
                    str(key): _ascii_escape_for_host_transport(str(value))
                    for key, value in (metadata or {}).items()
                }
                # 保留调用方的业务阶段，同时给每一次主 Sampling 标记尝试序号。
                metadata_payload.setdefault("sampling_stage", "primary")
                metadata_payload["sampling_attempt"] = str(attempt + 1)
                request = {
                    "messages": [
                        {
                            "role": "user",
                            "content": {
                                "type": "text",
                                "text": _ascii_escape_for_host_transport(
                                    _build_sampling_json_user_message(system_prompt, user_message, retry_note)
                                ),
                            },
                        }
                    ],
                    "system_prompt": _ascii_escape_for_host_transport(system_prompt),
                    "temperature": temperature,
                    "include_context": "none",
                    "metadata": metadata_payload,
                    "timeout": timeout,
                    "stop_sequences": stop_sequences,
                    "response_format": response_format,
                    "on_unsupported": on_unsupported,
                    "max_tokens": request_max_tokens,
                }
                result = await sampling_create_message(**request)
                text = extract_sampling_text(result)
                if not isinstance(text, str) or not text.strip():
                    raise ValueError(f"empty Anna sampling response ({_sampling_result_shape(result)})")
                text = _sanitize_str(text)
                last_text = text
                try:
                    payload, parse_kind = _parse_json_response_with_kind(text)
                    # Host 已报告达到输出上限，或仅靠闭合半截结构才解析成功时，
                    # 表面合法的 payload 也不能作为完整回答返回。
                    if _sampling_stop_reason_is_length(result) or parse_kind == "truncation_salvage":
                        raise TruncatedJsonResponse(
                            "LLM response was truncated before its JSON object was complete"
                        )
                except Exception as parse_exc:
                    # 本地 salvage 已在 parse_json_response 内尝试；失败则落盘完整原文后抛错。
                    # 不再调用二次 sampling json_repair。
                    error_kind = _classify_json_parse_failure(text, parse_exc)
                    _log_json_parse_failure(
                        tool_name,
                        text,
                        error=str(parse_exc),
                        error_kind=error_kind,
                        model=str(result.get("model") or ""),
                        shape=_sampling_result_shape(result),
                        attempt=attempt + 1,
                    )
                    raise
                result_obj = {
                    "payload": _sanitize_value(payload),
                    "text": text,
                    "raw_text": text,
                    "model": result.get("model"),
                    "usage": result.get("usage"),
                    "provider": "anna-sampling",
                    "json_repair_used": False,
                    # 明确标记成功响应，调用方不能把内部 fallback 状态猜成正常文本。
                    "truncated": False,
                    "fallback_kind": "",
                }
                _write_llm_log(
                    tool_name,
                    "output",
                    text[:4000],
                    {"model": str(result.get("model") or ""), "json_repair": "False"},
                )
                return result_obj
            except Exception as exc:
                last_error = exc
                last_response_was_truncated = isinstance(exc, TruncatedJsonResponse)
                # Host 明确标记的预算/授权错误在同一 invoke 内不可恢复，禁止盲目重试。
                if (
                    getattr(exc, "code", None) in {-32006, -32007, -32009}
                    or type(exc).__name__ in {"SamplingBudgetExceeded", "SamplingCallLimitExceeded"}
                ):
                    break
                if attempt >= attempts - 1:
                    break
                await asyncio.sleep(min(BASE_DELAY ** (attempt + 1), 6.0))

        if allow_sampling_provider_fallback and dashscope_available():
            return await asyncio.to_thread(
                _call_dashscope_json,
                system_prompt=system_prompt,
                user_message=user_message,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )

        raise RuntimeError(
            f"Anna sampling failed after {attempts} attempts: {last_error}"
        ) from last_error

    # No Anna Sampling at all; go directly to DashScope
    return await asyncio.to_thread(
        _call_dashscope_json,
        system_prompt=system_prompt,
        user_message=user_message,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )


async def call_llm_json_safe(
    sampling_create_message: Any,
    *,
    system_prompt: str,
    user_message: str,
    fallback: dict[str, Any],
    temperature: float = 0.1,
    max_tokens: int = 512,
    timeout: float = 60.0,
    metadata: dict[str, str] | None = None,
    allow_fallback: bool = True,
    allow_sampling_provider_fallback: bool = False,
    stop_sequences: list[str] | None = None,
    response_format: dict[str, Any] | None = None,
    on_unsupported: str | None = None,
    max_attempts: int | None = None,
    retry_max_tokens: int | None = None,
) -> dict[str, Any]:
    """Call LLM with fallback on failure."""
    try:
        result = await call_llm_json(
            sampling_create_message,
            system_prompt=system_prompt,
            user_message=user_message,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            metadata=metadata,
            allow_sampling_provider_fallback=allow_sampling_provider_fallback,
            stop_sequences=stop_sequences,
            response_format=response_format,
            on_unsupported=on_unsupported,
            max_attempts=max_attempts,
            retry_max_tokens=retry_max_tokens,
        )
        result["fallback_used"] = False
        return result
    except Exception as exc:
        if not allow_fallback:
            raise
        return {
            "payload": fallback,
            "text": "",
            "model": None,
            "usage": None,
            "fallback_used": True,
            "fallback_reason": str(exc),
            # 保留失败类别供需要基于证据兜底的调用方判断，避免展示半截答案。
            "truncated": isinstance(exc, TruncatedJsonResponse),
            "fallback_kind": "truncated_json" if isinstance(exc, TruncatedJsonResponse) else "llm_error",
        }
