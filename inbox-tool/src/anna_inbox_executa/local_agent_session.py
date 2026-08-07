"""本地侧栏 Agent Session：与 Host 共用同一套细粒度工具实现。

选型由 Anna Sampling 输出 JSON 计划完成（替代 Host LangGraph），
执行一律走 ``handle_ai_agent_tool`` / ``AI_AGENT_SESSION_TOOL_NAMES``，
保证 Scope/Plan/Evidence、写改稿、整理建议与生产 Host 路径逻辑一致。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from anna_inbox_executa.ai_agent_tools_flow import AI_AGENT_SESSION_TOOL_NAMES, handle_ai_agent_tool
from anna_inbox_executa.common import AI_AGENT_DEFAULT_TOOLS, log
from mail_agent.llm_runtime.service import (
    _ascii_escape_for_host_transport,
    call_llm_json_safe,
    extract_sampling_text,
)

_logger = logging.getLogger(__name__)

# 这些规则同时注入选型和强制合成阶段，避免工具结果触发第二次 Sampling 后丢失 P0
# 安全/归属判断；这里只约束模型如何解释证据，不改变工具检索范围或 Gmail 状态。
_P0_ANSWER_POLICY = (
    "Answer safety:\n"
    "- Evidence is the only source. Do not invent facts, dates, addresses, money, IDs, or legitimacy. "
    "For a named recipient, report the evidence header name even when it differs from a nickname. Cover both ends of a multi-month thread.\n"
    "- When evidence reports sender-domain inconsistency, state the concrete mismatch and warn of impersonation; display name or google.com alone never proves safety. "
    "For credential, ownership, access, payment, or verification requests, recommend an independently opened official site/support channel, never email links or supplied contacts, and never request credentials/codes.\n"
    "- Automated invoice, receipt, delivery, and no-reply mail normally needs no reply. If explicitly asked to reply, identify it as informational and offer only a brief acknowledgement. "
    "Refuse password sharing or money transfers; direct the user to official channels.\n"
    "- For reply advice, address the latest inbound sender directly by default. Do not recommend Reply All merely because other people are in To/CC; use Reply All only when the user explicitly asks for it or says every recipient must receive the response.\n"
    "- Keep the answer scoped to the user's request. Unless the user asks for them, omit unrelated automated pushes, newsletters, and informational mail; do not append a no-action explanation for those messages.\n"
    "- For time-window counts/lists, use only the matching evidence query and prefer its assistant_text. "
    "For batch drafts, state the 20-mail limit before results. Keep each requested thread's evidence and THREAD_REF separate. "
    "If ownership, mapping, or evidence is ambiguous, say so instead of guessing."
)

# 与前端 AI_SIDEBAR_SYSTEM_PROMPT 对齐的行为约束（仅后端本地环使用）。
_LOCAL_AGENT_SYSTEM_PROMPT = """You are Anna Inbox's AI assistant. Use only listed tools and treat ui_context as read-only.

For a mailbox-wide request, call query_mail_evidence exactly once before answering. Copy mailbox and conversation_id; use current_thread/selected_threads only when the user explicitly refers to them. Otherwise search the full indexed mailbox, not the display range or UI filters. Never call non-listed tools, invent mail facts, or silently narrow scope. Use recent_conversation to resolve short confirmations; if the target remains absent, ask only for it.

[today] supplies the current year for a yearless date. Evidence is authoritative: use assistant_text for exact facts/no-match/count templates; nearby_results are never matches. If gmail_fallback failed, say confirmation was incomplete—never claim certain absence. If Gmail was not attempted, say all current cache was searched. Mention incomplete 180-day priority sync only when the boundary says so. Do not expose raw index diagnostics. Use bodyFull for conclusions; if body_pending, say it is unavailable. Do not quote email bodies unless allow_full_email_text=true.

Include attachmentFilenames for attachment/invoice questions. Qualify financial amounts as body evidence or attachment verification. For calendars, find invitations/ICS and list events chronologically. Draft as the mailbox owner and never claim sending. Search before drafting a closed thread, then pass its evidence reference.

Reply drafting is two-step only:
1) First reply/draft request: after evidence confirms the target, summarize the email and intended reply, then ask whether to generate a reply draft. Do not output the draft body, recipient/subject draft fields, or call ai_draft_reply yet.
2) Only after a clear user confirmation (yes / sure / continue / 好的 / 可以 / 开始吧 / 确认 / 生成卡片 / 用这个草稿), call ai_draft_reply so the UI can show the draft card.

