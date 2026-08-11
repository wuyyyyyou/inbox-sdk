"""收件附件 90s 单一总预算与超时分类的回归测试。

运行：uv --directory inbox-tool/src run python tests/test_attachment_deadline_budget.py
或：cd inbox-tool/src && python -m pytest tests/test_attachment_deadline_budget.py -q

覆盖点：
1. _AttachmentDeadline 共享单调时钟，remaining/timeout 在预算耗尽时抛
   _AttachmentOperationTimeout，且超时诊断不泄露敏感值；
2. record_span 首参 stage 不再被当作关键字重复传入（修复参数冲突）；
3. _wait_for_attachment_stage 把 asyncio.TimeoutError 收敛为安全分类；
4. _put_presigned_url_sync 使用传入的 timeout 而非写死 120s；
5. 收件附件交付（探测→Gmail 回源→begin→PUT→complete→URL）全部计入同一个
   总预算，任一阶段挂起时整体在预算内结束。
"""

from __future__ import annotations

import asyncio
import sys
import time
import tempfile
from pathlib import Path
from typing import Any


_SRC = str(Path(__file__).resolve().parents[1])
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _new_deadline(budget: float) -> Any:
    from anna_inbox_executa.v2_tools import _AttachmentDeadline

    return _AttachmentDeadline(budget_seconds=budget)


def test_deadline_remaining_obeys_shared_clock() -> None:
    """remaining 随单调时钟递减，最终在预算耗尽时抛超时。"""
    from anna_inbox_executa.v2_tools import _AttachmentOperationTimeout

    deadline = _new_deadline(1.0)
    first = deadline.remaining("test_stage")
    assert 0.0 < first <= 1.0
    time.sleep(1.1)
    try:
        deadline.remaining("test_stage")
    except _AttachmentOperationTimeout:
        pass
    else:
        raise AssertionError("budget exhausted must raise _AttachmentOperationTimeout")


def test_deadline_timeout_applies_caller_limit() -> None:
    """timeout(limit) 取「剩余预算」与「调用方单阶段上限」的较小值。"""
    deadline = _new_deadline(90.0)
    # 剩余预算充足时受单阶段 limit 约束。
    assert deadline.timeout("test_stage", limit=3.0) == 3.0
    # 剩余预算小于 limit 时以剩余预算为准（且不小于 0.1s 下限）。
    small = _new_deadline(1.0)
    time.sleep(0.3)
    value = small.timeout("test_stage", limit=20.0)
    assert 0.0 < value <= 0.9
    assert value < 20.0


def test_deadline_timeout_span_does_not_collide_on_stage() -> None:
    """record_span 首个位置参数是 stage；超时记录不能把 stage 再当关键字重复传入。

    这是修复前会抛 TypeError: record_span() got multiple values for argument 'stage'
    的回归。新增 detail/budget_seconds/remaining_seconds 均为非敏感数值字段。
    """
    from anna_inbox_executa.diagnostics import activate_trace, create_trace, deactivate_trace, snapshot
    from anna_inbox_executa.v2_tools import _AttachmentOperationTimeout

    trace = create_trace(operation="attachment-deadline-span")
    token = activate_trace(trace)
    try:
        deadline = _new_deadline(1.0)
        time.sleep(1.1)
        try:
            deadline.remaining("aps_upload_begin")
        except _AttachmentOperationTimeout:
            pass
        else:
            raise AssertionError("expected timeout")
        payload = snapshot(trace) or {}
    finally:
        deactivate_trace(token)

    spans = [dict(span) for span in payload.get("spans", [])]
    assert spans, "timeout must record a span"
    budget_span = spans[-1]
    assert budget_span["stage"] == "attachment.budget"
    assert budget_span["outcome"] == "error"  # record_span 将 timeout 归一为 error
    assert budget_span["detail"] == "aps_upload_begin"
    # 诊断不能包含邮箱/令牌/URL 等敏感值。
    assert "example.com" not in str(payload)
    assert "@" not in str(payload)


def test_wait_for_attachment_stage_converts_timeout_to_safe_class() -> None:
    """asyncio.wait_for 超时应被收敛为 _AttachmentOperationTimeout，不泄露底层内容。"""
    from anna_inbox_executa.v2_tools import _AttachmentOperationTimeout, _wait_for_attachment_stage

    async def _hang() -> None:
        await asyncio.sleep(3.0)

    deadline = _new_deadline(1.0)
    try:
        asyncio.run(_wait_for_attachment_stage(_hang(), deadline, "gmail_bytes"))
    except _AttachmentOperationTimeout as exc:
        assert str(exc) == "Attachment operation timed out."
    else:
        raise AssertionError("hanging stage must raise _AttachmentOperationTimeout")


