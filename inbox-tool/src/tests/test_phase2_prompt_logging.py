"""Phase 2 prompt logging should emit structured metrics without prompt previews."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def check_true(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)


async def _run() -> None:
    from anna_inbox_executa import brief_flow
    from mail_agent.core import context as context_mod
    from mail_agent.domain.types import CandidateContext, CandidateItem, FinalDecision, JudgmentResult
    from mail_agent.judgment_engine import service as judgment_service
    from mail_agent.llm_runtime import service as llm_service

    original_read_candidate_context = context_mod.read_candidate_context
    original_build_prompt = judgment_service.build_anna_single_judgment_prompt
    original_call_llm_json_safe = llm_service.call_llm_json_safe
    original_parse_compact_batch_item = judgment_service._parse_compact_batch_item
    original_persist_cards = brief_flow._brief_persist_cards
    original_save_run_checkpoint = brief_flow._save_run_checkpoint
    original_log = brief_flow.log

    run_id = "test_phase2_prompt_logging"
    candidate = CandidateItem(
        candidate_id="cand-log",
        kind="safe_account_record",
        message_ids=["msg-log"],
        thread_id="thread-log",
        evidence={"user_action": "review"},
        priority_hint="low",
    )
    brief_flow.MAIL_AGENT_RUNS[run_id] = {
        "run_id": run_id,
        "status": "running",
        "stage": "phase2",
        "progress": {},
        "warnings": [],
        "started_at": "2026-06-22T00:00:00+08:00",
        "updated_at": "2026-06-22T00:00:00+08:00",
        "result": None,
        "error": "",
        "partial": {"_args": {"mailbox": "user@example.com", "user_request": "Find the billing email from Stripe and summarize it."}},
        "needs_continue": True,
        "cards_added": 0,
        "brief": {
            "stage": "phase2",
            "strategy_mode": "default_secretary",
            "task_plan": {"strategy_mode": "default_secretary", "raw_user_request": "Find the billing email from Stripe and summarize it."},
            "candidates": brief_flow._brief_to_dict_list([candidate]),
            "phase2_cursor": 0,
            "judgments": [],
            "messages": [],
        },
    }

    logs: list[str] = []

    async def fake_read_candidate_context(mailbox: str, candidate: CandidateItem) -> CandidateContext:
        return CandidateContext(type="header_only", candidate=candidate)

    async def fake_call_llm_json_safe(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"payload": {"ok": True}, "fallback_used": False}

    def fake_parse_compact_batch_item(raw: dict[str, Any], strategy: Any) -> JudgmentResult:
        return JudgmentResult(
            candidate_id=str(raw.get("candidate_id") or ""),
            strategy_mode="default_secretary",
            final_decision=FinalDecision(should_show_in_main_result=True, priority="medium"),
        )

    def fake_log(message: str) -> None:
        logs.append(message)

    sensitive_prompt = """You are Anna.
## Email
candidate_id: cand-log
kind: safe_account_record
priority_hint: low
from: Stripe Billing <billing@stripe.example>
subject: Your invoice for May
snippet: Card ending in 4242 was charged.
date: Jun 25, 2026
context_type: message_detail

--- Full Message Body ---
Body text: The payment for account 4242 failed because the card expired.

## User request
Find the billing email from Stripe and summarize it.

## Output format
Return JSON.
"""

    try:
        context_mod.read_candidate_context = fake_read_candidate_context
        judgment_service.build_anna_single_judgment_prompt = lambda *args, **kwargs: sensitive_prompt
        llm_service.call_llm_json_safe = fake_call_llm_json_safe
        judgment_service._parse_compact_batch_item = fake_parse_compact_batch_item
        brief_flow._brief_persist_cards = lambda run_id, judgments: asyncio.sleep(0, result=len(judgments))
        brief_flow._save_run_checkpoint = lambda run_id: None
        brief_flow.log = fake_log

        await brief_flow._brief_run_phase2_slice(run_id, None)

        prompt_logs = [entry for entry in logs if entry.startswith("phase2 prompt: ")]
        result_logs = [entry for entry in logs if entry.startswith("phase2 result: ")]
        check_true("prompt log emitted", len(prompt_logs) == 1)
        check_true("result log emitted", len(result_logs) == 1)
        prompt_log = prompt_logs[0]
        check_true("candidate id present", '"candidate_id": "cand-log"' in prompt_log)
        check_true("prompt profile present", '"prompt_profile": "compact_review"' in prompt_log)
        check_true("prompt timeout is fixed at 60s", '"timeout_s": 60.0' in prompt_log)
        check_true("prompt preview omitted", '"preview"' not in prompt_log)
        check_true("subject absent", "Your invoice for May" not in prompt_log)
        check_true("body absent", "card expired" not in prompt_log)
        check_true("user request absent", "Find the billing email from Stripe" not in prompt_log)
    finally:
        context_mod.read_candidate_context = original_read_candidate_context
        judgment_service.build_anna_single_judgment_prompt = original_build_prompt
        llm_service.call_llm_json_safe = original_call_llm_json_safe
        judgment_service._parse_compact_batch_item = original_parse_compact_batch_item
        brief_flow._brief_persist_cards = original_persist_cards
        brief_flow._save_run_checkpoint = original_save_run_checkpoint
        brief_flow.log = original_log
        brief_flow.MAIL_AGENT_RUNS.pop(run_id, None)


def main() -> None:
    asyncio.run(_run())
    print("PASS phase2 prompt logging tests")


if __name__ == "__main__":
    main()
