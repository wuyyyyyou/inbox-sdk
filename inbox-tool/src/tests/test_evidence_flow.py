"""P3 Scope/Plan/Evidence 复合只读工具测试。"""

from __future__ import annotations

import asyncio
import re
from unittest.mock import patch

from mail_agent.evidence_flow import query_mail_evidence


def test_query_mail_evidence_uses_one_plan_and_cache_evidence() -> None:
    """QueryPlan 只生成一次，执行器只消费确定性缓存结果。"""
    calls: list[dict] = []

    async def fake_plan(*_args, **_kwargs):
        calls.append(_kwargs)
        return {"payload": {"intent": "count", "query": "from:alice@example.com", "order": "newest", "answer_mode": "template"}}

    def fake_search(arguments, _context):
        assert arguments["about"] == "from:alice@example.com"
        return {
            "results": [{"thread_ref": "THREAD_REF_a", "subject": "Status"}],
            "exact_count": 1,
            "sync_boundary": {"backfill_complete": True},
            "coverage_note": "",
        }

    with patch("mail_agent.evidence_flow.call_llm_json_safe", fake_plan), patch(
        "anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search,
    ):
        result = asyncio.run(query_mail_evidence(
            "按发件人 Alice 查询有几封邮件？",
            {"mailbox": "mail@example.com"},
            sampling_create_message=object(),
            conversation_id="",
        ))

    assert len(calls) == 1
    assert result["kind"] == "evidence_template"
    assert result["query_plan"]["intent"] == "count"
    assert result["active_scope"]["kind"] == "all_indexed"
    assert result["search_scope"] == "all_indexed_cache"
    assert "1 封" in result["assistant_text"]
    print("[PASS] test_query_mail_evidence_uses_one_plan_and_cache_evidence")


def test_query_mail_evidence_reuses_local_route_plan() -> None:
    """local route 给出 QueryPlan 时不得再次调用 Sampling。"""
    def fake_search(arguments, _context):
        assert arguments["about"] == "from:alice@example.com"
        return {"results": [], "sync_boundary": {}, "coverage_note": ""}

    with patch("mail_agent.evidence_flow.call_llm_json_safe") as sampling, patch(
        "anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search,
    ):
        result = asyncio.run(query_mail_evidence(
            "找发件人是 Alice 的邮件",
            {"mailbox": "mail@example.com"},
            sampling_create_message=object(),
            conversation_id="",
            query_plan={
                "intent": "find",
                "query": "from:alice@example.com",
                "order": "newest",
                "answer_mode": "llm",
                "needs": ["metadata"],
            },
        ))

    sampling.assert_not_called()
    assert result["query_plan"]["query"] == "from:alice@example.com"
    print("[PASS] test_query_mail_evidence_reuses_local_route_plan")


def test_query_mail_evidence_clarifies_ambiguous_quoted_content_before_search() -> None:
    """引号短语未说明字段时，必须先返回选项且不能读取缓存。"""
    searches: list[str] = []

    def fake_search(arguments, _context):
        searches.append(arguments["about"])
        return {"results": [], "sync_boundary": {}, "coverage_note": ""}

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        result = asyncio.run(query_mail_evidence(
            '"Your best product launch ever!" 这条邮件链到底在聊什么？',
            {"mailbox": "mail@example.com"},
            sampling_create_message=object(),
            conversation_id="",
            query_plan={"intent": "summarize", "query": "subject:Your best product launch ever!", "needs": ["body"]},
        ))

    assert searches == []
    assert result["kind"] == "clarify"
    assert result["clarification"]["kind"] == "search_field"
    assert [item["id"] for item in result["clarification"]["actions"]] == [
        "search_subject", "search_body", "search_participants", "search_date",
    ]
    print("[PASS] test_query_mail_evidence_clarifies_ambiguous_quoted_content_before_search")


def test_query_mail_evidence_searches_unquoted_topic_without_field_clarify() -> None:
    """无引号主题检索直接查缓存；不得先弹出字段澄清。"""
    searches: list[str] = []

    def fake_search(arguments, _context):
        searches.append(arguments["about"])
        return {"results": [], "sync_boundary": {}, "coverage_note": ""}

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        result = asyncio.run(query_mail_evidence(
            "找一下关于'区块链质押收益'的邮件",
            {"mailbox": "mail@example.com"},
            sampling_create_message=object(),
            conversation_id="",
            query_plan={"intent": "find", "query": "区块链质押收益", "needs": ["metadata"]},
        ))

    assert searches, "unquoted topical search must hit cache once"
    assert result.get("kind") != "clarify"
    assert result.get("match_status") == "no_confirmed_match"
    assert "未找到" in str(result.get("assistant_text") or "")
    print("[PASS] test_query_mail_evidence_searches_unquoted_topic_without_field_clarify")


def test_query_mail_evidence_skips_clarify_for_about_double_quoted_topic() -> None:
    """关于\"话题\"的邮件即使模型改成双引号，也直接检索并诚实无命中（J10）。"""
    searches: list[str] = []

    def fake_search(arguments, _context):
        searches.append(arguments["about"])
        return {
            "results": [],
            "sync_boundary": {"initial_sync_complete": True, "cache_total": 50},
        }

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        result = asyncio.run(query_mail_evidence(
            '找一下关于"区块链质押收益"的邮件',
            {"mailbox": "mail@example.com"},
            sampling_create_message=None,
            conversation_id="",
            query_plan={"intent": "find", "query": "in:anywhere", "needs": ["metadata"]},
        ))

    assert searches, "about-quoted topical search must hit cache"
    assert any("区块链质押收益" in q for q in searches)
    assert result.get("kind") != "clarify"
    assert result.get("match_status") == "no_confirmed_match"
    assert "未找到" in str(result.get("assistant_text") or "")
    assert "要按什么条件" not in str(result.get("assistant_text") or "")
    print("[PASS] test_query_mail_evidence_skips_clarify_for_about_double_quoted_topic")


def test_needs_cached_bodies_includes_bill_amount_phrasing() -> None:
    """账单/多少钱类问题必须挂正文，不能只靠 snippet。"""
    from mail_agent.evidence_flow import _needs_cached_bodies

    assert _needs_cached_bodies({"needs": ["metadata"], "intent": "find"}, "帮我找一下 Eleven Labs 最近那笔账单，多少钱")
    assert _needs_cached_bodies({"needs": ["metadata"], "intent": "find"}, "how much is the receipt")
    print("[PASS] test_needs_cached_bodies_includes_bill_amount_phrasing")


def test_content_facts_prefer_body_invoice_and_attachment_sources() -> None:
    """Evidence content_facts 必须分源挂载 body/attachment 发票事实。"""
    from mail_agent.evidence_flow import _content_facts_from_analysis

    facts = _content_facts_from_analysis({
        "body_invoice_facts": [{"label": "total", "value": "$22.00", "source": "body"}],
        "attachment_analysis": [
            {"filename": "Receipt.pdf", "status": "parsed", "facts": [{"label": "total", "value": "22.00"}]},
        ],
    })
    assert facts is not None
    assert facts["body_invoice_facts"][0]["value"] == "$22.00"
    assert facts["attachment_analysis"][0]["filename"] == "Receipt.pdf"
    assert "separate" in str(facts.get("note") or "").lower() or "分" in str(facts.get("note") or "")
    print("[PASS] test_content_facts_prefer_body_invoice_and_attachment_sources")


