from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_SRC_DIR = str(Path(__file__).resolve().parents[1])
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from anna_inbox_executa.common import *
import anna_inbox_executa.common as common
from anna_inbox_executa.dispatcher import handle_invoke

def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

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
            return make_response(request_id, result=handle_invoke(params))
        if method == "shutdown":
            return make_response(request_id, result={"ok": True})
        return make_response(request_id, error=make_error(-32601, f"Method not found: {method}"))
    except ValueError as exc:
        return make_response(request_id, error=make_error(-32601, str(exc)))
    except RuntimeError as exc:
        try:
            error_data = json.loads(str(exc))
        except json.JSONDecodeError:
            error_data = {"code": -32603, "message": str(exc)}
        return make_response(request_id, error=make_error(int(error_data.get("code", -32603)), str(error_data.get("message", exc)), error_data.get("data")))
    except StorageError as exc:
        log(f"storage error: {exc}")
        return make_response(request_id, error=make_error(exc.code, exc.message, exc.data))
    except Exception as exc:
        trace = traceback.format_exc()
        log(f"internal error: {type(exc).__name__}: {exc}\n{trace}")
        return make_response(
            request_id,
            error=make_error(
                -32603,
                f"{type(exc).__name__}: {exc}",
                {"traceback": trace},
            ),
        )


def handle_line(line: str) -> None:
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
        return

    if not isinstance(message, dict):
        write_frame(make_response(None, error=make_error(-32600, "Invalid request")))
        return

    if "method" not in message:
        if not common.sampling.dispatch_response(message) and not common.dispatch_storage_response(message) and not common.dispatch_host_upload_response(message):
            log(f"unmatched response id={message.get('id')!r}")
        return

    response = handle_request(message)
    if response is not None and message.get("id") is not None:
        write_frame(response)


def main() -> None:
    # Write startup log so we can diagnose harness crashes even without stderr.
    _diag_dir = data_root() / "anna-inbox" / "diagnostics"
    _diag_dir.mkdir(parents=True, exist_ok=True)
    _diag_path = _diag_dir / "agent_startup.log"
    with open(_diag_path, "a", encoding="utf-8") as _df:
        _df.write(f"{beijing_now()} startup stdin={sys.stdin.encoding} stdout={sys.stdout.encoding}\n")

    log("ready")
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="mail-agent-rpc") as pool:
        try:
            for raw_line in sys.stdin:
                line = raw_line.strip()
                if line:
                    pool.submit(handle_line, line)
        except Exception as exc:
            with open(_diag_path, "a", encoding="utf-8") as _df:
                import traceback
                _df.write(f"{beijing_now()} CRASH {type(exc).__name__}: {exc}\n{traceback.format_exc()}\n")
            log(f"stdin loop crashed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()

__all__ = [name for name in globals() if not name.startswith("__")]
