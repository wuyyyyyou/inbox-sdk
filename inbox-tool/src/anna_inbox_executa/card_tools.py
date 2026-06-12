from __future__ import annotations

from anna_inbox_executa.common import *

def _sync_get_active_cards(arguments: dict[str, Any]) -> dict[str, Any]:
    """同步入口通过统一 storage_ops 读取 active cards 和 scan state。"""
    mailbox = str(arguments.get("mailbox", "")).strip()
    if not mailbox:
        return {"error": "mailbox is required"}
    offset = int(arguments.get("offset", 0))
    limit = int(arguments.get("limit", 50))

    from mail_agent.cards.service import cards_to_frontend
    from mail_agent.storage.ops import get_active_cards_page, get_scan_state
    from mail_agent.storage.types import ActiveCards as ActiveCardsType

    try:
        if mailbox.lower() == "all":
            from mail_agent.storage.ops import get_mailbox_registry

            async def _load_all_page() -> dict[str, Any]:
                registry = await get_mailbox_registry()
                entries = list(getattr(registry, "mailboxes", []) or [])
                selected_entries = [entry for entry in entries if getattr(entry, "selected", False)]
                scoped_entries = selected_entries or entries
                mailboxes = [
                    str(getattr(entry, "email", "") or "").strip().lower()
                    for entry in scoped_entries
                ]
                mailboxes = sorted(dict.fromkeys(item for item in mailboxes if item))
                page_limit = max(offset + limit, limit, 50) if limit > 0 else 50
                page_results = await asyncio.gather(
                    *(get_active_cards_page(item, 0, page_limit) for item in mailboxes),
                    return_exceptions=True,
                )
                all_cards: list[Any] = []
                total_cards = 0
                latest_updated = ""
                for result in page_results:
                    if isinstance(result, Exception) or not isinstance(result, dict):
                        continue
                    total_cards += int(result.get("total") or 0)
                    active_page = result.get("active")
                    latest_updated = max(latest_updated, getattr(active_page, "updated_at", "") or "")
                    for card in getattr(active_page, "cards", []) or []:
                        all_cards.append(card)
                priority_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
                all_cards.sort(key=lambda c: (priority_rank.get(str(getattr(c, "priority", "") or "").lower(), 0), getattr(c, "created_at", "") or ""), reverse=True)
                page_cards = all_cards[offset:offset + limit] if limit > 0 else all_cards
                state_results = await asyncio.gather(
                    *(get_scan_state(item) for item in mailboxes),
                    return_exceptions=True,
                )
                valid_states = [item for item in state_results if not isinstance(item, Exception)]
                return {
                    "active": ActiveCardsType(cards=list(page_cards), updated_at=latest_updated),
                    "total": total_cards,
                    "scan_state": {
                        "last_scan_ts": max((getattr(s, "last_scan_ts", "") for s in valid_states), default=""),
                        "last_message_internal_date": "",
                        "total_scans": max((getattr(s, "total_scans", 0) for s in valid_states), default=0),
                        "total_processed": max((getattr(s, "total_processed", 0) for s in valid_states), default=0),
                    },
                }

            loaded = _run_storage_query(_load_all_page(), timeout=45.0)
            active = loaded["active"]
            total = int(loaded["total"])
            scan_state = loaded["scan_state"]
            action_count = sum(1 for c in active.cards if c.status not in ("resolved", "dismissed") and c.user_action in ("reply", "review"))
        else:
            page_result = _run_storage_query(get_active_cards_page(mailbox, offset, limit), timeout=45.0)
            active = page_result["active"]
            total = page_result["total"]
            state = _run_storage_query(get_scan_state(mailbox), timeout=45.0)
            scan_state = {
                "last_scan_ts": getattr(state, "last_scan_ts", ""),
                "last_message_internal_date": getattr(state, "last_message_internal_date", ""),
                "total_scans": getattr(state, "total_scans", 0),
                "total_processed": getattr(state, "total_processed", 0),
            }
            action_count = 0  # computed below from active page for consistency; full count would need all cards
            action_count = sum(1 for c in active.cards if c.status not in ("resolved", "dismissed") and c.user_action in ("reply", "review"))

        cleanup_bundle = None
        cleanup_total = 0
        cleanup_has_more = False

        return {
            "cards": cards_to_frontend(active),
            "total": total,
            "count": len(active.cards),
            "has_more": (offset + limit) < total if limit > 0 else False,
            "offset": offset,
            "limit": limit,
            "action_count": action_count,
            "scan_state": scan_state,
            "cleanup_bundle": cleanup_bundle,
            "cleanup_total": cleanup_total,
            "cleanup_has_more": cleanup_has_more,
        }
    except Exception as exc:
        log(f"get_active_cards sync entry failed: {type(exc).__name__}: {exc}")
        return {
            "cards": [],
            "total": 0,
            "count": 0,
            "has_more": False,
            "cleanup_bundle": None,
            "cleanup_total": 0,
            "cleanup_has_more": False,
            "action_count": 0,
            "scan_state": {"total_scans": 0, "total_processed": 0, "last_scan_ts": "", "last_message_internal_date": ""},
            "error": f"{type(exc).__name__}: {exc}"[:200],
        }


