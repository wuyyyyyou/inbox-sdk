"""邮箱同步 P0：180 天优先 metadata、后台无硬顶回填、边界字段、Watch 注册。

设计要点（与产品对齐）：
- 首次优先同步 180 天邮件元数据（主题/参与人/时间/标签/snippet/附件名）；
- 更早历史在后台继续回填，除非用户明确要求否则不设硬顶；
- 正文/附件解析完整度字段预留（P1/P2），本阶段默认 False；
- 增量：history.list；Watch 在配置了 Pub/Sub topic 时注册，否则依赖前端轮询 sync；
- AI 默认只读缓存；边界字段供「无法判断范围外是否存在」话术。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
from typing import Any

_logger = logging.getLogger("mail_agent.gmail.sync")

# 首次可检索工作集：180 天 metadata
INITIAL_PRIORITY_DAYS = 180
# 单次 invoke 内最多拉取的新摘要条数，保证首屏不卡死
PRIORITY_BATCH_PER_INVOKE = 200
# 后台回填每 tick 条数
BACKFILL_BATCH_PER_TICK = 100
# 单次 messages.list 目标上限（Gmail 分页上限由 search_gmail 处理）
_SEARCH_PAGE_CAP = 500

_backfill_lock = threading.Lock()
_backfill_running: set[str] = set()


def _emit_sync_log(message: str, *args: Any) -> None:
    """同时写入 logging 与 Executa stderr，确保生产环境能看到 INFO 进度。"""
    _logger.info(message, *args)
    try:
        rendered = message % args
        from anna_inbox_executa.common import log

        log(rendered)
    except Exception:
        # 直接运行同步模块或测试时 common 可能不可用，logging 仍保留进度记录。
        pass


def _log_sync_phase(
    *,
    phase: str,
    fetched: int = 0,
    complete: bool = False,
    body_pending: int | None = None,
    attachment_pending: int | None = None,
    cache_total: int | None = None,
    has_next_page: bool | None = None,
    page_token_present: bool | None = None,
    tick: int | None = None,
    error_type: str | None = None,
) -> None:
    """记录不含邮箱、查询词、邮件 ID 和凭据的同步阶段进度。"""
    message = "gmail_sync_phase phase=%s fetched=%s complete=%s"
    args: list[Any] = [phase, fetched, complete]
    if cache_total is not None:
        message += " cache_total=%s"
        args.append(cache_total)
    if has_next_page is not None:
        message += " has_next_page=%s"
        args.append(has_next_page)
    if page_token_present is not None:
        message += " page_token_present=%s"
        args.append(page_token_present)
    if tick is not None:
        message += " tick=%s"
        args.append(tick)
    if body_pending is not None or attachment_pending is not None:
        message += " body_pending=%s attachment_pending=%s"
        args.extend([body_pending if body_pending is not None else 0, attachment_pending if attachment_pending is not None else 0])
    if error_type is not None:
        message += " error_type=%s"
        args.append(error_type)
    _emit_sync_log(message, *args)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ms_to_iso(ms: int) -> str:
    if ms <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return ""


def _before_date_for_days(days: int) -> str:
    """Gmail before: 使用 YYYY/MM/DD（不含该日）。"""
    day = datetime.now(timezone.utc).date() - timedelta(days=max(1, int(days)))
    return day.strftime("%Y/%m/%d")


def configured_sync_range() -> dict[str, Any]:
    """当前产品配置的同步策略（无用户硬顶时 backfill 为 unbounded）。"""
    return {
        "initial_days": INITIAL_PRIORITY_DAYS,
        "backfill": "unbounded",
        "body_sync": "on_demand_p1",
        "attachment_parse": "metadata_only_p0",
    }


def empty_boundary(mailbox: str = "") -> dict[str, Any]:
    """无缓存时的边界默认值。"""
    boundary = {
        "mailbox": mailbox,
        "earliest_indexed_at": "",
        "latest_indexed_at": "",
        "initial_sync_complete": False,
        "body_sync_complete": False,
        "attachment_sync_complete": False,
        "backfill_complete": False,
        "last_history_id": "",
        "configured_sync_range": configured_sync_range(),
        "cache_total": 0,
        "priority_days": INITIAL_PRIORITY_DAYS,
        "watch_status": "unknown",
        "updated_at": _utc_now_iso(),
    }
    boundary["sync_stage"] = _sync_stage_from_state(boundary)
    return boundary


def _sync_stage_from_state(state: dict[str, Any]) -> str:
    """根据已持久化的同步状态计算对外同步阶段，不读取缓存或外部数据。"""
    if not bool(state.get("initial_sync_complete")):
        return "priority_metadata"
    if not bool(state.get("backfill_complete")):
        return "backfill_metadata"
    if not bool(state.get("body_sync_complete")) or not bool(state.get("attachment_sync_complete")):
        return "content_preprocess"
    watch_status = str(state.get("watch_status") or "unknown")
    if watch_status.startswith("error:"):
        return "watch_error"
    if watch_status in {"", "unknown"}:
        return "watch_setup"
    return "ready"


def _compute_index_bounds(messages: list[dict[str, Any]]) -> tuple[str, str, int, int]:
    """从缓存消息计算 earliest/latest ISO 与 internal_date 毫秒。"""
    earliest_ms = 0
    latest_ms = 0
    for item in messages:
        if not isinstance(item, dict):
            continue
        try:
            ts = int(item.get("internal_date") or 0)
        except (TypeError, ValueError):
            ts = 0
        if ts <= 0:
            continue
        if earliest_ms == 0 or ts < earliest_ms:
            earliest_ms = ts
        if ts > latest_ms:
            latest_ms = ts
    return _ms_to_iso(earliest_ms), _ms_to_iso(latest_ms), earliest_ms, latest_ms


def read_sync_state(mailbox: str) -> dict[str, Any]:
    from .adapter import _read_history_sync_state, normalize_mailbox

    return dict(_read_history_sync_state(normalize_mailbox(mailbox)) or {})


def write_sync_state(mailbox: str, patch: dict[str, Any]) -> dict[str, Any]:
    """合并写入同步状态；保留 history_id 与边界字段。"""
    from .adapter import _history_sync_lock, _read_history_sync_state, _write_history_sync_state, normalize_mailbox

    normalized = normalize_mailbox(mailbox)
    with _history_sync_lock(normalized):
        previous = _read_history_sync_state(normalized)
        merged = {k: v for k, v in previous.items() if k != "_etag"}
        merged.update({k: v for k, v in patch.items() if k != "_etag"})
        merged["schema_version"] = 2
        merged["updated_at"] = _utc_now_iso()
        # 兼容旧字段：scope_days 记录优先窗口
        if "scope_days" not in merged:
            merged["scope_days"] = INITIAL_PRIORITY_DAYS
        if "configured_sync_range" not in merged:
            merged["configured_sync_range"] = configured_sync_range()
        _write_history_sync_state(
            normalized,
            merged,
            if_match=str(previous.get("_etag") or "") or None,
        )
        return merged


def reset_mailbox_sync_state(mailbox: str) -> dict[str, Any]:
    """清空邮件缓存后重置同步游标与分页进度。

    缓存清空后保留旧 historyId 会让 History 增量误认为已有完整基线，导致首屏永久
    为空。因此 cache reset 必须连同 priority/backfill 状态一并复位。
    """
    from .adapter import _history_sync_lock, _read_history_sync_state, _write_history_sync_state, normalize_mailbox

    normalized = normalize_mailbox(mailbox)
    with _history_sync_lock(normalized):
        previous = _read_history_sync_state(normalized)
        state = empty_boundary(normalized)
        # 仅保留 Watch 配置痕迹，实际 watch 会在下一次 list/sync 自动续订。
        state["watch_status"] = str(previous.get("watch_status") or "unknown")
        _write_history_sync_state(
            normalized,
            state,
            if_match=str(previous.get("_etag") or "") or None,
        )
    return state


def refresh_boundary_from_cache(mailbox: str, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """根据当前缓存重算 earliest/latest/cache_total 并写回状态。"""
    from .adapter import list_messages, normalize_mailbox

    normalized = normalize_mailbox(mailbox)
    messages = [item for item in list_messages(normalized) if isinstance(item, dict)]
    earliest, latest, _, _ = _compute_index_bounds(messages)
    state = read_sync_state(normalized)
    patch: dict[str, Any] = {
        "history_id": str(state.get("history_id") or state.get("last_history_id") or ""),
        "last_history_id": str(state.get("history_id") or state.get("last_history_id") or ""),
        "scope_days": int(state.get("scope_days") or INITIAL_PRIORITY_DAYS),
        "earliest_indexed_at": earliest,
        "latest_indexed_at": latest,
        "cache_total": len(messages),
        "initial_sync_complete": bool(state.get("initial_sync_complete")),
        "backfill_complete": bool(state.get("backfill_complete")),
        "body_sync_complete": bool(state.get("body_sync_complete")),
        "attachment_sync_complete": bool(state.get("attachment_sync_complete")),
        "configured_sync_range": state.get("configured_sync_range") or configured_sync_range(),
        "backfill_page_token": str(state.get("backfill_page_token") or ""),
        "priority_page_token": str(state.get("priority_page_token") or ""),
        "watch_status": str(state.get("watch_status") or "unknown"),
        "watch_expiration": str(state.get("watch_expiration") or ""),
        "watch_resource_id": str(state.get("watch_resource_id") or ""),
    }
    if extra:
        patch.update(extra)
    return write_sync_state(normalized, patch)


def get_mailbox_sync_boundary(mailbox: str) -> dict[str, Any]:
    """对外暴露的数据边界字段（AI / UI 共用）。"""
    from .adapter import list_messages, normalize_mailbox

    try:
        normalized = normalize_mailbox(mailbox)
    except ValueError:
        return empty_boundary(str(mailbox or ""))
    state = read_sync_state(normalized)
    messages = [item for item in list_messages(normalized) if isinstance(item, dict)]
    earliest, latest, _, _ = _compute_index_bounds(messages)
    if not earliest:
        earliest = str(state.get("earliest_indexed_at") or "")
    if not latest:
        latest = str(state.get("latest_indexed_at") or "")
    boundary = {
        "mailbox": normalized,
        "earliest_indexed_at": earliest,
        "latest_indexed_at": latest,
        "initial_sync_complete": bool(state.get("initial_sync_complete")),
        "body_sync_complete": bool(state.get("body_sync_complete")),
        "attachment_sync_complete": bool(state.get("attachment_sync_complete")),
        "backfill_complete": bool(state.get("backfill_complete")),
        "last_history_id": str(state.get("history_id") or state.get("last_history_id") or ""),
        "configured_sync_range": state.get("configured_sync_range") or configured_sync_range(),
        "cache_total": len(messages),
        "priority_days": INITIAL_PRIORITY_DAYS,
        "watch_status": str(state.get("watch_status") or "unknown"),
        "updated_at": str(state.get("updated_at") or _utc_now_iso()),
    }
    boundary["sync_stage"] = _sync_stage_from_state(boundary)
    return boundary


def boundary_honesty_note(boundary: dict[str, Any], language: str = "zh") -> str:
    """生成范围外问题应使用的诚实说明（代码侧事实，不靠模型编造）。"""
    earliest = str(boundary.get("earliest_indexed_at") or "").strip()
    latest = str(boundary.get("latest_indexed_at") or "").strip()
    initial_done = bool(boundary.get("initial_sync_complete"))
    backfill_done = bool(boundary.get("backfill_complete"))
    total = int(boundary.get("cache_total") or 0)
    days = int(boundary.get("priority_days") or INITIAL_PRIORITY_DAYS)
    if language == "zh":
        if not initial_done:
            return (
                f"当前优先窗口（约 {days} 天）元数据同步尚未完成"
                + (f"，已索引 {total} 封" if total else "")
                + (f"，已覆盖 {earliest or '…'} ~ {latest or '…'}" if earliest or latest else "")
                + "。范围外是否存在邮件尚无法判断。"
            )
        if not backfill_done:
            return (
                f"当前本地已索引约 {total} 封邮件"
                + (f"，最早 {earliest}" if earliest else "")
                + (f"，最晚 {latest}" if latest else "")
                + f"。优先 {days} 天窗口已就绪，更早历史仍在后台回填；"
                "若目标时间早于已索引最早时间，我无法判断邮箱中是否存在相关邮件。"
            )
        return (
            f"当前本地已完整回填索引约 {total} 封"
            + (f"，最早 {earliest}" if earliest else "")
            + (f"，最晚 {latest}" if latest else "")
            + "。若问题时间仍早于 earliest_indexed_at，则无法从本地判断是否存在。"
        )
    # en
    if not initial_done:
        return (
            f"Priority metadata sync (~{days}d) is still running"
            + (f" ({total} indexed)" if total else "")
            + (f", covering {earliest or '…'} ~ {latest or '…'}" if earliest or latest else "")
            + ". I cannot judge mail outside the indexed range yet."
        )
    if not backfill_done:
        return (
            f"Local index has ~{total} messages"
            + (f", earliest {earliest}" if earliest else "")
            + (f", latest {latest}" if latest else "")
            + f". The {days}d priority window is ready; older history is still backfilling. "
            "If your target is older than earliest_indexed_at, I cannot prove presence or absence."
        )
    return (
        f"Local index is fully backfilled (~{total})"
        + (f", earliest {earliest}" if earliest else "")
        + (f", latest {latest}" if latest else "")
        + "."
    )


def _priority_query() -> str:
    return f"in:anywhere -in:chats newer_than:{INITIAL_PRIORITY_DAYS}d"


def _backfill_query() -> str:
    return f"in:anywhere -in:chats before:{_before_date_for_days(INITIAL_PRIORITY_DAYS)}"


def _fetch_metadata_page(
    mailbox: str,
    *,
    query: str,
    page_token: str,
    batch_limit: int,
) -> tuple[list[str], str]:
    """分页读取 Gmail message id，并把该页 metadata 合并进本地缓存。

    ``live_search_metadata_and_cache`` 适合即时查询，但不暴露 page token；首次
    基线和无限历史回填必须保存 Gmail 的 page token，才能在 Executa 重启后从上次
    位置继续，而不是反复扫描同一批最新命中。
    """
    from .adapter import (
        SUMMARY_FETCH_MAX_WORKERS,
        fetch_message_summary,
        get_access_token,
        gmail_request,
        read_cache,
        write_index,
    )

    requested = max(1, min(int(batch_limit), 100))
    params: dict[str, Any] = {
        "q": query,
        "maxResults": requested,
        "fields": "messages/id,nextPageToken",
        "includeSpamTrash": "true",
    }
    if page_token:
        params["pageToken"] = page_token
    # 一页最多 100 个 metadata 请求：本页只取一次 Connected Accounts token，再显式
    # 传给 worker，避免每封邮件都触发一次 Host reverse-RPC。
    access_token = get_access_token(mailbox)
    payload = gmail_request(
        mailbox,
        "/users/me/messages",
        params,
        access_token=access_token,
        request_timeout_seconds=20.0,
    )
    refs = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    message_ids = [str(item.get("id") or "") for item in refs if isinstance(item, dict) and item.get("id")]
    next_token = str(payload.get("nextPageToken") or "")
    if not message_ids:
        return [], next_token

    cache = read_cache(mailbox)
    by_id = {
        str(item.get("id") or ""): dict(item)
        for item in cache.get("messages") or []
        if isinstance(item, dict) and item.get("id")
    }
    missing_ids = [message_id for message_id in message_ids if message_id not in by_id]
    if missing_ids:
        worker_context = copy_context()
        with ThreadPoolExecutor(max_workers=SUMMARY_FETCH_MAX_WORKERS) as pool:
            futures = {
                pool.submit(
                    worker_context.copy().run,
                    fetch_message_summary,
                    mailbox,
                    message_id,
                    access_token=access_token,
                    strict=True,
                ): message_id
                for message_id in missing_ids
            }
            for future in as_completed(futures):
                summary = future.result()
                if not summary:
                    raise RuntimeError("Gmail message metadata request returned no data")
                by_id[str(summary.get("id") or futures[future])] = summary
    write_index(mailbox, sorted(by_id.values(), key=lambda item: int(item.get("internal_date") or 0), reverse=True))
    return message_ids, next_token


def progress_priority_metadata_sync(
    mailbox: str,
    *,
    batch_limit: int = PRIORITY_BATCH_PER_INVOKE,
    profile_history_id: str = "",
) -> dict[str, Any]:
    """推进 180 天 metadata 基线；可多次调用直至 initial_sync_complete。"""
    from .adapter import normalize_mailbox

    normalized = normalize_mailbox(mailbox)
    state = read_sync_state(normalized)
    if bool(state.get("initial_sync_complete")):
        boundary = refresh_boundary_from_cache(normalized)
        _log_sync_phase(
            phase="priority_metadata",
            fetched=0,
            complete=True,
            cache_total=int(boundary.get("cache_total") or 0),
            has_next_page=False,
            page_token_present=bool(state.get("priority_page_token")),
        )
        return {
            "mailbox": normalized,
            "mode": "priority_done",
            "fetched": 0,
            "initial_sync_complete": True,
            "boundary": get_mailbox_sync_boundary(normalized),
            "state": boundary,
        }

    query = _priority_query()
    try:
        cap = max(1, min(int(batch_limit or PRIORITY_BATCH_PER_INVOKE), _SEARCH_PAGE_CAP))
    except (TypeError, ValueError):
        cap = PRIORITY_BATCH_PER_INVOKE

    # 保存 Gmail page token，以便被中断后继续基线而不重扫。
    page_token = str(state.get("priority_page_token") or "")
    fetched_ids: list[str] = []
    remaining = cap
    next_token = page_token
    while remaining > 0:
        page_ids, next_token = _fetch_metadata_page(
            normalized,
            query=query,
            page_token=next_token,
            batch_limit=remaining,
        )
        fetched_ids.extend(page_ids)
        remaining -= len(page_ids)
        if not next_token or not page_ids:
            break
    # page token 耗尽才算 180 天 priority 基线完成。
    complete = not next_token

    history_id = str(profile_history_id or state.get("history_id") or "")
    extra: dict[str, Any] = {
        "history_id": history_id,
        "last_history_id": history_id,
        "scope_days": INITIAL_PRIORITY_DAYS,
        "configured_sync_range": configured_sync_range(),
        "priority_page_token": "" if complete else next_token,
        "initial_sync_complete": complete,
        "body_sync_complete": False,
        "attachment_sync_complete": False,
    }
    if complete and not bool(state.get("backfill_started")):
        extra["backfill_complete"] = False
        extra["backfill_page_token"] = ""

    refresh_boundary_from_cache(normalized, extra=extra)
    boundary = get_mailbox_sync_boundary(normalized)
    _log_sync_phase(
        phase="priority_metadata",
        fetched=len(fetched_ids),
        complete=complete,
        cache_total=int(boundary.get("cache_total") or 0),
        has_next_page=bool(next_token),
        page_token_present=bool(page_token),
    )
    return {
        "mailbox": normalized,
        "mode": "priority",
        "query": query,
        "fetched": len(fetched_ids),
        "new_count": len(fetched_ids),
        "initial_sync_complete": complete,
        "boundary": get_mailbox_sync_boundary(normalized),
    }


def progress_backfill_metadata_sync(
    mailbox: str,
    *,
    batch_limit: int = BACKFILL_BATCH_PER_TICK,
) -> dict[str, Any]:
    """后台回填 180 天之前的邮件 metadata；无硬顶，靠 Gmail 分页耗尽结束。"""
    from .adapter import normalize_mailbox

    normalized = normalize_mailbox(mailbox)
    state = read_sync_state(normalized)
    if not bool(state.get("initial_sync_complete")):
        boundary = get_mailbox_sync_boundary(normalized)
        _log_sync_phase(
            phase="backfill_metadata",
            fetched=0,
            complete=False,
            cache_total=int(boundary.get("cache_total") or 0),
            has_next_page=False,
            page_token_present=bool(state.get("backfill_page_token")),
        )
        return {
            "mailbox": normalized,
            "mode": "backfill_wait_priority",
            "fetched": 0,
            "backfill_complete": False,
            "boundary": get_mailbox_sync_boundary(normalized),
        }
    if bool(state.get("backfill_complete")):
        boundary = get_mailbox_sync_boundary(normalized)
        _log_sync_phase(
            phase="backfill_metadata",
            fetched=0,
            complete=True,
            cache_total=int(boundary.get("cache_total") or 0),
            has_next_page=False,
            page_token_present=bool(state.get("backfill_page_token")),
        )
        return {
            "mailbox": normalized,
            "mode": "backfill_done",
            "fetched": 0,
            "backfill_complete": True,
            "boundary": get_mailbox_sync_boundary(normalized),
        }

    query = _backfill_query()
    try:
        cap = max(1, min(int(batch_limit or BACKFILL_BATCH_PER_TICK), _SEARCH_PAGE_CAP))
    except (TypeError, ValueError):
        cap = BACKFILL_BATCH_PER_TICK

    page_token = str(state.get("backfill_page_token") or "")
    fetched_ids: list[str] = []
    remaining = cap
    next_token = page_token
    while remaining > 0:
        page_ids, next_token = _fetch_metadata_page(
            normalized,
            query=query,
            page_token=next_token,
            batch_limit=remaining,
        )
        fetched_ids.extend(page_ids)
        remaining -= len(page_ids)
        if not next_token or not page_ids:
            break
    # 无硬顶：仅 Gmail 分页耗尽时标为完成。
    complete = not next_token
    extra = {
        "backfill_started": True,
        "backfill_complete": complete,
        "backfill_page_token": "" if complete else next_token,
        "scope_days": INITIAL_PRIORITY_DAYS,
        "configured_sync_range": configured_sync_range(),
        "history_id": str(state.get("history_id") or state.get("last_history_id") or ""),
        "last_history_id": str(state.get("history_id") or state.get("last_history_id") or ""),
        "initial_sync_complete": True,
        "body_sync_complete": bool(state.get("body_sync_complete")),
        "attachment_sync_complete": bool(state.get("attachment_sync_complete")),
    }
    refresh_boundary_from_cache(normalized, extra=extra)
    boundary = get_mailbox_sync_boundary(normalized)
    _log_sync_phase(
        phase="backfill_metadata",
        fetched=len(fetched_ids),
        complete=complete,
        cache_total=int(boundary.get("cache_total") or 0),
        has_next_page=bool(next_token),
        page_token_present=bool(page_token),
    )
    return {
        "mailbox": normalized,
        "mode": "backfill",
        "query": query,
        "fetched": len(fetched_ids),
        "new_count": len(fetched_ids),
        "backfill_complete": complete,
        "boundary": get_mailbox_sync_boundary(normalized),
    }


def schedule_background_backfill(mailbox: str) -> bool:
    """在守护线程中推进回填；同邮箱不重复起线程。"""
    from .adapter import normalize_mailbox

    try:
        normalized = normalize_mailbox(mailbox)
    except ValueError:
        return False
    with _backfill_lock:
        if normalized in _backfill_running:
            return False
        state = read_sync_state(normalized)
        if not bool(state.get("initial_sync_complete")) or bool(state.get("backfill_complete")):
            return False
        _backfill_running.add(normalized)

    def _worker() -> None:
        ticks = 0
        completed = False
        _emit_sync_log("gmail_sync_background_start max_ticks=%s", 5)
        try:
            # 多 tick，避免一次占满；进程退出则中断，下次 sync 再续。
            for tick in range(1, 6):
                ticks = tick
                result = progress_backfill_metadata_sync(normalized)
                boundary = result.get("boundary") if isinstance(result.get("boundary"), dict) else {}
                _log_sync_phase(
                    phase="backfill_background_tick",
                    fetched=int(result.get("fetched") or 0),
                    complete=bool(result.get("backfill_complete")),
                    cache_total=int(boundary.get("cache_total") or 0),
                    has_next_page=not bool(result.get("backfill_complete")),
                    tick=tick,
                )
                if result.get("backfill_complete"):
                    completed = True
                    break
                if int(result.get("new_count") or 0) == 0 and int(result.get("fetched") or 0) == 0:
                    break
                time.sleep(0.05)
        except Exception as exc:
            _logger.warning("background_backfill_failed error_type=%s", type(exc).__name__)
        finally:
            _emit_sync_log("gmail_sync_background_end ticks=%s complete=%s", ticks, completed)
            with _backfill_lock:
                _backfill_running.discard(normalized)

    thread = threading.Thread(target=_worker, name=f"gmail-backfill-{normalized[:24]}", daemon=True)
    thread.start()
    return True


def schedule_background_sync(mailbox: str, *, profile_history_id: str = "") -> bool:
    """后台续跑 priority 基线及更早历史回填。

    Executa 不是常驻 worker：每次进程存活时只推进有限批次；page token 持久化，
    下次打开 App 或 History 轮询时从上次位置继续。这样不会阻塞首屏，也不会丢进度。
    """
    from .adapter import normalize_mailbox

    try:
        normalized = normalize_mailbox(mailbox)
    except ValueError:
        return False
    with _backfill_lock:
        if normalized in _backfill_running:
            return False
        _backfill_running.add(normalized)

    def _record_content_progress(result: dict[str, Any]) -> None:
        """把正文/附件后台处理进度写入同步边界字段。"""
        body_pending = int(result.get("body_pending") or 0)
        attachment_pending = int(result.get("attachment_pending") or 0)
        state = read_sync_state(normalized)
        write_sync_state(normalized, {
            **{key: value for key, value in state.items() if key != "_etag"},
            "body_sync_complete": body_pending == 0,
            "attachment_sync_complete": attachment_pending == 0,
        })
        _log_sync_phase(
            phase="content_preprocess",
            fetched=0,
            complete=body_pending == 0 and attachment_pending == 0,
            body_pending=body_pending,
            attachment_pending=attachment_pending,
        )

    def _worker() -> None:
        ticks = 0
        completed = False
        _emit_sync_log("gmail_sync_background_start max_ticks=%s", 5)
        try:
            for tick in range(1, 6):
                ticks = tick
                state = read_sync_state(normalized)
                if not bool(state.get("initial_sync_complete")):
                    result = progress_priority_metadata_sync(
                        normalized,
                        profile_history_id=profile_history_id,
                    )
                    boundary = result.get("boundary") if isinstance(result.get("boundary"), dict) else {}
                    _log_sync_phase(
                        phase="priority_background_tick",
                        fetched=int(result.get("fetched") or 0),
                        complete=bool(result.get("initial_sync_complete")),
                        cache_total=int(boundary.get("cache_total") or 0),
                        has_next_page=not bool(result.get("initial_sync_complete")),
                        tick=tick,
                    )
                    if not result.get("initial_sync_complete"):
                        # priority metadata 写入后同步补少量正文/附件派生内容；不阻塞首屏。
                        try:
                            from .adapter import preprocess_cached_content_batch

                            _record_content_progress(preprocess_cached_content_batch(normalized, limit=2))
                        except Exception:
                            pass
                        time.sleep(0.05)
                        continue
                result = progress_backfill_metadata_sync(normalized)
                boundary = result.get("boundary") if isinstance(result.get("boundary"), dict) else {}
                _log_sync_phase(
                    phase="backfill_background_tick",
                    fetched=int(result.get("fetched") or 0),
                    complete=bool(result.get("backfill_complete")),
                    cache_total=int(boundary.get("cache_total") or 0),
                    has_next_page=not bool(result.get("backfill_complete")),
                    tick=tick,
                )
                try:
                    from .adapter import preprocess_cached_content_batch

                    _record_content_progress(preprocess_cached_content_batch(normalized, limit=2))
                except Exception:
                    pass
                if result.get("backfill_complete"):
                    completed = True
                    break
                time.sleep(0.05)
        except Exception as exc:
            _logger.warning("background_sync_failed error_type=%s", type(exc).__name__)
        finally:
            _emit_sync_log("gmail_sync_background_end ticks=%s complete=%s", ticks, completed)
            with _backfill_lock:
                _backfill_running.discard(normalized)

    thread = threading.Thread(target=_worker, name=f"gmail-sync-{normalized[:24]}", daemon=True)
    thread.start()
    return True


def ensure_gmail_watch(mailbox: str) -> dict[str, Any]:
    """注册 Gmail users.watch（需 ANNA_INBOX_GMAIL_WATCH_TOPIC=projects/.../topics/...）。

    无 topic 时返回 skipped，依赖前端 auto_sync 轮询 history.list。
    Host 收到 Pub/Sub 推送后应调用 sync_inbox_cache。
    """
    from .adapter import gmail_request, normalize_mailbox

    normalized = normalize_mailbox(mailbox)
    topic = str(os.environ.get("ANNA_INBOX_GMAIL_WATCH_TOPIC") or "").strip()
    if not topic:
        write_sync_state(normalized, {
            **{k: v for k, v in read_sync_state(normalized).items() if k != "_etag"},
            "watch_status": "skipped_no_topic",
        })
        return {
            "mailbox": normalized,
            "status": "skipped",
            "reason": "ANNA_INBOX_GMAIL_WATCH_TOPIC not set",
            "watch_status": "skipped_no_topic",
        }

    state = read_sync_state(normalized)
    # 未过期则跳过续订（Gmail watch 最长约 7 天）
    exp_raw = str(state.get("watch_expiration") or "")
    try:
        if exp_raw.isdigit() and int(exp_raw) > int(time.time() * 1000) + 3600_000:
            return {
                "mailbox": normalized,
                "status": "active",
                "watch_status": "active",
                "expiration": exp_raw,
                "resource_id": str(state.get("watch_resource_id") or ""),
            }
    except (TypeError, ValueError):
        pass

    try:
        payload = gmail_request(
            normalized,
            "/users/me/watch",
            method="POST",
            # 不设置 labelIds：Watch 必须覆盖 Sent/Trash/Spam 等未展示邮件，
            # 后续由 history.list 统一合并全量缓存，而非只跟踪收件箱标签。
            body={"topicName": topic},
        )
    except Exception as exc:
        _logger.warning("gmail_watch_failed error_type=%s", type(exc).__name__)
        write_sync_state(normalized, {
            **{k: v for k, v in read_sync_state(normalized).items() if k != "_etag"},
            "watch_status": f"error:{type(exc).__name__}",
        })
        return {
            "mailbox": normalized,
            "status": "error",
            "error_type": type(exc).__name__,
            "watch_status": f"error:{type(exc).__name__}",
        }

    history_id = str(payload.get("historyId") or state.get("history_id") or "")
    expiration = str(payload.get("expiration") or "")
    # Gmail watch 响应不含 resourceId 时保留旧值
    resource_id = str(payload.get("resourceId") or state.get("watch_resource_id") or "")
    write_sync_state(normalized, {
        **{k: v for k, v in read_sync_state(normalized).items() if k != "_etag"},
        "history_id": history_id or str(state.get("history_id") or ""),
        "last_history_id": history_id or str(state.get("last_history_id") or ""),
        "watch_status": "active",
        "watch_expiration": expiration,
        "watch_resource_id": resource_id,
        "scope_days": INITIAL_PRIORITY_DAYS,
    })
    return {
        "mailbox": normalized,
        "status": "active",
        "watch_status": "active",
        "history_id": history_id,
        "expiration": expiration,
        "resource_id": resource_id,
    }


def run_mailbox_sync_tick(
    mailbox: str,
    *,
    profile_history_id: str = "",
    run_priority: bool = True,
    run_backfill: bool = True,
    ensure_watch: bool = True,
) -> dict[str, Any]:
    """单次同步节拍：优先 180d → 可选回填 tick → Watch 续订。"""
    from .adapter import normalize_mailbox

    normalized = normalize_mailbox(mailbox)
    priority = None
    if run_priority:
        priority = progress_priority_metadata_sync(
            normalized,
            profile_history_id=profile_history_id,
        )
    backfill = None
    if run_backfill and bool((priority or {}).get("initial_sync_complete") or read_sync_state(normalized).get("initial_sync_complete")):
        # 前台只做一小批，其余丢后台
        backfill = progress_backfill_metadata_sync(normalized, batch_limit=min(40, BACKFILL_BATCH_PER_TICK))
        if not bool(backfill.get("backfill_complete")):
            schedule_background_backfill(normalized)
    watch = None
    if ensure_watch:
        watch = ensure_gmail_watch(normalized)
    boundary = get_mailbox_sync_boundary(normalized)
    return {
        "mailbox": normalized,
        "priority": priority,
        "backfill": backfill,
        "watch": watch,
        "boundary": boundary,
    }


__all__ = [
    "INITIAL_PRIORITY_DAYS",
    "boundary_honesty_note",
    "configured_sync_range",
    "empty_boundary",
    "ensure_gmail_watch",
    "get_mailbox_sync_boundary",
    "progress_backfill_metadata_sync",
    "progress_priority_metadata_sync",
    "read_sync_state",
    "refresh_boundary_from_cache",
    "reset_mailbox_sync_state",
    "run_mailbox_sync_tick",
    "schedule_background_backfill",
    "schedule_background_sync",
    "write_sync_state",
]
