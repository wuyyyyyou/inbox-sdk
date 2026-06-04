#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-5180}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXECUTA_DIR="$SCRIPT_DIR/executas/inbox-tool"
VENV_DIR="${ANNA_MAIL_AGENT_VENV:-$HOME/.venvs/anna-inbox-executa}"
INBOX_TOOL_MANIFEST="$SCRIPT_DIR/../inbox-tool/manifest.json"
TOOL_ID="$(python3 -c 'import json, sys; data = json.load(open(sys.argv[1], encoding="utf-8-sig")); print(data.get("tool_id") or data["name"])' "$INBOX_TOOL_MANIFEST")"

export PATH="$HOME/.local/bin:$PATH"
export NODE_OPTIONS="${NODE_OPTIONS:---dns-result-order=ipv4first}"

# WSL proxy — 通过 Windows 宿主机 Clash 代理访问外网（Google API 等）
# 宿主机 IP 探测：先用 ip route 拿网关（最可靠），失败再用 /etc/resolv.conf nameserver
HOST_IP=$(ip route show default 2>/dev/null | awk '{print $3; exit}')
if [ -z "${HOST_IP:-}" ] && [ -f /etc/resolv.conf ]; then
  HOST_IP=$(awk '/^nameserver/ {print $2; exit}' /etc/resolv.conf 2>/dev/null)
fi

PROXY_PORT="${ANNA_PROXY_PORT:-7890}"
if [ -n "${HOST_IP:-}" ] && curl -s --connect-timeout 1 --max-time 2 "http://$HOST_IP:$PROXY_PORT" >/dev/null 2>&1; then
  export HTTP_PROXY="http://$HOST_IP:$PROXY_PORT"
  export HTTPS_PROXY="http://$HOST_IP:$PROXY_PORT"
  export http_proxy="http://$HOST_IP:$PROXY_PORT"
  export https_proxy="http://$HOST_IP:$PROXY_PORT"
  export NO_PROXY="localhost,127.0.0.1,::1"
  export no_proxy="localhost,127.0.0.1,::1"
  echo "[dev-wsl] proxy enabled: $HOST_IP:$PROXY_PORT" >&2
else
  echo "[dev-wsl] proxy skipped (host=$HOST_IP port=$PROXY_PORT unreachable)" >&2
fi

if [ -f "$HOME/.anna-mail-agent.env" ]; then
  # 中文注释：本地调试密钥只从 WSL 用户目录读取，避免写入项目和分发包。
  set -a
  . "$HOME/.anna-mail-agent.env"
  set +a
fi

EXECUTA_SPEC="dir=$EXECUTA_DIR,tool_id=$TOOL_ID,type=python,command=env UV_PROJECT_ENVIRONMENT=$VENV_DIR UV_LINK_MODE=copy uv --directory ../../../inbox-tool/src run anna-inbox-executa"

export ANNA_INBOX_TOKEN_DIR="$SCRIPT_DIR/../scripts/google_token/.secrets/gmail_tokens"

cd "$SCRIPT_DIR"
exec anna-app dev --port "$PORT" --executa "$EXECUTA_SPEC" --storage legacy "$@"
