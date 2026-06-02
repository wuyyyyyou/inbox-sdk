#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
SOURCE_MANIFEST = ROOT_DIR / "inbox-tool" / "manifest.json"
APP_MANIFEST = ROOT_DIR / "anna-inbox" / "manifest.json"
EXECUTA_STUB = ROOT_DIR / "anna-inbox" / "executas" / "inbox-tool" / "executa.json"


def load_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as json_file:
        data = json.load(json_file)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def write_json(path: Path, data: dict[str, Any], *, check: bool, changed: list[str]) -> None:
    existing = path.read_text(encoding="utf-8")
    next_content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if existing == next_content:
        return
    changed.append(str(path.relative_to(ROOT_DIR)))
    if not check:
        path.write_text(next_content, encoding="utf-8")


def json_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return before != after


def read_identity() -> tuple[str, str | None]:
    manifest = load_json(SOURCE_MANIFEST)
    explicit_tool_id = manifest.get("tool_id")
    manifest_name = manifest.get("name")
    if explicit_tool_id and manifest_name and explicit_tool_id != manifest_name:
        raise ValueError("inbox-tool/manifest.json has different tool_id and name values")
    tool_id = explicit_tool_id or manifest_name
    if not isinstance(tool_id, str) or not tool_id.strip():
        raise ValueError("inbox-tool/manifest.json must define a non-empty name or tool_id")
    version = manifest.get("version")
    if version is not None and not isinstance(version, str):
        raise ValueError("inbox-tool/manifest.json version must be a string when present")
    return tool_id.strip(), version


def sync_app_manifest(tool_id: str, version: str | None, *, check: bool, changed: list[str]) -> None:
    manifest = load_json(APP_MANIFEST)
    before = deepcopy(manifest)
    required_executas = manifest.get("required_executas")
    if not isinstance(required_executas, list):
        raise ValueError("anna-inbox/manifest.json required_executas must be a list")
    if len(required_executas) != 1 or not isinstance(required_executas[0], dict):
        raise ValueError("sync script expects exactly one required Executa in anna-inbox/manifest.json")
    required_executas[0]["tool_id"] = tool_id
    if version:
        required_executas[0]["min_version"] = version

    host_api = manifest.setdefault("ui", {}).setdefault("host_api", {})
    tools = host_api.get("tools")
    if not isinstance(tools, list):
        raise ValueError("anna-inbox/manifest.json ui.host_api.tools must be a list")
    required_ref = f"required:{tool_id}"
    replaced = False
    for index, value in enumerate(tools):
        if isinstance(value, str) and value.startswith("required:"):
            tools[index] = required_ref
            replaced = True
    if not replaced:
        tools.append(required_ref)

    if json_changed(before, manifest):
        write_json(APP_MANIFEST, manifest, check=check, changed=changed)


def sync_executa_stub(tool_id: str, version: str | None, *, check: bool, changed: list[str]) -> None:
    stub = load_json(EXECUTA_STUB)
    before = deepcopy(stub)
    stub["tool_id"] = tool_id
    stub["name"] = tool_id
    if version:
        stub["version"] = version
    if json_changed(before, stub):
        write_json(EXECUTA_STUB, stub, check=check, changed=changed)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Anna Inbox Executa identity from inbox-tool/manifest.json.")
    parser.add_argument("--check", action="store_true", help="Only check whether generated targets are up to date.")
    args = parser.parse_args()

    changed: list[str] = []
    tool_id, version = read_identity()
    sync_app_manifest(tool_id, version, check=args.check, changed=changed)
    sync_executa_stub(tool_id, version, check=args.check, changed=changed)

    if changed:
        print("Executa identity targets differ:" if args.check else "Synced Executa identity targets:")
        for path in changed:
            print(f"- {path}")
        return 1 if args.check else 0

    print("Executa identity is already in sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
