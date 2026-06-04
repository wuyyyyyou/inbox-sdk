# Ask 身份指代统一优化

## 问题

Ask 链路中，LLM 回答中对邮箱所属人的称呼不统一——有时用第三人称（"Kate should reply"、"the user received"），有时用邮箱地址（"kate@anna.partners needs to..."），而非统一的 "you"。

## 根因

整个 Ask 链路的 prompt 把 LLM 定位为"第三方评估者"而非"用户代理人"：

| 阶段 | 原文 | LLM 理解 |
|------|------|---------|
| Planner | `Mailbox owner: {mailbox}` | 这是被分析的对象 |
| Executor system prompt | `You are Anna... Analyze the emails below` | 我是外部分析师 |
| Executor user prompt | `You are evaluating mail for: {mailbox}` | 我在帮别人看邮件 |

LLM 认为自己是为 mailbox owner 评估邮件的第三方，自然用第三人称输出。

## 修复

### 改动 1：Executor 执行 prompt（`pipeline.py` `_build_user_prompt`）

```
Before:
## Mailbox Owner
You are evaluating mail for: {mailbox}

After:
## Your Identity
You are Anna, executive assistant to {mailbox}.
In all output text, address your principal directly as "you" / "your".
Say "You received an email from Sarah" NOT "the user received..." or "Kate received...".
```

### 改动 2：Executor 系统 prompt（`pipeline.py` `_EXECUTION_SYSTEM_PROMPT`）

```
Before:
You are Anna, an executive email assistant. Analyze the emails below...

After:
You are Anna, an executive email assistant. The mailbox owner is your principal —
address them directly as "you" in all title, summary, context, and suggestion text.
Analyze the emails below...
```

## 效果

| Before | After |
|--------|-------|
| "Kate should reply to Sarah Kim about the proposal" | "You should reply to Sarah Kim about the proposal" |
| "The user received a security alert from Google" | "You received a security alert from Google" |
| "kate@anna.partners needs to review this invoice" | "You need to review this invoice" |
