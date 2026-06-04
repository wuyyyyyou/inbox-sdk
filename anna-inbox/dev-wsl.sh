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

# WSL proxy — 通过 Windows 宿主机代理访问外网
HOST_IP=$(ip route show default 2>/dev/null | awk '{print $3}' | head -1)
if [ -n "${HOST_IP:-}" ]; then
  export HTTP_PROXY="http://$HOST_IP:7890"
  export HTTPS_PROXY="http://$HOST_IP:7890"
  export NO_PROXY="localhost,127.0.0.1,::1"
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
