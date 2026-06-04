# judgment 线程渲染修复

## 问题

Phase2（LLM 评估生成卡片文案）阶段，线程上下文渲染存在两个问题：

1. **Anna-llm 路径**：`build_anna_single_judgment_prompt` 调用了批量渲染函数 `_render_context_for_batch_prompt`，该函数只截取 `ctx.thread.messages[:4]`。由于 `get_thread_context` 按日期升序排列（最旧在前），`[:4]` 取到的是线程中最旧的 4 封。对方连发多封新邮件时，LLM 完全看不到最新内容，卡片文案基于无关的旧消息生成。

2. **DashScope 批量路径**：同样使用 `_render_context_for_batch_prompt`，同样截取最旧而非最新。此外两条路径都没有标注线程中哪一封是最新消息，LLM 无法聚焦。

## 改动

### 改动 1：`_render_context_for_batch_prompt` — 取最新 4 条 + 最新标注

**文件**：`judgment.py` 第 281 行

```python
# Before
for i, msg in enumerate(ctx.thread.messages[:4], start=1):
    thread_parts.append(
        f"Message {i}: from={msg.from_addr}; date={_fmt_ts(msg.internal_date)}; "
        f"subject={msg.subject}; body={msg.body_text[:260]}"
    )
parts.append("Thread:\n" + "\n".join(thread_parts))

# After
msgs = ctx.thread.messages[-4:]
for i, msg in enumerate(msgs, start=1):
    thread_parts.append(
        f"Message {i}: from={msg.from_addr}; date={_fmt_ts(msg.internal_date)}; "
        f"subject={msg.subject}; body={msg.body_text[:260]}"
    )
if msgs:
    thread_parts.append(f"↑ Message {i} is the LATEST. Focus card on recent messages.")
parts.append("Thread:\n" + "\n".join(thread_parts))
```

### 改动 2：`build_anna_single_judgment_prompt` — 单条评估用完整渲染

**文件**：`judgment.py` 第 377 行

```python
# Before
{_render_context_for_batch_prompt(ctx)}

# After
{_render_context_for_prompt_body_only(ctx)}
```

由于 `_render_context_for_prompt` 包含候选元信息（Candidate ID / Kind / From / Subject 等），与 `build_anna_single_judgment_prompt` 第 369–376 行已输出的字段重复。需新增一个只渲染消息体/线程的函数 `_render_context_for_prompt_body_only`，或直接内联线程渲染逻辑。

### 改动 3：`_render_context_for_prompt` — 最新标注

**文件**：`judgment.py` 第 65–73 行

在遍历完线程所有消息后，追加一行标注：

```python
# After 遍历
parts.append(f"\n↑ Message {len(t.messages)} is the LATEST. Focus card content on recent messages.")
```

## 影响范围

| 路径 | 改动 | 效果 |
|------|------|------|
| Anna-llm 单条 | 改动 2 + 3 | 从看 4 条最旧 → 看全部 10 条最新 + 聚焦最新 |
| DashScope 单条 | 改动 3 | 已看全部，新增聚焦指令 |
| DashScope 批量 | 改动 1 | 从看 4 条最旧 → 看 4 条最新 + 聚焦最新 |
