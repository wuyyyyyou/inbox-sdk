# AGENTS.md

## 开发约定

- 代码编写前先保证对功能和内容的理解和我完全对齐，发现存在不明确的内容先与我沟通，最后再进行代码编写
- 每次只改和当前任务直接相关的文件，设计遵从最简原则，完成前说明验证命令和结果
- 所有的后端代码编写都要有详细清晰的`中文`注释，如果读取到的后端代码没有`中文`注释，应该及时补充
- 所有文档必须在 `anna-inbox/docs/` 中，且文档必须为中文文档
- 对于`提交前的审核`/`准备提交`的需求，需要完成以下几件事
  - 更新当前版本号：App 与 Tool **独立**维护（见下方「版本约束」）；未指定时各自按小版本 +1，存在不明确的内容先与我沟通
  - 更新项目文档：`README.md` `CONTEXT.md` `AGENTS.md` 以及 `anna-inbox/docs` 下的文档，存在不明确的内容先与我沟通。更新内容包括：
    - 版本号（App / Tool 分别写清）
    - 当前版本内容
    - 某个功能完成进度
    - 项目基线文档（只保留当前Tool版本下的，修改完成以后前面版本的基线文档应该删除）
- 根据当前工作树内容生成git commit的中文消息，不要包含测试补充、文档更新、版本同步的消息，最后我审核后手动提交，message格式如下：
    ```md
    version: (tool的版本号)
    - 消息内容...
    - 消息内容...
    ```

## 项目基线

- **App（前端）**：`2.1.6` — 位于 `anna-inbox/`
- **Tool（Executa）**：`2.2.6` — 位于 `inbox-tool/`

- 唯一智能入口：Inbox Workspace + AI 侧栏（`start_ai_turn`）；Brief 产品面下线。
- AI生成内容过程中，滚动条自动滑动到底部。
- `anna-inbox/src/features/home/HomeView.tsx`：2.0 Inbox 工作台、AI 侧栏、账户切换和邮件列表；侧栏提交透传只读 `inboxListContext`（视图/筛选/搜索与 todo/done/snoozed ids）；列表「加载更多 / 扩大范围」在后台 refresh 时仍可点，同步中按钮文案为 Syncing；Router 澄清弹层支持范围选择；详情打开期间列表短暂丢消息不关抽屉。
- `anna-inbox/src/features/mail-detail/`：线程详情、正文、富文本草稿（`RichTextEditor`）和附件预览；详情打开后固定滚到线程底部；同线程同步刷新不清附件预览。
- `anna-inbox/src/app/useAppController.ts`：主要状态与工作流控制；AI 侧栏默认 Host Agent Session，支持流式工具结果、取消与会话清理；邮件缓存刷新后预热联系人头像；详情页协助统一走 `start_ai_turn`；soft prune 保留 `mailDetailMessageId`。
- `anna-inbox/src/api/agentSessionClient.ts`：Host Agent Session 创建、流式帧解析、工具结果消费、run 取消和会话清理。
- `anna-inbox/src/api/mailAgentClient.ts`：所有 Executa 工具调用的统一 facade。
- `anna-inbox/manifest.json`、`inbox-tool/src/anna_inbox_executa/common.py` 与 `mailbox_tools.py`：Google Connected accounts 声明、多账号发现状态和安全错误提示；APS Files reverse-RPC 响应始终可路由。
- `inbox-tool/src/anna_inbox_executa/`：JSON-RPC 入口与工具分发（含 Agent 细粒度白名单工具、`start_ai_turn`、整理确认与 Saved prompts / Memory）；Sampling 预算快照与短 wait 建 run；Brief 主路径工具已移除。
- `inbox-tool/src/anna_inbox_executa/ai_agent_tools_flow.py`、`inbox-tool/src/mail_agent/local_query.py`：Agent 工具白名单执行；`search_email` 实时 Gmail 搜索（不回退本地缓存），普通搜索只拉返回条数摘要、工作流筛选候选上限 60，请求超时 12s；本地工作流 `is:todo/done/snoozed` 仅 AND；单次命中上限 20，线程引用短映射。
- `inbox-tool/src/mail_agent/ai_turn/`：详情/兼容路径使用的本地 Router 与白名单 Runner；侧栏主路径已迁移到 Host Agent Session。
- `inbox-tool/src/mail_agent/mail_providers/gmail/adapter.py`：Gmail API、OAuth、本地缓存和正文解码；Gmail HTTP / token 请求支持可配置超时；联系人头像仅 People API（无 Gravatar 回退）；inline/CID 不计入下载附件。
- `inbox-tool/src/mail_agent/mail_providers/gmail/outgoing_html.py`：外发富文本白名单净化。
- `inbox-tool/src/anna_inbox_executa/v2_tools.py`：收件附件优先 Host transient upload，失败回退 APS Files；反向 RPC 超时 20s；邮箱级 workflow state 与 AI Ask history 读写工具。
- `inbox-tool/src/mail_agent/storage/`：APS/local storage 的统一 async 层；`inbox_workflow_state` 与 `ask_history` 按邮箱隔离，乐观并发 etag。
- `inbox-tool/src/mail_agent/ask/`：Ask 规划、搜索和回答（条数解析、预算分配、截断 JSON 不伪装成功）。
- `inbox-tool/src/mail_agent/core/`：custom scan 等残留；Brief Phase1 管线已删除。
- 连通性检测：反向 RPC 响应必须由 stdin 线程直接路由；LLM / Gmail 检测共用 12 秒后端总预算，避免与业务 worker 或正常邮箱操作互相阻塞。
- 平台超时诊断：每个 invoke 及后台 AI run 以独立安全 trace 记录 Sampling、Connected accounts、Gmail HTTP 和 Executa 阶段耗时；不得包含邮件或凭据。

