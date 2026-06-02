# AGENTS.md

## 项目概览

这个仓库是一个 Anna App 项目，主体目录是 `anna-inbox/`。

- `anna-inbox/manifest.json` 声明 App、静态 SPA bundle、必需 Executa、Host API、权限和本地开发默认值。
- `anna-inbox/src/` 是计划中的前端源码目录，使用 Vite + React + TypeScript 组织 Anna Inbox UI。
- `anna-inbox/bundle/` 是 Anna App 读取的静态 SPA 构建产物目录，不作为主要手写源码维护。
- `anna-inbox/executas/tool-zhaopy-inbox-tool-373sf2et/` 是邮件代理的 Python Executa 插件。
- `anna-inbox/executas/tool-zhaopy-inbox-tool-373sf2et/src/mail_agent/` 包含 Gmail 扫描、Brief 管线、Ask 流程、存储、卡片、LLM 和本地缓存逻辑。
- `anna-inbox/executas/tool-zhaopy-inbox-tool-373sf2et/src/zhaopy_mail_agent/main.py` 是 JSON-RPC stdio 入口，同时内嵌 `describe` 返回的 Executa manifest 数据。
- `anna-inbox/.docs/` 是复制到仓库内的 Anna 协议参考。修改平台协议相关行为前先读这里，不要凭印象猜。
- `anna-inbox/docs/` 是项目设计文档和实施计划，主要为中文。

核心产品形态：

- Brief 是工作流管线：Gmail 扫描 -> Phase 1 分类 -> Phase 2 判断 -> 规则守护 -> 卡片生成 -> 存储 -> 前端卡片视图。
- Ask 是 agent 式自定义查询：规划/搜索/读取选定邮件 -> 一次 LLM 生成答案容器 -> 前端展示结果和历史。
- 存储统一走 `storage_ops.py`，平台可用时由 Anna APS 支撑，本地开发时可走 local JSON storage。

## 关键协议背景

修改协议、存储、凭据或 Executa 启动行为前，先阅读这些文件：

- `anna-inbox/.docs/anna-rpc/protocol-spec.md`
- `anna-inbox/.docs/anna-rpc/protocol-spec.zh-CN.md`
- `anna-inbox/.docs/anna-rpc/authorization.zh-CN.md`
- `anna-inbox/.docs/anna-storate/01-concepts-and-decision.md`
- `anna-inbox/.docs/anna-storate/02-executa-aps-reverse-rpc.md`
- `anna-inbox/.docs/anna-storate/03-anna-app-host-api-storage-files.md`

本项目必须遵守的 Executa 协议规则：

- 使用 JSON-RPC 2.0 over stdio，每行一条 JSON 消息。
- `stdout` 只能输出协议响应。日志和调试信息必须写到 `stderr`。
- 消息必须使用 UTF-8。
- 工具凭据在 manifest 的 `credentials` 中声明，通过 `params.context.credentials` 接收；不要把密钥暴露为工具参数。
- 大字节内容不能通过 JSON-RPC result 返回。使用 APS files/object storage 或 host upload，并在工具结果里返回轻量引用。
- APS KV 只用于小型 JSON 状态，例如 cursor、计划、偏好和索引。不要把大 blob、长 HTML、图片、PDF 或大数组塞进 KV。

## 安装与环境

从仓库根目录执行：

```sh
cd anna-inbox/executas/tool-zhaopy-inbox-tool-373sf2et/src
uv sync
```

Executa 项目使用 `uv` 和 `pyproject.toml`：

- 包名：`zhaopy-anna-mail-agent`
- Python：`>=3.10`
- 命令入口：`zhaopy-mail-agent = zhaopy_mail_agent.main:main`

本地密钥刻意放在仓库外。`anna-inbox/dev-wsl.sh` 会读取可选的本地环境文件：

```sh
$HOME/.anna-mail-agent.env
```

不要提交 OAuth token、DashScope key、Gmail token 文件、生成的本地缓存或 `.local_storage`。

## 开发流程

从仓库根目录启动 Anna App：

```sh
cd anna-inbox
PORT=5180 ./dev-wsl.sh
```

`dev-wsl.sh` 实际启动：

```sh
anna-app dev --port "$PORT" --executa "dir=...,tool_id=tool-zhaopy-inbox-tool-373sf2et,type=python,command=env UV_PROJECT_ENVIRONMENT=... UV_LINK_MODE=copy uv --directory src run zhaopy-mail-agent"
```

开发脚本使用的本地 Gmail token 目录：

```sh
anna-inbox/executas/anna-inbox-tool/.secrets/gmail_tokens
```

直接 smoke-test Executa manifest：

```sh
printf '%s\n' '{"jsonrpc":"2.0","method":"describe","id":1}' \
  | uv --directory anna-inbox/executas/tool-zhaopy-inbox-tool-373sf2et/src run zhaopy-mail-agent
```

