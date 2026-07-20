from __future__ import annotations

import json

from anna_inbox_executa.common import *
from anna_inbox_executa.diagnostics import record_span


# Anna Host 按同一 invoke_id 累计计算 maxTokens；保留余量避免
# 重试或 JSON repair 让后续小请求触发 -32007 MAX_TOKENS_EXCEEDED。
ANNA_SAMPLING_TIMEOUT_SECONDS = 60.0
ANNA_SAMPLING_MIN_TIMEOUT_SECONDS = 30.0
ANNA_SAMPLING_TOTAL_TOKENS = 32000
ANNA_SAMPLING_MAX_TOKENS_PER_CALL = 8192
ANNA_SAMPLING_MAX_CALLS = 8


class SamplingBudgetExceeded(RuntimeError):
    """本地累计 Sampling 预算耗尽，调用方应使用既有 fallback。"""


class SamplingCallLimitExceeded(RuntimeError):
    """本地 Sampling 调用次数已达 Host 单轮上限。"""


def _sampling_grant_limits(grant: Any) -> tuple[int, int]:
    """解析 Host 下发的 Sampling 授权，并始终收敛到官方 v1 上限。"""
    raw = grant if isinstance(grant, dict) else {}
    try:
        max_calls = int(raw.get("maxCalls") or ANNA_SAMPLING_MAX_CALLS)
    except (TypeError, ValueError):
        max_calls = ANNA_SAMPLING_MAX_CALLS
    try:
        max_tokens = int(raw.get("maxTokensTotal") or ANNA_SAMPLING_TOTAL_TOKENS)
    except (TypeError, ValueError):
        max_tokens = ANNA_SAMPLING_TOTAL_TOKENS
    return (
        max(1, min(max_calls, ANNA_SAMPLING_MAX_CALLS)),
        max(1, min(max_tokens, ANNA_SAMPLING_TOTAL_TOKENS)),
    )


def _sampling_prompt_bytes(request: dict[str, Any]) -> int:
    """计算 Sampling 反向 JSON-RPC 的 UTF-8 请求字节数，不保留任何邮件内容。"""
    # 反向请求不能走 Host 文件上传；观测实际 frame 大小才能定位 JSON-RPC 溢出。
    params = {
        "messages": request.get("messages", []),
        "maxTokens": request.get("max_tokens"),
        "systemPrompt": request.get("system_prompt"),
        "temperature": request.get("temperature"),
        "includeContext": request.get("include_context", "none"),
        "metadata": request.get("metadata", {}),
        "_clientTimeoutS": request.get("timeout"),
    }
    payload = {"jsonrpc": "2.0", "method": "sampling/createMessage", "params": params}
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