def test_put_presigned_uses_passed_timeout_not_hardcoded() -> None:
    """_put_presigned_url_sync 必须透传 timeout 给底层 opener，而非写死 120s。"""
    from anna_inbox_executa import v2_tools

    kwdefaults = v2_tools._put_presigned_url_sync.__kwdefaults__
    assert kwdefaults is not None
    assert kwdefaults["timeout"] == v2_tools.HOST_UPLOAD_REVERSE_RPC_TIMEOUT_SECONDS


def test_upload_inbox_attachment_to_aps_shares_one_deadline() -> None:
    """收件附件 APS 交付全流程共用同一个总预算，最终 URL 阶段挂起时整体及时失败。"""
    from anna_inbox_executa import v2_tools
    from anna_inbox_executa.v2_tools import _AttachmentDeadline, _AttachmentOperationTimeout

    class HangFinalAps:
        """探测阶段立即 miss；后续 download_url（最终 URL）永久挂起。"""

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def download_url(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append("download_url")
            if len(self.calls) == 1:
                raise TimeoutError("[-32030] timed out")  # 探测 miss
            await asyncio.sleep(10.0)  # 最终 URL 挂起，等待共享预算触发

        async def upload_begin(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append("upload_begin")
            return {"put_url": "https://upload.example.test/obj", "headers": {"Content-Type": "application/pdf"}}

        async def upload_complete(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append("upload_complete")
            return {"completed": True}

    original_aps_files = v2_tools._aps_files
    original_put = v2_tools._put_presigned_url_sync
    v2_tools._aps_files = HangFinalAps()
    v2_tools._put_presigned_url_sync = lambda *a, **k: "etag-test"
    try:
        deadline = _AttachmentDeadline(budget_seconds=1.0)
        started = time.monotonic()

        async def _run() -> Any:
            return await v2_tools._upload_inbox_attachment_to_aps(
                "user@example.com",
                "msg-1",
                {
                    "id": "token-1",
                    "gmail_attachment_id": "gmail-1",
                    "filename": "report.pdf",
                    "mime_type": "application/pdf",
                    "size": 5,
                },
                lambda *a, **k: b"bytes",
                deadline=deadline,
            )

        try:
            asyncio.run(_run())
        except _AttachmentOperationTimeout:
            pass
        else:
            raise AssertionError("upload must fail within the shared budget")
        elapsed = time.monotonic() - started
        assert elapsed < 2.5, f"expected budget-bound failure, took {elapsed:.2f}s"
        assert elapsed >= 0.8, f"expected to wait near the 1s budget, took {elapsed:.2f}s"
    finally:
        v2_tools._aps_files = original_aps_files
        v2_tools._put_presigned_url_sync = original_put


def test_load_inbox_attachment_bytes_applies_deadline_to_gmail_fetch() -> None:
    """缓存未命中时，阻塞的 Gmail 回源必须纳入总预算。"""
    from anna_inbox_executa import v2_tools
    from anna_inbox_executa.v2_tools import _AttachmentDeadline, _AttachmentOperationTimeout

    original_dir = v2_tools._attachment_download_dir
    try:
        with tempfile.TemporaryDirectory() as cache_root:
            v2_tools._attachment_download_dir = lambda: Path(cache_root)  # type: ignore[assignment]

            def _slow_fetch(*a: Any) -> bytes:
                time.sleep(3.0)
                return b"late"

            deadline = _AttachmentDeadline(budget_seconds=1.0)

            async def _run() -> float:
                started = time.monotonic()
                try:
                    await v2_tools._load_inbox_attachment_bytes(
                        "user@example.com",
                        "msg-1",
                        {
                            "id": "token-1",
                            "gmail_attachment_id": "gmail-1",
                            "filename": "report.pdf",
                            "size": 4,
                        },
                        _slow_fetch,
                        deadline=deadline,
                    )
                except _AttachmentOperationTimeout:
                    pass
                else:
                    raise AssertionError("slow Gmail fetch must be bounded by deadline")
                return time.monotonic() - started

            # 在协程内部测量：asyncio.run 退出时还会等待默认线程池里仍在
            # sleep 的回源线程，外层计时会被线程池清理拖长，不能代表预算行为。
            elapsed = asyncio.run(_run())
            assert elapsed < 2.5, f"expected budget-bound failure, took {elapsed:.2f}s"
            assert elapsed >= 0.8, f"expected to wait near the 1s budget, took {elapsed:.2f}s"
    finally:
        v2_tools._attachment_download_dir = original_dir


if __name__ == "__main__":
    test_deadline_remaining_obeys_shared_clock()
    test_deadline_timeout_applies_caller_limit()
    test_deadline_timeout_span_does_not_collide_on_stage()
    test_wait_for_attachment_stage_converts_timeout_to_safe_class()
    test_put_presigned_uses_passed_timeout_not_hardcoded()
    test_upload_inbox_attachment_to_aps_shares_one_deadline()
    test_load_inbox_attachment_bytes_applies_deadline_to_gmail_fetch()
    print("PASS attachment deadline budget tests")