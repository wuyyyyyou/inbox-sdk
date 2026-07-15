# AI 侧边栏意图路由

## 当前实现（2.0.18）

AI Sidebar 位于 `HomeView.tsx`。会话历史保存在前端 localStorage，最多保留 30 条索引；新会话不会自动恢复成旧会话。

**默认路径（阶段 A）**：`sendAiChatMessage` 调用 Executa `start_ai_turn`，由后端本地 Router 在白名单工具中选型，前端只透传 `user_text` 与只读 `ui_context`（当前线程、邮箱、展示天数等）。详见 [AI 对话本地 Router 与白名单工具](AI对话本地Router与白名单工具设计.md)。

**旁路路径**：`localStorage anna-inbox-use-ai-turn=0` 时仍使用 `app/aiRoute.ts` 确定性路由：

- `mail_context`：当前邮件或已有 draft 上的总结、回复、改写请求 → `start_inbox_mail_prompt` / Compose prompt。
- `scan`：找邮件、整理邮箱等 → `start_custom_scan`（Ask）。
- `chat`：一般对话 → Host `llm.complete`。

长任务使用 run ID 轮询，用户可停止前端等待。Ask 扫描默认对齐 Settings 展示天数与 Scan Plan 数量上限，初等等待 60 秒。

## 瞬时失败恢复

- AI 读取和生成调用遇到网络、超时、服务端 5xx 或 HTML 错误页时自动重试两次（0.5 秒、1 秒）。
- Ask 规划阶段若 Anna Sampling 返回空内容，不执行无效重试；改为立即生成保守的本地搜索计划，继续检索近期收件箱并标记该计划为 fallback。
- 扫描和邮件上下文生成复用客户端 `run_id`；重试不会创建重复后台任务。
- 发送邮件、标记已读、删除等 Gmail 状态变更不会自动重试。
- 用户仅会看到可行动的恢复提示，不会看到 HTML/JSON 解析等底层错误。

## Sampling 稳定性约束

- 所有生产 Anna Sampling 请求统一由后端 sampler 在发送时设为 60 秒；Gmail、存储、前端轮询和健康探针不属于该约束。
- 同一 Executa invoke 的应用侧累计预算为 6000 tokens，单次最多 4096 tokens。预算按请求值预留，重试和 JSON repair 也会计入；预算耗尽在本地进入既有 fallback，不再向 Host 发送会触发 `-32007` 的请求。
- 后端仅记录工具名、请求与授予 token、剩余 token、超时、耗时和错误类型，用于评估真实延迟；不得记录 prompt、邮件正文、主题、地址、凭据或完整 metadata。

## AI 扫描范围与执行过程

- 统一 turn 与自定义扫描均读取当前邮箱 Scan Plan / Settings 展示天数，传递 `scan_window_days` 与 `max_messages`，初始等待统一为 60 秒；超过初始等待后仍使用 `run_id` 轮询后台任务。
- 用户未明确指定时间时，后端强制使用 Scan Plan 的时间范围，不接受 Planner 模型自行猜测的 `timeframe`。用户明确写出“最近 30 天”“本周”“本月”等时间时，才覆盖默认范围；无论何种时间范围都不超过 Scan Plan 的邮件数量上限，也不会继续分页。
- 待完成的 AI 消息显示固定执行步骤：理解请求、检索所选时间范围、发现邮件和线程数量、筛选、读取必要上下文、整理结果。步骤只显示阶段和数量，不显示 Gmail 查询、邮件主题、地址或正文。
- 扫描结果仍保留可打开的来源线程；“全量扫描”指在范围和数量上限内检索全部匹配邮件，不表示把全部正文交给模型。

## 邮件引用

Ask 结果可以携带 mailbox、message ID 和 thread ID。用户点击引用后，前端定位已有邮件或加载对应线程，并打开 Mail Detail。

## 安全边界

- 上一版 draft 作为引用内容包裹，不能被当成新系统指令。
- 只有当前或最近明确的邮件上下文可以进入 `mail_context` 路由。
- 路由失败应给出可恢复错误，不能静默执行 Gmail mutation。
- AI 生成内容不会自动发送邮件或改变 Gmail 状态。