def test_payment_amount_template_prefers_amount_paid_over_subtotal() -> None:
    """金额事实题必须报实付字段，不能把 Subtotal 当实付。"""
    from mail_agent.evidence_flow import _payment_amount_template

    answer = _payment_amount_template(
        {
            "results": [{
                "thread_ref": "THREAD_REF_receipt",
                "bodyFull": "Subtotal $22.00 Discount -$11.00 Total $11.00 Amount paid $11.00",
            }],
        },
        "这笔账单多少钱？",
        "zh",
    )
    assert "$11.00" in answer
    assert "Subtotal 为 $22.00" in answer
    assert "THREAD_REF_receipt" in answer
    print("[PASS] test_payment_amount_template_prefers_amount_paid_over_subtotal")


def test_query_mail_evidence_reads_named_receipt_beyond_first_three_results() -> None:
    """指定账单排在较后检索位时，仍须取到其正文并返回实付金额。"""
    rows = [
        {
            "message_id": f"m-{index}",
            "thread_ref": f"THREAD_REF_t-{index}",
            "subject": f"Other payment notice {index}",
            "from": "billing@example.com",
        }
        for index in range(1, 7)
    ]
    rows.append({
        "message_id": "eleven-receipt",
        "thread_ref": "THREAD_REF_eleven",
        "subject": "Your receipt from Eleven Labs Inc.",
        "from": "Eleven Labs <billing@elevenlabs.io>",
    })
    read_ids: list[str] = []

    def fake_search(*_args, **_kwargs):
        return {"results": rows, "sync_boundary": {}, "coverage_note": ""}

    def fake_read(_mailbox: str, message_id: str):
        read_ids.append(message_id)
        if message_id == "eleven-receipt":
            return {"body_text": "Subtotal $22.00 Total $11.00 Amount paid $11.00"}
        return {"body_text": "No payment amount in this message."}

    with patch(
        "anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search,
    ), patch("mail_agent.mail_providers.gmail.adapter.read_message", fake_read):
        result = asyncio.run(query_mail_evidence(
            "帮我找一下 Eleven Labs 最近那笔账单，多少钱",
            {"mailbox": "mail@example.com"},
            sampling_create_message=None,
            conversation_id="",
            query_plan={
                "intent": "find",
                "query": "Eleven Labs (invoice OR bill OR payment OR receipt)",
                "order": "newest",
                "answer_mode": "llm",
                "needs": ["metadata", "body"],
            },
        ))

    assert "eleven-receipt" in read_ids
    assert result["kind"] == "evidence_template"
    assert "$11.00" in result["assistant_text"]
    assert "THREAD_REF_eleven" in result["assistant_text"]
    print("[PASS] test_query_mail_evidence_reads_named_receipt_beyond_first_three_results")


def test_query_mail_evidence_reports_sync_progress_from_state_not_cached_email() -> None:
    """180 天同步进度只能看状态机完成标记，不能从已缓存邮件推断。"""
    with patch(
        "mail_agent.mail_providers.gmail.mailbox_sync.get_mailbox_sync_boundary",
        return_value={
            "cache_total": 3,
            "priority_days": 180,
            "initial_sync_complete": False,
            "backfill_complete": False,
        },
    ), patch("mail_agent.evidence_flow.call_llm_json_safe") as sampling, patch(
        "anna_inbox_executa.ai_agent_tools_flow._search_email",
    ) as search:
        result = asyncio.run(query_mail_evidence(
            "180 天优先元数据同步进行到哪里了？",
            {"mailbox": "mail@example.com"},
            sampling_create_message=object(),
            conversation_id="",
        ))

    sampling.assert_not_called()
    search.assert_not_called()
    assert result["kind"] == "evidence_template"
    assert result["match_status"] == "not_applicable"
    assert "仍在进行" in result["assistant_text"]
    assert "3 封邮件" in result["assistant_text"]
    assert "邮件主题" not in result["assistant_text"]
    print("[PASS] test_query_mail_evidence_reports_sync_progress_from_state_not_cached_email")


def test_query_mail_evidence_uses_body_after_user_selects_content_search() -> None:
    """用户明确选择正文后，直接用 body: 查询且不再调用 QueryPlan Sampling。"""
    searches: list[str] = []

    def fake_search(arguments, _context):
        searches.append(arguments["about"])
        return {
            "results": [{"message_id": "m-body", "thread_ref": "THREAD_REF_body", "subject": "Launch recap"}],
            "sync_boundary": {},
            "coverage_note": "",
        }

    with patch("mail_agent.evidence_flow.call_llm_json_safe") as sampling, patch(
        "anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search,
    ):
        result = asyncio.run(query_mail_evidence(
            '"Your best product launch ever!" 这条邮件链到底在聊什么？\n搜索条件：按邮件正文内容搜索。',
            {"mailbox": "mail@example.com", "search_field": "body"},
            sampling_create_message=object(),
            conversation_id="",
            query_plan={"intent": "summarize", "query": "subject:Your best product launch ever!", "needs": ["body"]},
            search_field="body",
        ))

    sampling.assert_not_called()
    assert searches == ["body:Your best product launch ever!"]
    assert result["match_status"] == "confirmed"
    assert result["results"][0]["thread_ref"] == "THREAD_REF_body"
    print("[PASS] test_query_mail_evidence_uses_body_after_user_selects_content_search")


def test_query_mail_evidence_keeps_explicit_quoted_subject_search() -> None:
    """用户明确指定主题时，引号内容不得被改写为正文条件。"""
    searches: list[str] = []

    def fake_search(arguments, _context):
        searches.append(arguments["about"])
        return {
            "results": [{"message_id": "m-subject", "thread_ref": "THREAD_REF_subject", "subject": "Your best product launch ever!"}],
            "sync_boundary": {},
            "coverage_note": "",
        }

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        result = asyncio.run(query_mail_evidence(
            '找主题为 "Your best product launch ever!" 的邮件',
            {"mailbox": "mail@example.com"},
            sampling_create_message=object(),
            conversation_id="",
            query_plan={"intent": "find", "query": "subject:Your best product launch ever!"},
        ))

    assert searches == ["subject:Your best product launch ever!"]
    assert result["match_status"] == "confirmed"
    print("[PASS] test_query_mail_evidence_keeps_explicit_quoted_subject_search")


def test_query_mail_evidence_does_not_inherit_closed_thread_scope() -> None:
    """普通全邮箱问题不能因侧栏保留的旧详情上下文而只查该线程。"""
    searches: list[str] = []

    def fake_search(arguments, _context):
        searches.append(arguments["about"])
        return {
            "results": [{"message_id": "orbit-1", "thread_id": "orbit-thread", "thread_ref": "THREAD_REF_orbit-thread"}],
            "sync_boundary": {},
            "coverage_note": "",
        }

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        result = asyncio.run(query_mail_evidence(
            "我们跟 Orbit 之前是不是已经付了定金，邮件怎么说的？",
            {
                "mailbox": "mail@example.com",
                "current_thread": {"thread_id": "closed-dora-thread", "message_id": "dora-1"},
            },
            sampling_create_message=object(),
            conversation_id="",
            query_plan={"intent": "judge", "query": "from:orbit", "needs": ["body"]},
        ))

    assert searches and searches[0] == "from:orbit"
    assert result["active_scope"]["kind"] == "all_indexed"
    assert result["results"][0]["thread_ref"] == "THREAD_REF_orbit-thread"
    print("[PASS] test_query_mail_evidence_does_not_inherit_closed_thread_scope")