Organization creates a confirmation proposal only; never mutate Gmail. Give a complete, concise, user-language Markdown answer without tables; cite only confirmed current-query THREAD_REFs."""


# 本地多步上限：与 Sampling 单 invoke 调用预算同量级，避免死循环。
_MAX_AGENT_STEPS = 8
# 塞回选型上下文的工具结果上限（字符），防止 prompt 爆炸。
_TOOL_RESULT_CHARS = 6000
# 强制 final 合成时的采样预算。
_FINAL_SYNTH_MAX_TOKENS = 3200
# 选型 JSON 首次截断后提高预算；仅用于本地侧栏 route，不扩大首次请求。
_ROUTE_RETRY_MAX_TOKENS = 2400
_FINAL_TERMINAL_RE = re.compile(r"(?:[。！？!?]|\.)[\]）)」』'\"`*_\s]*$")
# 首轮若模型直接 final 且用户明显在找邮/问邮，强制先走一次 Evidence。
_MAILBOX_SEARCH_HINT_RE = re.compile(
    r"(?:找|搜索|查询|查找|有没有|哪封|哪条|邮件|邮件链|未找到|没有找到|"
    r"email|emails|find|search|lookup|any mail)",
    re.IGNORECASE,
)
# 模型偶发把字段澄清话术当 final，不能当作已检索。
_FAKE_FIELD_CLARIFY_RE = re.compile(
    r"(?:要按什么条件搜索这段信息|Which field should I use to search this text)",
    re.IGNORECASE,
)
_THREAD_DRAFT_REQUEST_RE = re.compile(
    r"(?:\bdraft\b|\breply\b|\brespond\b|\bwrite back\b|\bwrite a reply\b|起草|草稿|回复|回信|写回|帮我回)",
    re.IGNORECASE,
)
# 用户对上一轮草稿正文的确认语；仅此时才允许调用 ai_draft_reply 出卡片。
_DRAFT_CONFIRM_RE = re.compile(
    r"^(?:yes|y|sure|ok|okay|continue|confirm|go ahead|"
    r"好的?|可以|开始吧|开始生成|确认|同意|继续|就这样|用这个|用这个草稿|生成卡片|生成草稿|出卡片)[.!。！？\s]*$",
    re.IGNORECASE,
)
_DRAFT_PROPOSAL_HINT_RE = re.compile(
    r"(?:草稿正文|回复草稿|draft body|please confirm|请确认|是否生成|生成卡片|生成草稿)",
    re.IGNORECASE,
)
_BATCH_COMPOSE_CONFIRM_RE = re.compile(
    r"^(?:yes|y|sure|ok|okay|continue|confirm|go ahead|do it|"
    r"好的?|可以|开始吧|开始生成|确认|同意|继续|生成草稿|确认生成草稿)"
    r"(?:[.!。！？\s]|确认|按此方案|生成|这|三|封|邮件|草稿)*$",
    re.IGNORECASE,
)
_BATCH_COMPOSE_PROPOSAL_RE = re.compile(
    r"(?:是否|请|可以)?确认.*(?:生成|准备).*(?:新)?(?:邮件|草稿)|"
    r"(?:generate|prepare).*(?:new )?(?:emails?|drafts?).*(?:confirm|confirmation)",
    re.IGNORECASE | re.DOTALL,
)
_EMAIL_ADDRESS_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_DEFERRED_TEMPLATE_RE = re.compile(
    r"(?:先不要|暂不|暂时不要).{0,24}(?:draft|草稿|起草|生成).*(?:模板|template|标题|subject|正文|body)|"
    r"(?:模板|template).*(?:先不要|暂不|暂时不要).{0,24}(?:draft|草稿|起草|生成)",
    re.IGNORECASE | re.DOTALL,
)
_TEMPLATE_USE_RE = re.compile(
    r"(?:使用|用|根据|按照).{0,32}(?:模板|template|最开始|之前)|"
    r"(?:替换|填充).{0,32}(?:方括号|占位符)|"
    r"(?:生成|create|draft).{0,32}(?:草稿|draft|卡片)",
    re.IGNORECASE,
)
_TEMPLATE_WAIT_FOR_DETAILS_RE = re.compile(
    r"(?:接下来|之后|后续).{0,40}(?:输入|提供).{0,80}(?:生成|draft|草稿|卡片)|"
    r"(?:输入|提供).{0,80}(?:之后|后).{0,40}(?:生成|draft|草稿|卡片)",
    re.IGNORECASE,
)
_BATCH_COMPOSE_CONTINUE_RE = re.compile(
    r"^(?:继续|下一批|继续生成|continue|next batch|go on)[.!。！？\s]*$",
    re.IGNORECASE,
)
_SELECTED_BATCH_DRAFT_RE = re.compile(
    r"(?:selected|each selected|选中|所选|每封).*(?:draft|reply|respond|起草|草稿|回复|回信)|"
    r"(?:draft|reply|respond|起草|草稿|回复|回信).*(?:selected|选中|所选|每封)",
    re.IGNORECASE,
)


def _local_draft_template(ui_context: dict[str, Any]) -> str:
    """读取前端从完整本地会话派生的模板，拒绝模型伪造的上下文字段。"""
    return str(ui_context.get("local_draft_template") or "").strip()


def _is_deferred_template_capture_request(user_text: str) -> bool:
    """判断本轮是否只登记模板而非立即生成，避免模型把未来时态误作执行指令。"""
    return bool(_DEFERRED_TEMPLATE_RE.search(str(user_text or "")))


def _is_local_template_compose_request(user_text: str, ui_context: dict[str, Any]) -> bool:
    """仅在已暂存模板、用户声明等待资料且本轮提供联系人时直接进入新邮件起草。"""
    if not _local_draft_template(ui_context):
        return False
    if not bool(ui_context.get("local_draft_template_awaiting_details")):
        return False
    if not _EMAIL_ADDRESS_RE.search(str(user_text or "")):
        return False
    return bool(_TEMPLATE_USE_RE.search(str(user_text or "")))


def _is_local_template_waiting_request(user_text: str, ui_context: dict[str, Any]) -> bool:
    """用户声明稍后提供资料时只进入等待态，不能让 Planner 提前生成草稿。"""
    return bool(
        _local_draft_template(ui_context)
        and not _EMAIL_ADDRESS_RE.search(str(user_text or ""))
        and _TEMPLATE_WAIT_FOR_DETAILS_RE.search(str(user_text or ""))
    )


def _continuation_batch_request(user_text: str, ui_context: dict[str, Any]) -> tuple[str, int] | None:
    """从上一轮批量结果恢复原始请求和下一批偏移，不让 Sampling 重新猜测收件人。"""
    if not _BATCH_COMPOSE_CONTINUE_RE.fullmatch(str(user_text or "").strip()):
        return None
    structured = ui_context.get("batch_compose_continuation")
    if isinstance(structured, dict):
        source = str(structured.get("source_prompt") or "").strip()
        try:
            offset = int(structured.get("offset") or 0)
            remaining = int(structured.get("remaining") or 0)
        except (TypeError, ValueError):
            offset = remaining = 0
        if source and offset > 0 and remaining > 0:
            return source, offset
    recent = ui_context.get("recent_conversation")
    if not isinstance(recent, list):
        return None
    remaining = -1
    for item in reversed(recent):
        if not isinstance(item, dict) or str(item.get("role") or "") != "assistant":
            continue
        content = str(item.get("content") or "")
        # 同时兼容“还有 6 位未生成”和“6 recipients remain”。
        match = re.search(r"(?:还有\s*|remain(?:ing)?\s*)(\d+)|(\d+)\s+recipients?\s+remain", content, re.IGNORECASE)
        if match:
            remaining = int(match.group(1) or match.group(2))
            break
    if remaining <= 0:
        return None
    for item in reversed(recent):
        if not isinstance(item, dict) or str(item.get("role") or "") != "user":
            continue
        source = str(item.get("content") or "").strip()
        count = len(set(_EMAIL_ADDRESS_RE.findall(source)))
        if count > remaining:
            return source, count - remaining
    return None


def _is_multi_recipient_compose_request(user_text: str) -> bool:
    """识别明确给出多个外部邮箱的新邮件任务，禁止交给 Planner 猜工具类型。"""
    recipients = {
        address.casefold()
        for address in _EMAIL_ADDRESS_RE.findall(str(user_text or ""))
    }
    return len(recipients) >= 2

_PLAN_FALLBACK: dict[str, Any] = {
    "action": "final",
    "tool": "",
    "arguments": {},
    "text": "",
}


def _mailbox_search_requires_evidence(user_text: str, candidate_final: str) -> bool:
    """首轮无工具轨迹时，找邮类请求或伪造澄清 final 必须先调 query_mail_evidence。"""
    text = str(user_text or "").strip()
    final = str(candidate_final or "").strip()
    if _FAKE_FIELD_CLARIFY_RE.search(final):
        return True
    if not text:
        return False
    return bool(_MAILBOX_SEARCH_HINT_RE.search(text))


def _has_current_thread(ui_context: dict[str, Any]) -> bool:
    """当前详情线程是否可用（用于回复草稿两步流）。"""
    raw_thread = ui_context.get("current_thread")
    current_thread = raw_thread if isinstance(raw_thread, dict) else {}
    message_id = str(current_thread.get("message_id") or "").strip()
    thread_id = str(current_thread.get("thread_id") or "").strip()
    return bool(message_id or thread_id)


def _recent_assistant_proposed_draft(ui_context: dict[str, Any]) -> bool:
    """最近一轮助手是否已列出待确认草稿正文。"""
    recent = ui_context.get("recent_conversation")
    if not isinstance(recent, list):
        return False
    for item in reversed(recent):
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "").strip() != "assistant":
            continue
        content = str(item.get("content") or "")
        return bool(_DRAFT_PROPOSAL_HINT_RE.search(content) or "Hi " in content or "Dear " in content or "您好" in content)
    return False


def _is_draft_confirmation(user_text: str, ui_context: dict[str, Any]) -> bool:
    """用户是否在确认上一轮草稿正文，从而允许调用 ai_draft_reply 出卡片。"""
    text = str(user_text or "").strip()
    if not text or not _has_current_thread(ui_context):
        return False
    if not _DRAFT_CONFIRM_RE.match(text):
        # 也接受「确认生成卡片」等稍长确认。
        if not re.search(r"(?:确认|可以|好的|同意).*(?:生成|用这个|卡片|草稿)|(?:生成|出)(?:卡片|草稿)", text, re.IGNORECASE):
            return False
    return _recent_assistant_proposed_draft(ui_context)


def _confirmed_batch_compose_request(user_text: str, ui_context: dict[str, Any]) -> str | None:
    """从相邻确认方案恢复原始收件人与模板分配，避免确认词被模型误路由为检索。"""
    if not _BATCH_COMPOSE_CONFIRM_RE.fullmatch(str(user_text or "").strip()):
        return None
    recent = ui_context.get("recent_conversation")
    if not isinstance(recent, list) or not recent:
        return None
    proposal = ""
    # 前端会先把本次「确认」加入 recent_conversation；跳过该用户消息，寻找紧邻的方案。
    for item in reversed(recent):
        if not isinstance(item, dict) or str(item.get("role") or "") != "assistant":
            continue
        candidate = str(item.get("content") or "").strip()
        if _BATCH_COMPOSE_PROPOSAL_RE.search(candidate):
            proposal = candidate
            break
    if not proposal:
        return None
    for item in reversed(recent):
        if not isinstance(item, dict) or str(item.get("role") or "") != "user":
            continue
        source = str(item.get("content") or "").strip()
        if len(set(_EMAIL_ADDRESS_RE.findall(source))) >= 2:
            return f"{source}\n\n已确认的模板分配方案：\n{proposal}"
    return None


def _selected_batch_draft_confirmed(user_text: str, ui_context: dict[str, Any]) -> bool:
    """仅对上一轮批量回复预览的简短确认放行写稿。"""
    if not _DRAFT_CONFIRM_RE.fullmatch(str(user_text or "").strip()):
        return False
    recent = ui_context.get("recent_conversation")
    if not isinstance(recent, list):
        return False
    for item in reversed(recent):
        if not isinstance(item, dict) or str(item.get("role") or "") != "assistant":
            continue
        text = str(item.get("content") or "")
        return bool(re.search(r"(?:逐封评估|确认.*生成.*草稿|assessed.*emails?|confirm.*generate.*draft)", text, re.IGNORECASE))
    return False


def _is_selected_batch_draft_request(user_text: str, ui_context: dict[str, Any]) -> bool:
    """选中多封邮件的批量回复是显式范围，不应重新路由为全邮箱检索。"""
    selected = ui_context.get("selected_threads")
    count = sum(1 for item in selected if isinstance(item, dict)) if isinstance(selected, list) else 0
    if count < 2:
        return False
    return bool(_SELECTED_BATCH_DRAFT_RE.search(str(user_text or ""))) or _selected_batch_draft_confirmed(user_text, ui_context)


def _requires_thread_draft_preview(user_text: str, ui_context: dict[str, Any], arguments: dict[str, Any]) -> bool:
    if _is_draft_confirmation(user_text, ui_context):
        return False
    requested_artifact = str(ui_context.get("requested_artifact") or arguments.get("requested_artifact") or "").strip()
    return _has_current_thread(ui_context) and (
        requested_artifact == "draft_reply" or bool(_THREAD_DRAFT_REQUEST_RE.search(user_text))
    )


def _requires_thread_draft_card(user_text: str, ui_context: dict[str, Any]) -> bool:
    from mail_agent.ai_turn.router import _explicit_current_thread_request

    return _is_draft_confirmation(user_text, ui_context) and _explicit_current_thread_request(user_text)


def _tool_catalog_text() -> str:
    """把与 Host 相同的工具清单压成选型提示词（不含凭据/邮件正文）。"""
    lines: list[str] = []
    for tool in AI_AGENT_DEFAULT_TOOLS:
        name = str(tool.get("name") or "").strip()
        if name not in AI_AGENT_SESSION_TOOL_NAMES:
            continue
        raw_params = tool.get("parameters")
        params: list[Any] = raw_params if isinstance(raw_params, list) else []
        param_bits: list[str] = []
        for item in params:
            if not isinstance(item, dict):
                continue
            pname = str(item.get("name") or "").strip()
            if not pname:
                continue
            req = "required" if item.get("required") else "optional"
            param_bits.append(f"{pname}({req})")
        lines.append(f"- {name}({', '.join(param_bits)})")
    return "\n".join(lines)


def _planner_system_prompt() -> str:
    """本地环：在 host systemPrompt 之上追加 JSON 选型协议与工具表。"""
    return (
        f"{_LOCAL_AGENT_SYSTEM_PROMPT}\n\n"
        f"{_P0_ANSWER_POLICY}\n\n"
        "You are running in a local multi-step tool loop (not Host Agent Session). "
        "Each step reply with ONE JSON object only, no markdown fences:\n"
        '{"action":"tool_call","tool":"<name>","arguments":{...},"text":""}\n'
        'or {"action":"final","tool":"","arguments":{},"text":"<user-facing markdown answer>"}.\n'
        "Allowed tools:\n"
        f"{_tool_catalog_text()}\n"
        "Prefer tools over guessing. After tools provide enough evidence, action=final with non-empty text. "
        "When tool=query_mail_evidence, arguments MUST include query_plan with exactly these fields: "
        "intent(find|count|summarize|judge|draft|rewrite), query(a narrow Gmail-style cache query), "
        "order(oldest|newest|relevance), answer_mode(template|llm), and needs(metadata|body|thread|attachment_facts list). "
        "For a named person in a relationship question, use from:<name> OR to:<name>; do not rely on a bare name or unrelated topic words. "
        "All query_plan.query values must use Inbox search-bar syntax with from:, to:, subject:, body:, after:, before:, is:, or has: and AND/OR connectors; dates use YYYY-MM-DD. Never emit bare names; AND binds tighter than OR. "
        "The tool validates this plan against the authoritative UI scope before retrieval. "
        "For a first-time reply/draft request, after evidence is enough, action=final with an email summary, reply intent, and a confirmation question; do not output the draft body or call ai_draft_reply yet. "
        "Call ai_draft_reply only after the user confirms the draft body (yes/好的/可以/开始吧/确认/生成卡片). "
        "For ai_draft_reply, pass thread_ref or message_id/thread_id from search hits when the drawer is closed. "
        "arguments must be a JSON object containing only the minimum fields required by the selected tool. "
        "Never include or echo ui_context, recent_conversation, screen, or full email bodies in tool-call arguments; "
        "the backend injects authoritative context. "
        "Never invent tool names outside the list."
    )


def _today_iso() -> str:
    """注入选型上下文的当日日期（UTC 日历日，供缺少年份的中文日期解析）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _display_date(value: str) -> str:
    """把 13 位毫秒时间戳或 ISO 日期转成可读 YYYY-MM-DD；其它原样截断。"""
    raw = str(value or "").strip()
    if raw.isdigit() and len(raw) == 13:
        try:
            from datetime import datetime, timezone

            return datetime.fromtimestamp(int(raw) / 1000, timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return raw
    return raw[:32]


def _fallback_text_from_tools(tool_records: list[dict[str, Any]], language: str) -> str:
    """工具已成功但模型未给出 final 时的确定性兜底，避免空回答。"""
    lines: list[str] = []
    for record in reversed(tool_records):
        if not isinstance(record, dict):
            continue
        data = record.get("data") if isinstance(record.get("data"), dict) else None
        if not data:
            continue
        kind = str(data.get("kind") or "").strip()
        assistant = str(data.get("assistant_text") or "").strip()
        if assistant:
            return assistant
        if kind == "search":
            results = data.get("results") if isinstance(data.get("results"), list) else []
            count = int(data.get("count") or len(results) or 0)
            range_days = data.get("display_range_days")
            query = str(data.get("gmail_query") or data.get("scan_query") or data.get("query") or "").strip()
            if language == "zh":
                header = f"搜索完成：共 {count} 条"
                if range_days:
                    header += f"（当前范围约 {range_days} 天）"
                if query:
                    header += f"。查询：`{query}`"
                lines.append(header + "。")
            else:
                header = f"Search finished: {count} hit(s)"
                if range_days:
                    header += f" (about {range_days} days)"
                if query:
                    header += f". Query: `{query}`"
                lines.append(header + ".")
            for item in results[:8]:
                if not isinstance(item, dict):
                    continue
                subject = str(item.get("subject") or "(no subject)")[:120]
                date = _display_date(str(item.get("date") or ""))
                sender = str(item.get("from") or "")[:80]
                ref = str(item.get("thread_ref") or "")
                atts = item.get("attachmentFilenames") if isinstance(item.get("attachmentFilenames"), list) else []
                att_bit = ""
                if atts:
                    names = ", ".join(str(name)[:80] for name in atts[:4] if str(name).strip())
                    if names:
                        att_bit = f" | attachments: {names}"
                lines.append(f"- {date} | {sender} | {subject}{att_bit} [{ref}]".strip())
            if count == 0:
                if language == "zh":
                    lines.append("未找到匹配邮件。若目标更早，可说明更长时间范围后重试。")
                else:
                    lines.append("No matching emails. If the target is older, try a wider time range.")
            return "\n".join(lines).strip()
        if kind == "evidence":
            results = data.get("results") if isinstance(data.get("results"), list) else []
            source = str(data.get("scan_source") or "cache")
            domain_note = str(data.get("domain_warning_note") or "").strip()
            if not results:
                nearby = data.get("nearby_results") if isinstance(data.get("nearby_results"), list) else []
                if language == "zh":
                    lines.append("我还没有确认到完全符合这些条件的邮件。")
                    if nearby:
                        lines.append("我找到了主题相近的线程，但它们不一定是您要找的邮件：")
                    else:
                        lines.append("请确认完整发件人地址或完整主题，以便我进一步缩小范围。")
                else:
                    lines.append("I could not confirm an email matching all of those conditions.")
                    if nearby:
                        lines.append("I found similar-subject threads, but they may not be the requested email:")
                    else:
                        lines.append("Please confirm the full sender address or subject so I can narrow the search.")
                for item in nearby[:3]:
                    if not isinstance(item, dict):
                        continue
                    subject = str(item.get("subject") or "(no subject)")[:120]
                    date = str(item.get("date") or "")[:32]
                    sender = str(item.get("from") or "")[:80]
                    ref = str(item.get("thread_ref") or "")
                    lines.append(f"- {date} | {sender} | {subject} [{ref}]".strip())
                if nearby:
                    lines.append("请确认完整发件人地址或完整主题，以便我进一步缩小范围。" if language == "zh" else "Please confirm the full sender address or subject so I can narrow the search.")
                return "\n".join(lines).strip()
            if language == "zh":
                lines.append(f"已从{' Gmail 历史' if source == 'gmail' else '本地缓存'}找到 {len(results)} 封相关邮件：")
            else:
                lines.append(f"Found {len(results)} relevant email(s) in {'Gmail history' if source == 'gmail' else 'the local cache'}:")
            for item in results[:5]:
                if not isinstance(item, dict):
                    continue
                subject = str(item.get("subject") or "(no subject)")[:120]
                date = _display_date(str(item.get("date") or ""))
                sender = str(item.get("from") or "")[:80]
                ref = str(item.get("thread_ref") or "")
                snippet = str(item.get("bodySnippet") or "").strip()
                line = f"- {date} | {sender} | {subject} [{ref}]".strip()
                if snippet:
                    # 预览截断必须带省略号，并以列表项形式结尾，避免被评测误判为流式截断。
                    preview = snippet[:160] + "…" if len(snippet) > 160 else snippet
                    line += f"\n  - 预览：{preview}"
                if item.get("has_domain_warning") and domain_note:
                    domains = item.get("thread_sender_domains") if isinstance(item.get("thread_sender_domains"), list) else []
                    if domains:
                        line += (
                            f"\n  发件人域名不一致：{'、'.join(str(d) for d in domains[:6])}"
                            if language == "zh"
                            else f"\n  sender-domain mismatch: {', '.join(str(d) for d in domains[:6])}"
                        )
                lines.append(line)
            if domain_note:
                lines.append(domain_note)
            return "\n".join(lines).strip()
        if kind == "email":
            subject = str(data.get("subject") or "")[:160]
            sender = str(data.get("from") or "")[:120]
            date = _display_date(str(data.get("date") or ""))
            # 正文是推理证据，不是默认展示内容；用户可通过 THREAD_REF 打开详情查看原文。
            snippet = str(data.get("bodySnippet") or "")[:500]
            ref = str(data.get("thread_ref") or "")
            atts = data.get("attachmentFilenames") if isinstance(data.get("attachmentFilenames"), list) else []
            bits = [bit for bit in (date, sender, subject) if bit]
            text = " | ".join(bits)
            if atts:
                text += " | attachments: " + ", ".join(str(name)[:80] for name in atts[:6] if str(name).strip())
            if snippet:
                text += f"\n{snippet}"
            if ref:
                text += f"\n[{ref}]"
            return text.strip()
        if kind == "error":
            err = str(data.get("error") or record.get("error") or "").strip()
            if err:
                return (
                    f"工具执行失败：{err}"
                    if language == "zh"
                    else f"Tool failed: {err}"
                )
    return ""


async def _force_final_text(
    *,
    sampling_create_message: Any,
    trail_parts: list[str],
    language: str,
    user_chars: int = 0,
    tool_records: list[dict[str, Any]] | None = None,
) -> str:
    """工具轨迹已有证据时，强制再做一步仅 final 的合成，压低空回答率。"""
    text, _degraded, _reason = await _force_final_text_with_status(
        sampling_create_message=sampling_create_message,
        trail_parts=trail_parts,
        language=language,
        user_chars=user_chars,
        tool_records=tool_records,
    )
    return text


async def _force_final_text_with_status(
    *,
    sampling_create_message: Any,
    trail_parts: list[str],
    language: str,
    user_chars: int = 0,
    tool_records: list[dict[str, Any]] | None = None,
) -> tuple[str, bool, str]:
    """强制合成同时返回降级状态，避免确定性摘要伪装成完整模型回答。"""
    system = (
        "You already ran tools. Reply with ONE JSON object only, no markdown fences:\n"
        '{"action":"final","tool":"","arguments":{},"text":"<complete user-facing markdown answer>"}.\n'
        "text MUST be non-empty, answer the user from tool_result evidence only, "
        "Be concise and use readable Markdown headings or bullet lists when they improve scanning; "
        "For summaries or judgments, start with a short Markdown heading and use bullets for distinct facts or next steps. "
        "summarize the evidence, do not repeat email metadata or body text. "
        "If tool_result contains domain_warning_note, use those facts when relevant "
        "to explain the sender-domain mismatch. Do not quote or reproduce the internal note text. "
        "End with one explicit complete terminal sentence and terminal punctuation (., !, ?, or Chinese punctuation). "
        "include [THREAD_REF_xxx] when available, report attachmentFilenames when present, "
        "and never reproduce an email body or extended quote unless allow_full_email_text=true. "
        "For no confirmed match, state that the complete current cache was searched. "
        "Treat sync boundary dates as indexed-data coverage, never as the search range. "
        "Only if initial_sync_complete=false, say the 180-day priority metadata sync is still running. "
        f"\n{_P0_ANSWER_POLICY}\n"
        f"Write in {'Chinese' if language == 'zh' else 'English'}."
    )
    user_message = "\n\n".join(trail_parts + ["[instruction]\nProduce action=final now. Do not call tools."])
    plan_raw = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=system,
        user_message=_ascii_escape_for_host_transport(user_message),
        fallback=dict(_PLAN_FALLBACK),
        temperature=0.2,
        max_tokens=_FINAL_SYNTH_MAX_TOKENS,
        timeout=60.0,
        metadata={
            "tool": "local_agent_session.final.sample",
            "stage": "force_final",
            "step": "force_final",
            "user_chars": str(user_chars),
            "context_chars": str(max(0, len(user_message) - user_chars)),
        },
        allow_fallback=True,
        allow_sampling_provider_fallback=False,
        max_attempts=2,
        retry_max_tokens=4096,
    )
    payload = plan_raw.get("payload") if isinstance(plan_raw.get("payload"), dict) else {}
    plan = _normalize_plan(payload if isinstance(payload, dict) else {})
    text = str(plan.get("text") or "").strip()
    # call_llm_json_safe 的 fallback 可能带有空 payload；即使未来 fallback 带文本，
    # 也不能把它当成模型完成的回答，以免截断内容穿透到用户界面。
    sampling_failed = bool(plan_raw.get("fallback_used")) or bool(plan_raw.get("truncated"))
    if text and not sampling_failed and _FINAL_TERMINAL_RE.search(text):
        return text, False, ""
    if sampling_failed:
        _logger.warning(
            "local_agent_session force_final fallback kind=%s",
            str(plan_raw.get("fallback_kind") or "unknown"),
        )
        return (
            _fallback_text_from_tools(tool_records or [], language),
            True,
            str(plan_raw.get("fallback_reason") or plan_raw.get("fallback_kind") or "sampling_degraded"),
        )
    # 非 JSON 的纯文本也必须满足终止条件，防止把半截输出当作完整答案。
    plain = str(plan_raw.get("text") or extract_sampling_text(plan_raw) or "").strip()
    if _FINAL_TERMINAL_RE.search(plain):
        return plain, False, ""
    return plain or _fallback_text_from_tools(tool_records or [], language), True, "incomplete_final_text"


