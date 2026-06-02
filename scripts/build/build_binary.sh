#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/build/build_binary.sh [--skip-smoke]

Build the Anna Inbox Executa binary for the current platform and package it.

Supported platforms:
  darwin-arm64
  darwin-x86_64
  windows-x86_64

Options:
  --skip-smoke   Skip describe/health smoke tests after building the binary.
  -h, --help     Show this help.
EOF
}

SKIP_SMOKE=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --skip-smoke)
      SKIP_SMOKE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC_DIR="$ROOT_DIR/inbox-tool/src"
SOURCE_MANIFEST="$ROOT_DIR/inbox-tool/manifest.json"
BUILD_ROOT="$ROOT_DIR/.build/inbox-tool"
BINARY_BASENAME="inbox-tool"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    echo "Missing required command: python3 or python" >&2
    exit 1
  fi
fi

require_command "$PYTHON_BIN"
require_command uv

read_manifest_field() {
  "$PYTHON_BIN" - "$SOURCE_MANIFEST" "$1" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
field = sys.argv[2]
value = manifest.get(field)
if not isinstance(value, str) or not value.strip():
    raise SystemExit(f"inbox-tool/manifest.json must define non-empty {field!r}")
print(value.strip())
PY
}

TOOL_ID="$(read_manifest_field name)"
VERSION="$(read_manifest_field version)"

detect_platform() {
  local os_name
  local arch_name
  os_name="$(uname -s)"
  arch_name="$(uname -m)"

  case "$os_name" in
    Darwin)
      case "$arch_name" in
        arm64|aarch64) echo "darwin-arm64" ;;
        x86_64|amd64) echo "darwin-x86_64" ;;
        *) echo "Unsupported macOS architecture: $arch_name" >&2; return 1 ;;
      esac
      ;;
    MINGW*|MSYS*|CYGWIN*)
      case "$arch_name" in
        x86_64|amd64) echo "windows-x86_64" ;;
        *) echo "Unsupported Windows architecture: $arch_name" >&2; return 1 ;;
      esac
      ;;
    *)
      echo "Unsupported platform: $os_name $arch_name" >&2
      return 1
      ;;
  esac
}

PLATFORM="$(detect_platform)"
IS_WINDOWS=0
if [[ "$PLATFORM" == windows-* ]]; then
  IS_WINDOWS=1
fi

if [ "$IS_WINDOWS" -eq 1 ]; then
  BINARY_NAME="$BINARY_BASENAME.exe"
  ARCHIVE_EXT="zip"
  ADD_DATA_SEP=";"
else
  BINARY_NAME="$BINARY_BASENAME"
  ARCHIVE_EXT="tar.gz"
  ADD_DATA_SEP=":"
fi

native_path() {
  if [ "$IS_WINDOWS" -eq 1 ] && command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$1"
  else
    printf '%s\n' "$1"
  fi
}

VERSION_DIST_DIR="$ROOT_DIR/dist/inbox-tool/$VERSION"
WORK_DIR="$BUILD_ROOT/$PLATFORM"
PYINSTALLER_WORK_DIR="$WORK_DIR/pyinstaller-work"
PYINSTALLER_DIST_DIR="$WORK_DIR/pyinstaller-dist"
PYINSTALLER_SPEC_DIR="$WORK_DIR/pyinstaller-spec"
PACKAGE_DIR="$WORK_DIR/package"
ARCHIVE_NAME="$BINARY_BASENAME-$VERSION-$PLATFORM.$ARCHIVE_EXT"
SHA256_NAME="$BINARY_BASENAME-$VERSION-$PLATFORM.sha256"
ARCHIVE_PATH="$VERSION_DIST_DIR/$ARCHIVE_NAME"
SHA256_PATH="$VERSION_DIST_DIR/$SHA256_NAME"

echo "Building $BINARY_NAME"
echo "tool_id: $TOOL_ID"
echo "version: $VERSION"
echo "platform: $PLATFORM"

rm -rf "$WORK_DIR"
mkdir -p "$PYINSTALLER_WORK_DIR" "$PYINSTALLER_DIST_DIR" "$PYINSTALLER_SPEC_DIR" "$PACKAGE_DIR/bin" "$VERSION_DIST_DIR"

PYINSTALLER_WORK_ARG="$(native_path "$PYINSTALLER_WORK_DIR")"
PYINSTALLER_DIST_ARG="$(native_path "$PYINSTALLER_DIST_DIR")"
PYINSTALLER_SPEC_ARG="$(native_path "$PYINSTALLER_SPEC_DIR")"
SRC_DIR_ARG="$(native_path "$SRC_DIR")"
SOURCE_MANIFEST_ARG="$(native_path "$SOURCE_MANIFEST")"
ENTRY_SCRIPT_ARG="$(native_path "$SRC_DIR/anna_inbox_executa/main.py")"

PYINSTALLER_ARGS=(
  --onefile
  --name "$BINARY_BASENAME"
  --clean
  --noconfirm
  --noupx
  --distpath "$PYINSTALLER_DIST_ARG"
  --workpath "$PYINSTALLER_WORK_ARG"
  --specpath "$PYINSTALLER_SPEC_ARG"
  --paths "$SRC_DIR_ARG"
  --collect-submodules mail_agent
  --collect-submodules executa_sdk
  --add-data "$SOURCE_MANIFEST_ARG${ADD_DATA_SEP}."
)

