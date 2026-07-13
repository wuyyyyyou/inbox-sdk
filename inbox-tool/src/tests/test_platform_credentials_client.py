"""Regression checks for the Anna credentials Reverse RPC client.

Run: uv --directory inbox-tool/src run python tests/test_platform_credentials_client.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1])
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from executa_sdk.credentials import (  # noqa: E402
    CredentialsClient,
    CredentialsError,
    METHOD_CREDENTIALS_GET_TOKEN,
    METHOD_CREDENTIALS_LIST_ACCOUNTS,
)


async def main() -> None:
    frames: list[dict] = []
    client = CredentialsClient(write_frame=frames.append)

    accounts_task = asyncio.create_task(client.list_accounts(provider="google"))
    await asyncio.sleep(0)
    assert frames[0]["method"] == METHOD_CREDENTIALS_LIST_ACCOUNTS
    assert frames[0]["params"] == {"provider": "google"}
    assert client.dispatch_response({
        "jsonrpc": "2.0",
        "id": frames[0]["id"],
        "result": {"accounts": [{"account_id": "acct-1", "email": "one@example.com"}]},
    })
    assert (await accounts_task)["accounts"][0]["email"] == "one@example.com"

    token_task = asyncio.create_task(client.get_token(provider="google", account_id="acct-1"))
    await asyncio.sleep(0)
    assert frames[1]["method"] == METHOD_CREDENTIALS_GET_TOKEN
    assert frames[1]["params"] == {"provider": "google", "account_id": "acct-1"}
    assert client.dispatch_response({
        "jsonrpc": "2.0",
        "id": frames[1]["id"],
        "error": {"code": -32061, "message": "not granted"},
    })
    try:
        await token_task
    except CredentialsError as exc:
        assert exc.code == -32061
    else:
        raise AssertionError("credentials/getToken error was not propagated")

    print("Platform credentials client: OK")


if __name__ == "__main__":
    asyncio.run(main())
