from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_SRC_DIR = str(Path(__file__).resolve().parents[1])
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from anna_inbox_executa.common import *
import anna_inbox_executa.common as common
from anna_inbox_executa.diagnostics import activate_trace, create_trace, deactivate_trace, record_span, snapshot
from anna_inbox_executa.dispatcher import handle_invoke


def _attach_diagnostics(result: dict[str, Any], trace: dict[str, Any]) -> dict[str, Any]:
    """仅向工具 data 附加安全时序摘要，不改变既有成功结果的协议形状。"""
    data = result.get("data")
    diagnostic = snapshot(trace)
    if not isinstance(data, dict) or not diagnostic:
        return result
    # 后台 run 已携带累计 diagnostics 时，外层 invoke trace 与其高度重叠。
    # 保留 run 级诊断，避免响应同时返回 diagnostics 与 invoke_diagnostics。
    if "diagnostics" in data:
        return result
    enriched = dict(result)
    enriched["data"] = {**data, "diagnostics": diagnostic}
    return enriched


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    trace: dict[str, Any] | None = None

    try:
        if method == "initialize":
            return make_response(request_id, result=handle_initialize(params))
        if method == "describe":
            return make_response(request_id, result=MANIFEST)
        if method == "health":
            return make_response(
                request_id,
                result={
                    "status": "healthy",
                    "timestamp": beijing_now(),
                    "version": VERSION,
                    "tools_count": len(MANIFEST["tools"]),
                },
            )
        if method == "invoke":
            invoke_params = params if isinstance(params, dict) else {}
            trace = create_trace(
                operation=str(invoke_params.get("tool") or "invoke"),
                invoke_id=str(invoke_params.get("invoke_id") or ""),
            )
            started = time.monotonic()
            trace_token = activate_trace(trace)
            try:
                result = handle_invoke(invoke_params)
                record_span("executa.invoke", started)
                return make_response(request_id, result=_attach_diagnostics(result, trace))
            except Exception as exc:
                record_span("executa.invoke", started, outcome="error", error_type=type(exc).__name__)
                raise
            finally:
                deactivate_trace(trace_token)
        if method == "shutdown":
            return make_response(request_id, result={"ok": True})
        return make_response(request_id, error=make_error(-32601, f"Method not found: {method}"))
    except ValueError as exc:
        diagnostic = snapshot(trace)
        data = {"diagnostics": diagnostic} if diagnostic else None
        return make_response(request_id, error=make_error(-32601, str(exc), data))
    except RuntimeError as exc:
        try:
            error_data = json.loads(str(exc))
        except json.JSONDecodeError:
            error_data = {"code": -32603, "message": str(exc)}
        data = error_data.get("data") if isinstance(error_data.get("data"), dict) else {}
        diagnostic = snapshot(trace)
        if diagnostic:
            data = {**data, "diagnostics": diagnostic}
        return make_response(request_id, error=make_error(int(error_data.get("code", -32603)), str(error_data.get("message", exc)), data or None))
    except StorageError as exc:
        log(f"storage error: {exc}")
        data = dict(exc.data or {})
        diagnostic = snapshot(trace)
        if diagnostic:
            data["diagnostics"] = diagnostic
        return make_response(request_id, error=make_error(exc.code, exc.message, data or None))
    except Exception as exc:
        traceback_text = traceback.format_exc()
        log(f"internal error: {type(exc).__name__}: {exc}\n{traceback_text}")
        data: dict[str, Any] = {"traceback": traceback_text}
        diagnostic = snapshot(trace)
        if diagnostic:
            data["diagnostics"] = diagnostic
        return make_response(
            request_id,
            error=make_error(
                -32603,
                f"{type(exc).__name__}: {exc}",
                data,
            ),
        )


def _decode_line(line: str) -> dict[str, Any] | None:
    # Windows 管道偶尔会在首行带 BOM，这里只清理协议行开头的 BOM。
    line = line.lstrip("\ufeff")
    # Diagnostic: check if stdin encoding is working for CJK text
    try:
        _diag_bytes = line.encode("utf-8")
    except Exception:
        _diag_bytes = b"<encode failed>"
    non_ascii = any(b > 127 for b in _diag_bytes)
    if non_ascii and len(line) > 40:
        log(f"stdin-diag first 80 chars: {repr(line[:80])}")

    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        write_frame(make_response(None, error=make_error(-32700, "Parse error")))
        return None

    if not isinstance(message, dict):
        write_frame(make_response(None, error=make_error(-32600, "Invalid request")))
        return None
    return message


def _dispatch_reverse_response(message: dict[str, Any]) -> None:
    """立即分发 Host 反向 RPC 响应，不能排队到可能已被 invoke 占满的业务 worker。"""
    if not common.sampling.dispatch_response(message) and not common.dispatch_storage_response(message) and not common.dispatch_host_upload_response(message) and not common.dispatch_platform_credentials_response(message):
        log(f"unmatched response id={message.get('id')!r}")


def _handle_request_frame(message: dict[str, Any]) -> None:
    response = handle_request(message)
    if response is not None and message.get("id") is not None:
        write_frame(response)


def handle_line(line: str) -> None:
    """同步处理单条协议帧，保留给本地调用与回归测试。"""
    message = _decode_line(line)
    if message is None:
        return
    if "method" not in message:
        _dispatch_reverse_response(message)
        return
    _handle_request_frame(message)


def main() -> None:
    # Write startup log so we can diagnose harness crashes even without stderr.
    _diag_dir = data_root() / "anna-inbox" / "diagnostics"
    _diag_dir.mkdir(parents=True, exist_ok=True)
    _diag_path = _diag_dir / "agent_startup.log"
    with open(_diag_path, "a", encoding="utf-8") as _df:
        _df.write(f"{beijing_now()} startup stdin={sys.stdin.encoding} stdout={sys.stdout.encoding}\n")

    log("ready")
    # 业务 invoke 并发执行；反向 RPC 响应在 stdin 线程直通，避免等待响应的 worker 相互饿死。
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="mail-agent-rpc") as pool:
        try:
            for raw_line in sys.stdin:
                line = raw_line.strip()
                if line:
                    message = _decode_line(line)
                    if message is None:
                        continue
                    if "method" not in message:
                        _dispatch_reverse_response(message)
                    else:
                        pool.submit(_handle_request_frame, message)
        except Exception as exc:
            with open(_diag_path, "a", encoding="utf-8") as _df:
                import traceback
                _df.write(f"{beijing_now()} CRASH {type(exc).__name__}: {exc}\n{traceback.format_exc()}\n")
            log(f"stdin loop crashed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()

__all__ = [name for name in globals() if not name.startswith("__")]
