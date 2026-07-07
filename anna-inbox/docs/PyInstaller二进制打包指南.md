# PyInstaller 二进制打包

权威入口是仓库根目录的 `scripts/build/build_binary.sh`。脚本从 `inbox-tool/manifest.json` 读取 tool ID 和版本，支持：

- `darwin-arm64`
- `darwin-x86_64`
- `linux-x86_64`
- `windows-x86_64`

## 构建

需要 Bash、Python 和 `uv`：

```sh
bash scripts/build/build_binary.sh
```

仅在已有等价验证时使用：

```sh
bash scripts/build/build_binary.sh --skip-smoke
```

产物写入 `dist/inbox-tool/<version>/`，包含平台压缩包和 `.sha256`。中间文件位于 `.build/inbox-tool/`。

脚本会安装 build dependency、运行 PyInstaller、生成包内 manifest、执行 `describe` / `health` smoke test、压缩并计算 SHA-256。

## 发布

GitHub workflow 使用 `inbox-tool-v<version>` tag 发布各平台 artifact。发布前确认 manifest、stub、Python package 和 App required Executa 版本一致，并在目标平台验证二进制启动和 newline-delimited JSON 输出。