`anna-inbox/bundle/` 是构建产物，不手写、不提交。

## 协议与安全

- Executa 使用 JSON-RPC 2.0 over stdio，每行一条 UTF-8 JSON。
- `stdout` 只能输出协议响应，日志写 `stderr`。
- 凭据在 manifest 中声明，通过 `params.context.credentials` 接收；不得新增密钥工具参数。
- 不记录 access token、refresh token、API key、authorization header 或完整 credential context。
- 大内容使用 Host upload、APS files 或本地 loopback URL，不放入 APS KV 或 JSON-RPC result。
- Gmail 状态变更和发送操作必须由明确用户操作触发，并保留现有 guardrail。

## 开发与版本

后端安装：

```sh
cd inbox-tool/src
uv sync
```

前端：

```sh
cd anna-inbox
npm test
npm run build
```

前端启动：

```sh
cd anna-inbox
anna-app dev
```


Anna App 本地开发读取 `anna-inbox/app.json` 和 `anna-inbox/executas/inbox-tool/executa.json`。旧 `anna-inbox/dev-wsl.sh` 不是权威入口。

### 版本约束

App 与 Tool **版本号解耦，互不强制对齐**：

| 端 | 当前版本 | 权威文件 | 须同步的文件 |
| --- | --- | --- | --- |
| App | `2.1.5` | `anna-inbox/app.json` | `./AGENTS.md`（项目基线） |
| Tool | `2.2.5` | `inbox-tool/manifest.json` | `inbox-tool/src/pyproject.toml`、`anna-inbox/executas/inbox-tool/executa.json`、`anna-inbox/manifest.json#required_executas[].min_version`、`./AGENTS.md`（项目基线） |

规则：

- 只改前端 / App 发布：只 bump **App** 版本（`anna-inbox/app.json`），**不要**改 Tool 版本。
- 只改后端 / Executa 发布：只 bump **Tool** 版本；平台若报「同版本已发布且内容不同」，必须再 bump Tool（不可覆盖已发布版本）。
- Tool 线自 `2.1.1` 起独立演进，现进入 `2.2.x`；App 线自 `2.1.1` 起。当前基线：App `2.1.6` / Tool `2.2.6`。
- `min_version` 跟随 **Tool** 版本，不跟随 App 版本。
- 提交前审核时，若未说明只升哪一端，先与我确认，再改版本号。

Executa 身份以 `inbox-tool/manifest.json` 为单一来源。修改其 `tool_id` 或 **Tool** 版本后，同步：

```sh
python scripts/sync/sync_executa_identity.py
python scripts/sync/sync_executa_identity.py --check
```

脚本会把 Tool 的 `version` 写入 `executa.json` 与 `min_version`；**不会**改 `anna-inbox/app.json` 或 `pyproject.toml`，发布前须单独核对。

## 测试

后端测试位于 `inbox-tool/src/tests/`，当前为可直接执行的脚本式测试。按改动范围运行相关文件；协议、manifest 或 dispatcher 变更还必须运行：

```sh
printf '%s\n' '{"jsonrpc":"2.0","method":"describe","id":1}' | uv --directory inbox-tool/src run anna-inbox-executa
printf '%s\n' '{"jsonrpc":"2.0","method":"health","id":1}' | uv --directory inbox-tool/src run anna-inbox-executa
```

前端测试使用 Vitest；构建必须先通过 TypeScript typecheck。

## 实现约束
- 前端组件不得直接散落工具名称；统一通过 `mailAgentClient.ts`。
- 修改前端UI时，优先复用现有组件和样式，不得随意新增全局样式。
- 不要引入新的UI库，除非明确要求。
- 修改组件时，注意 props、状态管理和副作用。
- 涉及表单、登录、权限判断时，要额外说明风险。
- 如果修改页面结构，请说明对移动端和响应式布局的影响。
- 存储调用统一通过 `mail_agent/storage/ops.py`，不要绕过高层入口。
- 读取 KV 时用 `result.get("exists")` 判断存在性；`null`、`false`、`0`、`[]` 都可能是合法值。
- 并发存储更新使用 `etag` / `if_match`。
- 修改邮件 DTO 时同时核对 `anna-inbox/src/types/mail.ts`、API facade 和后端返回边界。
- 修改协议、Gmail auth、LLM sampling、存储或卡片 schema 前，先阅读对应当前文档和实现，不凭旧设计记录猜测。
- 保留无关工作树改动，不覆盖 token、本地缓存、release artifact 或用户密钥。
