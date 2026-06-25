"""Brief Phase 2 should keep throughput, emit progress, and resume safely."""

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

    def make_run(run_id: str, *, candidate_count: int, judgments: list[JudgmentResult] | None = None) -> list[CandidateItem]:
        candidates = [
            CandidateItem(candidate_id=f"cand-{idx}", kind="reply_required_possible", message_ids=[f"msg-{idx}"], thread_id=f"thread-{idx}")
            for idx in range(candidate_count)
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
                "judgments": brief_flow._brief_to_dict_list(judgments or []),
                "messages": [],
            },
        }
        return candidates

    active_context_reads = 0
    max_context_reads = 0
    llm_calls: list[str] = []

    async def fake_read_candidate_context(mailbox: str, candidate: CandidateItem) -> CandidateContext:
        nonlocal active_context_reads, max_context_reads
        active_context_reads += 1
        max_context_reads = max(max_context_reads, active_context_reads)
        try:
            await asyncio.sleep(0.01)
            return CandidateContext(type="header_only", candidate=candidate)
        finally:
            active_context_reads -= 1

    async def fake_call_llm_json_safe(*args: Any, **kwargs: Any) -> dict[str, Any]:
        candidate_id = str(kwargs.get("user_message") or "")
        llm_calls.append(candidate_id)
        return {"payload": {"candidate_id": candidate_id, "ok": True}, "fallback_used": False}

    def fake_parse_compact_batch_item(raw: dict[str, Any], strategy: Any) -> JudgmentResult:
        return JudgmentResult(
            candidate_id=str(raw.get("candidate_id") or ""),
            strategy_mode="default_secretary",
            final_decision=FinalDecision(should_show_in_main_result=True, priority="medium"),
        )

    try:
        context_mod.read_candidate_context = fake_read_candidate_context
        judgment_service.build_anna_single_judgment_prompt = lambda *args, **kwargs: args[3].candidate.candidate_id
        llm_service.call_llm_json_safe = fake_call_llm_json_safe
        judgment_service._parse_compact_batch_item = fake_parse_compact_batch_item
        brief_flow._brief_persist_cards = lambda run_id, judgments: asyncio.sleep(0, result=len(judgments))
        brief_flow._save_run_checkpoint = lambda run_id: None

        run_id = "test_phase2_slice"
        make_run(run_id, candidate_count=3)
        await brief_flow._brief_run_phase2_slice(run_id, None)

        brief = brief_flow.MAIL_AGENT_RUNS[run_id]["brief"]
        progress = brief_flow.MAIL_AGENT_RUNS[run_id]["progress"]
        check("phase2 cursor advances the whole small batch", brief["phase2_cursor"], 3)
        check("all judgments persisted to run state", len(brief["judgments"]), 3)
        check("progress evaluated all items", progress["evaluated"], 3)
        check("stage reaches finalizing", brief_flow.MAIL_AGENT_RUNS[run_id]["stage"], "finalizing")
        check("context reads were concurrent", max_context_reads > 1, True)
        check("progress exposes real max tokens", progress["max_tokens"], 8000)
        check("progress exposes context read timing", "phase2_context_read_ms" in progress, True)
        check("progress uses README-compatible batch size", progress["phase2_llm_concurrency"], 4)
        check("progress exposes fixed single-candidate timeout", progress["phase2_sampling_timeout_s"], 60.0)

        resumed_run_id = "test_phase2_resume"
        existing = JudgmentResult(
            candidate_id="cand-0",
            strategy_mode="default_secretary",
            final_decision=FinalDecision(should_show_in_main_result=True, priority="medium"),
        )
        llm_calls.clear()
        make_run(resumed_run_id, candidate_count=3, judgments=[existing])
        await brief_flow._brief_run_phase2_slice(resumed_run_id, None)

        resumed_brief = brief_flow.MAIL_AGENT_RUNS[resumed_run_id]["brief"]
        resumed_progress = brief_flow.MAIL_AGENT_RUNS[resumed_run_id]["progress"]
        check("resume keeps all three judgments after rerun", len(resumed_brief["judgments"]), 3)
        check("resume finishes the run", brief_flow.MAIL_AGENT_RUNS[resumed_run_id]["stage"], "finalizing")
        check("resume reports all items evaluated", resumed_progress["evaluated"], 3)
        check("resume only evaluates remaining candidates", len(llm_calls), 2)
        check("resume skips already completed candidate id", "cand-0" in llm_calls, False)
    finally:
        context_mod.read_candidate_context = original_read_candidate_context
        judgment_service.build_anna_single_judgment_prompt = original_build_prompt
        llm_service.call_llm_json_safe = original_call_llm_json_safe
        judgment_service._parse_compact_batch_item = original_parse_compact_batch_item
        brief_flow._brief_persist_cards = original_persist_cards
        brief_flow._save_run_checkpoint = original_save_run_checkpoint
        brief_flow.MAIL_AGENT_RUNS.pop("test_phase2_slice", None)
        brief_flow.MAIL_AGENT_RUNS.pop("test_phase2_resume", None)


def main() -> None:
    asyncio.run(_run())
    print("PASS brief phase2 slice tests")


if __name__ == "__main__":
    main()