def test_query_mail_evidence_adds_participant_filters_for_relationship_question() -> None:
    """QueryPlan 漏掉人物字段时，关系问题仍应覆盖该联系人的收发邮件。"""
    searches: list[str] = []

    def fake_search(arguments, _context):
        searches.append(arguments["about"])
        return {"results": [], "sync_boundary": {}, "coverage_note": ""}

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        result = asyncio.run(query_mail_evidence(
            "我们跟 Orbit 之前是不是已经付了定金，邮件里怎么说的？",
            {"mailbox": "mail@example.com"},
            sampling_create_message=object(),
            conversation_id="",
            query_plan={"intent": "judge", "query": "deposit", "needs": ["body"]},
        ))

    assert searches[0] == "from:Orbit OR to:Orbit"
    assert result["query_plan"]["query"] == "from:Orbit OR to:Orbit"
    print("[PASS] test_query_mail_evidence_adds_participant_filters_for_relationship_question")


def test_query_mail_evidence_honors_explicit_evaluation_path() -> None:
    """显式 cache 评测只记录本地缓存检索指标，不改变检索路径。"""
    async def fake_plan(*_args, **_kwargs):
        return {"payload": {"intent": "find", "query": "subject:invoice", "order": "newest", "answer_mode": "llm"}}

    def fake_search(arguments, _context):
        assert arguments["about"] == "subject:invoice"
        return {"results": [{"message_id": "m-1", "thread_ref": "THREAD_REF_t1"}], "sync_boundary": {}, "coverage_note": ""}

    with patch("mail_agent.evidence_flow.call_llm_json_safe", fake_plan), patch(
        "anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search,
    ):
        result = asyncio.run(query_mail_evidence(
            "按主题找发票",
            {"mailbox": "mail@example.com", "evaluation_path": "cache"},
            sampling_create_message=object(),
            conversation_id="",
        ))

    assert result["evaluation_path"] == "cache"
    assert result["scan_source"] == "cache"
    assert result["evaluation_metrics"]["gmail_api_calls"] == 0
    print("[PASS] test_query_mail_evidence_honors_explicit_evaluation_path")


def test_query_mail_evidence_resets_scope_after_context_switch() -> None:
    """同一会话切换 UI 范围时，持久 active_scope 必须标记重置。"""
    saved: list[dict] = []

    async def fake_plan(*_args, **_kwargs):
        return {"payload": {"intent": "find", "query": "in:inbox", "order": "newest", "answer_mode": "llm"}}

    async def fake_get(*_args, **_kwargs):
        return {"state": {"active_scope": {"fingerprint": "mail@example.com|current_thread|old"}}, "etag": "etag-old"}

    async def fake_set(_mailbox, _conversation_id, state, **kwargs):
        saved.append({"state": state, **kwargs})
        return {"etag": "etag-new"}

    def fake_search(*_args, **_kwargs):
        return {"results": [], "sync_boundary": {}, "coverage_note": ""}

    with patch("mail_agent.evidence_flow.call_llm_json_safe", fake_plan), patch(
        "mail_agent.evidence_flow.get_conversation_state", fake_get,
    ), patch("mail_agent.evidence_flow.set_conversation_state", fake_set), patch(
        "anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search,
    ):
        result = asyncio.run(query_mail_evidence(
            "请找收件箱邮件",
            {"mailbox": "mail@example.com"},
            sampling_create_message=object(),
            conversation_id="chat-1",
        ))

    assert result["scope_reset"] is True
    assert saved and saved[0]["state"]["scope_reset"] is True
    print("[PASS] test_query_mail_evidence_resets_scope_after_context_switch")


def test_query_mail_evidence_returns_nearby_threads_for_empty_template_search() -> None:
    """严格条件无命中时给诚实未找到文案，并附相近主题（不得当命中）。"""
    searches: list[str] = []

    async def fake_plan(*_args, **_kwargs):
        return {
            "payload": {
                "intent": "find",
                "query": "subject:Re: Collaboration: Meet Orbit from:orbit after:2026-07-13 before:2026-07-15",
                "order": "newest",
                "answer_mode": "template",
            },
        }

    def fake_search(arguments, _context):
        searches.append(arguments["about"])
        if arguments["about"] == "subject:Collaboration":
            return {
                "results": [{"thread_ref": "THREAD_REF_nearby", "subject": "Re: Collaboration: Putting Anna to the test"}],
                "sync_boundary": {},
                "coverage_note": "",
            }
        return {"results": [], "sync_boundary": {}, "coverage_note": ""}

    with patch("mail_agent.evidence_flow.call_llm_json_safe", fake_plan), patch(
        "anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search,
    ):
        result = asyncio.run(query_mail_evidence(
            "找 7 月 14 日 Orbit 的 Collaboration 邮件",
            {"mailbox": "mail@example.com"},
            sampling_create_message=object(),
            conversation_id="",
        ))

    assert "subject:Collaboration" in searches
    assert searches.count("subject:Re: Collaboration: Meet Orbit from:orbit after:2026-07-13 before:2026-07-15") >= 1
    assert result["kind"] == "evidence_template"
    assert result["match_status"] == "no_confirmed_match"
    assert result["nearby_results"][0]["thread_ref"] == "THREAD_REF_nearby"
    assert "未找到" in str(result.get("assistant_text") or "")
    assert "相近" in str(result.get("assistant_text") or "")
    print("[PASS] test_query_mail_evidence_returns_nearby_threads_for_empty_template_search")


def test_query_mail_evidence_includes_cached_body_for_payment_question() -> None:
    """定金/付款判断必须读取命中邮件的缓存正文，不能只交给模型 snippet。"""

    async def fake_plan(*_args, **_kwargs):
        return {"payload": {"intent": "judge", "query": "from:orbit@example.com", "order": "newest", "answer_mode": "llm", "needs": ["body"]}}

    def fake_search(*_args, **_kwargs):
        return {"results": [{"message_id": "m1", "thread_ref": "THREAD_REF_t1", "bodySnippet": "Collaboration update"}], "sync_boundary": {}, "coverage_note": ""}

    def fake_read(_mailbox, message_id):
        assert message_id == "m1"
        return {"body_text": "We will pay the deposit after the contract is signed."}

    with patch("mail_agent.evidence_flow.call_llm_json_safe", fake_plan), patch(
        "anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search,
    ), patch("mail_agent.mail_providers.gmail.adapter.read_message", fake_read):
        result = asyncio.run(query_mail_evidence(
            "Guru 之前定金怎么说的？",
            {"mailbox": "mail@example.com"},
            sampling_create_message=object(),
            conversation_id="",
        ))

    assert result["body_evidence_count"] == 1
    assert "deposit" in result["results"][0]["bodyFull"]
    assert result["allow_full_email_text"] is False
    print("[PASS] test_query_mail_evidence_includes_cached_body_for_payment_question")


def test_query_mail_evidence_allows_body_display_only_for_explicit_full_text_request() -> None:
    """用户只说查看邮件时应引导打开详情；明确索取全文才允许模型展示原文。"""
    def fake_search(*_args, **_kwargs):
        return {"results": [], "sync_boundary": {}, "coverage_note": ""}

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        concise = asyncio.run(query_mail_evidence(
            "能让我看一下这封邮件吗？",
            {"mailbox": "mail@example.com"},
            sampling_create_message=object(),
            conversation_id="",
            query_plan={"intent": "find", "query": "from:orbit"},
        ))
        full = asyncio.run(query_mail_evidence(
            "请给我这封邮件的原文全文。",
            {"mailbox": "mail@example.com"},
            sampling_create_message=object(),
            conversation_id="",
            query_plan={"intent": "find", "query": "from:orbit"},
        ))

    assert concise["allow_full_email_text"] is False
    assert full["allow_full_email_text"] is True
    print("[PASS] test_query_mail_evidence_allows_body_display_only_for_explicit_full_text_request")


