#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"

# Windows and WSL virtual environments contain platform-specific executables.
export UV_PROJECT_ENVIRONMENT="$PROJECT_ROOT/inbox-tool/src/.venv-wsl"
export UV_LINK_MODE="${UV_LINK_MODE:-copy}"

exec uv --directory "$PROJECT_ROOT/inbox-tool/src" "$@"
