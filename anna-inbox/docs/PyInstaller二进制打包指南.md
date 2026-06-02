# PyInstaller 二进制打包指南

将 Executa Python 工具打包为 Windows 单文件 exe，通过 GitHub Releases 分发。

## 1. 前置条件

```bash
pip install pyinstaller
```

## 2. 准备 .spec 文件

在 `src/` 目录下创建 `<tool-id>.spec`：

```python
# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
hiddenimports += collect_submodules('mail_agent')
hiddenimports += collect_submodules('executa_sdk')

a = Analysis(
    ['zhaopy_mail_agent\\main.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='tool-zhaopy-mail-agent-rd6b87r5',  # 与 tool_id 一致
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,              # JSON-RPC stdio 必须为 True
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
```

关键点：
- `collect_submodules` 自动收集包内所有子模块，防止 PyInstaller 漏掉动态 import
- `console=True` 必须开启，Executa 通过 stdio 通信
- `name` 必须与 `executa.json` 中的 `tool_id` 一致

## 3. 执行打包

```bash
cd new/executas/tool-zhaopy-mail-agent-rd6b87r5/src

# --clean 清除缓存，确保干净构建
pyinstaller tool-zhaopy-mail-agent-rd6b87r5.spec --clean
```

输出文件：`dist/tool-zhaopy-mail-agent-rd6b87r5.exe`

## 4. 本地验证

启动 exe 后发送 JSON-RPC `initialize` 请求验证：

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2.0","clientInfo":{"name":"test"}}}' | ./dist/tool-zhaopy-mail-agent-rd6b87r5.exe
```

预期返回包含 `serverInfo` 和 `capabilities` 的正常响应。

## 5. 打包 zip

```bash
VERSION="1.0.0"
TOOL_ID="tool-zhaopy-mail-agent-rd6b87r5"

mkdir -p release/bin
cp dist/${TOOL_ID}.exe release/bin/
cd release && zip -r ../${TOOL_ID}-${VERSION}-windows-x86_64.zip bin/
```

zip 内部结构：
```
bin/
  tool-zhaopy-mail-agent-rd6b87r5.exe
```

## 6. 计算校验值

```bash
sha256sum ${TOOL_ID}-${VERSION}-windows-x86_64.zip
stat ${TOOL_ID}-${VERSION}-windows-x86_64.zip
```

记录 SHA256 和文件大小（字节）。

## 7. 发布到 GitHub

1. 在 GitHub 仓库创建 Release，tag 为 `v<VERSION>`
2. 上传 zip 文件
3. 记录 zip 的下载 URL

## 8. 更新 executa.json

```json
{
  "type": "binary",
  "binary_urls": {
    "windows-x86_64": {
      "url": "https://github.com/<user>/<repo>/releases/download/v<VERSION>/<tool-id>-<VERSION>-windows-x86_64.zip",
      "sha256": "<sha256>",
      "size": <bytes>,
      "entrypoint": "bin/<tool-id>.exe",
      "format": "zip"
    }
  }
}
```

`type` 从 `"python"` 改为 `"binary"`，`command` 字段可保留用于本地 debug 模式。

## 常见问题

### ModuleNotFoundError

通常因为 PyInstaller 未检测到动态 import 的模块。在 `.spec` 中添加：

```python
hiddenimports += collect_submodules('遗漏的包名')
# 或逐个添加
hiddenimports += ['missing.module.name']
```

### exe 闪退

检查 `console=True` 是否设置。如果设为 `False`，exe 无控制台窗口，但 stdio 通信会断开。

### 打包后体积过大

可尝试：
- `upx=True` 启用 UPX 压缩
- `excludes=['tkinter', 'unittest', 'pydoc']` 排除不需要的标准库
- 使用 `strip=True`
