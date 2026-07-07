# Anna Inbox 2.0.2

Anna Inbox 是运行在 Anna App 中的 Gmail 工作台。2.0 以完整收件箱和 AI 侧栏为主界面：用户可以浏览和处理邮件、查看线程详情、保存草稿、预览附件，并让 Anna 搜索、汇总或协助回复邮件。

## 当前能力

- Inbox、Todos、Starred、Snoozed、Done、Drafts、Sent、Trash、Spam 和 All mail 视图。
- Important / Other 分类、本地缓存、增量加载和 Gmail 刷新。
- 星标、重要、Todo、Snooze、已读、完成和移至垃圾箱操作。
- 线程详情、清洗后的文本或安全 HTML、AI overview、回复草稿与发送。
- 图片、PDF、文本附件预览以及附件下载。
- 可折叠、可调宽的 Anna AI 侧栏，支持普通对话、邮箱搜索和当前邮件上下文操作。
- 多 Gmail 账户发现与切换。
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
uv run python tests/test_storage_integration.py
```

协议 smoke test：

```sh
printf '%s\n' '{"jsonrpc":"2.0","method":"describe","id":1}' | uv --directory inbox-tool/src run anna-inbox-executa
printf '%s\n' '{"jsonrpc":"2.0","method":"health","id":1}' | uv --directory inbox-tool/src run anna-inbox-executa
```

## 版本与发布

App、Executa manifest、开发 stub 和 Python package 当前统一为 `2.0.2`。

Executa 身份以 `inbox-tool/manifest.json` 为单一来源。修改 `tool_id` 或 Executa 版本后运行：

```sh
python scripts/sync/sync_executa_identity.py
python scripts/sync/sync_executa_identity.py --check
```

发布前至少执行前端测试与构建、相关 Python 测试、身份同步检查，以及 `describe` / `health` smoke test。

## 进一步文档

当前文档索引见 [`anna-inbox/docs/README.md`](anna-inbox/docs/README.md)。