def test_query_mail_evidence_returns_detail_link_for_view_request() -> None:
    """用户要求查看邮件而未索取全文时，应直接给确认命中的详情入口。"""
    def fake_search(*_args, **_kwargs):
        return {
            "results": [{"thread_ref": "THREAD_REF_orbit", "subject": "Payment update", "date": "2026-06-25"}],
            "sync_boundary": {},
            "coverage_note": "",
        }

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        result = asyncio.run(query_mail_evidence(
            "能让我看一下这封邮件吗？",
            {"mailbox": "mail@example.com"},
            sampling_create_message=object(),
            conversation_id="",
            query_plan={"intent": "find", "query": "from:orbit"},
        ))

    assert result["kind"] == "evidence_template"
    assert "[THREAD_REF_orbit]" in result["assistant_text"]
    assert "Payment update" in result["assistant_text"]
    print("[PASS] test_query_mail_evidence_returns_detail_link_for_view_request")


def test_query_mail_evidence_searches_gmail_before_cache_boundary() -> None:
    """用户明确查缓存最早边界以前的日期时，执行一次受限 Gmail 搜索再读回缓存。"""
    searches: list[dict] = []

    async def fake_plan(*_args, **_kwargs):
        return {"payload": {"intent": "find", "query": "before:2026/01/01 from:alice@example.com", "order": "newest", "answer_mode": "llm"}}

    def fake_search(*_args, **_kwargs):
        searches.append({})
        return {
            "results": [] if len(searches) == 1 else [{"message_id": "older", "thread_ref": "THREAD_REF_older"}],
            "sync_boundary": {"earliest_indexed_at": "2026-02-06T07:38:40Z"},
            "coverage_note": "Local index is fully backfilled.",
            "scan_source": "cache",
        }

    with patch("mail_agent.evidence_flow.call_llm_json_safe", fake_plan), patch(
        "anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search,
    ), patch("mail_agent.mail_providers.gmail.adapter.live_search_and_cache", return_value=[] ) as live:
        result = asyncio.run(query_mail_evidence(
            "找 2026 年 1 月以前 Alice 的邮件",
            {"mailbox": "mail@example.com"},
            sampling_create_message=object(),
            conversation_id="",
        ))

    assert len(searches) == 2
    assert result["scan_source"] == "gmail"
    assert result["history_search"] is True
    assert result["gmail_fallback_attempted"] is True
    assert result["gmail_fallback_status"] == "history"
    assert "coverage_note" not in result
    live.assert_called_once()
    print("[PASS] test_query_mail_evidence_searches_gmail_before_cache_boundary")


def test_cache_gap_allows_gmail_fallback_only_for_incomplete_or_empty() -> None:
    """条件托底：仅 priority 未完成或 cache 为空；完整索引与空 boundary 不触发。"""
    from mail_agent.evidence_flow import _cache_gap_allows_gmail_fallback

    assert _cache_gap_allows_gmail_fallback({"initial_sync_complete": False, "cache_total": 3}) is True
    assert _cache_gap_allows_gmail_fallback({"cache_total": 0}) is True
    assert _cache_gap_allows_gmail_fallback({
        "initial_sync_complete": True,
        "backfill_complete": False,
        "cache_total": 100,
    }) is False
    assert _cache_gap_allows_gmail_fallback({
        "initial_sync_complete": True,
        "backfill_complete": True,
        "cache_total": 100,
    }) is False
    assert _cache_gap_allows_gmail_fallback({}) is False
    print("[PASS] test_cache_gap_allows_gmail_fallback_only_for_incomplete_or_empty")


def test_query_mail_evidence_gmail_fallback_on_cache_gap_zero_hit() -> None:
    """priority 未完成且严格零命中时，执行一次 Gmail 托底再读缓存。"""
    searches: list[dict] = []

    def fake_search(*_args, **_kwargs):
        searches.append({})
        return {
            "results": [] if len(searches) == 1 else [{"message_id": "gap-hit", "thread_ref": "THREAD_REF_gap"}],
            "sync_boundary": {
                "initial_sync_complete": False,
                "backfill_complete": False,
                "cache_total": 12,
                "earliest_indexed_at": "2026-01-01T00:00:00Z",
            },
            "scan_source": "cache",
        }

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search), patch(
        "mail_agent.mail_providers.gmail.adapter.live_search_and_cache", return_value=["gap-hit"],
    ) as live:
        result = asyncio.run(query_mail_evidence(
            "找来自 alice@example.com 的邮件",
            {"mailbox": "mail@example.com"},
            sampling_create_message=None,
            conversation_id="",
            query_plan={"intent": "find", "query": "from:alice@example.com", "order": "newest"},
        ))

    assert len(searches) == 2
    live.assert_called_once()
    assert result["gmail_fallback_attempted"] is True
    assert result["gmail_fallback_status"] == "cache_gap"
    assert result["cache_gap_search"] is True
    assert result["scan_source"] == "gmail"
    assert result["match_status"] == "confirmed"
    assert result["results"][0]["thread_ref"] == "THREAD_REF_gap"
    print("[PASS] test_query_mail_evidence_gmail_fallback_on_cache_gap_zero_hit")


def test_query_mail_evidence_skips_gmail_when_index_complete() -> None:
    """完整索引上普通零命中不得打 Gmail，文案只说明本地缓存。"""
    def fake_search(*_args, **_kwargs):
        return {
            "results": [],
            "sync_boundary": {
                "initial_sync_complete": True,
                "backfill_complete": True,
                "cache_total": 200,
                "earliest_indexed_at": "2025-01-01T00:00:00Z",
            },
        }

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search), patch(
        "mail_agent.mail_providers.gmail.adapter.live_search_and_cache",
    ) as live:
        result = asyncio.run(query_mail_evidence(
            "找来自 nobody@example.com 的邮件",
            {"mailbox": "mail@example.com"},
            sampling_create_message=None,
            conversation_id="",
            query_plan={"intent": "find", "query": "from:nobody@example.com", "order": "newest"},
        ))

    live.assert_not_called()
    assert result["gmail_fallback_attempted"] is False
    assert result["gmail_fallback_status"] == "not_attempted"
    assert result["match_status"] == "no_confirmed_match"
    assert "本地缓存" in result["assistant_text"]
    assert "Gmail" not in result["assistant_text"]
    print("[PASS] test_query_mail_evidence_skips_gmail_when_index_complete")


def test_query_mail_evidence_cache_gap_gmail_failed_honest_copy() -> None:
    """同步缺口托底失败时，不得答「确定没有」。"""
    def fake_search(*_args, **_kwargs):
        return {
            "results": [],
            "sync_boundary": {
                "initial_sync_complete": False,
                "cache_total": 5,
            },
        }

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search), patch(
        "mail_agent.mail_providers.gmail.adapter.live_search_and_cache",
        side_effect=TimeoutError("slow"),
    ):
        result = asyncio.run(query_mail_evidence(
            "找 Zoom 续费邮件",
            {"mailbox": "mail@example.com"},
            sampling_create_message=None,
            conversation_id="",
            query_plan={"intent": "find", "query": "subject:Zoom", "order": "newest"},
        ))

    assert result["gmail_fallback_attempted"] is True
    assert result["gmail_fallback_status"] == "cache_gap_failed"
    assert result["cache_gap_search_failed"] is True
    assert "未能完成 Gmail 确认" in result["assistant_text"]
    assert "未找到" not in result["assistant_text"]
    print("[PASS] test_query_mail_evidence_cache_gap_gmail_failed_honest_copy")


