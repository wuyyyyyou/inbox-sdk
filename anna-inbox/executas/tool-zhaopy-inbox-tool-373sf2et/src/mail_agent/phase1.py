"""Phase 1 LLM 批量分类模块。

用一次 LLM 调用对所有邮件头进行批量分类，替换/增强基于规则的候选生成。
设计文档 §11.1。

工作流程：
  1. 将所有邮件的紧凑头信息（发件人、主题、片段、标签等）打包成一个 JSON
  2. 调用 LLM 一次性批量分类，输出每条邮件的 item_type、is_candidate、candidate_kind 等
  3. 解析 LLM 响应，与规则信号融合，生成 CandidateItem 列表
  4. LLM 失败时 fallback 到纯规则生成（generate_candidates）

相比纯规则方式，LLM 可以识别更微妙的语境（如区分"真实的合作邮件"
和"伪装成合作的平台通知"）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .types import (
    CandidateItem,
    CandidateKind,
    MailStrategy,
    MailboxProfile,
    MessageLite,
    ReadDepth,
)

# ── user_action → CandidateKind 映射 ──────────────────────────────

def _user_action_to_kind(user_action: str) -> CandidateKind:
    if user_action == "reply":
        return "reply_required_possible"
    if user_action == "review":
        return "account_notice_possible"
    return "safe_cleanup_bundle"

# Anna sampling 目前没有 schema/streaming 约束，Phase 1 大批量 JSON 容易输出不完整。
_ANNA_PHASE1_BATCH_SIZE = 8

# ── Prompt 构建 ──────────────────────────────────────────────────

_PHASE1_SYSTEM = """You are Anna's batch email triage engine. Classify ALL email headers in one pass.

## Priority
- high: security incident, payment failure, direct ask from known contact, deadline today
- medium: needs reply/confirmation, collaboration opportunity, account notice worth checking
- low: newsletters, promotions, automated notifications, receipts, already-handled threads

## Read Depth
- header_only: obvious low-value (promotions, newsletters, social notifications)
- message_detail: needs body text to confirm (security, billing, account notices)
- thread_context: needs full thread history (ongoing conversations, collaboration)

## Safety Rules (NEVER violate)
1. Login alerts, password changes, suspicious sign-ins → user_action=reply + priority_hint=high
2. Payment failed, chargeback, subscription expired → user_action=reply + priority_hint=high
3. Permission changes, account recovery → user_action=reply + priority_hint=high
4. If the sender's email address matches the mailbox owner, the message is OUTGOING.
   SENT → user_action=ignore (user already sent it). DRAFT → user_action=reply, reason="unsent draft".

## User Action
Instead of is_candidate, output a "user_action" field with one of three values:

reply — A person is asking/requesting/following up and needs a response.
        Internal colleagues (@anna.partners) asking questions ARE reply.
        Candidates following up with continued interest ARE reply.
        Any email with a direct question or time-sensitive decision IS reply.

review — No reply needed, but contains useful signal worth 5 seconds.
         Pipeline stage changes, scorecard results, interview reminders,
         new applications, referral notifications, calendar reschedules,
         internal candidate notes, feedback reminders with deadlines,
         candidate thank-you notes that show continued engagement,
         interview prep material shared ahead of a scheduled interview.

ignore — You can delete this without opening and miss nothing useful.
         ONLY: mass-mail newsletters, daily digests with no personal action
         items, one-line thank-you with zero follow-up, internal guides/FYI
         with no ask and no deadline. If in doubt between review and ignore,
         choose review.

Self-check: before outputting, re-read your own reason. If the reason says
the email is "useful", requires "awareness", "attention", or contains a
"signal" or "deadline", user_action CANNOT be ignore — use review instead.

CRITICAL — Time format: NEVER use relative time ("tomorrow", "next Monday", "this Friday"). ALWAYS use absolute calendar dates (e.g. "Jun 3", "May 28, 2:30 PM"). The Date field in each header is already in absolute format — use it directly.

