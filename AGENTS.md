# AGENTS.md

## 项目约定

- 代码编写前先保证对功能和内容的理解和我完全对齐，发现存在不明确的内容先与我沟通，最后再进行代码编写
- 每次只改和当前任务直接相关的文件，完成前说明验证命令和结果
- 对于比较复杂的业务需求，应询问 `是否开启 subagent 进行代码实现，最后由主 agent 进行审查验收`
- 所有的后端代码编写都要有详细清晰的中文注释，如果读取到的后端代码没有中文注释，应该及时补充
- 所有文档必须在 `anna-inbox/docs/` 中
- 对于`提交前的审核`/`准备提交`的需求，需要完成以下几件事
  - 更新当前版本号：如果不指定则按小版本加1，存在不明确的内容先与我沟通
  - 更新项目所有基线文档：包括版本号信息、进度，存在不明确的内容先与我沟通
  - 根据当前工作树内容生成git commit的中文消息，不要包含测试补充、文档更新、版本同步的消息，最后我审核后手动提交，message格式如下：
    ```md
    version: x.x.x
    - 消息内容...
    - 消息内容...
    ```

## 项目基线

Anna Inbox 当前版本为 `2.0.15`。前端位于 `anna-inbox/`，后端 Executa 位于 `inbox-tool/`。

- `anna-inbox/src/features/home/HomeView.tsx`：2.0 Inbox 工作台、AI 侧栏、账户切换和邮件列表。
- `anna-inbox/src/features/mail-detail/`：线程详情、正文、草稿和附件预览。
- `anna-inbox/src/app/useAppController.ts`：主要状态与工作流控制。
- `anna-inbox/src/api/mailAgentClient.ts`：所有 Executa 工具调用的统一 facade。
- `anna-inbox/manifest.json`、`inbox-tool/src/anna_inbox_executa/common.py` 与 `mailbox_tools.py`：Google Connected accounts 声明、多账号发现状态和安全错误提示。
- `inbox-tool/src/anna_inbox_executa/`：JSON-RPC 入口和工具分发。
- `inbox-tool/src/mail_agent/mail_providers/gmail/adapter.py`：Gmail API、OAuth、本地缓存和正文解码。
- `inbox-tool/src/mail_agent/storage/`：APS/local storage 的统一 async 层。
- `inbox-tool/src/mail_agent/ask/`：Ask 规划、搜索和回答。
- `inbox-tool/src/mail_agent/core/`、`cards/`、`judgment_engine/`：Brief 管线。

`anna-inbox/bundle/` 是构建产物，不手写、不提交。设置入口在 2.0.1 前端暂时隐藏；除非任务明确要求，不删除设置相关后端工具和状态。

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

Executa 身份以 `inbox-tool/manifest.json` 为单一来源。修改其 `tool_id` 或版本后，同步：

```sh
python scripts/sync/sync_executa_identity.py
python scripts/sync/sync_executa_identity.py --check
```

保持以下版本一致：

- `anna-inbox/app.json`
- `inbox-tool/manifest.json`
- `inbox-tool/src/pyproject.toml`
- `anna-inbox/executas/inbox-tool/executa.json`
- `anna-inbox/manifest.json#required_executas[].min_version`
- `./AGENTS.md`

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
- 修改公共工具行为时，同步更新 `anna-inbox/docs/` 中的文档。