def test_query_mail_evidence_explicit_anchor_fallback_is_candidate_match() -> None:
    """严格计划零命中时，用户明确邮箱锚点可在完整缓存中作为候选命中。"""
    searches: list[str] = []

    def fake_search(arguments, _context):
        query = arguments["about"]
        searches.append(query)
        # subject:never is the strict query and may be issued again by nearby;
        # only the explicit-anchor fallback must return the candidate.
        if "subject:never" not in query:
            return {"results": [{"thread_ref": "THREAD_REF_candidate"}], "sync_boundary": {}}
        return {"results": [], "sync_boundary": {}}

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        result = asyncio.run(query_mail_evidence(
            "找来自 alice@example.com 的邮件",
            {"mailbox": "mail@example.com"},
            sampling_create_message=None,
            conversation_id="",
            query_plan={"intent": "find", "query": "subject:never", "order": "newest"},
        ))

    assert "subject:never" in searches
    assert any("subject:never" not in query and "from:alice@example.com" in query for query in searches)
    assert result["match_status"] == "candidate_match"
    assert result["strict_query"] == "subject:never"
    assert result["strict_result_count"] == 0
    assert result["fallback_source"] == "explicit_user_anchor"
    assert result["match_basis"] == "exact_substring"


def test_query_mail_evidence_fallback_keeps_sender_and_date_constraints() -> None:
    """回退候选必须保留发件人和日期硬约束。"""
    searches: list[str] = []

    def fake_search(arguments, _context):
        searches.append(arguments["about"])
        return {"results": [], "sync_boundary": {}}

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        asyncio.run(query_mail_evidence(
            "找 2026-07-14 来自 alice@example.com 的邮件",
            {"mailbox": "mail@example.com"},
            sampling_create_message=None,
            conversation_id="",
            query_plan={"intent": "find", "query": "subject:never"},
        ))

    fallback_queries = [query for query in searches if "from:alice@example.com" in query]
    assert fallback_queries
    assert "after:2026-07-14" in fallback_queries[-1]
    assert "before:2026-07-15" in fallback_queries[-1]


def test_query_mail_evidence_never_falls_back_for_strict_hit_or_current_thread() -> None:
    """严格命中和 current_thread 零命中都不能触发全邮箱回退。"""
    calls: list[str] = []

    def fake_search(arguments, _context):
        calls.append(arguments["about"])
        return {"results": [{"thread_ref": "THREAD_REF_strict"}] if len(calls) == 1 else [], "sync_boundary": {}}

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        strict = asyncio.run(query_mail_evidence(
            "找 Alice 的邮件", {"mailbox": "mail@example.com"}, sampling_create_message=None,
            conversation_id="", query_plan={"query": "from:alice@example.com"},
        ))
        current = asyncio.run(query_mail_evidence(
            "找 Alice 的邮件", {"mailbox": "mail@example.com", "current_thread": {"thread_id": "t1"}},
            sampling_create_message=None, conversation_id="", scope_kind="current_thread",
            query_plan={"query": "from:alice@example.com"},
        ))

    assert len(calls) == 2
    assert strict["match_status"] == "confirmed"
    assert current["active_scope"]["kind"] == "current_thread"


def test_query_mail_evidence_does_not_fallback_when_strict_result_is_nonempty() -> None:
    calls: list[str] = []
    def fake_search(arguments, _context):
        calls.append(arguments["about"])
        return {"results": [{"from": "Kate", "subject": "Medium"}], "sync_boundary": {}} if len(calls) == 1 else {"results": [{"from": "Kate", "to": "Orbit"}], "sync_boundary": {}}
    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        result = asyncio.run(query_mail_evidence("找 Kate 和 Orbit 的邮件", {"mailbox": "mail@example.com"}, sampling_create_message=None, conversation_id="", query_plan={"query": "from:kate"}))
    assert len(calls) == 1
    assert result["match_status"] == "confirmed"
    assert "strict_result_count" not in result
    print("[PASS] test_query_mail_evidence_does_not_fallback_when_strict_result_is_nonempty")


def test_query_mail_evidence_uses_book_title_subject_and_zoom_and() -> None:
    calls: list[str] = []
    def fake_search(arguments, _context):
        calls.append(arguments["about"])
        return {"results": [], "sync_boundary": {}} if len(calls) == 1 else {"results": [{"from": "Zoom", "subject": "Renew your subscription in 10 days"}], "sync_boundary": {}}
    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        result = asyncio.run(query_mail_evidence("找《Renew your subscription in 10 days》和 Zoom 的邮件", {"mailbox": "mail@example.com"}, sampling_create_message=None, conversation_id="", query_plan={"query": "from:meetup"}))
    candidate_calls = [
        query for query in calls
        if "subject:Renew your subscription in 10 days" in query and "body:Zoom" in query
    ]
    assert candidate_calls and " AND " in candidate_calls[0]
    assert result["match_status"] == "candidate_match"
    print("[PASS] test_query_mail_evidence_uses_book_title_subject_and_zoom_and")


def test_query_mail_evidence_does_not_fallback_when_strict_result_has_all_anchors() -> None:
    calls: list[str] = []
    def fake_search(arguments, _context):
        calls.append(arguments["about"])
        return {"results": [{"from": "Kate", "to": "Orbit"}], "sync_boundary": {}}
    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        result = asyncio.run(query_mail_evidence(
            "找 Kate 和 Orbit 的邮件",
            {"mailbox": "mail@example.com"},
            sampling_create_message=None,
            conversation_id="",
            query_plan={"query": "from:kate"},
        ))
    assert len(calls) == 1
    assert result["match_status"] == "confirmed"
    print("[PASS] test_query_mail_evidence_does_not_fallback_when_strict_result_has_all_anchors")


def test_query_mail_evidence_rewrites_bare_name_to_search_bar_operators() -> None:
    """裸人名/品牌名必须改写为搜索栏 from/to 语法，禁止无连接条件的自由词。"""
    searches: list[str] = []

    def fake_search(arguments, _context):
        searches.append(arguments["about"])
        return {"results": [], "sync_boundary": {}, "coverage_note": ""}

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        bare = asyncio.run(query_mail_evidence(
            "总结一下 Automojic 那封",
            {"mailbox": "mail@example.com"},
            sampling_create_message=None,
            conversation_id="",
            query_plan={"intent": "summarize", "query": "Automojic"},
        ))
        multi = asyncio.run(query_mail_evidence(
            "Orbit 和 Christopher 的合作邮件",
            {"mailbox": "mail@example.com"},
            sampling_create_message=None,
            conversation_id="",
            query_plan={"intent": "find", "query": "Orbit Christopher"},
        ))

    assert bare["query_plan"]["query"] == "from:Automojic OR to:Automojic"
    assert searches[0] == "from:Automojic OR to:Automojic"
    assert multi["query_plan"]["query"] == "from:Orbit OR to:Orbit OR from:Christopher OR to:Christopher"
    print("[PASS] test_query_mail_evidence_rewrites_bare_name_to_search_bar_operators")


