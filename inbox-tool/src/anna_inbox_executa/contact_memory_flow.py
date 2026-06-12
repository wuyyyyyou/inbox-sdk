from __future__ import annotations

from anna_inbox_executa.common import *
from anna_inbox_executa.sampling_tools import *
from anna_inbox_executa.mailbox_tools import _discover_mailboxes

def _memory_mailboxes(arguments: dict[str, Any]) -> list[str]:
    raw_mailboxes = arguments.get("mailboxes")
    if isinstance(raw_mailboxes, list):
        mailboxes = [str(item).strip().lower() for item in raw_mailboxes if str(item).strip()]
        if mailboxes:
            return sorted(dict.fromkeys(mailboxes))
    mailbox = str(arguments.get("mailbox", "")).strip().lower()
    if mailbox and mailbox != "all":
        return [mailbox]
    from mail_agent.storage.ops import get_mailbox_registry

    registry = _run_storage_query(get_mailbox_registry())
    selected = [entry.email for entry in getattr(registry, "mailboxes", []) if getattr(entry, "selected", False)]
    if selected:
        return sorted(dict.fromkeys(str(item).strip().lower() for item in selected if str(item).strip()))
    discovered = _discover_mailboxes()
    return sorted(dict.fromkeys(str(item.get("email", "")).strip().lower() for item in discovered if str(item.get("email", "")).strip()))


def _sync_list_contact_memories(arguments: dict[str, Any]) -> dict[str, Any]:
    from mail_agent.contact_memory.manager import list_memory_summaries

    mailboxes = _memory_mailboxes(arguments)
    return _run_storage_query(list_memory_summaries(mailboxes))


def _sync_get_contact_memory(arguments: dict[str, Any]) -> dict[str, Any]:
    from mail_agent.contact_memory.manager import get_memory_detail

    return _run_storage_query(get_memory_detail(
        str(arguments.get("mailbox", "")).strip().lower(),
        str(arguments.get("contact_email", "")).strip().lower(),
    ))


def _sync_delete_contact_memory(arguments: dict[str, Any]) -> dict[str, Any]:
    from mail_agent.contact_memory.manager import delete_memory

    return _run_storage_query(delete_memory(
        str(arguments.get("mailbox", "")).strip().lower(),
        str(arguments.get("contact_email", "")).strip().lower(),
    ))


def _sync_clear_contact_memories(arguments: dict[str, Any]) -> dict[str, Any]:
    from mail_agent.contact_memory.manager import clear_memory

    return _run_storage_query(clear_memory(_memory_mailboxes(arguments)))


def _start_contact_memory_run(arguments: dict[str, Any]) -> dict[str, Any]:
    run_id = str(arguments.get("run_id") or "").strip()
    if not run_id or len(run_id) < 8:
        run_id = f"cm_{uuid.uuid4().hex[:12]}"
    MAIL_AGENT_RUNS[run_id] = {
        "run_id": run_id,
        "status": "queued",
        "stage": "contact_memory_prepare",
        "progress": {},
        "warnings": [],
        "started_at": beijing_now(),
        "updated_at": beijing_now(),
        "result": None,
        "error": "",
        "partial": {"_args": dict(arguments), "contact_memory": {"prepared": False, "items": [], "cursor": 0, "backfilled": 0, "skipped_old": 0, "failed": 0}},
        "needs_continue": True,
    }
    _save_run_checkpoint(run_id)
    return _public_run_view(MAIL_AGENT_RUNS[run_id])


