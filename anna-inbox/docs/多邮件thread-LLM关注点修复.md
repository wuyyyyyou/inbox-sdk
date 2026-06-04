# 多邮件 Thread 中 LLM 关注点修复

## 背景

Brief 扫描 Gmail 线程时，如果对方连续发了多封邮件（用户均未回复），LLM 应该聚焦于最新的连续收信来生成卡片文案。但当前实现存在两个问题：

1. 线程消息按日期升序排列，渲染时取了前 N 条 = 最旧的 N 条
2. 没有截断用户已回复之前的旧消息，LLM 被历史噪音干扰

---

## 第一轮：现状审查

### 问题 1：Anna-llm 路径取到了最旧消息

`build_anna_single_judgment_prompt` 调用了 `_render_context_for_batch_prompt`，该函数截取 `ctx.thread.messages[:4]`。由于 `get_thread_context` 按 `internal_date` 升序排列（最旧在前），`[:4]` 取到的是线程中最旧的 4 封。对方连发多封新邮件时，LLM 完全看不到最新内容。

DashScope 批量路径也有同样问题。

### 问题 2：没有"最新"标注

`_render_context_for_prompt` 虽展示全部线程消息（最多 10 条），但没有任何标注告诉 LLM 哪条是最新的、哪些是未回复的。LLM 只能自行比对日期字符串。

### 三条路径对比（修复前）

| 路径 | 渲染函数 | 看到的消息 | 问题 |
|------|----------|-----------|------|
| Anna-llm 单条 | `_render_context_for_batch_prompt` | 最旧 4 条 | 看不到最新消息 |
| DashScope 单条 | `_render_context_for_prompt` | 全部（最多 10 条） | 无聚焦指令 |
| DashScope 批量 | `_render_context_for_batch_prompt` | 最旧 4 条 | 看不到最新消息 |

### 修复

| # | 改动 | 位置 | 效果 |
|---|------|------|------|
| 1 | `[:4]` → `[-4:]` + 最新标注 | `_render_context_for_batch_prompt` | 批量路径取最新 4 条 |
| 2 | 替换为 `_render_body_and_thread` | `build_anna_single_judgment_prompt` | Anna-llm 单条看完整线程（最多 10 条） |
| 3 | 新增 `_render_body_and_thread` + 最新标注 | `judgment.py` | 纯渲染 body/thread，末尾标注最新 |

---

## 第二轮：仅标注"最新一封"不够

修复后 LLM 能看到全部消息 + 最新标注，但用户指出这仍然不够。以 8 条消息的线程为例：

```
Msg 1: alice@x.com   (对方发)
Msg 2: kate@...      (用户回复了)
Msg 3: alice@x.com   (对方再发)
Msg 4: alice@x.com   (对方又发)
Msg 5: alice@x.com   (对方连发)
Msg 6: kate@...      (用户再次回复)
Msg 7: alice@x.com   (对方最新)
Msg 8: alice@x.com   (对方最新)
```

仅标注 Msg 8 为 "LATEST"，LLM 仍会被 Msg 1-6 的历史噪音干扰，可能忽略 Msg 7-8 的连续追进意图。

---

## 第三轮：方案对比与设计

### 方案 A：只给连续收信（从最后回复截断）

只展示用户最后一次回复之后的所有对方邮件。

**优点**：LLM 完全聚焦于待处理内容，零噪音。
**缺点**：LLM 不知道用户上次回复说了什么，可能误判对方意图。如用户说"价格不合适"，对方连发 3 封在还价——没有用户的回复做参照，LLM 看不出这是"协商中"还是"新请求"。

### 方案 B：全部给 + 标注

展示全部消息，用 `UNREPLIED` / `LATEST` 标注待关注的。

**优点**：完整上下文 + 焦点明确。
**缺点**：旧消息仍占 token；LLM 可能在标注和噪音之间摇摆。

### 方案 C（推荐）：保留最后回复 + 之后全部收信

从线程尾部向前扫描，遇到第一条 `from_addr == owner` 就截断，展示从该点到末尾的全部消息。

**优点**：
- LLM 知道"用户说了什么"→ 能判断对方后续发言是在回应什么问题/立场
- 只有 1 条用户回复作为锚点，成本极低
- 锚点清晰：用户回复为界，之后的全部是待处理

---

## 最终 Prompt 设计

### 场景 A：用户回复过，对方又发了新邮件

```
--- Thread Context (4 of 8 messages, since your last reply) ---

Message 1 (your last reply):
  From: Kate Zhou <kate@anna.partners>
  Date: 2026-05-25 14:00
  Body: Thanks Alice, but the price is above our budget...

Message 2 ← UNREPLIED:
  From: Alice <alice@x.com>
  Date: 2026-05-28 09:00
  Subject: Re: Pricing
  Body: I understand. We can adjust the pricing to...

Message 3 ← UNREPLIED:
  From: Alice <alice@x.com>
  Date: 2026-05-29 16:30
  Subject: Re: Pricing
  Body: Following up — we also offer a discount...

Message 4 ← LATEST UNREPLIED:
  From: Alice <alice@x.com>
  Date: 2026-05-30 10:15
  Subject: Re: Pricing - final decision?
  Body: Kate, can we finalize this week?

↑ Messages marked UNREPLIED need your attention.
  Your last reply is provided for context.
```

- 用户最后回复标 `(your last reply)`：告诉 LLM 这是背景
- 之后收信逐条标 `← UNREPLIED`，最新一封加 `LATEST`
- 尾部指令明确：只关注 UNREPLIED 部分

### 场景 B：从未回复过

```
--- Thread Context (5 messages, no reply from you yet) ---

Message 1:
  From: Alice <alice@x.com>
  Date: 2026-05-20
  ...

...

Message 5 ← LATEST:
  From: Alice <alice@x.com>
  Date: 2026-05-30
  ...

↑ Focus on the most recent messages above.
```

- 全部展示（全部未回复），最新一封标 `LATEST`
- 不区分"已处理/未处理"

### 设计要点

| 要点 | 说明 |
|------|------|
| 截断规则 | 从尾部向前扫，第一条 `from_addr == owner` 为截断点，展示到末尾 |
| 无回复时 | 展示全部线程消息 |
| Token 节省 | 用户最后回复之前的旧消息全部丢弃 |
| 锚点 | 用户最后回复作为上下文锚点，LLM 可判断对方后续意图 |
| 标注语意 | `(your last reply)` vs `← UNREPLIED` vs `← LATEST UNREPLIED`，层次清晰 |

---

## 实施状态

- [x] 第一轮修复：`[:4]` → `[-4:]`，新增 `_render_body_and_thread`，最新标注（已实施）
- [ ] 第三轮方案 C：截断 + 标注 UNREPLIED / LATEST（待实施）
