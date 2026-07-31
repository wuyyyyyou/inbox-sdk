"""Shared storage singleton for the mail agent.

main.py initialises the singleton during startup; all other modules import
``get_storage`` / ``get_files`` to access the configured persistence backend
without circular imports. The default backend is local JSON storage.
"""

from __future__ import annotations

from typing import Any

try:
    from executa_sdk.storage import StorageClient, FilesClient, StorageError  # noqa: E402
except ImportError:  # pragma: no cover — executa_sdk not available outside Anna runtime
    StorageClient = None  # type: ignore[assignment]
    FilesClient = None  # type: ignore[assignment]
    StorageError = RuntimeError  # type: ignore[assignment]

_storage: Any = None
_files: Any = None
_local_storage: Any = None
_aps_storage: Any = None
_scope: str = "user"
_backend: str = ""


def init(storage: StorageClient, files: FilesClient, *, scope: str = "user", backend: str = "") -> None:
    """Called once by main.py after creating the clients."""
    global _storage, _files, _local_storage, _aps_storage, _scope, _backend
    _storage = storage
    _files = files
    _local_storage = storage if backend == "local" else None
    _aps_storage = storage if backend == "aps" else None
    _scope = scope
    _backend = str(backend or "")


def init_selective(
    local_storage: Any,
    local_files: Any,
    aps_storage: Any,
    aps_files: Any,
    *,
    scope: str = "user",
) -> None:
    """初始化 APS 白名单同步模式，其余业务数据始终写入本地。

    文件客户端保留 APS 实例，维持 Host transient upload 与 APS Files 的现有
    附件传输语义；KV 数据则由 SelectiveStorageClient 按键路由。
    """
    from .selective import SelectiveStorageClient

    global _storage, _files, _local_storage, _aps_storage, _scope, _backend
    _local_storage = local_storage
    _aps_storage = aps_storage
    _storage = SelectiveStorageClient(local_storage, aps_storage)
    _files = aps_files or local_files
    _scope = scope
    _backend = "selective"


def get_storage() -> StorageClient:
    """Return the shared StorageClient singleton."""
    if _storage is None:
        raise RuntimeError("StorageClient not initialised — call storage_client.init() first")
    return _storage


def get_files() -> FilesClient:
    """Return the shared FilesClient singleton."""
    if _files is None:
        raise RuntimeError("FilesClient not initialised — call storage_client.init() first")
    return _files


def get_local_storage() -> Any:
    """返回本地存储实例，供需要同时维护本地完整状态的调用方使用。"""
    if _local_storage is None:
        raise RuntimeError("Local storage is not initialised")
    return _local_storage


def get_aps_storage() -> Any:
    """返回 APS 存储实例，仅供白名单同步和旧键迁移使用。"""
    if _aps_storage is None:
        raise RuntimeError("APS storage is not initialised")
    return _aps_storage


def scope() -> str:
    """Return the default storage scope."""
    return _scope


def backend() -> str:
    return _backend


def is_ready() -> bool:
    return _storage is not None and _files is not None
