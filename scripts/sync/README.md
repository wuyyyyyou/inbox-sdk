# Executa 身份同步

`inbox-tool/manifest.json` 是 Executa `tool_id` 和版本的单一来源。

```sh
python scripts/sync/sync_executa_identity.py
python scripts/sync/sync_executa_identity.py --check
```

同步目标：

- `anna-inbox/manifest.json#required_executas[].min_version`
- `anna-inbox/executas/inbox-tool/executa.json#tool_id`
- `anna-inbox/executas/inbox-tool/executa.json#name`
- `anna-inbox/executas/inbox-tool/executa.json#version`

App manifest 继续使用 `bundled:inbox-executa`，Anna App 根据 `anna-inbox/app.json#bundled_executas` 解析本地 Executa。

脚本不会同步 `anna-inbox/app.json` 或 `inbox-tool/src/pyproject.toml`，发布时必须单独检查它们的版本。

修改 `tool_id` 会建立新的 Executa 身份。变更前必须确认授权、凭据绑定、安装记录和发布迁移策略。
