# AGENTS.md — 核心开发规约与 AI 指引

> **AI 必读**：本仓库遵循高度一致的开发约定。在修改任何代码、调试问题或执行发布同步前，请务必完整理解本规约并严格执行。

---

## 一、 核心协作与开发约定

### 1. 沟通与确认
- **先对齐后编码**：在编写任何代码（无论是前端还是后端）前，必须确保对功能需求、修改范围和架构约束的理解完全对齐。如存在任何歧义，**必须先与用户进行澄清沟通**，再动手修改。
- **改动最小化**：严禁无目的的全局格式化或顺手重构无关代码。每次修改应仅限于与当前 Task 直接相关的范围，设计遵从最简可行原则。在完成修改后，需明确说明验证命令和验证结果。
- **环境安全保护**：在执行任何代码修改或环境变动前，优先检查当前工作区的 `git status`。**严禁删除、重置（如 `git reset --hard`）或覆盖任何与当前任务无关的未提交修改（Uncommitted Changes）**；如需清理环境，必须先征得明确许可。
- **限制重构范围**：修改文件时仅限定在当前 Task 影响范围内，禁止顺手重构无关文件或批量格式化未涉及的代码。

### 2. 交付与测试
- **前端部署限制**：前端 `anna-inbox` 构建完成后，**严禁在后台或前台擅自运行**，必须由用户手动运行并亲自验收。
- **注释要求（重要）**：所有的后端代码编写（Python）都必须有**详细清晰的中文注释**。如果读取到的既有后端代码缺少中文注释，在修改该部分时应当主动予以补充。
- **文档规范**：所有设计文档、变更说明等，必须放置于 `anna-inbox/docs/` 目录下，且文档必须使用**中文**编写。

### 3. 提交前的审核与准备
在被要求“提交前审核 / 准备提交 / Release”时，AI 必须严格执行以下三项检查：
1. **更新当前版本号**：App 与 Tool 的主版本和次版本可以独立维护，但三段语义化版本中的小版本（Patch，第三段）必须保持一致。无特别指定时，各自将 Patch +1；准备提交前必须校验两端 Patch 相同，如不一致不得继续发布准备。
2. **同步更新项目文档**：必须同步更新 `README.md`、`CONTEXT.md`、`AGENTS.md` 以及 `anna-inbox/docs/` 下对应的基线文档。
   - **更新内容包含**：App/Tool 版本号、当前版本包含的具体内容、某个功能的完成进度。
   - **基线清理**：项目基线文档**只保留当前 Tool 版本下**的（例如 `2.3.4架构与发布基线.md`），在更新版本后，必须删除历史旧版本的基线文档。
3. **Git Commit Message 规范**：分析当前工作树中的代码改动，生成符合以下格式 of 中文 Commit 消息。注意：**不要**在 Commit 消息中包含任何“测试补充”、“文档更新”、“版本同步”的噪音，交由用户审核后手动提交。
   ```md
   version: (Tool的版本号，如 2.3.4)
   - 改动内容1...
   - 改动内容2...
   ```
4. **清理临时与测试产物**：提交前必须删除本地生成的临时日志与测试产物
   - `inbox-tool/.data/llm_logs/` 下的 LLM 运行日志
   - `tests/artifacts/` 下的测试产物

---

## 二、 项目基线

### 1. 当前版本与权威文件位置
- **App (前端)**：`2.3.6` — 权威定义文件：`anna-inbox/app.json`
- **Tool (Executa/后端)**：`2.4.6` — 权威定义文件：`inbox-tool/manifest.json`

