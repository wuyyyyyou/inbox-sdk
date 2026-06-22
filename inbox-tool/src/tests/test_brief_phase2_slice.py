"""Brief Phase 2 should keep concurrent throughput while updating progress."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def check(label: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


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

    run_id = "test_phase2_slice"
    candidates = [
        CandidateItem(candidate_id=f"cand-{idx}", kind="reply_required_possible", message_ids=[f"msg-{idx}"], thread_id=f"thread-{idx}")
        for idx in range(3)
    ]
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
        "partial": {"_args": {"mailbox": "user@example.com", "user_request": "scan"}},
        "needs_continue": True,
        "cards_added": 0,
        "brief": {
            "stage": "phase2",
            "strategy_mode": "default_secretary",
            "task_plan": {"strategy_mode": "default_secretary", "raw_user_request": "scan"},
            "candidates": brief_flow._brief_to_dict_list(candidates),
            "phase2_cursor": 0,
            "judgments": [],
            "messages": [],
        },
    }

    async def fake_read_candidate_context(mailbox: str, candidate: CandidateItem) -> CandidateContext:
        return CandidateContext(type="header_only", candidate=candidate)

    async def fake_call_llm_json_safe(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"payload": {"ok": True}, "fallback_used": False}

    def fake_parse_compact_batch_item(raw: dict[str, Any], strategy: Any) -> JudgmentResult:
        return JudgmentResult(
            candidate_id="cand-0",
            strategy_mode="default_secretary",
            final_decision=FinalDecision(should_show_in_main_result=True, priority="medium"),
        )

    try:
        context_mod.read_candidate_context = fake_read_candidate_context
        judgment_service.build_anna_single_judgment_prompt = lambda *args, **kwargs: "{}"
        llm_service.call_llm_json_safe = fake_call_llm_json_safe
        judgment_service._parse_compact_batch_item = fake_parse_compact_batch_item
        brief_flow._brief_persist_cards = lambda run_id, judgments: asyncio.sleep(0, result=len(judgments))
        brief_flow._save_run_checkpoint = lambda run_id: None

        await brief_flow._brief_run_phase2_slice(run_id, None)

        brief = brief_flow.MAIL_AGENT_RUNS[run_id]["brief"]
        check("phase2 cursor advances the whole small batch", brief["phase2_cursor"], 3)
        check("all judgments persisted to run state", len(brief["judgments"]), 3)
        check("progress evaluated all items", brief_flow.MAIL_AGENT_RUNS[run_id]["progress"]["evaluated"], 3)
        check("stage reaches finalizing", brief_flow.MAIL_AGENT_RUNS[run_id]["stage"], "finalizing")
    finally:
        context_mod.read_candidate_context = original_read_candidate_context
        judgment_service.build_anna_single_judgment_prompt = original_build_prompt
        llm_service.call_llm_json_safe = original_call_llm_json_safe
        judgment_service._parse_compact_batch_item = original_parse_compact_batch_item
        brief_flow._brief_persist_cards = original_persist_cards
        brief_flow._save_run_checkpoint = original_save_run_checkpoint
        brief_flow.MAIL_AGENT_RUNS.pop(run_id, None)


def main() -> None:
    asyncio.run(_run())
    print("PASS brief phase2 slice tests")


if __name__ == "__main__":
    main()
