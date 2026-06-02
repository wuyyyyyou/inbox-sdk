# Ask 路径方向感知修复

## 背景

Brief 路径已通过三层防线（L1 查询过滤 + L2 线程去重 + L3 LLM 身份告知）修复了"自己回复自己"的问题。但 Ask 路径是独立管线，完全不经过这些防线：

```
Brief: Gmail → _dedupe_by_thread → Phase1 → Phase2 → Card → Persist
Ask:   用户自然语言 → planner LLM 生成查询 → run_mail_scan → 一次 LLM 出结果
```

Ask 不调用 `_dedupe_by_thread`，不使用策略查询，不经过 Phase 1/2。方向过滤不能硬编码——用户可能想看 SENT："看看我发了哪些合作邀约还没回复"、"找一下我的 outreach 邮件"。

## 设计思路

**Brief**：方向过滤在代码层（硬编码）。因为 Brief 语义固定——"收件箱里有什么需要我处理"。

**Ask**：方向过滤在 LLM 层（语义驱动）。因为 Ask 灵活——用户自然语言决定查询方向。

两层改动：

| 层 | 位置 | 作用 |
|----|------|------|
| Planner 层 | `planner.py` prompt | 让 LLM 根据用户意图选择 `in:inbox` / `in:sent` / `-in:sent` |
| 执行层 | `pipeline.py` `run_custom_scan` prompt | 让执行 LLM 知道用户身份，正确解读 SENT/DRAFT 消息 |

## 实施

### 1. Planner prompt — 方向选择指引

**文件**: `new/executas/tool-zhaopy-inbox-executa-adumwz8w/src/mail_agent/planner.py`

在 `_PLANNER_SYSTEM_PROMPT` 中新增 `## Direction guidance` 段：

```
## Direction guidance — choose the right scope for each query

The mailbox owner is provided in the user request. Match by EMAIL ADDRESS.

- User asks "check my inbox" / "what needs attention" / "pending" → in:inbox or -in:sent -in:draft
- User asks "what did I send" / "my outreach" / "proposals I sent" → in:sent
- User asks to see full conversations regardless of direction → no direction filter
- User asks about collaboration / partnership threads → -in:sent -in:draft
- Default (unclear intent): use -in:sent -in:draft
```

用户模板改为 `Mailbox owner` 开头：
```
Mailbox owner: {mailbox}
User request: {user_request}
```

### 2. 执行 LLM prompt — 用户身份

**文件**: `new/executas/tool-zhaopy-inbox-executa-adumwz8w/src/mail_agent/pipeline.py` (`run_custom_scan`)

在 `user_prompt` 最前面增加：
```
## Mailbox Owner
You are evaluating mail for: {mailbox}
Match by EMAIL ADDRESS (between < >), not by display name.
- If the sender's email IS the mailbox owner → OUTGOING mail (sent or draft).
  SENT: the user already sent it. Include or exclude based on the user's request.
  DRAFT: the user started writing but didn't send yet.
```

### 3. 不改动的部分

- `run_mail_scan` — 不做任何方向过滤，查询由 planner 决定
- `_dedupe_by_thread` — Ask 路径不调用，不受影响
- `strategies.py` — Ask 不使用策略查询

## 影响范围

| 文件 | 改动 | 行数 |
|------|------|------|
| `planner.py` | prompt 加方向指引 + 用户模板加邮箱 | ~15 |
| `pipeline.py` | `run_custom_scan` prompt 加用户身份 | ~6 |

## 典型场景推演

| 用户请求 | Planner 生成的查询 | 结果 |
|---------|-------------------|------|
| "帮我看看收件箱有什么" | `in:inbox newer_than:3d` | 只看 INBOX |
| "我发了哪些合作提案还没回复" | `in:sent (collaboration OR proposal) newer_than:30d` | 只看 SENT |
| "找一下和 Kevin 的完整往来" | `(from:kevin OR to:kevin) -in:draft` | 双方向，排除草稿 |
| "最近有什么安全告警" | `(security OR login) -in:sent -in:draft newer_than:30d` | 排除自己发出的 |