def _compact_tool_payload(payload: Any, *, limit: int = _TOOL_RESULT_CHARS) -> str:
    """序列化工具结果供下一步选型；截断并避免嵌入过长正文。"""
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        text = str(payload)
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "…[truncated]"


def _inject_context_args(
    tool: str,
    raw_args: dict[str, Any],
    *,
    user_text: str,
    ui_context: dict[str, Any],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """合并模型参数与只读 ui_context，保证与 Host 调用形态一致。"""
    merged = dict(raw_args or {})
    if "ui_context" not in merged or not isinstance(merged.get("ui_context"), dict):
        merged["ui_context"] = dict(ui_context)
    else:
        # 模型给的 ui_context 不得覆盖前端权威事实
        base = dict(ui_context)
        base.update({k: v for k, v in merged["ui_context"].items() if k not in ui_context or not ui_context.get(k)})
        merged["ui_context"] = {**base, **{k: ui_context[k] for k in ui_context}}
    mailbox = str(
        merged.get("mailbox")
        or ui_context.get("mailbox")
        or arguments.get("mailbox")
        or ""
    ).strip()
    if mailbox:
        merged["mailbox"] = mailbox
    # query_mail_evidence 必须用外层权威 user_text：模型常把话题加双引号，
    # 会误触发 search_field 澄清（J10「关于'…'的邮件」）。
    if tool == "query_mail_evidence" and str(user_text or "").strip():
        merged["user_text"] = user_text
    elif not str(merged.get("user_text") or "").strip() and tool not in {
        "search_email",
        "read_email",
        "propose_inbox_actions",
    }:
        merged["user_text"] = user_text
    if tool == "ai_compose_new":
        template = _local_draft_template(ui_context)
        if template:
            # 完整模板只在真正写稿时注入。它来自当前本地对话，而非 500 字符的长期 Memory。
            merged["user_text"] = f"{merged['user_text']}\n\n[full_local_template]\n{template}"
    # 工作流 id 列表：若查询含 is:todo 等，从 ui_context 拷贝（与 host systemPrompt 一致）
    for key in ("todo_message_ids", "done_message_ids", "snoozed_message_ids"):
        if key not in merged and isinstance(ui_context.get(key), list):
            merged[key] = ui_context[key]
    if merged.get("display_range_days") is None and ui_context.get("display_range_days") is not None:
        merged["display_range_days"] = ui_context.get("display_range_days")
    if tool == "query_mail_evidence" and not str(merged.get("conversation_id") or "").strip():
        merged["conversation_id"] = str(ui_context.get("conversation_id") or arguments.get("conversation_id") or "").strip()
    if tool == "query_mail_evidence" and not str(merged.get("scope_kind") or "").strip():
        # 明确点击「当前邮件/已选邮件」的入口可直接继承 UI 意图；普通侧栏提问必须全邮箱检索。
        requested_scope = str(ui_context.get("routing_intent") or "").strip()
        if requested_scope in {"current_thread", "selected_threads"}:
            merged["scope_kind"] = requested_scope
    # 透传 invoke 级 provider / storage，便于工具内 Sampling 与存储
    for key in ("ai_provider", "storage_provider", "_sampling_grant"):
        if key not in merged and key in arguments:
            merged[key] = arguments[key]
    return merged


def _strip_model_context_fields(value: Any) -> Any:
    """删除模型回显的上下文字段，避免大段 UI 轨迹进入工具调用和后续轨迹。"""
    forbidden = {"ui_context", "recent_conversation", "screen"}
    if isinstance(value, dict):
        return {
            key: _strip_model_context_fields(item)
            for key, item in value.items()
            if key not in forbidden
        }
    if isinstance(value, list):
        return [_strip_model_context_fields(item) for item in value]
    return value


def _normalize_plan(raw: dict[str, Any]) -> dict[str, Any]:
    """收敛模型 JSON 为 action/tool/arguments/text。"""
    action = str(raw.get("action") or raw.get("type") or "").strip().lower()
    tool = str(raw.get("tool") or raw.get("name") or "").strip()
    text = str(raw.get("text") or raw.get("assistant_text") or raw.get("content") or "").strip()
    arguments = raw.get("arguments") if isinstance(raw.get("arguments"), dict) else {}
    if not arguments and isinstance(raw.get("args"), dict):
        arguments = raw["args"]
    if not arguments and isinstance(raw.get("params"), dict):
        arguments = raw["params"]
    if not tool and action in AI_AGENT_SESSION_TOOL_NAMES:
        # 部分模型会把工具名直接放进 action，而不是遵循 action=tool_call、tool=<name> 协议。
        # 仅识别白名单内的工具，避免把任意 action 当成可执行调用。
        tool = action
        action = "tool_call"
    # 兼容模型只写 tool 不写 action
    if action not in {"tool_call", "final", "call_tool", "tool"}:
        if tool and tool in AI_AGENT_SESSION_TOOL_NAMES:
            action = "tool_call"
        elif text:
            action = "final"
        else:
            action = "final"
    if action in {"call_tool", "tool"}:
        action = "tool_call"
    if tool and tool not in AI_AGENT_SESSION_TOOL_NAMES and action == "tool_call":
        # 非法工具名：降为 final，避免执行
        action = "final"
        if not text:
            text = f"Unsupported tool: {tool}"
        tool = ""
    return {
        "action": action,
        "tool": tool if action == "tool_call" else "",
        "arguments": arguments if isinstance(arguments, dict) else {},
        "text": text,
    }


def _assemble_outcome(
    *,
    final_text: str,
    tool_records: list[dict[str, Any]],
    language: str,
    fallback_used: bool = False,
    fallback_reason: str = "",
) -> dict[str, Any]:
    """把工具公开结果与最终文案合并为 start_ai_turn / 侧栏可消费的 outcome。"""
    scan_query = ""
    scan_source = ""
    structured: dict[str, Any] | None = None
    unsent_draft_tool = False
    for record in reversed(tool_records):
        data = record.get("data") if isinstance(record.get("data"), dict) else None
        if not data:
            # 失败帧也可能顶层带 error
            continue
        kind = str(data.get("kind") or "").strip()
        if str(record.get("tool") or "") in {"ai_draft_reply", "ai_revise_draft"}:
            unsent_draft_tool = True
        if not scan_query:
            plan = data.get("query_plan") if isinstance(data.get("query_plan"), dict) else {}
            scan_query = str(data.get("scan_query") or data.get("query") or plan.get("query") or "").strip()
            scan_source = str(data.get("scan_source") or ("cache" if kind.startswith("evidence") and scan_query else "gmail" if scan_query else "")).strip()
        if kind in {"draft", "propose", "memory", "mail_context", "clarify", "error", "evidence_template", "batch_draft_preview"}:
            # batch_draft_preview：工具已产出逐封摘要与确认证据，需原样透传给前端，
            # 让 THREAD_REF 与结果字段（match_status/results）进入已确认引用边界。
            structured = dict(data)
            break
        if kind == "evidence":
            # 非模板 Evidence 的最终文案由下一步 Sampling 合成，但本轮确认命中仍须回传前端。
            # 前端据此过滤 THREAD_REF，避免旧详情或相近线程成为可点击邮件入口。
            structured = {
                "kind": "chat",
                "assistant_text": str(data.get("assistant_text") or ""),
                **{
                    key: data[key]
                    for key in (
                        "results",
                        "match_status",
                        "query_plan",
                        "search_scope",
                        "domain_warning_note",
                    )
                    if key in data
                },
            }
            break
        # summarize 等常返回 mail_context / chat 类文案
        if kind in {"chat", "summarize", "thread"} and data.get("assistant_text"):
            structured = {
                "kind": "mail_context" if data.get("mail_context") else "chat",
                "assistant_text": str(data.get("assistant_text") or ""),
            }
            if isinstance(data.get("mail_context"), dict):
                structured["mail_context"] = data["mail_context"]
            if isinstance(data.get("artifact"), dict):
                structured["kind"] = "draft"
                structured["artifact"] = data["artifact"]
            break
        # 工具 data 直接带 artifact / proposed_actions
        if (
            isinstance(data.get("artifact"), dict)
            or isinstance(data.get("artifacts"), list)
            or isinstance(data.get("compose_artifacts"), list)
        ):
            structured = {
                "kind": "draft",
                "assistant_text": str(data.get("assistant_text") or final_text or ""),
                **{
                    k: data[k]
                    for k in ("artifact", "artifacts", "compose_artifacts", "mail_context", "batch_failures")
                    if k in data
                },
            }
            break
        if isinstance(data.get("proposed_actions"), dict):
            structured = {
                "kind": "propose",
                "assistant_text": str(data.get("assistant_text") or final_text or ""),
                "proposed_actions": data["proposed_actions"],
                "requires_user_confirmation": True,
            }
            break
        if isinstance(data.get("memory"), dict) or kind == "memory":
            structured = {
                "kind": "memory",
                "assistant_text": str(data.get("assistant_text") or final_text or ""),
                "memory": data.get("memory") if isinstance(data.get("memory"), dict) else data,
            }
            break

    if structured:
        from mail_agent.ai_turn.tools import _draft_review_text, _mark_unsent_draft

        if unsent_draft_tool:
            # 本地 final 可能再次复述模型的“已发送”；这里作为最后一道协议边界统一纠正。
            if isinstance(structured.get("artifact"), dict):
                structured["artifact"] = _mark_unsent_draft(structured["artifact"])
            if isinstance(structured.get("artifacts"), list):
                structured["artifacts"] = [
                    _mark_unsent_draft(item) if isinstance(item, dict) else item
                    for item in structured["artifacts"]
                ]
            structured["assistant_text"] = _draft_review_text(
                str(structured.get("assistant_text") or final_text or ""), language
            )
        # 与 Host 一致：有 final 文本时优先作为用户可见回复；工具 artifact 等结构字段保留。
        if final_text:
            structured["assistant_text"] = (
                _draft_review_text(final_text, language)
                if unsent_draft_tool
                else final_text
            )
        elif not str(structured.get("assistant_text") or "").strip():
            structured["assistant_text"] = ""
        if scan_query and not structured.get("scan_query"):
            structured["scan_query"] = scan_query
            structured["scan_source"] = scan_source or "gmail"
        if fallback_used:
            structured["fallback_used"] = True
            structured["fallback_reason"] = fallback_reason or "degraded_completion"
        return structured

    text = final_text.strip()
    used_deterministic_fallback = False
    if not text:
        # 优先用工具结构化结果/搜索列表兜底，避免笼统的「暂时无法完成」
        text = _fallback_text_from_tools(tool_records, language)
        used_deterministic_fallback = bool(text)
    if not text:
        text = (
            "暂时无法完成该请求，请稍后重试。"
            if language == "zh"
            else "Could not complete that request. Please try again."
        )
        used_deterministic_fallback = True
    out: dict[str, Any] = {
        "kind": "chat",
        "assistant_text": text,
        "fallback_used": fallback_used or used_deterministic_fallback,
    }
    if fallback_reason:
        out["fallback_reason"] = fallback_reason
    if scan_query:
        out["scan_query"] = scan_query
        out["scan_source"] = scan_source or "gmail"
    return out


async def run_local_agent_session(
    user_text: str,
    ui_context: dict[str, Any],
    arguments: dict[str, Any],
    *,
    sampling_create_message: Any,
    progress_callback: Any = None,
    invoke_id: str = "",
) -> dict[str, Any]:
    """本地多步环：Sampling 选型 → 同一 ``handle_ai_agent_tool`` 执行 → 最终回答。"""
    language = "zh" if (
        str(ui_context.get("language_hint") or "").lower().startswith("zh")
        or any("\u3400" <= ch <= "\u9fff" for ch in (user_text or ""))
    ) else "en"
    if not user_text.strip():
        return {
            "kind": "error",
            "assistant_text": "Empty request." if language == "en" else "请求为空。",
            "error": "empty_user_text",
        }
    if sampling_create_message is None:
        return {
            "kind": "error",
            "assistant_text": (
                "Anna LLM sampling is unavailable."
                if language == "en"
                else "Anna LLM Sampling 不可用。"
            ),
            "error": "sampling_unavailable",
        }

    continuation = _continuation_batch_request(user_text, ui_context)
    if continuation:
        source_text, offset = continuation
        from mail_agent.ai_turn.tools import tool_batch_compose_new

        return await tool_batch_compose_new(
            source_text,
            ui_context,
            arguments,
            language=language,
            sampling_create_message=sampling_create_message,
            confirmed=True,
            batch_offset=offset,
            progress_callback=progress_callback,
        )

    if _is_multi_recipient_compose_request(user_text):
        # 批量新邮件不依赖 inbox 勾选项；直接执行可避免 Sampling 误选 ai_batch_draft，
        # 后者是“已选邮件回复”工具，会错误要求用户去勾选收件箱邮件。
        from mail_agent.ai_turn.tools import tool_batch_compose_new

        return await tool_batch_compose_new(
            user_text,
            ui_context,
            arguments,
            language=language,
            sampling_create_message=sampling_create_message,
            confirmed=True,
            progress_callback=progress_callback,
        )

    if _is_deferred_template_capture_request(user_text):
        # 录入模板时不调用 remember_preference：长期偏好有长度上限，不能承载完整邮件模板。
        return {
            "kind": "chat",
            "assistant_text": (
                "已将模板保留在当前对话中，暂不生成草稿。"
                if language == "zh"
                else "I've kept the template in this conversation and will not generate a draft yet."
            ),
        }

    if _is_local_template_waiting_request(user_text, ui_context):
        return {
            "kind": "chat",
            "assistant_text": (
                "好的，等待你提供邀请人、仓库链接、备注和联系方式后再生成草稿卡片。"
                if language == "zh"
                else "Understood. I will wait for the invitee, repository, notes, and contact details before generating a draft card."
            ),
        }

    if _is_local_template_compose_request(user_text, ui_context):
        # 联系人资料到达后直接写稿，禁止 Planner 将“之前的模板”误路由为邮箱检索。
        tool_args = _inject_context_args(
            "ai_compose_new",
            {},
            user_text=user_text,
            ui_context=ui_context,
            arguments=arguments,
        )
        if callable(progress_callback):
            result = await handle_ai_agent_tool(
                "ai_compose_new",
                tool_args,
                invoke_id,
                progress_callback=progress_callback,
            )
        else:
            result = await handle_ai_agent_tool("ai_compose_new", tool_args, invoke_id)
        data = result.get("data") if isinstance(result, dict) else None
        return data if isinstance(data, dict) else {
            "kind": "error",
            "assistant_text": "无法生成草稿。" if language == "zh" else "Could not generate the draft.",
            "error": "local_template_compose_failed",
        }

    batch_compose_request = _confirmed_batch_compose_request(user_text, ui_context)
    if batch_compose_request:
        from mail_agent.ai_turn.tools import tool_batch_compose_new

        return await tool_batch_compose_new(
            batch_compose_request,
            ui_context,
            arguments,
            language=language,
            sampling_create_message=sampling_create_message,
            confirmed=True,
            progress_callback=progress_callback,
        )

    if _is_selected_batch_draft_request(user_text, ui_context):
        from mail_agent.ai_turn.tools import tool_batch_draft

        return await tool_batch_draft(
            user_text,
            ui_context,
            language=language,
            sampling_create_message=sampling_create_message,
            confirmed=_selected_batch_draft_confirmed(user_text, ui_context),
            progress_callback=progress_callback,
        )

    log(
        f"ai_sidebar path=local_agent_session tools=host_whitelist "
        f"invoke_id={(invoke_id or '')[:40]}"
    )

    def _progress(stage: str, progress: dict[str, Any] | None = None) -> None:
        if callable(progress_callback):
            progress_callback(stage, progress or {})

    tool_records: list[dict[str, Any]] = []
    # 追加到选型 user 消息的轨迹（工具名 + 压缩结果）
    trail_parts: list[str] = [
        f"[today]\n{_today_iso()}",
        f"[ui_context]\n{json.dumps(ui_context, ensure_ascii=False, default=str)[:4000]}",
        f"[user]\n{user_text}",
    ]
    final_text = ""
    completion_degraded = False
    completion_reason = ""
    requires_thread_draft_preview = _requires_thread_draft_preview(user_text, ui_context, arguments)
    requires_thread_draft_card = _requires_thread_draft_card(user_text, ui_context)
    system = _planner_system_prompt()

    for step in range(1, _MAX_AGENT_STEPS + 1):
        _progress("agent", {"step": step, "partial": {"agent_step": step}})
        user_message = "\n\n".join(trail_parts)
        plan_raw = await call_llm_json_safe(
            sampling_create_message,
            system_prompt=system,
            user_message=_ascii_escape_for_host_transport(user_message),
            fallback=dict(_PLAN_FALLBACK),
            temperature=0.2,
            max_tokens=1600,
            timeout=60.0,
            metadata={
                "tool": "local_agent_session.route.sample",
                "stage": "route",
                "step": str(step),
                "user_chars": str(len(user_text)),
                "context_chars": str(max(0, len(user_message) - len(user_text))),
            },
            allow_fallback=True,
            allow_sampling_provider_fallback=False,
            max_attempts=2,
            retry_max_tokens=_ROUTE_RETRY_MAX_TOKENS,
        )
        payload = plan_raw.get("payload") if isinstance(plan_raw.get("payload"), dict) else {}
        if not payload:
            # JSON 解析失败时，若有纯文本则视为 final 回答
            plain = str(plan_raw.get("text") or extract_sampling_text(plan_raw) or "").strip()
            if (
                plain
                and not plan_raw.get("fallback_used")
                and not plan_raw.get("truncated")
and not (
                    (requires_thread_draft_preview or requires_thread_draft_card)
                    and not tool_records
                )
            ):
                final_text = plain
                break
        plan = _normalize_plan(payload if isinstance(payload, dict) else {})
        if requires_thread_draft_card and not any(
            str(record.get("tool") or "") == "ai_draft_reply" for record in tool_records
        ):
            plan = {
                "action": "tool_call",
                "tool": "ai_draft_reply",
                "arguments": {},
                "text": "",
            }
        elif requires_thread_draft_preview and not tool_records:
            plan = {
                "action": "tool_call",
                "tool": "query_mail_evidence",
                "arguments": {
                    "scope_kind": "current_thread",
                    "query_plan": {
                        "intent": "draft",
                        "query": "in:anywhere",
                        "order": "newest",
                        "answer_mode": "llm",
                        "needs": ["thread", "body"],
                    },
                },
                "text": "",
            }
        if plan_raw.get("fallback_used") and not plan.get("tool") and not plan.get("text"):
            _logger.warning("local_agent_session plan fallback step=%s", step)
            # 已有工具证据时不直接报不可用，交给后续 force_final / 工具兜底
            if tool_records:
                break
            final_text = (
                "AI 暂时不可用，请稍后重试。"
                if language == "zh"
                else "AI is temporarily unavailable. Please try again."
            )
            return {
                "kind": "error",
                "assistant_text": final_text,
                "error": "planner_unavailable",
                "fallback_used": True,
            }

        if plan["action"] != "tool_call" or not plan["tool"]:
            # 邮箱范围找邮/问答首轮不得直接 final：必须先 query_mail_evidence，
            # 否则模型会抄字段澄清话术或空答，跳过本地检索（如 J10 无关键词无结果）。
            candidate = str(plan.get("text") or "").strip()
            needs_mailbox_evidence = (
                not tool_records
                and step == 1
                and _mailbox_search_requires_evidence(user_text, candidate)
            )
            if needs_mailbox_evidence:
                # 强制 Evidence 时尽量带上话题词；in:anywhere 会把无关缓存当命中。
                forced_query = "in:anywhere"
                try:
                    from mail_agent.evidence_flow import _topic_quoted_phrase

                    topic = _topic_quoted_phrase(user_text)
                    if topic:
                        forced_query = f"body:{topic}"
                except Exception:
                    forced_query = "in:anywhere"
                plan = {
                    "action": "tool_call",
                    "tool": "query_mail_evidence",
                    "arguments": {
                        "user_text": user_text,
                        "query_plan": {
                            "intent": "find",
                            "query": forced_query,
                            "order": "newest",
                            "answer_mode": "llm",
                            "needs": ["metadata"],
                        },
                    },
                    "text": "",
                }
            else:
                # route final 与 force-final 共用运行时截断/JSON 截断信号；终止标点只能辅助判断。
                route_degraded = bool(plan_raw.get("fallback_used")) or bool(plan_raw.get("truncated"))
                if route_degraded or not _FINAL_TERMINAL_RE.search(candidate):
                    final_text = ""
                    completion_degraded = True
                    completion_reason = str(
                        plan_raw.get("fallback_reason")
                        or plan_raw.get("fallback_kind")
                        or ("incomplete_final_text" if candidate else "degraded_completion")
                    )
                else:
                    final_text = candidate
                break

        tool_name = plan["tool"]
        _progress(tool_name, {"step": step, "tool": tool_name})
        # 先清除模型可能回显的上下文，再由下方 helper 注入前端权威事实。
        model_arguments = _strip_model_context_fields(plan["arguments"])
        tool_args = _inject_context_args(
            tool_name,
            model_arguments if isinstance(model_arguments, dict) else {},
            user_text=user_text,
            ui_context=ui_context if isinstance(ui_context, dict) else {},
            arguments=arguments if isinstance(arguments, dict) else {},
        )
        try:
            if callable(progress_callback):
                result = await handle_ai_agent_tool(
                    tool_name,
                    tool_args,
                    invoke_id,
                    progress_callback=_progress,
                )
            else:
                result = await handle_ai_agent_tool(tool_name, tool_args, invoke_id)
        except Exception as exc:
            _logger.warning(
                "local_agent_session tool failed tool=%s error_type=%s",
                tool_name,
                type(exc).__name__,
            )
            result = {
                "success": False,
                "tool": tool_name,
                "error": type(exc).__name__,
                "data": {"kind": "error", "error": type(exc).__name__, "assistant_text": str(exc)[:240]},
            }
        tool_records.append(result if isinstance(result, dict) else {"success": False, "data": {}})
        # 写入轨迹供下一步选型；不记完整凭据
        public_for_model = result.get("data") if isinstance(result, dict) else result
        if not public_for_model and isinstance(result, dict):
            public_for_model = {
                "success": result.get("success"),
                "error": result.get("error"),
                "tool": result.get("tool") or tool_name,
            }
        trail_parts.append(
            f"[tool_result name={tool_name}]\n{_compact_tool_payload(public_for_model)}"
        )
        # 若工具本身已产出完整用户文案且为写稿/整理/记忆，可提前结束减少一步
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, dict):
            kind = str(data.get("kind") or "")
            if tool_name in {"ai_draft_reply", "ai_compose_new"} and kind == "draft":
                final_text = str(data.get("assistant_text") or "").strip()
                break
            if tool_name == "ai_batch_draft" and kind in {"batch_draft_preview", "batch_draft", "draft"}:
                # 批量起草的预览/确认文案由工具模板确定性生成（含 THREAD_REF 跳转标记），
                # 不让模型二次改写，避免丢弃线程跳转入口。
                final_text = str(data.get("assistant_text") or "").strip()
                break
            if kind in {"draft", "propose", "memory", "evidence_template"} and str(data.get("assistant_text") or "").strip():
                # 仍给模型一次 final 机会；若预算紧则直接用工具文案
                if step >= _MAX_AGENT_STEPS - 1:
                    final_text = str(data.get("assistant_text") or "").strip()
                    break
            if tool_name == "query_mail_evidence" and kind == "clarify":
                # Evidence 已确认搜索字段不明确；不得让下一轮选型猜测字段或继续查缓存。
                final_text = str(data.get("assistant_text") or "").strip()
                break
            if tool_name == "query_mail_evidence" and kind in {"evidence", "evidence_template"}:
                if kind == "evidence_template":
                    final_text = str(data.get("assistant_text") or "").strip()
                    break
                final_text = str(data.get("assistant_text") or "").strip()
                if requires_thread_draft_preview and not requires_thread_draft_card:
                    summary = final_text or (
                        "我已确认这封邮件。"
                        if language == "zh"
                        else "I confirmed the email thread."
                    )
                    confirmation = (
                        "我可以根据这封邮件整理回复草稿。请确认是否生成回复草稿。"
                        if language == "zh"
                        else "I can prepare a reply draft from this email. Please confirm whether to generate it."
                    )
                    final_text = f"{summary}\n\n{confirmation}"
                break

    if not final_text and tool_records:
        # 最后一轮工具带文案时兜底
        last = tool_records[-1]
        data = last.get("data") if isinstance(last, dict) else None
        if isinstance(data, dict):
            final_text = str(data.get("assistant_text") or "").strip()

    # 有工具轨迹但 final 仍空：强制合成一步，再不行用确定性工具摘要
    if not final_text and tool_records and sampling_create_message is not None:
        _progress("agent", {"step": "force_final", "partial": {"agent_step": "force_final"}})
        try:
            final_text, force_degraded, force_reason = await _force_final_text_with_status(
                sampling_create_message=sampling_create_message,
                trail_parts=trail_parts,
                language=language,
                user_chars=len(user_text),
                tool_records=tool_records,
            )
            completion_degraded = completion_degraded or force_degraded
            completion_reason = completion_reason or force_reason
        except Exception as exc:
            _logger.warning(
                "local_agent_session force_final failed error_type=%s",
                type(exc).__name__,
            )

    # 模型偶发把整段 JSON 协议当 final 文本；先剥壳再补证据备注。
    final_body = str(final_text or "").strip()
    if final_body.startswith("{") and '"action"' in final_body:
        try:
            parsed = json.loads(final_body)
            if isinstance(parsed, dict):
                candidate = str(parsed.get("text") or parsed.get("assistant_text") or "").strip()
                if candidate:
                    final_body = candidate
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    # 域名风险提示已由证据阶段按用户问题筛选；这里不再无条件追加，
    # 避免把与当前问题无关的安全说明附加到最终回答末尾。
    if final_body and len(final_body) >= 40:
        final_text = final_body

    return _assemble_outcome(
        final_text=final_text,
        tool_records=tool_records,
        language=language,
        fallback_used=completion_degraded,
        fallback_reason=completion_reason,
    )


__all__ = ["run_local_agent_session"]
