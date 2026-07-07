# AI 侧边栏意图路由

## 当前实现

AI Sidebar 位于 `HomeView.tsx`。会话历史保存在前端 localStorage，最多保留 30 条索引；新会话不会自动恢复成旧会话。

`app/aiRoute.ts` 先做确定性路由：

- `mail_context`：当前邮件或已有 draft 上的总结、回复、改写请求。
- `scan`：找邮件、整理邮箱、未读、紧急、附件等邮箱检索请求。
- `chat`：不需要邮箱检索的一般对话。

邮件上下文请求调用 `start_inbox_mail_prompt`，scan 调用 Ask/custom scan 链路，普通对话使用 Anna LLM。长任务使用 run ID 轮询，用户可停止前端等待。

## 邮件引用

Ask 结果可以携带 mailbox、message ID 和 thread ID。用户点击引用后，前端定位已有邮件或加载对应线程，并打开 Mail Detail。

## 安全边界

- 上一版 draft 作为引用内容包裹，不能被当成新系统指令。
- 只有当前或最近明确的邮件上下文可以进入 `mail_context` 路由。
- 路由失败应给出可恢复错误，不能静默执行 Gmail mutation。
- AI 生成内容不会自动发送邮件或改变 Gmail 状态。
