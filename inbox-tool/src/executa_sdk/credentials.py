"""Anna Executa SDK helpers for platform multi-account credentials."""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .context import inject_reverse_rpc_context
from .sampling import _write_frame

METHOD_CREDENTIALS_LIST_ACCOUNTS = "credentials/listAccounts"
METHOD_CREDENTIALS_GET_TOKEN = "credentials/getToken"

CREDENTIALS_ERR_NOT_GRANTED = -32061
CREDENTIALS_ERR_INVALID_REQUEST = -32062
CREDENTIALS_ERR_UPSTREAM = -32063
CREDENTIALS_ERR_TIMEOUT = -32064


class CredentialsError(Exception):
    """承载 Host credentials Reverse RPC 返回的可安全展示错误。

    ``data`` 只保留 Host 的结构化错误附加信息；调用方不得将其中可能
    存在的凭据内容写入日志、持久化存储或 JSON-RPC 结果。
    """

    def __init__(self, code: int, message: str, data: Optional[dict] = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data or {}


@dataclass
class _Pending:
    future: "asyncio.Future[dict]"


class CredentialsClient:
    """按账户查询平台凭据，并按需交换短期 access token。

    Executa 的 stdin 读取器必须把 Host 的响应帧转交给
    :meth:`dispatch_response`，以解除对应协程的等待。token 仅返回给当前
    调用方，绝不能放入 Executa result、日志或持久化存储。
    """

    DEFAULT_TIMEOUT = 30.0

    def __init__(self, *, write_frame: Callable[[dict], None] | None = None) -> None:
        self._write_frame = write_frame or _write_frame
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._disabled_reason: Optional[str] = None

    def disable(self, reason: str) -> None:
        self._disabled_reason = reason

    def dispatch_response(self, message: dict) -> bool:
        """接收 Host 响应并唤醒同一请求 ID 对应的等待协程。

        仅消费没有 ``method`` 字段的 JSON-RPC 响应；通知、请求和不属于
        credentials 客户端的响应会返回 ``False``，交由其他协议处理逻辑继续
        分发。
        """
        if not isinstance(message, dict) or "method" in message:
            return False
        request_id = message.get("id")
        if request_id is None:
            return False
        with self._lock:
            pending = self._pending.pop(str(request_id), None)
        if pending is None:
            return False
        loop = self._loop
        if loop is None or pending.future.done():
            return True

        def resolve() -> None:
            if pending.future.done():
                return
            error = message.get("error")
            if isinstance(error, dict):
                pending.future.set_exception(CredentialsError(
                    int(error.get("code", -32603)),
                    str(error.get("message", "Unknown credentials error")),
                    error.get("data") if isinstance(error.get("data"), dict) else None,
                ))
                return
            result = message.get("result")
            pending.future.set_result(result if isinstance(result, dict) else {})

        try:
            loop.call_soon_threadsafe(resolve)
        except RuntimeError:
            resolve()
        return True

    async def list_accounts(self, *, provider: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
        return await self._call(METHOD_CREDENTIALS_LIST_ACCOUNTS, {"provider": provider}, timeout)

    async def get_token(
        self,
        *,
        provider: str,
        account_id: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> dict:
        params: dict[str, Any] = {"provider": provider}
        if account_id is not None:
            params["account_id"] = account_id
        return await self._call(METHOD_CREDENTIALS_GET_TOKEN, params, timeout)

    async def _call(self, method: str, params: dict, timeout: float) -> dict:
        """发送 Reverse RPC 请求，并在超时或异常时清理挂起状态。

        挂起表由线程锁保护，避免 stdin 线程分发响应时与 asyncio 调用协程
        竞争；无论成功、错误还是超时，已完成请求都不会遗留在内存中。
        """
        if self._disabled_reason:
            raise CredentialsError(CREDENTIALS_ERR_NOT_GRANTED, self._disabled_reason)
        loop = asyncio.get_running_loop()
        self._loop = loop
        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict] = loop.create_future()
        with self._lock:
            self._pending[request_id] = _Pending(future=future)
        try:
            # 多 invoke 并发时 Host 要求 params.context.invoke_id 才能路由 reverse RPC。
            wire_params = inject_reverse_rpc_context(params)
            self._write_frame({"jsonrpc": "2.0", "id": request_id, "method": method, "params": wire_params})
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            with self._lock:
                self._pending.pop(request_id, None)
            raise CredentialsError(CREDENTIALS_ERR_TIMEOUT, f"{method} timed out after {timeout}s") from exc
        except Exception:
            with self._lock:
                self._pending.pop(request_id, None)
            raise


__all__ = [
    "CredentialsClient",
    "CredentialsError",
    "METHOD_CREDENTIALS_LIST_ACCOUNTS",
    "METHOD_CREDENTIALS_GET_TOKEN",
    "CREDENTIALS_ERR_NOT_GRANTED",
    "CREDENTIALS_ERR_INVALID_REQUEST",
    "CREDENTIALS_ERR_UPSTREAM",
    "CREDENTIALS_ERR_TIMEOUT",
]
