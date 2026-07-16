"""Inbox 设置的邮箱隔离与默认值回归测试。"""

import asyncio
from typing import Any

from mail_agent.storage.ops import get_inbox_settings, set_inbox_settings


class FakeStorageClient:
    """用于 storage ops 单测的最小 APS 内存替身。"""

    async def get(self, key: str, *, scope: str = "user", timeout: float = 30) -> dict[str, Any]:
        return self.values.get(key, {"exists": False, "value": None, "etag": ""})

    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    async def set(self, key: str, value: Any, *, scope: str = "user", if_match: str | None = None, timeout: float = 30) -> dict[str, Any]:
        current = self.values.get(key)
        if if_match and (not current or current["etag"] != if_match):
            raise RuntimeError("etag mismatch")
        etag = f"etag-{len(self.values) + 1}"
        self.values[key] = {"exists": True, "value": value, "etag": etag}
        return {"etag": etag}


async def test_inbox_settings_defaults_are_mailbox_scoped() -> None:
    from mail_agent.storage.client import init

    fake = FakeStorageClient()
    init(fake, fake, scope="user")  # type: ignore[arg-type]
    first = await get_inbox_settings("one@example.com")
    second = await get_inbox_settings("two@example.com")

    assert first["settings"].mailbox == "one@example.com"
    assert first["settings"].display_range_days == 30
    assert first["settings"].llm_status_poll_seconds == 60
    assert first["settings"].initial_list_size == 100
    assert second["settings"].mailbox == "two@example.com"
    assert second["settings"].display_range_days == 30

    saved = await set_inbox_settings(
        "one@example.com",
        {"display_range_days": 60, "llm_status_poll_seconds": 30, "initial_list_size": 200},
        if_match=first["etag"],
    )
    assert saved["settings"].display_range_days == 60
    assert saved["settings"].llm_status_poll_seconds == 30
    assert saved["settings"].initial_list_size == 200
    assert (await get_inbox_settings("two@example.com"))["settings"].display_range_days == 30
    assert (await get_inbox_settings("two@example.com"))["settings"].llm_status_poll_seconds == 60
    assert (await get_inbox_settings("two@example.com"))["settings"].initial_list_size == 100

    # 非法轮询档位应被忽略，保留原值
    ignored = await set_inbox_settings("one@example.com", {"llm_status_poll_seconds": 15})
    assert ignored["settings"].llm_status_poll_seconds == 30

    categories = [
        {"id": "invoices", "name": "Invoices", "query": "subject:invoice", "hide_when_empty": True, "bundling_behavior": "by_sender"},
        {"id": "invoices", "name": "Duplicate", "query": "from:duplicate@example.com"},
        {"id": "legacy", "name": "Legacy", "query": "from:legacy@example.com"},
        {"id": "empty-name", "name": " ", "query": "from:ignored@example.com"},
        {"id": "empty-query", "name": "Ignored", "query": " "},
    ]
    categorized = await set_inbox_settings("one@example.com", {"custom_categories": categories})
    assert [item.id for item in categorized["settings"].custom_categories] == ["invoices", "legacy"]
    assert categorized["settings"].custom_categories[0].name == "Invoices"
    assert categorized["settings"].custom_categories[0].hide_when_empty is True
    assert categorized["settings"].custom_categories[0].bundling_behavior == "by_sender"
    assert categorized["settings"].custom_categories[1].hide_when_empty is False
    assert categorized["settings"].custom_categories[1].bundling_behavior == "default"
    assert (await get_inbox_settings("two@example.com"))["settings"].custom_categories == []


if __name__ == "__main__":
    asyncio.run(test_inbox_settings_defaults_are_mailbox_scoped())
