# Anna Inbox

Anna Inbox 是运行在 Anna App 中的 Gmail 工作台。当前产品以 **Inbox Workspace + Anna AI 侧栏**为唯一主路径：用户可以浏览和处理邮件、查看线程详情、管理草稿与附件，并让 AI 在受控的证据范围内搜索、汇总或协助起草回复。

当前基线为 **App `2.3.6` / Tool `2.4.6`**。两端主版本、次版本可以独立演进，Patch 必须保持一致。

## 交接导览

建议按以下顺序阅读：

1. 本文的“运行模型”和“核心约束”，先了解边界。
2. `anna-inbox/docs/README.md`，确认文档状态与当前基线。
3. `anna-inbox/docs/2.4.6架构与发布基线.md`，核对本版本产品行为。
4. 下面的“关键架构与代码入口”，再进入具体实现。
5. 修改前先检查对应代码、manifest 和运行日志；设计文档只能作为参考，不能替代代码事实。

## 产品概览

- 顶层箱组为 Inbox、Done、Sent、Spam、Trash；Inbox 子标签为 All / Starred / Todos / Snoozed，Sent 子标签为 Sent / Drafts。
- 支持归档、星标、重要、Todo、Snooze、已读、完成和移至垃圾箱；Sent 与 Done 是相互独立的状态。
- 支持线程详情、清洗后的文本或安全 HTML、AI overview、回复/转发/Compose 富文本草稿（`body_html`）、Cc/Bcc 和可撤销发送。
- 附件下载优先使用 Host transient upload，回退 APS Files；本地开发保留 loopback URL。外发附件合计不超过 25MB。
- 多邮箱切换会取消上一邮箱扫描，首屏优先读取本地缓存，后台执行 180 天 priority metadata 同步和 History 增量同步。
- AI 侧栏默认使用本地 Sampling（`start_ai_turn`），只读检索统一经过 `query_mail_evidence`；host 仅保留为隐藏的 localStorage 调试开关。
- 回复草稿必须经过“两步确认”：先展示摘要并请求确认，确认后才展示可审阅草稿卡片；“仅正文”请求不展示卡片。

## 关键架构与代码入口

### 前端 `anna-inbox/`

- `anna-inbox/src/features/home/HomeView.tsx`：Inbox Workspace、AI 侧栏、账户切换、邮件列表和分页交互。
- `anna-inbox/src/features/mail-detail/`：线程详情、正文渲染、富文本草稿和附件预览。
- `anna-inbox/src/app/useAppController.ts`：应用状态、Host Agent Session，以及 AI 侧栏 localStorage 兼容模式开关。
- `anna-inbox/src/api/agentSessionClient.ts`：Host Session 帧流解析和 DONE 标记处理。
- `anna-inbox/src/api/mailAgentClient.ts`：Executa 工具调用 facade；组件不得绕过 facade 直接散落工具名。
- `anna-inbox/src/features/home/aiMessageFormatting.ts`：AI Markdown 输出归一化、表格折叠和引用过滤。
- `anna-inbox/app.json`：App 版本权威文件。
- `anna-inbox/manifest.json`：权限、Host API 和必需 Executa 声明。

### 后端 `inbox-tool/`

- `inbox-tool/src/anna_inbox_executa/`：JSON-RPC 入口、分发、本地 session 和工具实现。
- `inbox-tool/src/executa_sdk/context.py`：`invoke_id` 的保存、透传和跨线程上下文绑定。
- `inbox-tool/src/mail_agent/evidence_flow.py`：Evidence 取证流程和 Gmail 托底边界。
- `inbox-tool/src/mail_agent/local_query.py`：本地缓存查询和 QueryPlan 相关逻辑。
- `inbox-tool/src/mail_agent/mail_providers/gmail/mailbox_sync.py`：180 天 priority metadata、History 监听和增量 backfill。
- `inbox-tool/src/mail_agent/storage/`：APS 与本地存储实现；统一存取入口为其中的 `ops.py`。
- `inbox-tool/manifest.json`：Tool 身份和版本权威文件。
- `inbox-tool/src/pyproject.toml`：Tool Python 包版本同步文件。
- `anna-inbox/executas/inbox-tool/executa.json`：App 随附的 Executa 身份信息。

## 运行模型

前端通过 Anna Host API 调用 bundled Executa。Executa 使用 JSON-RPC 2.0 over stdio：每行只能有一条 UTF-8 JSON 消息，`stdout` 只能输出合法协议响应，日志和诊断必须写入 `stderr`。

Host API 的每次调用都必须透传 `params.context.invoke_id`。后台线程或协程开始工作前必须重新绑定调用上下文，避免并发 invoke 超时或响应路由错误。

## 核心数据与安全边界