## Output Constraint
Your entire response must be a single JSON object. The very first character you output must be `{`. Do NOT wrap the JSON in markdown fences. Do NOT write any text before or after the JSON."""


def _fmt_phase1_date(epoch_ms: str) -> str:
    """Convert Gmail internalDate (epoch ms) to short readable form for the Phase 1 LLM prompt."""
    if not epoch_ms:
        return ""
    try:
        from datetime import datetime, timezone, timedelta
        dt = datetime.fromtimestamp(int(epoch_ms) / 1000.0, tz=timezone(timedelta(hours=8)))
        return dt.strftime("%b %d %H:%M")
    except (ValueError, TypeError, OSError):
        return str(epoch_ms)[:20]


def _compact_header(msg: MessageLite, index: int) -> dict[str, Any]:
    """将 MessageLite 压缩为 LLM prompt 可用的紧凑字典。

    使用单字母 key 以减少 token 消耗：
      i=序号, id=消息ID, f=发件人, s=主题, sn=片段,
      d=日期, l=标签, u=未读, st=星标, im=重要, at=有附件
    """
    return {
        "i": index,
        "id": msg.message_id[:40] if msg.message_id else "",
        "f": (msg.from_addr or "")[:80],
        "s": (msg.subject or "")[:120],
        "sn": (msg.snippet or "")[:100],
        "d": _fmt_phase1_date(msg.internal_date),
        "l": (msg.label_ids or [])[:5],
        "u": msg.unread,
        "st": msg.starred,
        "im": msg.important,
        "at": msg.has_attachment,
    }


def build_phase1_user_prompt(
    messages: list[MessageLite],
    strategy: MailStrategy,
    profile: MailboxProfile,
) -> str:
    """构建 Phase 1 的 user prompt，包含所有邮件的紧凑头信息。

    返回的 prompt 指引 LLM 对每封邮件输出分类和判断理由。
    """
    headers_json = json.dumps(
        [_compact_header(m, i) for i, m in enumerate(messages)],
        ensure_ascii=False,
    )

    allowed_kinds = strategy.candidate_policy.candidate_kinds
    kinds_hint = ", ".join(allowed_kinds)

    return f"""## Strategy
{strategy.name}: {strategy.description}
Allowed candidate kinds: {kinds_hint}
{strategy.candidate_policy.llm_candidate_hints}

## Mailbox Owner
{profile.mailbox_id} — match by EMAIL ADDRESS (between < >), not by display name.
- Sender IS the mailbox owner → OUTGOING. SENT: is_candidate=false. DRAFT: is_candidate=true, item_type=NeedsReply, reason="unsent draft".

## Headers ({len(messages)} emails)
Each header: i=index, id=message_id, f=from, s=subject, sn=snippet, d=date, l=labels, u=unread, st=starred, im=important, at=has_attachment.
{headers_json}

## Required Output
Return a JSON object whose FIRST character is `{{`. Shape:

{{"classifications": [
  {{{{
    "message_id": "<copy the exact 'id' from the input header>",
    "user_action": "reply",
    "priority_hint": "high",
    "read_depth": "message_detail",
    "confidence": 0.92,
    "reason": "Unknown Windows login from Shanghai — user must verify"
  }}}}
]}}