### 2. 前端项目基线 (App)
- **UI 架构与唯一入口**：系统唯一智能入口为 **Inbox Workspace + AI 侧栏**。Brief 产品面已彻底下线，AI 侧栏只读主路径中不再暴露或使用旧的 `search_email` 或 `read_email` 工具直接拉取 Gmail，一律改由 `query_mail_evidence` 托管。
- **滚动与草稿交互**：AI 生成内容过程中，侧栏滚动条必须自动滑动到底部；草稿产物（卡片形式）出现后强制贴底展示。
- **分页控制**：前端列表分页固定首屏为 100 条（`INBOX_FEED_PAGE_SIZE=100`），触底自动续页（无 Show more 按钮）。Inbox 标签角标上限展示为 `99+`。
- **引言与引用**：仅将本轮用户确认（Confirmed Evidence）的内容打上 `THREAD_REF` 进行引用。前端自动过滤未确认的引用或直接 Gmail 链接；引用按钮支持打开详情抽屉。
- **草稿两步确认流**：AI 生成回复草稿必须分两步：第一步输出回复摘要并提请确认，第二步用户确认后，前端才展示 `draft_reply` 编辑卡片。「仅正文」请求不展示任何卡片。
- **本次发布变更**：Inbox Workspace 收敛为 5 个顶层箱组（Inbox / Done / Sent / Spam / Trash），Inbox 与 Sent 各自使用紧凑子标签（All / Starred / Todos / Snoozed；Sent / Drafts），下线旧 Important/Other/自定义 Split 导航与 Manage Splits 入口；Sent 邮件不再隐含 Done，Done 为独立 workflow 状态；新增归档操作；AI 侧栏产品默认走本地 Sampling，host 仅保留为隐藏 localStorage 调试开关；邮箱发现以平台快照为准，平台删除的邮箱自动清理本地数据；`上周/last week` 检索按完整自然周解析；Tool 版本升级为 `2.4.6`。

### 3. 后端项目基线 (Tool/Executa)
- **通信与 RPC**：
  - Executa 采用 JSON-RPC 2.0 over stdio 通信，必须确保**每行只有一条 UTF-8 JSON**。
  - `stdout` 仅允许输出合法的 JSON-RPC 响应，所有日志、调试或诊断输出**必须写入 `stderr`**，严禁混入 `stdout`。
- **多 Invoke 路由设计**：调用 Host API 时，全链路必须注入并透传 `params.context.invoke_id`；多线程/协程后台任务必须进行跨线程 re-bind，以防并发 invoke 超时或返回路由丢失。
- **只读检索流**：AI 侧栏通过只读工具 `query_mail_evidence` 获取上下文（执行 Scope 解析 -> 生成 QueryPlan -> 检索本地缓存 Evidence）。禁止 AI 隐式 Gmail 回源，普通零命中时严禁回源。
- **邮箱同步**：后台执行 180 天 priority metadata 同步，随后通过 History API 注册进行 Watch 监听与增量 backfill。列表展示天数 `display_range_days` 仅控制 UI，不限制 AI 全局检索。
- **存储架构**：APS 仅允许同步 AI Ask 历史、工作流分类（Todo/Done/Snooze）、用户设置和草稿；其余大量邮件元数据必须保留在本地，利用 Etag 机制避免并发覆盖冲突。
- **连通性与检测**：反向 RPC 响应必须由 stdin 线程直接路由；LLM / Gmail 检测共用 12 秒后端总预算。

---

## 三、 代码路径与开发约束

### 1. 前端关键路径 (`anna-inbox/`)
- `src/features/home/HomeView.tsx`：2.0 工作台、AI 侧栏、账户切换和邮件列表；包含分页加载与抽屉交互控制。
- `src/features/mail-detail/`：线程详情、正文渲染、富文本编辑器草稿、附件预览。
- `src/app/useAppController.ts`：状态控制器。管理 Host Agent Session，以及本地 local 侧栏的兼容模式开关（`localStorage anna-inbox-ai-sidebar-mode`）。
- `src/api/agentSessionClient.ts`：Host Session 的帧流解析与 DONE 标记处理。
- `src/api/mailAgentClient.ts`：Executa 工具调用的 facade 代理，**所有前端组件必须通过此 facade 间接调用工具，严禁在组件中直接散落工具名**。
- `src/features/home/aiMessageFormatting.ts`：AI 输出 Markdown 文本的归一化与表格折叠过滤。

### 2. 后端关键路径 (`inbox-tool/`)
- `src/executa_sdk/context.py`：负责 `invoke_id` 的存储、传递、跨线程池上下文绑定。
- `src/anna_inbox_executa/`：JSON-RPC 协议入口、分发与本地 session 会话管理。
- `src/mail_agent/evidence_flow.py`、`local_query.py`：本地数据库检索，QueryPlan 组装以及 Gmail 托底取证控制。
- `src/mail_agent/mail_providers/gmail/mailbox_sync.py`：History 监听与 All-mail 同步。
- `src/mail_agent/storage/`：APS 存储与 Local 本地存储的抽象。

---

## 四、 协议、安全与开发规约