def _sync_get_cleanup_bundle_page(arguments: dict[str, Any]) -> dict[str, Any]:
    mailbox = str(arguments.get("mailbox", "")).strip()
    if not mailbox:
        return {"error": "mailbox is required"}
    offset = max(0, int(arguments.get("offset", 0)))
    limit = max(1, min(int(arguments.get("limit", 100)), 100))
    from mail_agent.storage.ops import get_cleanup_bundle_page, get_mailbox_registry

    try:
        if mailbox.lower() == "all":
            async def _load_cleanup_all() -> dict[str, Any]:
                registry = await get_mailbox_registry()
                entries = list(getattr(registry, "mailboxes", []) or [])
                selected_entries = [entry for entry in entries if getattr(entry, "selected", False)]
                scoped_entries = selected_entries or entries
                mailboxes = [
                    str(getattr(entry, "email", "") or "").strip().lower()
                    for entry in scoped_entries
                ]
                mailboxes = sorted(dict.fromkeys(item for item in mailboxes if item))
                results = await asyncio.gather(
                    *(get_cleanup_bundle_page(item, offset, limit) for item in mailboxes),
                    return_exceptions=True,
                )
                items: list[dict[str, Any]] = []
                total_items = 0
                for result in results:
                    if isinstance(result, Exception) or not isinstance(result, dict):
                        continue
                    items.extend(result.get("items") or [])
                    total_items += int(result.get("total") or 0)
                return {"items": items[:limit], "total": total_items}

            page_result = _run_storage_query(_load_cleanup_all(), timeout=45.0)
        else:
            page_result = _run_storage_query(get_cleanup_bundle_page(mailbox, offset, limit), timeout=45.0)

        total = int(page_result.get("total") or 0)
        items = list(page_result.get("items") or [])
        return {
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": (offset + limit) < total,
        }
    except Exception as exc:
        log(f"get_cleanup_bundle_page failed: {type(exc).__name__}: {exc}")
        return {"items": [], "total": 0, "offset": offset, "limit": limit, "has_more": False, "error": str(exc)}


def _sync_get_run_history() -> dict[str, Any]:
    from mail_agent.storage.ops import get_run_history

    history = _run_storage_query(get_run_history(limit=20))
    return {"history": [serialize_value(entry) for entry in history]}


def get_mail_agent_run(run_id_arg: str) -> dict[str, Any]:
    run_id = str(run_id_arg or "")
    state = _get_run_state(run_id)
    if not state:
        return {"success": False, "error": "run not found", "run_id": run_id}
    status = state.get("status")
    result = _compact_run_result(state.get("result"))
    cards = None
    scan_state = None
    if status == "done":
        full_result = state.get("result")
        if isinstance(full_result, dict):
            cards = full_result.get("cards")
            # Provide scan_state from the pipeline result so the frontend
            # shows category tabs immediately without needing loadActiveCards.
            scan_state = full_result.get("scan_state")
            if not isinstance(scan_state, dict):
                scan_state = {"total_scans": 1, "total_processed": 0}
    return {
        "success": True,
        "run_id": run_id,
        "status": status,
        "stage": state.get("stage") or "",
        "progress": state.get("progress") or {},
        "partial": state.get("partial") or {},
        "warnings": state.get("warnings") or [],
        "started_at": state.get("started_at"),
        "updated_at": state.get("updated_at"),
        "error": state.get("error") or "",
        "needs_continue": bool(state.get("needs_continue")),
        "cards_added": int(state.get("cards_added") or 0),
        "cards_version": int((state.get("brief") or {}).get("cards_version") or state.get("cards_version") or 0),
        "result": result,
        "cards": cards,
        "scan_state": scan_state,
    }


