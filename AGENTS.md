# AGENTS.md

## 开发约定

- 代码编写前先保证对功能和内容的理解和我完全对齐，发现存在不明确的内容先与我沟通，最后再进行代码编写
- 每次只改和当前任务直接相关的文件，完成前说明验证命令和结果
- 所有的后端代码编写都要有详细清晰的`中文`注释，如果读取到的后端代码没有`中文`注释，应该及时补充
- 所有文档必须在 `anna-inbox/docs/` 中，且文档必须为中文文档
- 对于`提交前的审核`/`准备提交`的需求，需要完成以下几件事
  - 更新当前版本号：App 与 Tool **独立**维护（见下方「版本约束」）；未指定时各自按小版本 +1，存在不明确的内容先与我沟通
  - 更新项目文档：`README.md` `CONTEXT.md` `AGENTS.md` 以及 `anna-inbox/docs` 下的文档，存在不明确的内容先与我沟通。更新内容包括：
    - 版本号（App / Tool 分别写清）
    - 当前版本内容
    - 某个功能完成进度
    - 项目基线
- 根据当前工作树内容生成git commit的中文消息，不要包含测试补充、文档更新、版本同步的消息，最后我审核后手动提交，message格式如下：
    ```md
    version: (tool的版本号)
    - 消息内容...
    - 消息内容...
    ```

## 项目基线

- **App（前端）**：`2.0.23` — 位于 `anna-inbox/`
- **Tool（Executa）**：`2.1.4` — 位于 `inbox-tool/`

- `anna-inbox/src/features/home/HomeView.tsx`：2.0 Inbox 工作台、AI 侧栏、账户切换和邮件列表。
- `anna-inbox/src/features/mail-detail/`：线程详情、正文、草稿和附件预览。
- `anna-inbox/src/app/useAppController.ts`：主要状态与工作流控制；AI 侧栏默认 `startAiTurn`。
- `anna-inbox/src/api/mailAgentClient.ts`：所有 Executa 工具调用的统一 facade。
- `anna-inbox/manifest.json`、`inbox-tool/src/anna_inbox_executa/common.py` 与 `mailbox_tools.py`：Google Connected accounts 声明、多账号发现状态和安全错误提示。
- `inbox-tool/src/anna_inbox_executa/`：JSON-RPC 入口和工具分发（含 `start_ai_turn`、整理确认与 Saved prompts / Memory）。
- `inbox-tool/src/mail_agent/ai_turn/`：AI 侧栏本地 Router 与白名单 Runner（阶段 A+B）。
- `inbox-tool/src/mail_agent/mail_providers/gmail/adapter.py`：Gmail API、OAuth、本地缓存和正文解码。
- `inbox-tool/src/mail_agent/storage/`：APS/local storage 的统一 async 层。
- `inbox-tool/src/mail_agent/ask/`：Ask 规划、搜索和回答。
- `inbox-tool/src/mail_agent/core/`、`cards/`、`judgment_engine/`：Brief 管线。
- 连通性检测：反向 RPC 响应必须由 stdin 线程直接路由；LLM / Gmail 检测共用 12 秒后端总预算，避免与业务 worker 或正常邮箱操作互相阻塞。

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
| App | `2.0.23` | `anna-inbox/app.json` | `./AGENTS.md`（项目基线） |
| Tool | `2.1.4` | `inbox-tool/manifest.json` | `inbox-tool/src/pyproject.toml`、`anna-inbox/executas/inbox-tool/executa.json`、`anna-inbox/manifest.json#required_executas[].min_version`、`./AGENTS.md`（项目基线） |

规则：

- 只改前端 / App 发布：只 bump **App** 版本（`anna-inbox/app.json`），**不要**改 Tool 版本。
- 只改后端 / Executa 发布：只 bump **Tool** 版本；平台若报「同版本已发布且内容不同」，必须再 bump Tool（不可覆盖已发布版本）。
- Tool 线自 `2.1.1` 起独立演进；App 线继续在 `2.0.x`（或后续自行决定）演进。当前基线：App `2.0.23` / Tool `2.1.4`。
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