def build_budgeted_sampling(sampling_fn: Any, *, invoke_id: str, sampling_grant: Any = None) -> Any:
    """为一个 Executa invoke 创建带累计预算、统一超时和安全日志的 sampler。"""
    max_calls, total_tokens = _sampling_grant_limits(sampling_grant)
    remaining_tokens = total_tokens
    call_count = 0
    reserved_tokens = 0
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    failed_calls = 0
    last_error = ""
    budget_lock = asyncio.Lock()

    def _snapshot() -> dict[str, Any]:
        """返回可公开给终端用户的脱敏预算快照。"""
        return {
            "grant": {"max_calls": max_calls, "max_tokens_total": total_tokens, "max_tokens_per_call": ANNA_SAMPLING_MAX_TOKENS_PER_CALL},
            "reserved": {"calls": call_count, "tokens": reserved_tokens},
            "usage": dict(usage),
            "remaining_reservation_tokens": remaining_tokens,
            "remaining_calls": max(0, max_calls - call_count),
            "failed_calls": failed_calls,
            "last_error": last_error,
        }

    def _allocate_tokens(*, weight: float, reserve_weights: tuple[float, ...] = ()) -> int:
        """按累计授权和剩余额度计算一个阶段可申请的输出 token。

        该方法只计算本次请求的上限，不提前修改 ``remaining_tokens``；真正的
        原子预留仍在下方 sampler 调用时完成。这样 Ask 可以为截断重试预留
        比例，同时并发调用也仍由 ``budget_lock`` 和 Host 上限兜底。
        """
        try:
            normalized_weight = max(0.0, float(weight))
        except (TypeError, ValueError):
            normalized_weight = 0.0
        reserve_total = 0.0
        for reserve_weight in reserve_weights:
            try:
                reserve_total += max(0.0, float(reserve_weight))
            except (TypeError, ValueError):
                continue
        target = max(1, int(total_tokens * normalized_weight))
        reserved_for_later = int(total_tokens * min(1.0, reserve_total))
        available_now = max(1, remaining_tokens - reserved_for_later)
        return min(target, ANNA_SAMPLING_MAX_TOKENS_PER_CALL, available_now)

    async def _budgeted_sampling(**kwargs: Any) -> dict[str, Any]:
        """在调用 Host 前原子预留 token，避免发送必然超额的请求。"""
        nonlocal remaining_tokens, call_count, reserved_tokens, failed_calls, last_error
        requested_tokens = kwargs.get("max_tokens")
        if not isinstance(requested_tokens, int) or requested_tokens <= 0:
            raise ValueError("max_tokens must be a positive integer")

        raw_metadata = kwargs.get("metadata")
        metadata = {
            str(key): str(value)
            for key, value in raw_metadata.items()
        } if isinstance(raw_metadata, dict) else {}
        tool_name = metadata.get("tool", "unknown")
        metadata["executa_invoke_id"] = invoke_id

        async with budget_lock:
            if call_count >= max_calls:
                last_error = "max_calls_exceeded"
                raise SamplingCallLimitExceeded("Anna sampling call budget exhausted")
            granted_tokens = min(
                requested_tokens,
                ANNA_SAMPLING_MAX_TOKENS_PER_CALL,
                remaining_tokens,
            )
            if granted_tokens <= 0:
                last_error = "max_tokens_exceeded"
                log(
                    "anna sampling rejected: "
                    f"tool={tool_name} requested_tokens={requested_tokens} "
                    f"granted_tokens=0 remaining_tokens=0 "
                    f"timeout_s={ANNA_SAMPLING_TIMEOUT_SECONDS} "
                    "error_type=SamplingBudgetExceeded"
                )
                raise SamplingBudgetExceeded("Anna sampling token budget exhausted")
            remaining_tokens -= granted_tokens
            call_count += 1
            reserved_tokens += granted_tokens
            remaining_after_reservation = remaining_tokens

        request = dict(kwargs)
        request["max_tokens"] = granted_tokens
        # Anna Host 对模型截止时间的有效下限为 30 秒。尊重阶段调用方给出的
        # 较短预算，同时把上限限制在 60 秒，防止单次 Sampling 独占后台 run。
        try:
            requested_timeout = float(request.get("timeout") or ANNA_SAMPLING_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            requested_timeout = ANNA_SAMPLING_TIMEOUT_SECONDS
        request["timeout"] = max(
            ANNA_SAMPLING_MIN_TIMEOUT_SECONDS,
            min(requested_timeout, ANNA_SAMPLING_TIMEOUT_SECONDS),
        )
        request["metadata"] = metadata
        prompt_bytes = _sampling_prompt_bytes(request)
        started = time.monotonic()
        log(
            "anna sampling started: "
            f"tool={tool_name} requested_tokens={requested_tokens} "
            f"granted_tokens={granted_tokens} remaining_tokens={remaining_after_reservation} "
            f"timeout_s={request['timeout']} prompt_bytes={prompt_bytes}"
        )
        try:
            result = await sampling_fn(**request)
        except Exception as exc:
            failed_calls += 1
            last_error = type(exc).__name__
            record_span(
                "sampling.create_message",
                started,
                outcome="error",
                error_type=type(exc).__name__,
            )
            log(
                "anna sampling failed: "
                f"tool={tool_name} requested_tokens={requested_tokens} "
                f"granted_tokens={granted_tokens} remaining_tokens={remaining_after_reservation} "
                f"timeout_s={request['timeout']} "
                f"prompt_bytes={prompt_bytes} "
                f"elapsed_ms={int((time.monotonic() - started) * 1000)} "
                f"error_type={type(exc).__name__}"
            )
            raise
        raw_usage = result.get("usage") if isinstance(result, dict) else {}
        if isinstance(raw_usage, dict):
            for source, target in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"), ("totalTokens", "total_tokens")):
                try:
                    usage[target] += max(0, int(raw_usage.get(source) or 0))
                except (TypeError, ValueError):
                    pass
        record_span("sampling.create_message", started)
        log(
            "anna sampling completed: "
            f"tool={tool_name} requested_tokens={requested_tokens} "
            f"granted_tokens={granted_tokens} remaining_tokens={remaining_after_reservation} "
            f"timeout_s={request['timeout']} "
            f"prompt_bytes={prompt_bytes} "
            f"elapsed_ms={int((time.monotonic() - started) * 1000)}"
        )
        return result

    # 调用方通过属性读取快照，不把闭包或邮件内容写入 run checkpoint。
    _budgeted_sampling.budget_snapshot = _snapshot
    # 调用方只传阶段比例，不接触凭据或原始 Host grant；实际扣减仍集中在
    # _budgeted_sampling 中，避免各业务管线各自维护一套累计预算。
    _budgeted_sampling.allocate_tokens = _allocate_tokens
    return _budgeted_sampling


def _build_sampling_for_run(arguments: dict[str, Any], invoke_id: str) -> Any:
    """为 Anna LLM 创建带预算的 sampling 调用；非 Anna provider 返回 None 走本地/DashScope。

    Ask/Brief 生产路径统一使用 sampling/createMessage，不走 Host Agent Session。
    """
    provider = str(arguments.get("ai_provider", "anna-llm")).strip()
    if provider == "anna-llm":
        return build_budgeted_sampling(
            sampling.create_message,
            invoke_id=invoke_id,
            sampling_grant=arguments.get("_sampling_grant"),
        )
    return None


async def _check_sampling_status(arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    started = time.time()
    req_id = uuid.uuid4().hex[:12]
    try:
        result = await sampling.create_message(
            messages=[{"role": "user", "content": {"type": "text", "text": "Reply exactly OK."}}],
            max_tokens=4,
            system_prompt="Return only OK.",
            temperature=0.0,
            include_context="none",
            metadata={"tool": "check_sampling_status", "executa_invoke_id": invoke_id, "test_req_id": req_id},
            timeout=8.0,
        )
        text = ""
        content = result.get("content") if isinstance(result, dict) else None
        if isinstance(content, dict):
            text = str(content.get("text") or "")
        return {
            "ok": True,
            "status": "connected",
            "provider": "anna-llm",
            "message": "Anna LLM sampling is connected.",
            "elapsed_ms": int((time.time() - started) * 1000),
            "invoke_id": invoke_id,
            "test_req_id": req_id,
            "text": text[:16],
        }
    except SamplingError as exc:
        return {
            "ok": False,
            "status": "unavailable",
            "provider": "anna-llm",
            "message": exc.message or "Anna LLM sampling is unavailable.",
            "code": exc.code,
            "elapsed_ms": int((time.time() - started) * 1000),
            "invoke_id": invoke_id,
            "test_req_id": req_id,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "provider": "anna-llm",
            "message": str(exc) or "Anna LLM sampling check failed.",
            "elapsed_ms": int((time.time() - started) * 1000),
            "invoke_id": invoke_id,
            "test_req_id": req_id,
        }


async def _test_sampling(arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    """Test Anna sampling with a minimal one-shot call. Returns detailed diagnostics."""
    started = time.time()
    req_id = ""
    try:
        sampling_fn = _build_sampling_for_run({"ai_provider": "anna-llm"}, invoke_id)
        if sampling_fn is None:
            return {"ok": False, "error": "sampling_fn is None — ai_provider must be 'anna-llm'", "invoke_id": invoke_id}

        req_id = uuid.uuid4().hex[:12]
        result = await sampling_fn(
            messages=[{"role": "user", "content": {"type": "text", "text": "Say 'hello' in exactly one word. Reply with only that word."}}],
            max_tokens=16,
            system_prompt="You are a test probe. Reply concisely.",
            temperature=0.0,
            include_context="none",
            metadata={"tool": "test_sampling", "executa_invoke_id": invoke_id, "test_req_id": req_id},
            timeout=30.0,
        )
        elapsed_ms = int((time.time() - started) * 1000)
        return {
            "ok": True,
            "elapsed_ms": elapsed_ms,
            "test_req_id": req_id,
            "invoke_id": invoke_id,
            "model": result.get("model"),
            "stop_reason": result.get("stopReason"),
            "content_type": result.get("content", {}).get("type") if isinstance(result.get("content"), dict) else "unknown",
            "text": str(result.get("content", {}).get("text", ""))[:200] if isinstance(result.get("content"), dict) else "",
            "usage": result.get("usage"),
        }
    except SamplingError as exc:
        elapsed_ms = int((time.time() - started) * 1000)
        return {
            "ok": False,
            "elapsed_ms": elapsed_ms,
            "test_req_id": req_id,
            "invoke_id": invoke_id,
            "error_code": exc.code,
            "error_message": exc.message,
            "error_data": exc.data,
        }
    except Exception as exc:
        elapsed_ms = int((time.time() - started) * 1000)
        return {
            "ok": False,
            "elapsed_ms": elapsed_ms,
            "test_req_id": req_id,
            "invoke_id": invoke_id,
            "error_code": "client_exception",
            "error_message": str(exc),
        }


# Mirrors the real pipeline's wire format:
#   system_prompt  = short JSON-only instruction
#   user_message   = rubric + email + output format + JSON instruction
# See call_llm_json() in llm_runtime/service.py:494 and
# build_anna_single_judgment_prompt() in judgment_engine/service.py:495.

_BRIEF_TEST_SYSTEM_PROMPT = "You are a strict JSON generator. Output ONLY valid JSON — no explanation, no markdown, no code fences."

_BRIEF_TEST_USER_MESSAGE = """\
You are Anna, an executive email assistant. Evaluate exactly ONE email candidate against the strategy below.
Output ONLY a single JSON object. Do NOT wrap in markdown. Do NOT explain. The very first character you write MUST be `{`.

## Strategy
Default 秘书模式: 找出真正需要用户注意的邮件事项。

## Judgment Rubric — Binary Decision

First, answer THIS question about the email:

  **"Does this email require the user to send a reply message?"**

  If YES → user_action = "reply"
    Then pick the BEST action_reason:
    - question_asked: the sender explicitly asked a question or made a request that needs an answer. EXCLUDE automated notifications from noreply/notification addresses and social media alerts.
    - waiting_for_you: the sender is clearly waiting for the user's input, approval, or decision. EXCLUDE automated reminders and system-generated messages.
    - unsent_draft: this is a draft the user wrote but never sent
    - courtesy_due: sender invested real effort (wrote 3+ substantive sentences, shared a document, or explicitly asked for the user's thoughts). Do NOT flag automated notifications, newsletters, receipts, or one-line status updates.

  If NO → user_action = "review"
    Then pick the BEST action_reason:
    - upcoming_event: interview/meeting/deadline reminder — worth noting the time
    - deal_or_pipeline: project/partnership/deal status update worth tracking
    - security_or_billing: security alert, billing issue, subscription — needs checking
    - receipt_or_notice: receipt, subscription confirmation, normal account notice — record only
    - cleanup: newsletter, promotion, automated digest — safe to archive

  CRITICAL: If the sender address looks automated (contains "noreply", "no-reply", "notification", "@linkedin.com" alerts, social media notification bots), set user_action="review" regardless of the email body content.

  Priority & action_reason mapping (you MUST follow):
    question_asked → priority=high when explicit deadline, money/pricing/contract, or key contact. priority=medium otherwise. surface=true.
    waiting_for_you → priority=high when time-sensitive AND sender says "let me know"/"please confirm". priority=medium otherwise. surface=true.
    unsent_draft → priority=medium, surface=true
    courtesy_due → priority=medium, surface=true
    security_or_billing → priority=critical when unauthorized/payment failed/imminent interruption. priority=high otherwise. surface=true.
    upcoming_event → priority=high when within 48h. priority=medium otherwise.
    deal_or_pipeline → priority=medium
    receipt_or_notice → priority=low
    cleanup → priority=low

## Mailbox Owner
You are evaluating mail for: test@example.com
Match by EMAIL ADDRESS (between < >), not by display name.
- If the sender's email IS the mailbox owner → OUTGOING mail.
  SENT: user already sent it → surface=false, priority=low, user_action=review, action_reason=cleanup.
  DRAFT: user hasn't sent it yet → user_action=reply, action_reason=unsent_draft, priority=medium.

## Email
candidate_id: test_cand_001
kind: reply_required_possible
priority_hint: high
from: "Alice Zhang" <alice@acmecorp.com>
subject: Q3 proposal review — need your sign-off by Friday
snippet: Hi, I've attached the updated Q3 proposal incorporating feedback from the leadership review. Could you take a look and provide sign-off by Friday EOD? The procurement team is waiting on this to finalize the vendor contract. Let me know if you need any clarifications.
date: 2026-06-09
context_type: message_detail
body_text: > Hi,\n> I've attached the updated Q3 proposal v3 incorporating the feedback from the leadership review last Thursday.\n> \n> Key changes:\n> - Budget adjusted from $180k to $210k to cover the expanded scope\n> - Timeline shifted: kickoff moved to July 15\n> - Vendor selection narrowed to two finalists (AcmeTech and BuildRight)\n> \n> Could you review and provide sign-off by Friday EOD? The procurement team is blocked on the vendor contract until they have your approval.\n> \n> Let me know if you need a call to walk through the changes.\n> \n> Thanks,\n> Alice

## User request
Find emails that need my attention.

## Output format
Return exactly this JSON shape:

{
  "candidate_id": "test_cand_001",
  "priority": "medium",
  "surface": true,
  "user_action": "reply",
  "action_reason": "question_asked",
  "title": "Short card title (English ≤12 words)",
  "context": "WHAT happened: who did what, when, and current status. Verifiable facts only. English ≤30 words.",
  "suggestion": "Specific next action. Be concrete, not generic. English ≤15 words.",
  "action": "create_draft",
  "needs": "English ≤4 words label for what the user needs to decide or do.",
  "latest_action": "English ≤8 words. What recently happened — the latest action by a person or service.",
  "latest_actor": "English name or service. Who performed the latest_action.",
  "reply_gaps": {"needs_user_input":true, "summary":"one-sentence summary", "questions":[{"id":"q1", "question":"What do you want to reply?", "hint":"short hint", "required":true}]},
  "confidence": 0.85
}

Allowed user_action: reply, review.
Allowed action_reason: question_asked, waiting_for_you, unsent_draft, courtesy_due, upcoming_event, deal_or_pipeline, security_or_billing, receipt_or_notice, cleanup.
Allowed priority: critical, high, medium, low, ignore.
Allowed action: create_draft, create_reminder, save_note, do_nothing.

## Rules
- user_action + action_reason MUST be consistent: question_asked/waiting_for_you/unsent_draft/courtesy_due → reply. upcoming_event/deal_or_pipeline/security_or_billing/receipt_or_notice/cleanup → review.
- priority: reply reasons → medium or high. security_or_billing → critical or high. schedule/logistics → medium. receipt/cleanup → low.

Return ONLY one valid JSON object. Do not include markdown fences, prose, analysis, or code comments.
"""


async def _test_sampling_brief(arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    """Test Anna sampling with a realistic brief-style judgment call.

    Uses the same message shape, system prompt complexity, and parameters
    (max_tokens=8000, temperature=0.1, timeout=120s) as the real brief pipeline.
    """
    import json as _json
    started = time.time()
    req_id = ""
    try:
        sampling_fn = _build_sampling_for_run({"ai_provider": "anna-llm"}, invoke_id)
        if sampling_fn is None:
            return {"ok": False, "error": "sampling_fn is None", "invoke_id": invoke_id}

        req_id = uuid.uuid4().hex[:12]
        result = await sampling_fn(
            messages=[{"role": "user", "content": {"type": "text", "text": _BRIEF_TEST_USER_MESSAGE}}],
            max_tokens=8000,
            system_prompt=_BRIEF_TEST_SYSTEM_PROMPT,
            temperature=0.1,
            include_context="none",
            metadata={"tool": "test_sampling_brief", "executa_invoke_id": invoke_id, "test_req_id": req_id},
            timeout=120.0,
        )
        elapsed_ms = int((time.time() - started) * 1000)
        text = str(result.get("content", {}).get("text", "")) if isinstance(result.get("content"), dict) else ""
        usage = result.get("usage", {})
        output_tokens = usage.get("outputTokens") if isinstance(usage, dict) else "unknown"

        json_ok = False
        json_error = ""
        parse_preview = ""
        if text:
            try:
                parsed = _json.loads(text.strip())
                json_ok = isinstance(parsed, dict) and "priority" in parsed
                parse_preview = _json.dumps({k: v for k, v in parsed.items() if k in ("priority", "user_action", "action_reason", "title")})[:300]
            except Exception as exc:
                json_error = str(exc)[:200]
                parse_preview = text.strip()[:300]

        return {
            "ok": True,
            "elapsed_ms": elapsed_ms,
            "test_req_id": req_id,
            "invoke_id": invoke_id,
            "model": result.get("model"),
            "stop_reason": result.get("stopReason"),
            "output_tokens": output_tokens,
            "json_ok": json_ok,
            "json_error": json_error,
            "parse_preview": parse_preview,
            "usage": usage,
        }
    except SamplingError as exc:
        elapsed_ms = int((time.time() - started) * 1000)
        return {
            "ok": False,
            "elapsed_ms": elapsed_ms,
            "test_req_id": req_id,
            "invoke_id": invoke_id,
            "error_code": exc.code,
            "error_message": exc.message,
            "error_data": exc.data,
        }
    except Exception as exc:
        elapsed_ms = int((time.time() - started) * 1000)
        return {
            "ok": False,
            "elapsed_ms": elapsed_ms,
            "test_req_id": req_id,
            "invoke_id": invoke_id,
            "error_code": "client_exception",
            "error_message": str(exc),
        }


def _start_test_sampling_async(arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    """Like test_sampling_brief, but returns immediately and runs sampling in background.

    This is the decisive experiment: if the background call fails with -32001
    while the synchronous test_sampling_brief succeeds, the Anna platform binds
    sampling authorization to the *active* invoke lifecycle.
    """
    run_id = f"tsa_{uuid.uuid4().hex[:12]}"
    MAIL_AGENT_RUNS[run_id] = {
        "run_id": run_id,
        "status": "queued",
        "stage": "queued",
        "progress": {},
        "warnings": [],
        "started_at": beijing_now(),
        "updated_at": beijing_now(),
        "result": None,
        "error": "",
        "partial": {"invoke_id": invoke_id},
    }
    asyncio.run_coroutine_threadsafe(_run_test_sampling_async(run_id, arguments, invoke_id), loop)
    return {
        "run_id": run_id,
        "status": "queued",
        "started_at": beijing_now(),
        "note": "Sampling scheduled in background — poll with get_mail_agent_run",
    }


async def _run_test_sampling_async(run_id: str, arguments: dict[str, Any], invoke_id: str) -> None:
    """Background sampling test — waits 2s then calls the brief-style sampling."""
    try:
        await asyncio.sleep(2.0)  # simulate real scan delay
        MAIL_AGENT_RUNS[run_id]["status"] = "running"
        MAIL_AGENT_RUNS[run_id]["stage"] = "sampling"
        MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
        result = await _test_sampling_brief(arguments, invoke_id)
        MAIL_AGENT_RUNS[run_id].update({
            "status": "done",
            "stage": "done",
            "updated_at": beijing_now(),
            "result": result,
        })
    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update({
            "status": "failed",
            "stage": "failed",
            "updated_at": beijing_now(),
            "error": str(exc),
        })

__all__ = [name for name in globals() if not name.startswith("__")]