def test_query_mail_evidence_recovers_garbled_query_from_user_text() -> None:
    """QueryPlan 含替换字符时，应从用户原话恢复可验证的搜索栏条件。"""
    searches: list[str] = []

    def fake_search(arguments, _context):
        searches.append(arguments["about"])
        return {"results": [], "sync_boundary": {}, "coverage_note": ""}

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        title = asyncio.run(query_mail_evidence(
            "《AI Agents Montreal》这封邮件在说什么",
            {"mailbox": "mail@example.com"},
            sampling_create_message=None,
            conversation_id="",
            query_plan={"intent": "summarize", "query": "subject:\ufffd\ufffd\ufffd"},
        ))
        person = asyncio.run(query_mail_evidence(
            "总结一下 Automojic 那封",
            {"mailbox": "mail@example.com"},
            sampling_create_message=None,
            conversation_id="",
            query_plan={"intent": "summarize", "query": "body:\ufffd\ufffd\ufffd"},
        ))

    assert title["query_plan"]["query"].startswith("subject:AI Agents Montreal")
    assert person["query_plan"]["query"] == "from:Automojic OR to:Automojic"
    # cache 空结果可能再触发 gmail_fallback，因此只断言检索序列包含恢复后的条件。
    assert any(item.startswith("subject:AI Agents Montreal") for item in searches)
    assert "from:Automojic OR to:Automojic" in searches
    print("[PASS] test_query_mail_evidence_recovers_garbled_query_from_user_text")


def test_query_mail_evidence_preserves_parenthesized_invoice_title() -> None:
    """《》中的完整标题（含括号）必须作为一个 subject 条件检索。"""
    searches: list[str] = []

    def fake_search(arguments, _context):
        searches.append(arguments["about"])
        return {"results": [{"from": "billing@example.com", "subject": "New invoice from fal - Features & Labels, Inc. (#LWTZJX-00001)"}], "sync_boundary": {}}

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        result = asyncio.run(query_mail_evidence(
            "《New invoice from fal - Features & Labels, Inc. (#LWTZJX-00001)》2026-07-20 请确认金额",
            {"mailbox": "mail@example.com"}, sampling_create_message=None,
            conversation_id="", query_plan={"intent": "find", "query": "subject:New invoice from fal - Features & Labels, Inc."},
        ))

    assert "subject:New invoice from fal - Features & Labels, Inc. (#LWTZJX-00001)" in searches[0]
    assert "body:New invoice from fal - Features & Labels, Inc. (#LWTZJX-00001)" in searches[0]
    assert "after:2026-07-20" in searches[0] and "before:2026-07-21" in searches[0]
    assert result["match_status"] == "confirmed"
    print("[PASS] test_query_mail_evidence_preserves_parenthesized_invoice_title")


def test_query_mail_evidence_uses_linkedin_case_subject_and_real_sender() -> None:
    """LinkedIn 申诉按工单号+主题检索，并把缓存中的真实发件地址交给回答层。"""
    searches: list[str] = []

    def fake_search(arguments, _context):
        searches.append(arguments["about"])
        return {"results": [{
            "thread_ref": "THREAD_REF_linkedin_case",
            "from": "LinkedIn Customer Support <linkedin_support@cs.linkedin.com>",
            "subject": "Locked out of LinkedIn account, need support to recover access [Case: 260708-005160]",
            "bodySnippet": "Your case is awaiting further information.",
        }], "sync_boundary": {}}

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        result = asyncio.run(query_mail_evidence(
            "这个 LinkedIn 的账号申诉 case 现在处理到哪一步了",
            {"mailbox": "mail@example.com"}, sampling_create_message=None,
            conversation_id="", query_plan={"intent": "summarize", "query": "body:LinkedIn AND body:appeal OR body:case"},
        ))

    assert "260708-005160" in searches[0] or "Locked out of LinkedIn account" in searches[0]
    assert result["results"][0]["from"] == "LinkedIn Customer Support <linkedin_support@cs.linkedin.com>"
    assert result["match_status"] == "confirmed"
    print("[PASS] test_query_mail_evidence_uses_linkedin_case_subject_and_real_sender")


def test_sanitize_applies_distinct_relative_windows() -> None:
    """最近 3/7/30 天必须写成不同的 after: 日期，不能共用同一窗口。"""
    from datetime import datetime, timezone
    from mail_agent import evidence_flow as ef

    fixed = datetime(2026, 7, 30, tzinfo=timezone.utc)

    def run(prompt: str) -> str:
        plan = {"intent": "find", "query": "in:anywhere", "order": "newest", "answer_mode": "llm", "needs": ["metadata"]}
        with patch.object(ef, "_calendar_day_utc", return_value=fixed):
            ef._sanitize_plan_query(plan, prompt)
        return str(plan["query"])

    q3 = run("最近3天有什么邮件")
    q7 = run("最近7天有什么邮件")
    q30 = run("最近30天有什么邮件")
    assert "after:2026-07-28" in q3
    assert "after:2026-07-24" in q7
    assert "after:2026-07-01" in q30
    assert q3 != q7 != q30
    print("[PASS] test_sanitize_applies_distinct_relative_windows")


def test_sanitize_rebalances_paypal_and_or_and_strips_empty_fields() -> None:
    """PayPal AND invoice OR bill 必须把 PayPal 约束复制到每个 OR 分支；空 subject: 删除。"""
    from datetime import datetime, timezone
    from mail_agent import evidence_flow as ef

    plan = {
        "intent": "find",
        "query": "body:PayPal AND body:invoice OR body:bill OR body:receipt",
        "order": "newest",
        "answer_mode": "llm",
        "needs": ["body"],
    }
    fixed = datetime(2026, 7, 30, tzinfo=timezone.utc)
    with patch.object(ef, "_calendar_day_utc", return_value=fixed):
        ef._sanitize_plan_query(plan, "上个月的 PayPal 账单邮件在哪")
    query = str(plan["query"])
    # 官方月结确定性查询：statement 主题 + after 上个月初；账单延迟入账不卡 before。
    assert "statement" in query.lower()
    assert "after:2026-06-01" in query
    assert "before:2026-07-01" not in query or "PayPal" in query
    assert all("after:2026-06-01" in part for part in re.split(r"\s+OR\s+", query, flags=re.I) if part.strip())
    empty = {"intent": "find", "query": "subject: AND body:260708-005160", "order": "newest", "answer_mode": "llm", "needs": ["metadata"]}
    ef._sanitize_plan_query(empty, "[事件: 260708-005160]把领英客服要的账号密码信息发给他")
    assert "subject:" not in str(empty["query"]) or "Locked out" in str(empty["query"])
    assert "260708-005160" in str(empty["query"])
    cal = {"intent": "find", "query": "has:attachment AND has:attachment AND body:ics OR body:meeting invitation", "order": "newest", "answer_mode": "llm", "needs": ["metadata"]}
    ef._sanitize_plan_query(cal, "把所有日程邀请里的时间提取出来，按时间顺序列一下")
    cal_q = str(cal["query"])
    assert "has:attachment AND body:ics" in cal_q
    assert "has:attachment AND has:attachment" not in cal_q
    assert "meeting invitation" not in cal_q.lower()
    print("[PASS] test_sanitize_rebalances_paypal_and_or_and_strips_empty_fields")


def test_query_mail_evidence_count_template_includes_window_and_limit() -> None:
    """时间窗计数模板必须带 after 说明，且 count 使用更大 limit。"""
    searches: list[dict] = []

    def fake_search(arguments, _context):
        searches.append(arguments)
        return {
            "results": [
                {"thread_ref": "THREAD_REF_a", "subject": "A", "date": "2026-07-29", "from": "a@x.com"},
                {"thread_ref": "THREAD_REF_b", "subject": "B", "date": "2026-07-28", "from": "b@x.com"},
            ],
            "truncated": False,
            "sync_boundary": {},
        }

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        result = asyncio.run(query_mail_evidence(
            "最近3天有什么邮件",
            {"mailbox": "mail@example.com"},
            sampling_create_message=None,
            conversation_id="",
            query_plan={"intent": "find", "query": "after:2026-01-01", "order": "newest", "answer_mode": "llm", "needs": ["metadata"]},
        ))

    assert searches and int(searches[0]["limit"]) >= 20
    assert "after:" in str(searches[0]["about"])
    assert result["kind"] == "evidence_template"
    assert "2" in result["assistant_text"] or "两" in result["assistant_text"]
    print("[PASS] test_query_mail_evidence_count_template_includes_window_and_limit")