async def _handle_summarize_background(run_id: str, arguments: dict[str, Any], invoke_id: str) -> None:
    MAIL_AGENT_RUNS[run_id]["status"] = "running"
    _save_run_checkpoint(run_id)

    mailbox = str(arguments.get("mailbox", "")).strip()
    card_id = str(arguments.get("card_id", "")).strip()
    try:
        from mail_agent.storage.ops import get_active_cards as storage_get_cards, set_active_cards
        from mail_agent.actions.service import summarize_thread
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            raise ValueError(f"Card {card_id} not found")
        _sampling = _build_sampling_for_run(arguments, invoke_id)
        result = await summarize_thread(card, mailbox, sampling_create_message=_sampling)
        summary = result.get("summary") if isinstance(result, dict) else {}
        if isinstance(summary, dict):
            card.thread_summary = json.dumps(summary, ensure_ascii=False)
            await set_active_cards(mailbox, cards)
        MAIL_AGENT_RUNS[run_id].update(status="done", result=result, updated_at=beijing_now())
    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update(status="failed", error=str(exc), updated_at=beijing_now())
    _save_run_checkpoint(run_id)


async def _handle_generate_draft_background(run_id: str, arguments: dict[str, Any], invoke_id: str) -> None:
    MAIL_AGENT_RUNS[run_id]["status"] = "running"
    _save_run_checkpoint(run_id)

    mailbox = str(arguments.get("mailbox", "")).strip()
    card_id = str(arguments.get("card_id", "")).strip()
    reply_mode = str(arguments.get("reply_mode", "reply_to_sender")).strip() or "reply_to_sender"
    current_draft = str(arguments.get("current_draft", "")).strip()
    revision_input = str(arguments.get("revision_input", "")).strip()
    user_answers = arguments.get("user_answers") if isinstance(arguments.get("user_answers"), dict) else None
    try:
        from mail_agent.storage.ops import get_active_cards as storage_get_cards, set_active_cards
        from mail_agent.actions.service import generate_draft_reply
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            raise ValueError(f"Card {card_id} not found")
        _sampling = _build_sampling_for_run(arguments, invoke_id)
        result = await generate_draft_reply(
            card, mailbox, reply_mode, sampling_create_message=_sampling,
            current_draft=current_draft, revision_input=revision_input,
            user_answers=user_answers,
        )
        draft_body = (result.get("draft") or {}).get("body", "") if isinstance(result, dict) else ""
        if draft_body:
            card.draft_reply = draft_body
            await set_active_cards(mailbox, cards)
        MAIL_AGENT_RUNS[run_id].update(status="done", result=result, updated_at=beijing_now())
    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update(status="failed", error=str(exc), updated_at=beijing_now())
    _save_run_checkpoint(run_id)

def _serialize_card_for_frontend(card: Any) -> dict[str, Any]:
    import re as _re

    def _dc(text: str) -> str:
        if not text:
            return ""
        text = _re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))) if int(m.group(1)) < 0x110000 else "?", text)
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        text = text.replace("&quot;", '"').replace("&#39;", "'")
        return text

    return {
        "id": card.card_id,
        "title": card.title,
        "summary": card.summary,
        "recommendation": card.recommendation,
        "label": card.label,
        "details": {
            "needs": card.details.needs,
            "latestActivity": card.details.latest_activity,
            "reviewed": card.details.reviewed,
            "mailbox": card.details.mailbox,
        },
        "original": {
            "source": card.original.source,
            "thread": card.original.thread,
            "from": card.original.from_addr,
            "to": card.original.to_addr,
            "time": card.original.time,
            "status": card.original.status,
            "body": _dc(card.original.body),
        },
        "actions": [
            {"id": a.id, "label": a.label, "primary": a.primary, "statusTitle": a.status_title, "status": a.status}
            for a in (card.actions or [])
        ],
        "status": card.status,
    }

__all__ = [name for name in globals() if not name.startswith("__")]