### 1. 安全与凭证保护
- **凭证安全性**：凭据仅能在 manifest 中声明，通过 `params.context.credentials` 传递。**严禁**在任何日志（尤其是 `stderr`）、临时文件或错误跟踪中记录 access token、refresh token、API key 或完整的 credentials 上下文。
- **大内容传输**：严禁将大体积的内容（如邮件正文原文、大附件、原始二进制）塞进 JSON-RPC 的 result 中，也不得存入 APS KV。必须使用 Host transient upload、APS files 或本地 loopback URL。
- **平台超时诊断**：诊断只允许包含随机 trace ID、耗时和错误类型，禁止日志含邮箱、查询、邮件内容或凭据。

### 2. 开发与存储规约
- **数据一致性校验**：统一使用 `mail_agent/storage/ops.py` 存取数据，**严禁绕过高层入口直接操作底层 KV 存储**。
- **存在性校验**：读取 KV 结果时，必须通过 `result.get("exists")` 判断键值是否存在。`null`、`false`、`0`、`[]` 均可能为合法业务值，不可直接用隐式布尔值判断。
- **并发与乐观锁**：并发存储更新使用 `etag` / `if_match` 乐观锁机制保护数据一致性。
- **安全变更 Guardrails**：Gmail 的状态变更（如标记已读、移至垃圾箱、发送草稿等）属于高危写操作，**必须由明确的用户交互行为触发**，严禁 AI 隐式修改。
- **本地 LLM 日志调试**：`inbox-tool/.data/llm_logs` 为本地环境允许下产生的 LLM 日志。在优化或修复 LLM 相关问题时，**应该先去读取相关日志，然后再去处理**。

### 3. 实现与代码修改约束
- **UI 组件与样式规范**：修改前端 UI 时，优先复用现有组件和样式，**不得随意新增全局样式**。不要引入新的 UI 库，除非明确要求。
- **组件修改防错**：修改组件时，务必注意 props、状态管理和副作用（useEffect）的影响，防止不必要的重复渲染或状态不一致。
- **高危与安全提示**：涉及表单、登录、权限判断时，要额外说明潜在的技术或安全风险。
- **响应式与多端兼容**：如果修改了页面结构，必须说明该修改对移动端和响应式布局（窄屏）的影响。
- **DTO 定义对齐**：修改邮件 DTO 时，须同时核对并修改 `anna-inbox/src/types/mail.ts`、前端 API facade 和后端返回边界，确保字段定义一致。
- **核心逻辑修改门槛**：在修改协议、Gmail 授权、LLM sampling、存储或卡片 schema 前，**必须先阅读对应当前的文档和实现，严禁凭旧设计记录猜测**。

---

## 五、 版本与发布同步规则

App 与 Tool 采用**主版本/次版本解耦、Patch 同步模式**：

- App 与 Tool 的主版本和次版本可以独立演进。
- 两端版本号的第三段 Patch 必须始终一致，例如 App `2.3.6` 对应 Tool `2.4.6`。
- 修改任一端版本时，必须同步检查另一端的 Patch；若不一致，必须先完成版本对齐，再执行提交前校验和发布。

| 端 | 权威文件 | 同步文件清单 |
| --- | --- | --- |
| **App (前端)** | `anna-inbox/app.json` | `AGENTS.md` （项目基线中的版本号） |
| **Tool (后端)** | `inbox-tool/manifest.json` | 1. `inbox-tool/src/pyproject.toml` (`[project] version`) <br>2. `anna-inbox/executas/inbox-tool/executa.json` (`version`) <br>3. `anna-inbox/manifest.json` (`required_executas[].min_version`) <br>4. `AGENTS.md` （项目基线中的版本号） |

### 版本修改后的操作指引
1. 修改 **Tool** 版本的权威文件 `inbox-tool/manifest.json` 及其上述同步文件。
2. 运行 identity 同步脚本，以将 Tool 的版本信息写入 `executa.json` 和 `min_version` 约束中：
   ```sh
   python3 scripts/sync/sync_executa_identity.py
   ```
3. 执行校验确认：
   ```sh
   python3 scripts/sync/sync_executa_identity.py --check
   ```
4. 如果被要求进行发布前的全套校验，请确保在 **Tool/后端** 路径下运行 JSON-RPC Smoke Test：
   ```sh
   printf '%s\n' '{"jsonrpc":"2.0","method":"describe","id":1}' | uv --directory inbox-tool/src run anna-inbox-executa
   printf '%s\n' '{"jsonrpc":"2.0","method":"health","id":1}' | uv --directory inbox-tool/src run anna-inbox-executa
   ```
5. **前端测试与构建验证**：
   ```sh
   cd anna-inbox
   npm test
   npm run build
   ```
   （注意：构建生成的 `bundle/` 目录属于发布产物，严禁提交到 git）
