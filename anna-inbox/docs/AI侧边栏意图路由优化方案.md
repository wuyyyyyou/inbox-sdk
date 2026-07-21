# AI 侧边栏意图路由

## 状态：历史实现；侧栏主路径已迁移 Host Agent Session（App 2.1.4 / Tool 2.2.4）

侧栏**不再**使用前端正则业务路由（原 `decideAiRoute` → `chat | scan | mail_context`）。

侧栏当前主路径：`sendAiChatMessage` → `anna.agent.session`，由 Host Agent 在 systemPrompt 与显式白名单工具约束下选型；前端透传用户文本和只读 `ui_context`，并消费流式文本与工具结果。Executa `start_ai_turn` 仍服务详情协助和兼容路径。

权威设计与工具枚举见 [AI 侧栏 Sampling → Sessions 改造方案](AI侧栏Sampling转Sessions改造方案.md)。

### 已删除 / 降级

| 原路径 | 现状 |
| --- | --- |
| `decideAiRoute` + 正则表 | **已删除** |
| `localStorage anna-inbox-use-ai-turn=0` 旁路 | **已删除** |
| 侧栏 `llm.complete` 闲聊 | 改为 tool `chat_general`（Sampling） |
| 侧栏直接 `start_custom_scan` | 由 Router 选 `search_mail` 复用 Ask 管线 |
| 侧栏直接 `start_inbox_mail_prompt` | 由 Router 选 `summarize_thread` / `draft_reply` 等 |

`app/aiRoute.ts` 仅保留 `buildRevisionPrompt`，供邮件详情等**非侧栏**上下文改写 prompt 包装。

`start_custom_scan` / `start_inbox_mail_prompt` 仍可作为独立工具存在，**不**再由侧栏意图表选择。

## 瞬时失败恢复

- AI 读取和生成调用遇到网络、超时、服务端 5xx 或 HTML 错误页时自动重试两次（0.5 秒、1 秒）。
- Ask 规划阶段若 Anna Sampling 返回空内容，不执行无效重试；改为立即生成保守的本地搜索计划。
- 扫描和 AI turn 复用客户端 `run_id`；重试不会创建重复后台任务。
- AI 侧栏 Ask 只读取当前活动邮箱；多账户选择状态不参与 Ask 检索范围。
- 发送邮件、标记已读、删除等 Gmail 状态变更不会自动重试，且必须由用户确认卡片触发。
- AI 失败消息只展示格式校验后的 `Diagnostic rt_...` 阶段摘要；原始宿主错误、邮件数据和凭据不得显示。

## Sampling 稳定性约束

- 所有生产 Anna Sampling 请求统一由后端 sampler 在发送时设为 60 秒。
- 同一 Executa invoke 的应用侧累计预算为 6000 tokens，单次最多 4096 tokens。
- 后端仅记录工具名、请求与授予 token、剩余 token、超时、耗时和错误类型；不得记录 prompt、邮件正文、主题、地址、凭据。

## AI 扫描与批量

- 统一 turn 与自定义扫描均读取当前邮箱 Scan Plan / Settings 展示天数；初等等待 60 秒，超时后 `run_id` 轮询。
- 收件箱勾选多封邮件后可请求 `batch_draft` / `batch_outreach`；结果按封隔离 artifact，须用户逐封核对后发送。
