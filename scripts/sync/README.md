# Executa 身份同步

`inbox-tool/manifest.json` 是 Anna Inbox Executa 身份的单一来源。当前同步脚本从这里读取：

- `name`：作为协议 `tool_id` 使用。
- `version`：同步到 Anna App 需要的 Executa 版本声明。

需要修改 `tool_id` 时，先修改 `inbox-tool/manifest.json` 的 `name`，然后从仓库根目录运行：

```sh
python scripts/sync/sync_executa_identity.py
```

脚本会同步这些静态目标：

- `anna-inbox/manifest.json`
- `anna-inbox/executas/inbox-tool/executa.json`

`anna-inbox/manifest.json` 使用 `bundled:inbox-executa` 作为 App bundled handle 引用；
脚本只同步版本号和 host API 引用形态，不会把 App manifest 改成真实 `tool_id`。
真实 `tool_id` 会同步到 `anna-inbox/executas/inbox-tool/executa.json`，供 `anna-app dev`
通过 `anna-inbox/app.json#bundled_executas` 解析成本地 Executa。

本地开发脚本 `anna-inbox/dev-wsl.sh` 和后端入口会直接读取 `inbox-tool/manifest.json`，不需要同步写入。

只检查是否同步、不写文件：

```sh
python scripts/sync/sync_executa_identity.py --check
```

注意：修改 `tool_id` 会让 Anna Host 把它识别为新的 Executa。变更前需要确认授权、凭据绑定、安装记录和发布版本是否都要一起迁移。
