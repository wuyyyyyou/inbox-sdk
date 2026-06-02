from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "executas" / "anna-inbox-tool" / "src"
sys.path.insert(0, str(SRC_ROOT))

from utils.paths import sanitize_mailbox_id, token_dir  # noqa: E402
from utils.time_utils import beijing_now_iso  # noqa: E402


DEFAULT_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
DEFAULT_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"

# 常用 scope 参考：
#   gmail.readonly           — 只读
#   gmail.modify             — 读写（标记已读/删除/归档/标签，不能永久删除）
#   https://mail.google.com/ — 完整 Gmail 权限（含发送、永久删除）


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    server: "OAuthCallbackServer"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        self.server.callback_query = query
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h1>Gmail authorization received</h1><p>You can close this tab and return to the terminal.</p></body></html>"
        )

    def log_message(self, _format: str, *_args: Any) -> None:
        return


class OAuthCallbackServer(HTTPServer):
    callback_query: dict[str, list[str]] | None = None


def main() -> None:
    args = parse_args()
    client = load_client(args)
    state = secrets.token_urlsafe(24)
    redirect_uri = resolve_redirect_uri(client, args)
    bind_host, bind_port = callback_bind_address(redirect_uri, args.port)
    server = OAuthCallbackServer((bind_host, bind_port), OAuthCallbackHandler)
    if not redirect_uri:
        redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth2callback"
    auth_url = build_auth_url(client, redirect_uri, state, args.scope)

    print("Open this URL to authorize Gmail access:", flush=True)
    print(auth_url, flush=True)
    if not args.no_browser:
        webbrowser.open(auth_url)

    print(f"Waiting for OAuth callback on {redirect_uri} ...", flush=True)
    while server.callback_query is None:
        server.handle_request()

    query = server.callback_query or {}
    if query.get("state", [""])[0] != state:
        raise SystemExit("OAuth state mismatch; token was not saved.")
    if "error" in query:
        raise SystemExit(f"OAuth error: {query['error'][0]}")
    code = query.get("code", [None])[0]
    if not code:
        raise SystemExit("OAuth callback did not include code.")

    token = exchange_code(client, code, redirect_uri)
    authorized_email = fetch_authorized_email(token["access_token"])
    verify_authorized_email(args.email, authorized_email)
    save_token(args.email, client, token, args.scope, authorized_email)
    print(f"Authorized Gmail account: {authorized_email}", flush=True)
    print(f"Saved Gmail token for {args.email} to {token_dir() / (sanitize_mailbox_id(args.email) + '.json')}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local Gmail OAuth helper for Anna Inbox — obtains access_token + refresh_token via browser-based OAuth flow.")
    parser.add_argument("--email", default="zhaopy2121@gmail.com", help="Mailbox email used as local token file name.")
    parser.add_argument("--client-secrets", help="Path to Google OAuth desktop client JSON.")
    parser.add_argument("--client-id", help="Google OAuth client id.")
    parser.add_argument("--client-secret", help="Google OAuth client secret.")
    parser.add_argument("--redirect-uri", help="Local OAuth redirect URI registered in Google Cloud.")
    parser.add_argument("--scope", default=DEFAULT_SCOPE, help="OAuth scope. Default is Gmail readonly.")
    parser.add_argument("--port", type=int, default=0, help="Local callback port. 0 picks a free port.")
    parser.add_argument("--no-browser", action="store_true", help="Print URL without opening browser.")
    return parser.parse_args()


def load_client(args: argparse.Namespace) -> dict[str, str]:
    client_secrets = args.client_secrets or os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS")
    if client_secrets:
        payload = json.loads(Path(client_secrets).read_text(encoding="utf-8"))
        client = payload.get("installed") or payload.get("web") or payload
        return {
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "auth_uri": client.get("auth_uri") or DEFAULT_AUTH_URI,
            "token_uri": client.get("token_uri") or DEFAULT_TOKEN_URI,
            "redirect_uris": client.get("redirect_uris") or [],
        }
    if args.client_id and args.client_secret:
        return {
            "client_id": args.client_id,
            "client_secret": args.client_secret,
            "auth_uri": DEFAULT_AUTH_URI,
            "token_uri": DEFAULT_TOKEN_URI,
            "redirect_uris": [],
        }
    env_client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    env_client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if env_client_id and env_client_secret:
        return {
            "client_id": env_client_id,
            "client_secret": env_client_secret,
            "auth_uri": DEFAULT_AUTH_URI,
            "token_uri": DEFAULT_TOKEN_URI,
            "redirect_uris": [],
        }
    raise SystemExit(
        "Provide --client-secrets, set GOOGLE_OAUTH_CLIENT_SECRETS, or provide both --client-id and --client-secret."
    )


def resolve_redirect_uri(client: dict[str, Any], args: argparse.Namespace) -> str:
    if args.redirect_uri:
        return str(args.redirect_uri)
    redirect_uris = client.get("redirect_uris")
    if isinstance(redirect_uris, list):
        for uri in redirect_uris:
            parsed = urllib.parse.urlparse(str(uri))
            if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"} and parsed.port:
                return str(uri)
    return ""


def callback_bind_address(redirect_uri: str, fallback_port: int) -> tuple[str, int]:
    if not redirect_uri:
        return ("127.0.0.1", fallback_port)
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"} or not parsed.port:
        raise SystemExit(f"Unsupported local redirect URI: {redirect_uri}")
    return (parsed.hostname, int(parsed.port))


def build_auth_url(client: dict[str, str], redirect_uri: str, state: str, scope: str) -> str:
    params = {
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return client["auth_uri"] + "?" + urllib.parse.urlencode(params)


def exchange_code(client: dict[str, str], code: str, redirect_uri: str) -> dict[str, Any]:
    body = urllib.parse.urlencode(
        {
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        client["token_uri"],
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_authorized_email(access_token: str) -> str:
    request = urllib.request.Request(
        GMAIL_PROFILE_URL,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            profile = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Failed to verify Gmail account: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Failed to verify Gmail account: {exc.reason}") from exc

    email = str(profile.get("emailAddress") or "").strip()
    if not email:
        raise SystemExit("Failed to verify Gmail account: profile did not include emailAddress.")
    return email


def verify_authorized_email(expected_email: str, authorized_email: str) -> None:
    expected = expected_email.strip().lower()
    actual = authorized_email.strip().lower()
    if expected and expected != actual:
        raise SystemExit(
            f"Authorized account mismatch: expected {expected_email}, got {authorized_email}. "
            "Token was not saved."
        )


def save_token(
    email: str,
    client: dict[str, str],
    token: dict[str, Any],
    scope: str,
    authorized_email: str | None = None,
) -> None:
    expires_at = int(time.time()) + int(token.get("expires_in", 3600))
    record = {
        "email": email,
        "authorized_email": authorized_email or email,
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "token_uri": client["token_uri"],
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token"),
        "scope": token.get("scope") or scope,
        "expires_at": expires_at,
        "created_at": beijing_now_iso(),
        "updated_at": beijing_now_iso(),
    }
    token_dir().mkdir(parents=True, exist_ok=True)
    (token_dir() / f"{sanitize_mailbox_id(email)}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