if [ "$IS_WINDOWS" -eq 0 ]; then
  PYINSTALLER_ARGS+=(--strip)
fi

PYINSTALLER_ARGS+=("$ENTRY_SCRIPT_ARG")

UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$BUILD_ROOT/.venv-$PLATFORM}" \
UV_LINK_MODE="${UV_LINK_MODE:-copy}" \
  uv --directory "$SRC_DIR" run --group build pyinstaller "${PYINSTALLER_ARGS[@]}"

BUILT_BINARY="$PYINSTALLER_DIST_DIR/$BINARY_NAME"
if [ ! -f "$BUILT_BINARY" ]; then
  echo "Expected binary not found: $BUILT_BINARY" >&2
  exit 1
fi

cp "$BUILT_BINARY" "$PACKAGE_DIR/bin/$BINARY_NAME"
if [ "$IS_WINDOWS" -eq 0 ]; then
  chmod 755 "$PACKAGE_DIR/bin/$BINARY_NAME"
  if command -v codesign >/dev/null 2>&1; then
    codesign --force --sign - "$PACKAGE_DIR/bin/$BINARY_NAME" >/dev/null
  fi
fi

"$PYTHON_BIN" - "$PACKAGE_DIR/manifest.json" "$TOOL_ID" "$VERSION" <<'PY'
import json
import sys
from pathlib import Path

output_path = Path(sys.argv[1])
tool_id = sys.argv[2]
version = sys.argv[3]

package_manifest = {
    "name": tool_id,
    "version": version,
    "runtime": {
        "binary": {
            "entrypoint": {
                "default": "bin/inbox-tool",
                "windows-x86_64": "bin/inbox-tool.exe",
                "windows-arm64": "bin/inbox-tool.exe",
            },
            "permissions": {
                "bin/inbox-tool": "0o755",
            },
        },
    },
}
output_path.write_text(json.dumps(package_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

run_smoke() {
  local method="$1"
  local output_path="$WORK_DIR/smoke-$method.out"
  local error_path="$WORK_DIR/smoke-$method.err"
  local smoke_storage_dir="$WORK_DIR/smoke-local-storage"

  printf '{"jsonrpc":"2.0","method":"%s","id":1}\n' "$method" \
    | env ZHAOPY_MAIL_AGENT_STORAGE_DIR="$smoke_storage_dir" "$PACKAGE_DIR/bin/$BINARY_NAME" >"$output_path" 2>"$error_path"

  "$PYTHON_BIN" - "$output_path" "$method" "$TOOL_ID" "$VERSION" <<'PY'
import json
import sys
from pathlib import Path

output_path = Path(sys.argv[1])
method = sys.argv[2]
tool_id = sys.argv[3]
version = sys.argv[4]

lines = [line for line in output_path.read_text(encoding="utf-8").splitlines() if line.strip()]
if len(lines) != 1:
    raise SystemExit(f"{method} smoke expected one JSON line, got {len(lines)}")
payload = json.loads(lines[0])
if payload.get("jsonrpc") != "2.0" or payload.get("id") != 1:
    raise SystemExit(f"{method} smoke returned invalid JSON-RPC envelope")
result = payload.get("result")
if not isinstance(result, dict):
    raise SystemExit(f"{method} smoke returned non-object result")
if method == "describe":
    if result.get("name") != tool_id:
        raise SystemExit(f"describe smoke returned name={result.get('name')!r}, expected {tool_id!r}")
    if result.get("version") != version:
        raise SystemExit(f"describe smoke returned version={result.get('version')!r}, expected {version!r}")
elif method == "health":
    if result.get("status") != "healthy":
        raise SystemExit(f"health smoke returned status={result.get('status')!r}")
PY
}

if [ "$SKIP_SMOKE" -eq 0 ]; then
  run_smoke describe
  run_smoke health
else
  echo "Skipping smoke tests."
fi

rm -f "$ARCHIVE_PATH" "$SHA256_PATH"
if [ "$IS_WINDOWS" -eq 1 ]; then
  "$PYTHON_BIN" - "$PACKAGE_DIR" "$ARCHIVE_PATH" <<'PY'
import sys
import zipfile
from pathlib import Path

package_dir = Path(sys.argv[1])
archive_path = Path(sys.argv[2])
with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(package_dir.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(package_dir).as_posix())
PY
else
  (cd "$PACKAGE_DIR" && tar -czf "$ARCHIVE_PATH" manifest.json bin)
fi

"$PYTHON_BIN" - "$ARCHIVE_PATH" "$SHA256_PATH" <<'PY'
import hashlib
import sys
from pathlib import Path

archive_path = Path(sys.argv[1])
sha256_path = Path(sys.argv[2])
digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
sha256_path.write_text(f"{digest}  {archive_path.name}\n", encoding="utf-8")
print(f"archive: {archive_path}")
print(f"sha256: {digest}")
print(f"size: {archive_path.stat().st_size}")
PY
