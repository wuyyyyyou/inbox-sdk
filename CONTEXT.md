# Anna Inbox

Anna Inbox 2.0 是 Anna App 中的 Gmail 工作台。产品主界面由 Inbox Workspace、Mail Detail 和 Anna AI Sidebar 组成；Brief、Attention Card 和 Custom Scan 是仍由后端提供的工作流能力，不再代表 2.0 的整体界面结构。

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

主界面左侧的对话入口。它根据请求和当前邮件上下文路由到普通对话、邮箱扫描或邮件上下文协助。

**Local Draft**

按 mailbox 和 thread 持久化、尚未发送到 Gmail 的用户草稿。发送成功或用户丢弃后删除。

**Brief**

后端中稳定、可续跑的邮箱注意力扫描工作流，产出 Attention Cards。Brief 不是 2.0 Inbox Workspace 的同义词。

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
- 设置入口暂时不在前端展示，后端配置和工具契约保留。
- 邮件发送、标记已读、标签变更和移至垃圾箱必须来自明确用户操作。
- 凭据不作为工具参数传递，也不得写入日志或持久化状态。