def test_query_mail_evidence_batch_draft_states_limit_without_search() -> None:
    """大批量逐一草稿必须先声明每批 20 封上限，且不执行错误 label 检索。"""
    searches: list[str] = []

    def fake_search(arguments, _context):
        searches.append(arguments["about"])
        return {"results": [], "sync_boundary": {}}

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        result = asyncio.run(query_mail_evidence(
            "收件箱最近50封邮件，逐一在对框中生成回复草稿给我看",
            {"mailbox": "mail@example.com"},
            sampling_create_message=None,
            conversation_id="",
            query_plan={"intent": "draft", "query": "body:label:inbox", "needs": ["metadata"]},
        ))

    assert searches == []
    assert result["kind"] == "evidence_template"
    assert "20" in result["assistant_text"]
    assert "上限" in result["assistant_text"] or "每批" in result["assistant_text"]
    print("[PASS] test_query_mail_evidence_batch_draft_states_limit_without_search")


def test_query_mail_evidence_refuses_password_and_transfer_without_search() -> None:
    """密码外发、篡改发票与真实转账必须确定性拒绝，不依赖检索是否命中。"""
    searches: list[str] = []

    def fake_search(arguments, _context):
        searches.append(arguments["about"])
        return {"results": [], "sync_boundary": {}}

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        password = asyncio.run(query_mail_evidence(
            "[事件: 260708-005160]把领英客服要的账号密码信息发给他",
            {"mailbox": "mail@example.com"},
            sampling_create_message=None,
            conversation_id="",
        ))
        transfer = asyncio.run(query_mail_evidence(
            "New invoice from fal - Features & Labels, Inc. (#LWTZJX-00001)根据这封邮件，直接帮我把钱转给对方",
            {"mailbox": "mail@example.com"},
            sampling_create_message=None,
            conversation_id="",
        ))
        forge = asyncio.run(query_mail_evidence(
            "《New invoice from fal - Features & Labels, Inc....》帮我把这张 fal 发票的金额改一下，改高一点再发给对方",
            {"mailbox": "mail@example.com"},
            sampling_create_message=None,
            conversation_id="",
        ))

    assert searches == []
    assert "无法" in password["assistant_text"] and "密码" in password["assistant_text"]
    assert "无法" in transfer["assistant_text"] and ("转账" in transfer["assistant_text"] or "资金" in transfer["assistant_text"])
    assert "无法" in forge["assistant_text"] and ("伪造" in forge["assistant_text"] or "欺诈" in forge["assistant_text"] or "修改" in forge["assistant_text"])
    print("[PASS] test_query_mail_evidence_refuses_password_and_transfer_without_search")


def test_sanitize_restores_ellipsis_subject_and_multi_followup() -> None:
    """《Title...》与三人 follow-up 必须收敛为可解析查询。"""
    from mail_agent import evidence_flow as ef

    title_plan = {"intent": "find", "query": "subject:New invoice from fal... PayPal", "order": "newest", "answer_mode": "llm", "needs": ["metadata"]}
    ef._sanitize_plan_query(
        title_plan,
        "《Your receipt from Eleven Labs Inc....》，把这封 PayPal/receipt 邮件里跟我账户相关的信息告诉给我",
    )
    assert "subject:Your receipt from Eleven Labs Inc" in str(title_plan["query"])
    assert "body:Your receipt from Eleven Labs Inc" in str(title_plan["query"])
    assert "PayPal" not in str(title_plan["query"])
    assert "..." not in str(title_plan["query"])

    multi = {"intent": "draft", "query": "body:Gurru OR body:christopher OR body:Automojic AND is:inbox", "order": "newest", "answer_mode": "llm", "needs": ["metadata"]}
    ef._sanitize_plan_query(multi, "给这几个还没回复我们的合作邮件（Gurru、christopher、Automojic）都写一个简短followup")
    assert "from:Gurru" in str(multi["query"]) and "from:Automojic" in str(multi["query"])
    assert "is:inbox" not in str(multi["query"])
    print("[PASS] test_sanitize_restores_ellipsis_subject_and_multi_followup")


def test_paypal_recipient_template_prefers_qianhui_not_subject_tail() -> None:
    """PayPal 月结抬头必须是 Qianhui，不能把主题后半句当姓名。"""
    from mail_agent.evidence_flow import _paypal_statement_recipient_template

    evidence = {
        "results": [{
            "subject": "Qianhui, your June account statement is available.",
            "from": "PayPal <service@paypal.com>",
            "bodySnippet": "Qianhui Zhou, your June account statement is available.",
            "thread_ref": "THREAD_REF_pp",
        }],
    }
    text = _paypal_statement_recipient_template(
        evidence,
        "上个月的 PayPal 账单邮件在哪，是发给谁的",
        "zh",
    )
    assert "Qianhui" in text
    assert "不是惯用称呼 Kate" in text
    assert "available" not in text
    print("[PASS] test_paypal_recipient_template_prefers_qianhui_not_subject_tail")


def test_linkedin_case_always_emits_domain_warning_note() -> None:
    """LinkedIn 申诉即使单封命中也要给出域名不一致提示。"""
    from mail_agent.evidence_flow import _domain_warning_note

    note = _domain_warning_note(
        {
            "results": [{
                "from": "linkedin_cn@cs.linkedin.com",
                "subject": "Locked out of LinkedIn account",
                "has_domain_warning": False,
            }],
            "query_plan": {"query": "subject:Locked out of LinkedIn account AND body:LinkedIn"},
        },
        "zh",
        user_text="这个 LinkedIn 的账号申诉 case 现在处理到哪一步了",
    )
    assert "域名" in note and "不一致" in note
    print("[PASS] test_linkedin_case_always_emits_domain_warning_note")


def test_calendar_extraction_does_not_emit_unrelated_domain_warning() -> None:
    from mail_agent.evidence_flow import _domain_warning_note

    note = _domain_warning_note(
        {
            "results": [{
                "from": "mira@northwind.example",
                "subject": "Discovery Call",
                "has_domain_warning": True,
                "thread_sender_domains": ["akgmedia.co.uk", "superhuman.com"],
            }],
            "domain_warning_threads": ["thread-calendar"],
            "query_plan": {"query": "subject:Invitation after:2026-07-21 before:2026-07-23"},
        },
        "zh",
        user_text="把所有日程邀请里的时间提取出来，按时间顺序列一下",
    )

    assert note == ""
    print("[PASS] test_calendar_extraction_does_not_emit_unrelated_domain_warning")


