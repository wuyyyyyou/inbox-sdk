# Ask 链路

Ask 是 AI Sidebar 使用的邮箱检索与综合回答流程，不经过 Brief Phase 1/2，也不生成 Attention Card 队列。

## 流程

1. `mail_agent/ask/planner.py` 将用户请求转换为 topics、people、timeframe、direction、goal 和 read depth。
2. `mail_agent/ask/search.py` 由代码构造 Gmail 查询，执行搜索并在零结果时受控放宽。
3. 按 read depth 读取 header、message 或 thread context。
4. `mail_agent/ask/answer.py` 对相关邮件生成结构化回答。
5. 前端展示回答、动作和可点击邮件引用。

自定义扫描通过 `start_custom_scan` 返回可轮询 run；保存的 Custom Scan Plan 可以再次执行。

## 约束

- Planner 不直接生成任意 Gmail mutation。
- 多邮箱结果必须携带来源 mailbox。
- Answer 输入超过预算时渐进压缩正文，最后回退 headers。
- reply、mark read、trash 等动作只能由用户点击结果中的明确操作触发。
- Ask history 是会话索引，不是邮件或完整 prompt 的长期存储。