- 只读检索优先使用本地 Evidence 缓存；邮件缓存和大部分邮件元数据不写入 APS。
- APS 只同步 AI Ask 历史、邮箱级 Todo/Done/Snoozed、设置和草稿；并发更新使用 `etag` / `if_match`。
- 所有存储访问走 `inbox-tool/src/mail_agent/storage/ops.py` 等高层入口，不直接操作底层 KV。读取 KV 时必须检查 `result.get("exists")`，不能用隐式布尔值判断存在性。
- 凭证只允许通过 manifest 声明并从 `params.context.credentials` 传入；禁止写入日志、临时文件、错误跟踪或文档。不要提交 Gmail token、OAuth client secret、API key、本地缓存或 `.local_storage`。
- 邮件正文、附件和原始二进制不得塞入 JSON-RPC result 或 APS KV；应使用 Host transient upload、APS files 或本地 loopback URL。
- Gmail 状态变更（发送、删除、标签和已读状态等）必须由明确的用户交互触发，AI 只能提出建议或生成草稿。
- 平台诊断只反馈随机 trace ID、耗时和错误类型，不得包含邮箱、查询、邮件内容或凭据。

## AI 与邮件同步关键约束

### AI

- AI 侧栏产品路径默认是本地 Sampling：`start_ai_turn(source=sidebar_local)`；host 模式只作为隐藏的 `localStorage["anna-inbox-ai-sidebar-mode"]="host"` 调试开关。
- AI 通过只读 `query_mail_evidence` 执行 Scope 解析、QueryPlan 生成和本地 Evidence 检索，禁止普通零命中时隐式回源 Gmail。
- `search_email` / `read_email` 为 cache-only；Evidence 每轮最多一次 Gmail 托底，只有超出最早边界，或 priority 未完成/缓存为空且严格零命中时才允许。完整索引的普通零命中不回源，`sync_boundary` 说明覆盖范围。
- 只有本轮用户确认的 Evidence 才能标记 `THREAD_REF`；未确认引用和直接 Gmail 链接必须过滤。流式生成期间侧栏保持贴底，草稿卡片出现后也必须贴底展示。
- “上周/last week”按上一个完整自然周解析，不按滚动 7 天解析。

### 邮箱同步

- 邮箱发现以平台账号快照为准；平台已删除的邮箱会清理本地缓存、工作流、草稿、联系人记忆和注册表，临时 discovery 失败不会触发清理。
- 后台先做 180 天 priority metadata 同步，再通过 History API 注册 Watch、监听并增量 backfill。列表的 `display_range_days` 只控制 UI 展示，不限制 AI 全局检索。
- 多邮箱切换时先取消旧邮箱扫描；首屏先用本地缓存，后台再补齐同步缺口。

## 本地环境

### 后端依赖

Windows 与 WSL 不能共用 Python 虚拟环境。推荐使用对应的 wrapper 管理后端依赖和命令；两端环境分别为 `inbox-tool/src/.venv-wsl` 与 `inbox-tool/src/.venv-windows`。

WSL：

```sh
scripts/dev/uv-wsl.sh sync
```

Windows PowerShell：

```powershell
.\scripts\dev\uv-windows.ps1 sync
```

本地 Gmail token 默认位于 `scripts/google_token/.secrets/gmail_tokens`，也可以用 `ANNA_INBOX_TOKEN_DIR` 指定仓库外目录。任何凭证都不得提交。

### Windows 与 WSL 的 uv wrapper

两端虚拟环境均为本地忽略文件；不要直接使用未指定 `UV_PROJECT_ENVIRONMENT` 的裸 `uv` 命令。

WSL：

```sh
scripts/dev/uv-wsl.sh run anna-inbox-executa
```

Windows PowerShell：

```powershell
.\scripts\dev\uv-windows.ps1 run anna-inbox-executa
```

### 前端

```sh
cd anna-inbox
npm install
npm run build
```

`npm run build` 只执行类型检查并生成 Vite 构建产物。遵守项目规约：只做构建，不要运行或启动前端；构建完成后交给人工启动并验收。`anna-inbox/bundle/` 是发布产物，不提交到 Git。

Anna App 本地启动使用 `anna-inbox/app.json` 和 `anna-inbox/executas/inbox-tool/executa.json`；旧的 `dev-wsl.sh` 不是当前权威启动入口。

## 验证命令

### 前端

```sh
cd anna-inbox
npm test
npm run build
```

其中 build 仍然只做构建，不启动前端；人工验收由交接人员执行。

### 后端优先验证：身份检查与协议 smoke test

修改 Tool 身份或版本后优先执行：

```sh
python scripts/sync/sync_executa_identity.py --check
```

协议 smoke test：

WSL（每次只发送一条请求）：