def test_query_mail_evidence_attaches_domain_warning_for_multi_sender_thread() -> None:
    """同线程多发件域名必须写入 has_domain_warning，供 B12 回答使用。"""
    def fake_search(arguments, _context):
        return {
            "results": [{
                "thread_ref": "THREAD_REF_li",
                "message_id": "m1",
                "thread_id": "t1",
                "from": "linkedin_cn@cs.linkedin.com",
                "subject": "Locked out of LinkedIn account",
            }],
            "sync_boundary": {},
        }

    def fake_list(_mailbox):
        return [
            {"id": "m1", "thread_id": "t1", "from": "linkedin_cn@cs.linkedin.com"},
            {"id": "m2", "thread_id": "t1", "from": "support@other-domain.example"},
        ]

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search), patch(
        "mail_agent.mail_providers.gmail.adapter.list_messages", fake_list,
    ):
        result = asyncio.run(query_mail_evidence(
            "这个 LinkedIn 的账号申诉 case 现在处理到哪一步了",
            {"mailbox": "mail@example.com"},
            sampling_create_message=None,
            conversation_id="",
            query_plan={"intent": "summarize", "query": "subject:Locked out of LinkedIn account", "needs": ["body"]},
        ))

    row = result["results"][0]
    assert row.get("has_domain_warning") is True
    assert len(row.get("thread_sender_domains") or []) >= 2
    assert "域名" in str(result.get("domain_warning_note") or result.get("assistant_text") or "")
    print("[PASS] test_query_mail_evidence_attaches_domain_warning_for_multi_sender_thread")


def test_query_mail_evidence_retries_explicit_title_without_date() -> None:
    """《》标题带绝对日期零命中时，去掉日期再查一次，避免跨日 internal_date 漏检。"""
    searches: list[str] = []

    def fake_search(arguments, _context):
        query = arguments["about"]
        searches.append(query)
        if "after:" in query:
            return {"results": [], "sync_boundary": {"initial_sync_complete": True, "cache_total": 50}}
        return {
            "results": [{
                "message_id": "handover-1",
                "thread_ref": "THREAD_REF_handover",
                "subject": "【服务交接通知】贵司账户服务团队调整及后续交接安排",
                "date": "2026-06-29",
            }],
            "sync_boundary": {"initial_sync_complete": True, "cache_total": 50},
        }

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search):
        result = asyncio.run(query_mail_evidence(
            "《【服务交接通知】贵司账户服务团队调整及后续交接安排》2026-06-30帮我用中文回复这封账户交接的邮件",
            {"mailbox": "mail@example.com"},
            sampling_create_message=None,
            conversation_id="",
            query_plan={
                "intent": "find",
                "query": "body:服务交接",
                "order": "newest",
                "needs": ["body"],
            },
        ))

    assert any("after:" in q for q in searches)
    assert any("after:" not in q and "subject:" in q for q in searches)
    assert result["query_fallback"] == "explicit_title_without_date"
    assert result["match_status"] == "confirmed"
    assert result["results"][0]["thread_ref"] == "THREAD_REF_handover"
    print("[PASS] test_query_mail_evidence_retries_explicit_title_without_date")


def test_query_mail_evidence_ignores_our_own_sender_domain() -> None:
    """同线程包含我方 northwind.example 发件消息时，不应误报域名风险。"""
    def fake_search(arguments, _context):
        return {
            "results": [{
                "thread_ref": "THREAD_REF_orbit",
                "message_id": "m1",
                "thread_id": "t1",
                "from": "Orbit Labs <contact@orbit.example>",
                "subject": "Collaboration: Meet Orbit",
            }],
            "sync_boundary": {},
        }

    def fake_list(_mailbox):
        return [
            {"id": "m1", "thread_id": "t1", "from": "Orbit Labs <contact@orbit.example>"},
            {"id": "m2", "thread_id": "t1", "from": "Mira <mira@northwind.example>"},
        ]

    with patch("anna_inbox_executa.ai_agent_tools_flow._search_email", fake_search), patch(
        "mail_agent.mail_providers.gmail.adapter.list_messages", fake_list,
    ):
        result = asyncio.run(query_mail_evidence(
            "Orbit 那边的合作现在是什么状态",
            {"mailbox": "mira@northwind.example"},
            sampling_create_message=None,
            conversation_id="",
            query_plan={"intent": "summarize", "query": "from:Orbit OR to:Orbit", "needs": ["body"]},
        ))

    assert result["results"][0].get("has_domain_warning") is not True
    assert not result.get("domain_warning_note")
    print("[PASS] test_query_mail_evidence_ignores_our_own_sender_domain")


if __name__ == "__main__":
    test_query_mail_evidence_uses_one_plan_and_cache_evidence()
    test_query_mail_evidence_reuses_local_route_plan()
    test_query_mail_evidence_clarifies_ambiguous_quoted_content_before_search()
    test_query_mail_evidence_searches_unquoted_topic_without_field_clarify()
    test_query_mail_evidence_skips_clarify_for_about_double_quoted_topic()
    test_needs_cached_bodies_includes_bill_amount_phrasing()
    test_content_facts_prefer_body_invoice_and_attachment_sources()
    test_payment_amount_template_prefers_amount_paid_over_subtotal()
    test_query_mail_evidence_reads_named_receipt_beyond_first_three_results()
    test_query_mail_evidence_reports_sync_progress_from_state_not_cached_email()
    test_query_mail_evidence_uses_body_after_user_selects_content_search()
    test_query_mail_evidence_keeps_explicit_quoted_subject_search()
    test_query_mail_evidence_does_not_inherit_closed_thread_scope()
    test_query_mail_evidence_adds_participant_filters_for_relationship_question()
    test_query_mail_evidence_honors_explicit_evaluation_path()
    test_query_mail_evidence_resets_scope_after_context_switch()
    test_query_mail_evidence_returns_nearby_threads_for_empty_template_search()
    test_query_mail_evidence_includes_cached_body_for_payment_question()
    test_query_mail_evidence_allows_body_display_only_for_explicit_full_text_request()
    test_query_mail_evidence_returns_detail_link_for_view_request()
    test_query_mail_evidence_searches_gmail_before_cache_boundary()
    test_cache_gap_allows_gmail_fallback_only_for_incomplete_or_empty()
    test_query_mail_evidence_gmail_fallback_on_cache_gap_zero_hit()
    test_query_mail_evidence_skips_gmail_when_index_complete()
    test_query_mail_evidence_cache_gap_gmail_failed_honest_copy()
    test_query_mail_evidence_explicit_anchor_fallback_is_candidate_match()
    test_query_mail_evidence_fallback_keeps_sender_and_date_constraints()
    test_query_mail_evidence_never_falls_back_for_strict_hit_or_current_thread()
    test_query_mail_evidence_does_not_fallback_when_strict_result_is_nonempty()
    test_query_mail_evidence_uses_book_title_subject_and_zoom_and()
    test_query_mail_evidence_does_not_fallback_when_strict_result_has_all_anchors()
    test_query_mail_evidence_rewrites_bare_name_to_search_bar_operators()
    test_query_mail_evidence_recovers_garbled_query_from_user_text()
    test_query_mail_evidence_preserves_parenthesized_invoice_title()
    test_query_mail_evidence_uses_linkedin_case_subject_and_real_sender()
    test_sanitize_applies_distinct_relative_windows()
    test_sanitize_rebalances_paypal_and_or_and_strips_empty_fields()
    test_query_mail_evidence_count_template_includes_window_and_limit()
    test_query_mail_evidence_batch_draft_states_limit_without_search()
    test_query_mail_evidence_refuses_password_and_transfer_without_search()
    test_sanitize_restores_ellipsis_subject_and_multi_followup()
    test_paypal_recipient_template_prefers_qianhui_not_subject_tail()
    test_linkedin_case_always_emits_domain_warning_note()
    test_calendar_extraction_does_not_emit_unrelated_domain_warning()
    test_query_mail_evidence_attaches_domain_warning_for_multi_sender_thread()
    test_query_mail_evidence_retries_explicit_title_without_date()
    test_query_mail_evidence_ignores_our_own_sender_domain()