直接 smoke-test health：

```sh
printf '%s\n' '{"jsonrpc":"2.0","method":"health","id":1}' \
  | uv --directory anna-inbox/executas/tool-zhaopy-inbox-tool-373sf2et/src run zhaopy-mail-agent
```

新增、删除或重命名工具时，保持这些文件同步：

- `anna-inbox/executas/tool-zhaopy-inbox-tool-373sf2et/src/zhaopy_mail_agent/main.py`
- `anna-inbox/executas/tool-zhaopy-inbox-tool-373sf2et/manifest.json`
- `anna-inbox/executas/tool-zhaopy-inbox-tool-373sf2et/executa.json`
- 准备 release 时同步 `anna-inbox/executas/tool-zhaopy-inbox-tool-373sf2et/release/` 下的副本
- 变更已发布 Executa 版本时，同步 `anna-inbox/manifest.json` 的 `required_executas[].min_version`

## 测试说明

从 Executa 的 `src` 目录运行本地 Python 测试：

```sh
cd anna-inbox/executas/tool-zhaopy-inbox-tool-373sf2et/src
uv run python tests/test_llm_json_repair.py
uv run python tests/test_storage_integration.py
```

当前测试是脚本式 async 测试，不是 pytest 测试套件。

修改 JSON-RPC 行为前，还要运行直接 `describe` smoke test，并检查输出是否是合法的 newline-delimited JSON：

```sh
printf '%s\n' '{"jsonrpc":"2.0","method":"describe","id":1}' \
  | uv --directory anna-inbox/executas/tool-zhaopy-inbox-tool-373sf2et/src run zhaopy-mail-agent
```

修改前端源码后，先从 `anna-inbox/` 运行前端构建，生成 `bundle/` 静态产物，再在 Anna App UI 中验证。前端工程化后，`npm run build` 应先执行 TypeScript typecheck，再执行 Vite build。

## 代码组织

邮件代理核心模块：

- `mail_adapter.py`：Gmail API 访问、OAuth token 解析与刷新、本地缓存。
- `pipeline.py`：Brief 和 custom scan 的编排。
- `phase1.py`：基于邮件头的批量分类，输出 `reply`、`review` 或 `ignore`。
- `judgment.py`：Phase 2 LLM 判断、解析和归一化。
- `guards.py`：规则型安全守护。
- `card_service.py`：持久卡片构建、cleanup bundle 构建、前端格式化和合并规则。
- `storage_types.py`：卡片、运行记录、偏好、计划和已处理消息的数据类。
- `storage_ops.py`：高层 async 持久化操作。
- `storage_client.py`、`local_storage.py`：APS/local storage 客户端接线。
- `llm.py`：DashScope 和 Anna Sampling JSON 调用、JSON repair fallback、安全 fallback。
- `planner.py`：自定义 Ask 规划。

有用的设计文档：

- `anna-inbox/docs/Brief管线全链路设计.md`
- `anna-inbox/docs/Ask-重设计计划.md`
- `anna-inbox/docs/Anna-Sampling-Brief全链路分析.md`
- `anna-inbox/docs/Draft与Summary持久化计划.md`
- `anna-inbox/docs/HTML邮件处理方案.md`
- `anna-inbox/docs/PyInstaller二进制打包指南.md`

## 代码风格

- Python 代码整体偏类型化；I/O 逻辑优先 async；持久化领域对象主要使用 dataclass。
- 存储 API 保持 async，并通过 `storage_ops.py` 走统一入口；不要让调用方直接散落访问 APS/local client。
- 协议响应必须 JSON 可序列化且尽量紧凑。除非工具语义明确需要，否则不要返回原始 Gmail 正文或大 artifact。
- Executa 进程里的诊断信息写到 `stderr`。除协议写出器输出 JSON-RPC 响应外，不要用普通 `print()` 写 `stdout`。
- 保留现有双语风格：已有文件用中文注释表达产品/领域意图时，可以继续使用中文注释。
- 保持改动聚焦。除非任务明确要求，不要在一次改动里同时重构静态前端、管线和协议入口。

## 前端约束

