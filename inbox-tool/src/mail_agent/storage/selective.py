"""APS 白名单同步与本地存储的选择性路由。"""

from __future__ import annotations

from typing import Any


def is_aps_sync_key(key: str) -> bool:
    """判断业务键是否属于允许跨端同步的五类数据。

    邮箱工作流使用 ``inbox_workflow_sync`` 独立键；本地仍保留完整副本，供
    离线读取与本地数据恢复使用。
    """
    normalized = str(key or "").replace("\\", "/").strip("/")
    parts = normalized.split("/")
    if len(parts) < 4 or parts[0] != "anna-inbox" or parts[1] != "mailbox":
        return False
    name = parts[3]
    return name in {"ask_history", "inbox_settings", "inbox_workflow_sync", "inbox-drafts", "compose-drafts"}


class SelectiveStorageClient:
    """将白名单业务键路由到 APS，其余所有键固定保存到本地。"""

    def __init__(self, local: Any, aps: Any) -> None:
        self._local = local
        self._aps = aps

    def _client_for(self, key: str) -> Any:
        return self._aps if is_aps_sync_key(key) else self._local

    async def get(self, key: str, **kwargs: Any) -> dict:
        return await self._client_for(key).get(key, **kwargs)

    async def set(self, key: str, value: Any, **kwargs: Any) -> dict:
        return await self._client_for(key).set(key, value, **kwargs)

    async def delete(self, key: str, **kwargs: Any) -> dict:
        return await self._client_for(key).delete(key, **kwargs)

    async def list(self, *, prefix: str | None = None, **kwargs: Any) -> dict:
        # 当前业务所有 list 均按完整数据类别传入 prefix；未指定或不完整的
        # prefix 保守地只列本地数据，避免将未同步数据暴露给 APS。
        return await self._client_for(prefix or "").list(prefix=prefix, **kwargs)
