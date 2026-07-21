# Anna Inbox

Anna Inbox 2.0 是 Anna App 中的 Gmail 工作台。产品主界面由 Inbox Workspace、Mail Detail 和 Anna AI Sidebar 组成；AI 侧栏是唯一智能入口。Brief 产品面已下线，不再代表 2.0 主路径。

当前发布基线：App `2.1.3` / Tool `2.2.3`。本版本重点：AI 侧栏统一与 Brief 下线、Ask 搜索/回答收敛、邮件详情滚动与附件预览在自动同步下的稳定性，以及既有富文本外发与 `start_ai_turn` 路由能力。

## Language

**Mailbox**

一个已授权并可在 Anna Inbox 中切换的 Gmail 邮箱。避免使用 source account。

**Inbox Workspace**

2.0 主工作区，负责邮件列表、邮箱文件夹、筛选、同步和邮件状态操作。避免将其称为 Brief 页面。

**Mailbox View**

Inbox、Todos、Starred、Snoozed、Done、Drafts、Sent、Trash、Spam 或 All mail 中的一个视图。

**Mail Detail**

线程详情抽屉，包含消息正文、附件、AI overview、草稿编辑和发送操作。它不同于旧 Attention Card 的 Handle 视图。

**Anna AI Sidebar**

主界面左侧的对话入口。默认调用 Executa `start_ai_turn`：由本地 Router 在白名单工具中选型（对话、搜索、总结、写/改稿、整理建议、记忆等），并透传只读屏上上下文；意图不清时返回澄清选项，用户选定 `routing_intent` 后直达范围级计划。整理类仅产出确认卡片，须用户确认后 mutation。

**Saved prompts / AI Memory**

AI Personalization：可复用提示词与长期偏好短句（无邮件正文）。Memory 每 turn 注入；Saved prompts 经输入框 ↑ 选择。

**Local Draft**

按 mailbox 和 thread 持久化、尚未发送到 Gmail 的用户草稿。可同时保存 `body`（纯文本）与 `body_html`（白名单净化后的富文本）。发送成功或用户丢弃后删除。

**Brief（已下线）**

历史上的邮箱注意力扫描工作流，曾产出 Attention Cards。产品面与主路径已下线；能力由 AI 侧栏 Ask / organize 承接。Brief 不是 2.0 Inbox Workspace 的同义词。

**Attention Card**

Brief 产出的待处理事项，包含 reply、review 或 cleanup 语义。

**Ask / Custom Scan**

由自然语言请求驱动的邮箱搜索与综合回答流程。AI Sidebar 可以触发该能力。

**Custom Scan Plan**

Ask 生成并可再次执行的查询计划，不等同于 Brief Scan Plan。

**Scan Plan**

Brief 的扫描窗口、数量和行为偏好。

**Contact Memory**

按 mailbox 和 contact 隔离的长期线程摘要，用于补充当前线程之外的关系上下文。

## Product boundaries

- 2.0.1 只支持 Gmail；Outlook 仍是未来方向。
- 设置页含 AI Personalization（Saved prompts / Memory）与 Connectivity check（LLM / Gmail API 延迟轮询间隔，持久化到 inboxSettings）。
- AI 侧栏底部展示 LLM 与 Gmail API 连通状态及延迟（ms）；点击各自手动重测，定时轮询并行刷新。检测请求去重，扫描或 AI turn 期间暂停轮询；后端反向 RPC 响应直通，Gmail 的账号、token 与 HTTP 请求共享 12 秒总预算。
- 调用链诊断只返回随机 trace ID、阶段、耗时、稳定 endpoint 类别、HTTP 状态码、token 来源枚举和错误类型；禁止包含邮箱地址、邮件内容、查询参数、提示词、模型输出或凭据。
- AI 侧栏 Ask 只检索 `ui_context.mailbox` 当前活动邮箱；`selected_mailboxes` 仅作界面上下文，不可扩大问答检索范围。
- AI 侧栏回复先完整解析 Markdown 再按稳定结构揭示；指定联系人检索不得放宽 `from:`/`to:`；Replace draft 直接覆盖编辑栏，Discard 后禁止旧草稿自动回填。
- 邮件发送、标记已读、标签变更、移至垃圾箱与整理确认必须来自明确用户操作。
- 凭据不作为工具参数传递，也不得写入日志或持久化状态。