- 前端工程根目录是 `anna-inbox/`；源码入口使用 `anna-inbox/src/index.html` 和 `anna-inbox/src/main.tsx`。
- 前端技术栈使用 Vite + React + TypeScript。第一阶段不引入路由库、Redux/Zustand、Tailwind、CSS-in-JS、组件库或图标库。
- 前端使用 npm。提交 `package-lock.json`，不要提交 `bundle/` 构建产物。
- `package.json` 第一阶段只需要最小脚本：`typecheck`、`build`、`test`。不要添加 `npm run dev`；本地验证通过 Anna App 测试环境完成。
- `npm run build` 必须先运行 `tsc --noEmit`，再运行 Vite build。
- 前端测试使用 Vitest，第一阶段优先覆盖纯逻辑、DTO adapter、reducers 和 API facade。不要为了第一阶段迁移引入 React Testing Library 或 jsdom。
- `anna-inbox/bundle/` 是构建产物目录，应由 `npm run build` 生成；`bundle/` 加入 `.gitignore`，不要把它当成源码目录手写维护。
- 构建产物保持稳定入口文件名：`bundle/index.html`、`bundle/app.js`、`bundle/style.css`。
- App manifest 的 CSP 当前只允许 `'self'` 下的 script 和 style；除非有意修改 `manifest.json`，否则不要引入外部 origin。
- `anna-inbox/manifest.json` 声明的 App Host API 包括 tools、chat write、storage get/set/list/delete、LLM complete 和 window title。
- Anna Runtime 适配层是前端源码的一部分，应维护在 `src/runtime/`，保留平台注入 SDK、官方 SDK 动态导入、bundle 内 compat 的降级顺序。
- 前端组件不要直接写 Executa tool 名称；工具调用集中在 API facade 中，例如 `src/api/mailAgentClient.ts`。
- 修改卡片渲染时，对照 `card_service.py::cards_to_frontend` 和 `storage_types.PersistentCard`，确保前后端字段名保持一致。前端应手写 DTO 类型，贴合实际 JSON 返回边界。
- 第一阶段迁移目标是行为等价和视觉尽量不变；不要顺手重设计 Brief、Ask、Handle、History 或 Scan Plan。
- 前端 UI 文案第一阶段保持现有英文文案，不引入 i18n。
- 第一阶段不要修改 Executa 工具契约或 Python 后端。API facade 只封装现有工具名、参数和返回值。
- 推荐迁移顺序：先跑通最小 React shell 和 runtime，再迁移 API/types、Brief、Drawers/Scan Plan/History、Handle、Ask，最后补测试和整理样式。

## 存储约束

- 邮箱级 storage key 在 `storage_ops.py` 中构造，前缀形如 `mailbox/<sanitized-mailbox>/...`。
- 读取 KV 时使用 `result.get("exists")` 判断是否存在。`null`、`false`、`0` 或 `[]` 都可能是合法存储值。
- 需要并发更新时使用 `etag` / `if_match` 模式。
- scan state、run history、cards、preferences、custom plans、summaries、drafts 作为小 JSON 持久化。
- 大文件或持久二进制 artifact 使用 APS files/object storage，并返回引用。
- 本地测试可以参考 `tests/test_storage_integration.py` 使用 fake storage client。

## 安全与隐私

- Gmail 凭据应来自 Anna platform authorization、插件凭据、环境变量或本地 token 文件。不要新增接收凭据的工具参数。
- 不要记录 OAuth token、access token、refresh token、API key、原始 authorization header 或完整 credential context。
- 只用于 JSON repair 的 prompt 不应泄露原始邮件正文；`test_llm_json_repair.py` 已覆盖这个行为。
- mark-read 或类似发送/删除的 Gmail 状态变更需要明确产品审查，并沿用现有 guardrail 模式。
- `main.py` / `mail_adapter.py` 中的 `SUPPORTED_MAILBOXES` 和 local-token fallback 行为应保持保守。

## 发布与打包

- 当前 Executa 版本是 `1.0.2`。
- `executa.json` 声明二进制分发元数据和本地开发命令。
- `manifest.json` 声明 Executa 工具和凭据。
- `release/describe-manifest-1.0.2.json`、`release/manifest.json`、`release/executa.json` 是 release artifact；仅在准备 release 时更新。
- 修改二进制打包前先读 `anna-inbox/docs/PyInstaller二进制打包指南.md`。

## Agent 工作规则

- 修改 Anna 协议、存储、LLM sampling、Gmail auth 或卡片 schema 前，先读最近的项目文档。
- 命令如果依赖网络、凭据或外部 Anna 服务，要先明确说明，不能把它当作无条件可用。
- 除非任务明确要求，不要覆盖用户密钥、生成的 token 文件、本地存储或 release artifact。
- 保留工作树中与当前任务无关的已有改动。
- 修改代码后，运行上面列出的聚焦脚本测试，并按改动范围运行相关 JSON-RPC smoke test。

## Agent skills

### Issue tracker

本仓库的 issue 和 PRD 使用本地 Markdown 文件管理，写入 `.scratch/` 目录。详见 `docs/agents/issue-tracker.md`。

### Triage labels

本仓库使用默认五个 triage 状态：`needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix`。详见 `docs/agents/triage-labels.md`。

### Domain docs

本仓库使用 single-context 领域文档布局：根目录 `CONTEXT.md`，以及存在时的 `docs/adr/`。详见 `docs/agents/domain.md`。