```sh
printf '%s\n' '{"jsonrpc":"2.0","method":"describe","id":1}' | scripts/dev/uv-wsl.sh run anna-inbox-executa
printf '%s\n' '{"jsonrpc":"2.0","method":"health","id":1}' | scripts/dev/uv-wsl.sh run anna-inbox-executa
```

Windows PowerShell（每次只发送一条请求）：

```powershell
'{"jsonrpc":"2.0","method":"describe","id":1}' | .\scripts\dev\uv-windows.ps1 run anna-inbox-executa
'{"jsonrpc":"2.0","method":"health","id":1}' | .\scripts\dev\uv-windows.ps1 run anna-inbox-executa
```

### 后端聚焦测试与 P0

`inbox-tool/src/tests/` 是仓库内的聚焦测试目录；执行前确认目录存在。聚焦测试必须通过对应 wrapper 运行：

WSL：

```sh
scripts/dev/uv-wsl.sh run python tests/test_inbox_feed.py
scripts/dev/uv-wsl.sh run python tests/test_gmail_history_sync.py
scripts/dev/uv-wsl.sh run python tests/test_ai_agent_tools.py
scripts/dev/uv-wsl.sh run python tests/test_local_query.py
scripts/dev/uv-wsl.sh run python tests/test_inbox_thread_response_budget.py
```

Windows PowerShell：

```powershell
.\scripts\dev\uv-windows.ps1 run python tests/test_inbox_feed.py
.\scripts\dev\uv-windows.ps1 run python tests/test_gmail_history_sync.py
.\scripts\dev\uv-windows.ps1 run python tests/test_ai_agent_tools.py
.\scripts\dev\uv-windows.ps1 run python tests/test_local_query.py
.\scripts\dev\uv-windows.ps1 run python tests/test_inbox_thread_response_budget.py
```

## 常见排障

- **smoke test 没有合法 JSON 响应**：检查是否有调试输出混入 `stdout`；所有日志应改写到 `stderr`，并确认每行只有一条 JSON-RPC 消息。
- **并发调用超时或响应串路**：检查 `params.context.invoke_id` 是否贯穿 Host 调用链，以及后台线程/协程是否重新绑定上下文。
- **AI 搜不到邮件**：先看本地缓存覆盖范围和 `sync_boundary`，再核对同步是否完成；普通完整索引零命中不会触发 Gmail 回源。
- **邮箱切换后出现旧数据**：以平台账号快照为准，确认旧邮箱扫描已取消；平台临时 discovery 失败不应自行清理本地数据。
- **Windows/WSL 依赖异常**：不要共享 `.venv`，分别使用对应的 `uv-wsl.sh` 或 `uv-windows.ps1` wrapper。
- **版本或 Executa 不匹配**：检查 App `2.3.6`、Tool `2.4.6` 及同步文件，再执行身份 `--check` 和 `describe` / `health` smoke test。
- **前端验收疑问**：`npm run build` 不等于启动验收；不要擅自启动，交由人工按 App 配置验收。

## 版本发布变更步骤

1. 以 `anna-inbox/app.json` 和 `inbox-tool/manifest.json` 为事实来源，确认当前为 App `2.3.6` / Tool `2.4.6`，并保持 Patch 一致。
2. 修改 Tool 版本或 `tool_id` 时，同步 `inbox-tool/src/pyproject.toml`、`anna-inbox/executas/inbox-tool/executa.json` 和 `anna-inbox/manifest.json` 的 `required_executas[].min_version`。
3. 运行身份同步，再执行检查：

   ```sh
   python scripts/sync/sync_executa_identity.py
   python scripts/sync/sync_executa_identity.py --check
   ```

4. 执行后端 `describe` / `health` smoke test；按上文通过 wrapper 执行 `inbox-tool/src/tests/` 聚焦测试。
5. 执行前端 `npm test` 和 `npm run build`。build 仅构建，不启动前端；人工验收由用户完成，`bundle/` 不提交。
6. 更新 `README.md`、`CONTEXT.md`、`AGENTS.md` 和 `anna-inbox/docs/` 下对应的当前基线文档；清理本地 LLM 日志与测试产物后再交付。

## 项目结构速览

```text
anna-inbox/
  app.json                         App 发布元数据与版本
  manifest.json                    权限、Host API 和必需 Executa
  src/                             前端源码
  bundle/                          Vite 构建产物，不提交

inbox-tool/
  manifest.json                    Executa 身份与版本
  src/anna_inbox_executa/          JSON-RPC 入口与工具分发
  src/mail_agent/                  Gmail、Evidence、同步与存储
  src/pyproject.toml               Python 包版本

scripts/
  google_token/                    本地 Gmail OAuth helper
  sync/                            Executa 身份同步检查
  dev/                             Windows/WSL uv wrapper
```

## 进一步文档

文档状态和分类见 [`anna-inbox/docs/README.md`](anna-inbox/docs/README.md)。代码与 manifest 优先于任何设计文档。
