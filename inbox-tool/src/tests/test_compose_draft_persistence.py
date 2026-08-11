"""Compose 草稿 APS 持久化与 APS-only 读写路径的回归测试。

Gmail Draft 同步暂时下线后，Compose 草稿只经 Anna APS 存取：
- set_compose_draft 写后读回校验，避免把未持久化的映射误报为“已保存”。
- list_compose_drafts 只返回轻量目录行（无完整正文，仅 body_preview），不触发 Gmail Draft API。
- get_compose_draft 按需读取单封草稿，超大正文改走 loopback body_url。
- delete_compose_draft 只删除 APS 数据，不再调用 Gmail Draft API。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any


SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class FakeStorageClient:
    """最小 APS 内存替身，可模拟服务端确认写入前丢失数据。"""

    def __init__(self, *, drop_writes: bool = False) -> None:
        self.drop_writes = drop_writes
        self.values: dict[str, dict[str, Any]] = {}

    async def get(self, key: str, **_: Any) -> dict[str, Any]:
        return self.values.get(key, {"exists": False, "value": None, "etag": ""})

    async def set(self, key: str, value: Any, **_: Any) -> dict[str, Any]:
        etag = f"etag-{len(self.values) + 1}"
        if not self.drop_writes:
            self.values[key] = {"exists": True, "value": value, "etag": etag}
        return {"etag": etag}

    async def list(self, *, prefix: str | None = None, **_: Any) -> dict[str, Any]:
        items = [
            {"key": key}
            for key in self.values
            if not prefix or key.startswith(prefix)
        ]
        return {"items": items}

    async def delete(self, key: str, **_: Any) -> dict[str, Any]:
        return self.values.pop(key, None) or {"ok": True}


async def main() -> None:
    from anna_inbox_executa import v2_tools
    from mail_agent.storage.client import init_selective
    from mail_agent.storage.ops import set_compose_draft

    local = FakeStorageClient()
    aps = FakeStorageClient()
    init_selective(local, local, aps, aps)
    saved = await set_compose_draft(
        "owner@example.com",
        {
            "id": "draft-1",
            "gmail_draft_id": "gmail-draft-1",
            "gmail_message_id": "gmail-message-1",
            "recipients": ["recipient@example.com"],
            "subject": "Subject",
            "body": "Body",
        },
    )
    assert saved["etag"] == "etag-1"
    assert saved["draft"]["gmail_message_id"] == "gmail-message-1"
    assert len(aps.values) == 1
    assert not local.values

    # 通过 SelectiveStorageClient 读取时，APS 中已有的草稿映射必须能被列出。
    from mail_agent.storage.ops import list_compose_drafts
    listed = await list_compose_drafts("owner@example.com")
    assert listed["count"] == 1
    assert listed["drafts"][0]["id"] == "draft-1"

    # Gmail Draft 同步下线：list 走 _handle_v2_tool 时必须返回轻量目录行，
    # 不含完整正文或 body_html，只保留 body_preview 与稳定的 Gmail 关联字段。
    directory = await v2_tools._handle_v2_tool(
        "list_compose_drafts",
        {"mailbox": "owner@example.com"},
        "test-invoke",
    )
    assert directory["count"] == 1
    assert directory["has_more"] is False
    assert directory["next_offset"] == 1
    row = directory["drafts"][0]
    assert row["id"] == "draft-1"
    assert row["body"] == ""
    assert "body_html" not in row
    assert row["body_preview"] == "Body"
    assert row["gmail_draft_id"] == "gmail-draft-1"
    assert row["gmail_message_id"] == "gmail-message-1"

    # 草稿详情按需读取：不存在的草稿返回 exists=False，不抛错。
    missing_detail = await v2_tools._handle_v2_tool(
        "get_compose_draft",
        {"mailbox": "owner@example.com", "draft_id": "draft-not-exists"},
        "test-invoke",
    )
    assert missing_detail["exists"] is False
    assert missing_detail["draft"] == {}

    # 已存在草稿返回完整详情与 etag，供编辑器恢复与乐观并发更新。
    detail = await v2_tools._handle_v2_tool(
        "get_compose_draft",
        {"mailbox": "owner@example.com", "draft_id": "draft-1"},
        "test-invoke",
    )
    assert detail["exists"] is True
    assert detail["etag"] == "etag-1"
    assert detail["draft"]["body"] == "Body"
    assert detail["draft"]["id"] == "draft-1"

    # 删除草稿只操作 APS；删除后再列出应回到空目录。
    deleted = await v2_tools._handle_v2_tool(
        "delete_compose_draft",
        {"mailbox": "owner@example.com", "draft_id": "draft-1"},
        "test-invoke",
    )
    assert deleted["ok"] is True
    after_delete = await list_compose_drafts("owner@example.com")
    assert after_delete["count"] == 0
    assert not aps.values

    # 单封异常大草稿不能穿透 JSON-RPC 响应帧上限：详情改走 loopback body_url，
    # body / body_html 清空，整体帧大小仍受 COMPOSE_DRAFT_DIRECTORY_RESPONSE_MAX_BYTES 约束。
    await set_compose_draft(
        "owner@example.com",
        {
            "id": "draft-large",
            "gmail_draft_id": "gmail-draft-large",
            "recipients": ["recipient@example.com"],
            "subject": "Large",
            "body": "x" * (80 * 1024),
            "body_html": "<p>" + ("x" * (80 * 1024)) + "</p>",
        },
    )
    large_detail = await v2_tools._get_aps_compose_draft("owner@example.com", "draft-large")
    assert large_detail["exists"] is True
    assert large_detail["draft"]["body"] == ""
    assert large_detail["draft"]["body_html"] == ""
    assert large_detail["draft"]["body_url"]
    assert v2_tools._compose_draft_detail_frame_size(large_detail) <= v2_tools.COMPOSE_DRAFT_DIRECTORY_RESPONSE_MAX_BYTES

    missing_aps = FakeStorageClient(drop_writes=True)
    init_selective(FakeStorageClient(), FakeStorageClient(), missing_aps, missing_aps)
    try:
        await set_compose_draft(
            "owner@example.com",
            {
                "id": "draft-missing",
                "gmail_draft_id": "gmail-draft-missing",
                "recipients": ["recipient@example.com"],
                "subject": "Subject",
                "body": "Body",
            },
        )
    except RuntimeError as exc:
        assert "verification" in str(exc).lower()
    else:
        raise AssertionError("APS write without a readable record must fail")

    print("PASS compose draft APS-only persistence and tool routing")


if __name__ == "__main__":
    asyncio.run(main())