def _parse_contact_memory_dt(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _contact_memory_before(a: datetime, b: datetime) -> bool:
    if a.tzinfo is None and b.tzinfo is not None:
        a = a.replace(tzinfo=b.tzinfo)
    elif a.tzinfo is not None and b.tzinfo is None:
        b = b.replace(tzinfo=a.tzinfo)
    return a < b


async def _prepare_contact_memory_run(run_id: str, arguments: dict[str, Any]) -> None:
    from mail_agent.contact_memory.indexer import parse_contact
    from mail_agent.storage.ops import get_active_cards

    state = MAIL_AGENT_RUNS[run_id]
    contact_state = state.setdefault("partial", {}).setdefault("contact_memory", {})
    since_dt = _parse_contact_memory_dt(str(arguments.get("since") or ""))
    items: list[dict[str, Any]] = []
    skipped_old = 0
    for mailbox in _memory_mailboxes(arguments):
        active = await get_active_cards(mailbox)
        for card in active.cards:
            if getattr(card, "card_type", "") == "cleanup_bundle":
                continue
            card_dt = _parse_contact_memory_dt(getattr(card, "created_at", "") or getattr(card, "updated_at", ""))
            if since_dt and card_dt and _contact_memory_before(card_dt, since_dt):
                skipped_old += 1
                continue
            contact_email, _ = parse_contact(getattr(card.original, "from_addr", ""))
            thread_id = getattr(card, "thread_id", "") or getattr(card, "message_id", "")
            card_id = getattr(card, "card_id", "")
            if not contact_email or not thread_id or not card_id:
                continue
            items.append({"mailbox": mailbox, "card_id": card_id, "thread_id": thread_id, "contact_email": contact_email})
    contact_state.update({
        "prepared": True,
        "items": items,
        "cursor": 0,
        "backfilled": 0,
        "skipped_old": skipped_old,
        "failed": 0,
    })
    if not items:
        state.update({
            "status": "done",
            "stage": "done",
            "needs_continue": False,
            "progress": {"current": 0, "total": 0, "skipped_old": skipped_old},
            "result": {"ok": True, "backfilled": 0, "skipped_old": skipped_old, "failed": 0},
            "updated_at": beijing_now(),
        })
    else:
        state.update({
            "status": "running",
            "stage": "contact_memory",
            "needs_continue": True,
            "progress": {"current": 0, "total": len(items), "skipped_old": skipped_old},
            "updated_at": beijing_now(),
        })
    _save_run_checkpoint(run_id)


async def _backfill_contact_memory_target(item: dict[str, Any], sampling_create_message: Any) -> None:
    from mail_agent.contact_memory.indexer import ingest_card_event
    from mail_agent.storage.ops import get_active_cards

    mailbox = str(item.get("mailbox") or "")
    card_id = str(item.get("card_id") or "")
    active = await get_active_cards(mailbox)
    card = next((card for card in active.cards if getattr(card, "card_id", "") == card_id), None)
    if card is None:
        raise ValueError(f"Card not found for contact memory backfill: {card_id}")
    await ingest_card_event(
        mailbox,
        card,
        event_type="card_created",
        source="brief_card_background",
        user_action="backfill",
        sampling_create_message=sampling_create_message,
    )


async def _continue_contact_memory_run_async(arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    run_id = str(arguments.get("run_id") or "")
    state = _get_run_state(run_id)
    if not state:
        return {"success": False, "run_id": run_id, "error": "run not found"}
    saved_args = (state.get("partial") or {}).get("_args") or {}
    saved_args.update({key: value for key, value in arguments.items() if key != "run_id" and value not in (None, "")})
    state.setdefault("partial", {})["_args"] = saved_args
    _apply_storage_provider(saved_args)
    if state.get("status") == "done":
        state["needs_continue"] = False
        return _public_run_view(state)
    try:
        contact_state = state.setdefault("partial", {}).setdefault("contact_memory", {})
        if not contact_state.get("prepared"):
            await _prepare_contact_memory_run(run_id, saved_args)
            state = MAIL_AGENT_RUNS[run_id]
            contact_state = state.setdefault("partial", {}).setdefault("contact_memory", {})
            if state.get("status") == "done":
                return _public_run_view(state)

        ai_provider = str(saved_args.get("ai_provider", "anna-llm") or "anna-llm")
        sampling_fn = _build_sampling_for_run({"ai_provider": ai_provider}, invoke_id)
        items = list(contact_state.get("items") or [])
        cursor = int(contact_state.get("cursor") or 0)
        batch_limit = max(1, min(int(saved_args.get("batch_limit") or 1), 2))
        processed = 0
        while cursor < len(items) and processed < batch_limit:
            state.update({
                "status": "running",
                "stage": "contact_memory",
                "needs_continue": True,
                "progress": {
                    "current": cursor,
                    "total": len(items),
                    "backfilled": int(contact_state.get("backfilled") or 0),
                    "failed": int(contact_state.get("failed") or 0),
                    "skipped_old": int(contact_state.get("skipped_old") or 0),
                },
                "updated_at": beijing_now(),
            })
            _save_run_checkpoint(run_id)
            item = items[cursor]
            try:
                await _backfill_contact_memory_target(item, sampling_fn)
                contact_state["backfilled"] = int(contact_state.get("backfilled") or 0) + 1
            except Exception as exc:
                contact_state["failed"] = int(contact_state.get("failed") or 0) + 1
                warnings = state.setdefault("warnings", [])
                warnings.append({"stage": "contact_memory_error", "at": beijing_now(), "detail": {"card_id": item.get("card_id", ""), "error": str(exc)[:240]}})
                state["warnings"] = warnings[-10:]
            cursor += 1
            processed += 1
            contact_state["cursor"] = cursor

        if cursor >= len(items):
            result = {
                "ok": True,
                "backfilled": int(contact_state.get("backfilled") or 0),
                "skipped_old": int(contact_state.get("skipped_old") or 0),
                "failed": int(contact_state.get("failed") or 0),
                "total": len(items),
            }
            state.update({
                "status": "done",
                "stage": "done",
                "needs_continue": False,
                "progress": {"current": len(items), "total": len(items), **result},
                "result": result,
                "updated_at": beijing_now(),
            })
        else:
            state.update({
                "status": "running",
                "stage": "contact_memory",
                "needs_continue": True,
                "progress": {
                    "current": cursor,
                    "total": len(items),
                    "backfilled": int(contact_state.get("backfilled") or 0),
                    "failed": int(contact_state.get("failed") or 0),
                    "skipped_old": int(contact_state.get("skipped_old") or 0),
                },
                "updated_at": beijing_now(),
            })
        _save_run_checkpoint(run_id)
        return _public_run_view(state)
    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update({
            "status": "failed",
            "stage": "failed",
            "updated_at": beijing_now(),
            "error": str(exc),
            "needs_continue": False,
        })
        _save_run_checkpoint(run_id)
        return _public_run_view(MAIL_AGENT_RUNS[run_id])

__all__ = [name for name in globals() if not name.startswith("__")]
