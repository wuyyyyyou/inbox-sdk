# Ask Planner 线程感知优化

## 问题

Ask 链路（plan-and-execute）中，用户问"Sarah Kim 有需要回复的邮件吗"，LLM 回答有一封，但实际上用户已经回复过。原因是 Planner 生成的计划没有包含"检查线程回复状态"这个维度——扫描时排除了 sent 邮件，执行 LLM 只能看到 Sarah 发来的邮件，看不到用户已回复的事实。

## 根因

Planner 当前把邮件当**独立对象**处理，缺乏"线程是一个对话"的思维。具体缺失：

| 缺失 | 影响 |
|------|------|
| 方向指导里没有"查回复"场景 | Planner 默认用 `-in:sent`，用户的回复邮件被排除在查询外 |
| `read_depth` 指导里没提线程分析 | Planner 选 `message_detail`（读正文）而不是 `thread_context`（读线程） |
| `task_prompt` 指导里没有"对比线程方向" | 执行 LLM 的指令里不会写"检查最新消息是谁发的" |

## 解决思路

不改执行器，只增强 Planner 的 system prompt，让它学会"线程感知"的规划：

1. 补充方向选择指导——告诉 Planner 什么场景不加 `-in:sent`、什么场景包含 sent
2. 补充 read_depth 指导——告诉 Planner 什么场景必须选 `thread_context`
3. 补充 task_prompt 指导——告诉 Planner 如何在 task_prompt 中指示执行 LLM 做线程级判断

## 线程相关的 Ask 场景

| 场景 | 用户问法示例 | Planner 需要 |
|------|------------|-------------|
| 查回复状态 | "Sarah 的邮件都回了吗" | 不加 `-in:sent`，查完整往来，看最新是谁发 |
| 查谁没回我 | "我发了邮件但谁还没回" | 含 sent，查"最新是用户自己"的线程 |
| 会话概要 | "总结我和 Alice 的沟通" | 不加方向过滤，thread_context，按时间线 |
| 跟进催促 | "哪些事该催一下了" | 查"用户发过但对方 N 天没回" |
| 忘了回复 | "有没有我忘了回的" | 查"对方发来超过 N 天、最新还是对方" |
| 谈判/协商 | "和 vendor X 谈得怎么样了" | 完整往来，标注双方立场变化 |

## 实施内容

### 改动 1：Direction guidance 扩展

在现有方向指导后追加三条：

```
- User asks about reply status / whether they responded → NO direction filter.
  The execution LLM needs BOTH sides to know who sent the latest message.
- User asks who hasn't replied / what they're waiting for → include sent mail.
  The execution LLM needs threads where the user was the last sender.
- User asks to summarize a conversation / catch up on a discussion → NO direction filter.
  The execution LLM needs the complete back-and-forth.
```

### 改动 2：read_depth 补充

在现有 read_depth 说明后追加：

```
- "thread_context": REQUIRED when the task involves understanding who-said-what —
  reply status checks, follow-up tracking, conversation summaries, negotiation status.
  The LLM needs message ordering and sender identity across the full thread.
```

### 改动 3：task_prompt 指导补充

在现有 task_prompt 指导的 3 条之后新增第 4 条：

```
4. For thread-aware tasks, tell the LLM explicitly:
   - "For each thread, check who sent the LATEST message."
   - "If the latest is from the mailbox owner → already handled / waiting for them."
   - "If the latest is from someone else → needs attention / they replied."
   For multi-message exchanges: "Describe the back-and-forth, note key turns."
```

## 与 Brief 路径的区别

| | Brief | Ask |
|---|---|---|
| 机制 | `_thread_latest_is_from_owner` 程序级过滤 | Planner 生成计划 → 执行 LLM 自行判断 |
| 灵活性 | 固定规则，已回复就丢掉 | LLM 可结合上下文灵活判断（e.g. "你回过但对方又追了一封"） |
| 设计理念 | 秘书自动化，减少噪音 | 定制化查询，用户自己定义"什么是需要处理的" |

这次优化只改 Planner，不改执行器，不改 Brief 路径。
