# Anna Inbox

Anna Inbox 是运行在 Anna App 中的 Gmail 工作台。2.0 以完整收件箱和 AI 侧栏为主界面：用户可以浏览和处理邮件、查看线程详情、保存草稿、预览附件，并让 Anna 搜索、汇总或协助回复邮件。

## 当前能力

- Inbox、Todos、Starred、Snoozed、Done、Drafts、Sent、Trash、Spam 和 All mail 视图；Trash 邮件仅在 Trash 中展示且排除草稿，恢复时仅移除 Gmail 的 `TRASH` 标签。
- Important / Other 分类、本地缓存、增量加载和 Gmail 刷新。
- 星标、重要、Todo、Snooze、已读、完成和移至垃圾箱操作。
- 线程详情、清洗后的文本或安全 HTML、AI overview、回复/转发/Compose 富文本草稿（`body_html`）、Cc·Bcc 与发送（10 秒可撤销）。
- 收件附件预览/下载（优先 Host transient upload，回退 APS Files；本地 dev 保留 loopback）；回复与 Compose 外发附件（合计 ≤25MB）。
- 多邮箱切换：立刻取消上一邮箱扫描，首屏读本地缓存，后台 History 静默同步；缓存刷新后预热联系人头像。
- 可折叠、可调宽的 Anna AI 侧栏：通过 Host Agent Session 流式选型并调用显式白名单工具；透传只读列表上下文（视图/筛选/搜索与 todo/done/snoozed）；支持当前邮件上下文、搜索结果引用、可审阅 Compose 草稿，以及停止、恢复和会话清理。
- Agent 工具仅执行只读检索、阅读、总结、草稿和整理建议；发送、删除、标签变更等状态修改仍须用户明确确认。
- `search_email` 实时检索 Gmail（不回退本地缓存）；普通搜索只补齐返回条数摘要，工作流筛选候选上限 60 且请求超时 12s；本地工作流 `is:todo/is:done/is:snoozed` 仅 AND；单次最多 20 条轻量摘要，正文读取与索引检索分离。
- 邮箱级 Todo/Done/Snoozed 与 AI Ask 历史经 APS KV 读写（`get/save_inbox_workflow_state`、`get/save_ai_ask_history`），乐观并发 etag。
- 多 Gmail 账户发现与切换。
- LLM 与 Gmail API 的真实连通性和延迟检测：反向 RPC 响应直通、12 秒统一总预算、检测去重，并在扫描或 AI turn 期间暂停轮询。
- 平台超时安全诊断：AI run 与同步收件箱调用返回 Sampling、Connected accounts、Gmail HTTP 和 Executa 阶段耗时，前端失败消息仅展示经过格式校验的诊断摘要。
- AI 侧栏 Ask 固定检索当前活动邮箱，不因多账户选择状态扩大 Gmail 搜索范围。
- 后端保留 Brief 注意力卡片、Ask、自定义扫描和联系人记忆能力。

设置入口在 2.0.1 前端中暂时隐藏；相关后端工具和状态结构仍然保留。

## 项目结构

```text
anna-inbox/
  app.json                         Anna App 发布元数据
  manifest.json                    权限、Host API 和必需 Executa
  src/
    api/mailAgentClient.ts         Executa API facade
    app/                           状态和应用控制器
    features/home/                 2.0 Inbox 工作台和 AI 侧栏
    features/mail-detail/          邮件详情、草稿和附件预览
    features/brief|ask|handle/      保留的工作流组件
    runtime/                       Anna Runtime 兼容层
  bundle/                          Vite 构建产物，不提交

inbox-tool/
  manifest.json                    Executa 身份、版本和工具契约
  src/anna_inbox_executa/          JSON-RPC 入口与工具分发
  src/mail_agent/                  Gmail、Ask、Brief、存储和联系人记忆
  src/tests/                       脚本式 Python 测试

scripts/
  google_token/                    本地 Gmail OAuth helper
  sync/                            Executa 身份同步检查
  build/                           二进制构建脚本
```

## 运行模型

前端通过 Anna Host API 调用 bundled Executa。Executa 使用 JSON-RPC 2.0 over stdio，每行一条 UTF-8 JSON 消息；`stdout` 只允许协议响应，诊断信息写入 `stderr`。

邮件和附件由 Gmail adapter 获取。小型状态通过统一的 async storage 层写入 APS KV 或本地 JSON；附件等大内容通过 Host transient upload、APS files 或本地 loopback URL 传递，不放入 KV 或 JSON-RPC result。

## 本地开发

### 后端

```sh
cd inbox-tool/src
uv sync
```

本地 Gmail token 默认位于：

```text
scripts/google_token/.secrets/gmail_tokens
```

也可以用 `ANNA_INBOX_TOKEN_DIR` 指定仓库外目录。不要提交 token、OAuth client secret、API key、本地缓存或 `.local_storage`。

### 前端

```sh
cd anna-inbox
npm install
npm run build
```

`npm run build` 会先执行 `tsc --noEmit`，再由 Vite 生成 `bundle/index.html`、`bundle/app.js` 和 `bundle/style.css`。

Anna App 本地启动使用 `anna-inbox/app.json` 和 `anna-inbox/executas/inbox-tool/executa.json`。旧的 `dev-wsl.sh` 不是当前权威启动入口。

## 测试

前端：

```sh
cd anna-inbox
npm test
npm run build
```

后端聚焦测试示例：

```sh
cd inbox-tool/src
uv run python tests/test_inbox_feed.py
uv run python tests/test_inbox_thread_storage.py
uv run python tests/test_attachment_download_host_upload.py
uv run python tests/test_llm_json_repair.py
uv run python tests/test_runtime_diagnostics.py
uv run python tests/test_ai_turn_current_mailbox.py
uv run python tests/test_storage_integration.py
```

协议 smoke test：

```sh
printf '%s\n' '{"jsonrpc":"2.0","method":"describe","id":1}' | uv --directory inbox-tool/src run anna-inbox-executa
printf '%s\n' '{"jsonrpc":"2.0","method":"health","id":1}' | uv --directory inbox-tool/src run anna-inbox-executa
```

## 版本与发布

App 与 Tool **版本解耦**（当前基线）：

| 端 | 版本 | 权威文件 |
| --- | --- | --- |
| App | `2.1.6` | `anna-inbox/app.json` |
| Tool | `2.2.6` | `inbox-tool/manifest.json`（同步 `pyproject.toml`、`executa.json`、`min_version`） |

Executa 身份以 `inbox-tool/manifest.json` 为单一来源。修改 `tool_id` 或 Tool 版本后运行：

```sh
python scripts/sync/sync_executa_identity.py
python scripts/sync/sync_executa_identity.py --check
```

发布前至少执行前端测试与构建、相关 Python 测试、身份同步检查，以及 `describe` / `health` smoke test。

## 进一步文档

当前文档索引见 [`anna-inbox/docs/README.md`](anna-inbox/docs/README.md)。

- [平台运行性能诊断与优化进度](anna-inbox/docs/平台运行性能诊断与优化进度.md)：正式环境性能差异、已完成优化、待验证指标和后续同步方案。
- [平台超时诊断与反馈流程](anna-inbox/docs/平台超时诊断与反馈流程.md)：安全 trace、平台复现判定、报告与论坛帖模板。