Every input header MUST have exactly one entry in the classifications array. Do NOT skip or add entries.
- message_id: copy the "id" field from the input header exactly.
- user_action: "reply" | "review" | "ignore" — per the User Action rules above.
- priority_hint: "high" | "medium" | "low"
- read_depth: "header_only" | "message_detail" | "thread_context"
- confidence: float between 0.0 and 1.0
- reason: Explain WHY you chose this user_action. If ignore: what info is being
  discarded and why it doesn't matter. If review: what useful signal this email
  contains. If reply: who is waiting and for what."""


# ── 响应解析 ──────────────────────────────────────────────────────

def parse_phase1_response(
    payload: dict[str, Any],
    messages: list[MessageLite],
    strategy: MailStrategy,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """解析 Phase 1 LLM 的 JSON 响应。

    Returns:
        (classifications, low_value_items)

        classifications: 每条是高价值候选项，包含：
          message_id, candidate_kind, priority_hint, read_depth,
          confidence, reason, item_type, source

        low_value_items: 低价值邮件条目，直接用于 cleanup bundle 卡片，包含：
          message_id, item_type, reason, confidence, source
    """
    raw_classifications = payload.get("classifications") if isinstance(payload.get("classifications"), list) else []

    msg_by_id: dict[str, MessageLite] = {}
    for m in messages:
        if m.message_id:
            msg_by_id[m.message_id] = m

    classifications: list[dict[str, Any]] = []
    low_value_items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for item in raw_classifications:
        if not isinstance(item, dict):
            continue

        msg_id = str(item.get("message_id") or "")
        if not msg_id or msg_id in seen_ids:
            continue
        seen_ids.add(msg_id)

        user_action = str(item.get("user_action") or "")
        if not user_action:
            is_candidate = bool(item.get("is_candidate", True))
            user_action = "reply" if is_candidate else "ignore"

        if user_action not in ("reply", "review", "ignore"):
            user_action = "ignore"

        if user_action == "ignore":
            low_value_items.append(_build_low_value_item(msg_id, item, "llm"))
            continue

        llm_kind = _user_action_to_kind(user_action)

        llm_priority = str(item.get("priority_hint") or item.get("priority") or "medium")
        priority_hint = "medium"
        if llm_priority in ("high", "critical", "action_needed"):
            priority_hint = "high"
        elif llm_priority in ("medium", "agent_can_handle"):
            priority_hint = "medium"
        elif llm_priority in ("low", "fyi", "ignore_or_archive"):
            priority_hint = "low"
        if user_action == "review" and priority_hint == "medium":
            priority_hint = "low"

        llm_depth = str(item.get("read_depth") or "message_detail")
        read_depth: ReadDepth = "message_detail"
        if llm_depth in ("header_only", "message_detail", "thread_context", "batch_summary"):
            read_depth = llm_depth  # type: ignore[assignment]
        elif llm_depth == "HeaderOnly":
            read_depth = "header_only"
        elif llm_depth == "MessageDetail":
            read_depth = "message_detail"
        elif llm_depth == "ThreadContext":
            read_depth = "thread_context"

        try:
            confidence = float(item.get("confidence", 0.7))
        except (TypeError, ValueError):
            confidence = 0.7

        classifications.append({
            "message_id": msg_id,
            "is_candidate": True,
            "candidate_kind": llm_kind,
            "priority_hint": priority_hint,
            "read_depth": read_depth,
            "confidence": confidence,
            "reason": str(item.get("reason") or "")[:200],
            "source": "llm",
            "user_action": user_action,
        })

    # LLM 未分类的邮件 → 低价值 fallback
    for m in messages:
        if m.message_id and m.message_id not in seen_ids:
            low_value_items.append({
                "message_id": m.message_id,
                "reason": "Not classified by LLM",
                "confidence": 0.3,
                "source": "fallback",
            })

    return classifications, low_value_items


def _build_low_value_item(msg_id: str, llm_item: dict[str, Any], source: str) -> dict[str, Any]:
    return {
        "message_id": msg_id,
        "reason": str(llm_item.get("reason") or "")[:200],
        "confidence": _safe_float(llm_item.get("confidence"), 0.5),
        "source": source,
    }


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ── 与规则信号融合 ──────────────────────────────────────────────

def create_candidate_id(message_id: str) -> str:
    """生成候选唯一标识。"""
    import uuid
    return f"cand_{uuid.uuid4().hex[:12]}"


def classifications_to_candidates(
    classifications: list[dict[str, Any]],
    messages: list[MessageLite],
    strategy: MailStrategy,
    profile: MailboxProfile,
) -> list[CandidateItem]:
    """将 Phase 1 分类结果转换为 CandidateItem 对象。

    融合 LLM 分类和规则信号检测：
    - LLM 提供 item_type 和 reason（存储到 evidence 中供 Phase 2 使用）
    - 规则检测提供 matched_signals（信号列表）
    - 置信度 = LLM 基础置信度 + 规则信号增强（每个强信号 +0.05，上限 0.95）
    """
    from .candidate import detect_signals

    msg_map: dict[str, MessageLite] = {m.message_id: m for m in messages if m.message_id}
    candidates: list[CandidateItem] = []

    for cls in classifications:
        msg_id = cls["message_id"]
        msg = msg_map.get(msg_id)
        if not msg:
            continue

        # 获取规则信号以丰富 evidence
        signals = detect_signals(msg, strategy, profile)

        kind: CandidateKind = cls["candidate_kind"]  # type: ignore[assignment]

        # 优先使用策略预设的读取深度，其次使用 LLM 建议的
        read_depth: ReadDepth = (
            strategy.context_policy.read_depth_by_candidate_kind.get(kind)
            or cls["read_depth"]
        )

        # 融合置信度：LLM 基础 + 规则信号增强
        confidence = cls["confidence"]
        if signals:
            strong_signals = {"security_keyword", "billing_keyword", "important", "starred", "human_reply"}
            boost = sum(0.05 for s in signals if s in strong_signals)
            confidence = min(0.95, confidence + boost)

        candidates.append(CandidateItem(
            candidate_id=create_candidate_id(msg_id),
            kind=kind,
            message_ids=[msg_id],
            thread_id=msg.thread_id,
            evidence={
                "from": msg.from_addr,
                "subject": msg.subject,
                "snippet": msg.snippet,
                "date": msg.internal_date,
                "labels": msg.label_ids,
                "matched_signals": signals,
                "llm_reason": cls.get("reason", ""),
                "user_action": cls.get("user_action", ""),
            },
            priority_hint=cls["priority_hint"],  # type: ignore[arg-type]
            read_depth_required=read_depth,
            source="rule_llm_merged",             # 标记为规则+LLM 融合
            confidence=confidence,
        ))

    # 按线程去重（与规则路径保持一致）
    return _dedupe_by_thread(candidates)


def _dedupe_by_thread(candidates: list[CandidateItem]) -> list[CandidateItem]:
    """按线程去重。同一线程只保留置信度最高的候选。"""
    thread_map: dict[str, CandidateItem] = {}
    for c in candidates:
        tid = c.thread_id or c.message_ids[0]
        if tid not in thread_map or c.confidence > thread_map[tid].confidence:
            thread_map[tid] = c
    return list(thread_map.values())


# ── 主入口 ────────────────────────────────────────────────────────

async def _run_phase1_single_batch(
    messages: list[MessageLite],
    strategy: MailStrategy,
    profile: MailboxProfile,
    sampling_create_message: Any,
    *,
    batch_index: int | None = None,
    batch_total: int | None = None,
) -> dict[str, Any]:
    from .llm import call_llm_json_safe

    system_prompt = _PHASE1_SYSTEM
    user_prompt = build_phase1_user_prompt(messages, strategy, profile)
    strict_anna_sampling = sampling_create_message is not None

    metadata = {
        "tool": "phase1_batch_classify",
        "strategy_mode": strategy.id,
        "message_count": str(len(messages)),
    }
    if batch_index is not None and batch_total is not None:
        metadata["batch_index"] = str(batch_index)
        metadata["batch_total"] = str(batch_total)

    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=system_prompt,
        user_message=user_prompt,
        fallback={"classifications": []},
        temperature=0.1,
        max_tokens=20480,
        timeout=240.0,
        metadata=metadata,
        allow_fallback=not strict_anna_sampling,
        allow_sampling_provider_fallback=not strict_anna_sampling,
    )

    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    if not payload or result.get("fallback_used"):
        from .candidate import generate_candidates
        return {"candidates": generate_candidates(messages, strategy, profile), "low_value_items": []}

    classifications, low_value_items = parse_phase1_response(payload, messages, strategy)
    candidates = classifications_to_candidates(classifications, messages, strategy, profile)
    return {"candidates": candidates, "low_value_items": low_value_items}


async def run_phase1_batch_classify(
    messages: list[MessageLite],
    strategy: MailStrategy,
    profile: MailboxProfile,
    sampling_create_message: Any = None,
) -> dict[str, Any]:
    """执行 Phase 1 的 LLM 批量分类。

    Returns:
        {"candidates": [...], "low_value_items": [...]}
    """
    if not messages:
        return {"candidates": [], "low_value_items": []}

    if sampling_create_message is None:
        return await _run_phase1_single_batch(messages, strategy, profile, sampling_create_message)

    batches = [
        messages[index:index + _ANNA_PHASE1_BATCH_SIZE]
        for index in range(0, len(messages), _ANNA_PHASE1_BATCH_SIZE)
    ]
    all_candidates: list[CandidateItem] = []
    all_low_value: list[dict[str, Any]] = []
    for batch_index, batch in enumerate(batches, start=1):
        batch_result = await _run_phase1_single_batch(
            batch,
            strategy,
            profile,
            sampling_create_message,
            batch_index=batch_index,
            batch_total=len(batches),
        )
        all_candidates.extend(batch_result["candidates"])
        all_low_value.extend(batch_result["low_value_items"])

    return {
        "candidates": _dedupe_by_thread(all_candidates),
        "low_value_items": all_low_value,
    }
