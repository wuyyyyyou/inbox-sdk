# 多邮箱 Ask 链路 Prompt 优化方案

## 一、现状

Ask 链路有两个 LLM 调用点，邮箱信息仅以裸字符串形式出现，无多邮箱上下文：

| 调用点 | 文件:行 | 当前写法 |
|---|---|---|
| Planner 用户 prompt | `planner.py:105` | `Mailbox owner: {mailbox}` |
| Executor 用户 prompt | `pipeline.py:614` | `You are Anna, executive assistant to {mailbox}` |
| 邮件渲染 | `pipeline.py:739` | **无邮箱字段** |
| Executor 系统 prompt | `pipeline.py:443` | **无多邮箱相关指引** |

`generate_custom_plan()` 和 `run_custom_scan()` 均接受单个 `mailbox: str` 参数。

---

## 二、多邮箱场景分析

假设用户有两个邮箱：`a@gmail.com`（个人）、`b@company.com`（工作）。

### 场景 1：跨邮箱模糊查询

> 用户：找一下关于 Q3 预算的邮件

**预期**：扫描两个邮箱，聚合所有相关邮件，每封标明来源。

**当前问题**：Planner 只看到 `Mailbox owner: a@gmail.com`，不知道还有 b。Executor 看到的邮件没有邮箱标签，无法区分来源。

### 场景 2：用户明确指定某个邮箱

> 用户：看一下 b@company.com 里 Amazon 的退货确认邮件

**预期**：只在 b 中搜索，不扫 a。

**当前问题**：Planner 完全不知道 b 的存在，可能把 "b@company.com" 当成搜索关键词在 a 里搜，彻底跑偏。

### 场景 3：用户用自然语言指代邮箱

> 用户：帮我看一下工作邮箱里的未读邮件

**预期**：LLM 将 "工作邮箱" 映射到 b@company.com。

**当前问题**：Planner 不知道有哪些邮箱可选，无法完成映射。如果用户有两个包含 "company" 的邮箱（`b@company.com`、`c@partner-company.com`），歧义更大。

### 场景 4：跨邮箱比较

> 用户：比较一下两个邮箱里来自 @client.com 的邮件各有多少

**预期**：Executor 分邮箱统计，给出对比结果。

**当前问题**：Executor 没有 "分邮箱统计" 的指引，只能把所有邮件混在一起数。

### 场景 5：部分邮箱无结果

> 用户：查一下关于年度计划的邮件
> 结果：b@company.com 有 3 封，a@gmail.com 无

**预期**：Executor 明确报告 "a@gmail.com 中未找到，b@company.com 中找到 3 封"。

**当前问题**：Executor 可能直接说 "找到 3 封" 不说明来源，或者更糟——在 a 无结果时就开始输出 "没找到"，忽略 b 的结果。

### 场景 6：跨邮箱操作

> 用户：把所有邮箱里来自 noreply@spam.com 的邮件标记已读

**预期**：Executor 返回的每个 action 带 `mailbox` 字段，后端按 mailbox 调 Gmail API。

**当前问题**：邮件渲染无 mailbox 字段 → LLM 无法给 action 标注邮箱 → `mark_read_from_ask` 不知道用哪个 mailbox。

### 场景 7：部分邮箱不可用

> a@gmail.com token 过期，Ask 只扫了 b@company.com

**预期**：Executor 在回答中说明 "a@gmail.com 暂时无法访问，以下结果仅来自 b@company.com"。

**当前问题**：Executor 根本不知道还有 a 这个邮箱存在，无法给出这种提示。

### 场景 8：用户不知道有哪些邮箱

> 用户：帮我查一下所有邮件

**预期**：LLM 知道当前勾选了哪些邮箱，并全部扫描。

**当前问题**：Planner 只知道一个邮箱，无法扩展。

### 场景 9：搜索结果的 thread 跨邮箱

> 不可能发生：Thread 天然属于一个 Gmail 账号，不存在跨邮箱 thread。**此处无问题。**

### 场景 10：用户临时切换 Ask 快速勾选

> Sources 勾了 A、B，Ask 临时只勾了 B

**预期**：Planner 和 Executor 只看到 B，按单邮箱行为即可。

**当前问题**：无。只要正确传入 Ask 的快速勾选列表，且列表只有一个元素，单邮箱退化自然正确。

---

## 三、具体修改点

### 3.1 Planner 用户 prompt（`planner.py`）

**当前**：
```python
_PLANNER_USER_TEMPLATE = """Mailbox owner: {mailbox}
User request: {user_request}"""
```

**改为**：
```python
_PLANNER_USER_TEMPLATE = """## Available Mailboxes
The following mailboxes are selected for this scan:
{mailbox_list}

## User Request
{user_request}

## Important
- If the user specifically names a mailbox by email address or description (e.g., "work mailbox", "company email"), map it to one of the available mailboxes and search ONLY that mailbox.
- If the user does NOT specify a particular mailbox, search ALL available mailboxes.
- Your Gmail query strings will be executed against each target mailbox independently — do NOT embed mailbox names into the search query syntax.
- If the user's request cannot be reasonably mapped to any mailbox (e.g., they ask about a mailbox not in the list), note this limitation."""
```

**`{mailbox_list}` 格式**：
```
- a@gmail.com
- b@company.com
```

### 3.2 Planner 系统 prompt（`planner.py`）

在现有 `_PLANNER_SYSTEM_PROMPT` 末尾增加一段多邮箱指引：

```
## Multi-Mailbox
When multiple mailboxes are available:
- The `gmail_queries` you generate will be executed against each target mailbox.
- You do NOT need to duplicate queries per mailbox — the system handles execution.
- Use the `task_prompt` to instruct the executor about per-mailbox expectations:
  - "Compare results across mailboxes"
  - "Report per-mailbox counts"
  - "Annotate each finding with its source mailbox"
```

### 3.3 Executor 用户 prompt — 身份部分（`pipeline.py`）

**当前**：
```
## Your Identity
You are Anna, executive assistant to {mailbox}.
In all output text, address your principal directly as "you" / "your".
Say "You received an email from Sarah" — NOT "the user received" or "Kate received".
Match by EMAIL ADDRESS (between < >), not by display name.
- If the sender's email IS your principal → this is OUTGOING mail (sent or draft).
```

**改为**：
```
## Your Identity
You are Anna, executive assistant. You manage the following mailboxes for your principal:
{mailbox_list}

In all output text, address your principal directly as "you" / "your".
Say "You received an email from Sarah" — NOT "the user received" or "Kate received".

Match by EMAIL ADDRESS (between < >), not by display name.
To determine if an email is outgoing: if the sender's email matches the mailbox it belongs to (see the Mailbox field on each email), it is OUTGOING mail (sent or draft).

## Mailbox Awareness
- Each email below is labeled with its source mailbox.
- When reporting results, mention which mailbox each finding comes from.
- If the user asks about a specific mailbox, focus on emails from that mailbox.
- When comparing across mailboxes, report per-mailbox counts.
- If a mailbox has no relevant results, say so explicitly (e.g., "No matching emails found in a@gmail.com").
- For any action you suggest (mark read, trash, reply), include the `mailbox` field so the action is routed correctly.
```

**`{mailbox_list}` 格式**：同 Planner。

### 3.4 邮件渲染（`pipeline.py`）

在 `_render_emails_for_llm` 中，每封邮件增加 `Mailbox` 字段：

```
## Email #1
From: Sarah <sarah@company.com>
Subject: Q3 Budget Review
Mailbox: b@company.com
Date: 2026-06-03
Message ID: <msg123@mail.gmail.com>
Thread ID: <thread456@mail.gmail.com>
Labels: INBOX, IMPORTANT

[Body content...]
```

`Mailbox` 行放在 `From`/`Subject` 之后、`Date` 之前。

### 3.5 Executor 系统 prompt（`pipeline.py`）

在现有 `_EXECUTION_SYSTEM_PROMPT` 末尾，输出格式指引之后增加：

```
## Multi-Mailbox Output
- The `mailbox` field is REQUIRED on every item when multiple mailboxes are in use.
- In `summary`: call out which mailboxes were searched and note any that were unavailable.
- In `context`: mention the source mailbox when it helps the user understand where the email came from.
- When no results are found in some mailboxes but not others, be explicit. Never say "no results found" without clarifying which mailbox.
```

### 3.6 函数签名变更

| 函数 | 当前 | 改为 |
|---|---|---|
| `generate_custom_plan(user_request, mailbox, ...)` | 单 `mailbox: str` | `mailboxes: list[str]` |
| `run_custom_scan(plan, mailbox, ...)` | 单 `mailbox: str` | `mailboxes: list[str]` |
| `_build_user_prompt(rendered_emails)` | 外层的单 mailbox 通过闭包传入 | 显式接收 `mailboxes: list[str]` |
| `_render_emails_for_llm(email_data, ...)` | 无 mailbox 上下文 | 接收 `mailbox_map: dict[str, str]` 用于查找每封邮件所属邮箱 |

### 3.7 Gmail 查询执行层

Planner 生成的 `gmail_queries` 需要在每个目标邮箱上分别执行：

```
for each target_mailbox in mailboxes:
    for each query in plan.gmail_queries:
        results[target_mailbox] = gmail_search(target_mailbox, query)
```

搜索结果合并后，每封邮件标记所属邮箱，再传给 Executor。

---

## 四、单邮箱退化

当 `len(mailboxes) == 1` 时，行为与当前完全一致：

- Planner 看到 `Available Mailboxes: - a@gmail.com`，等价于 `Mailbox owner: a@gmail.com`
- Executor 看到 `You manage the following mailboxes: - a@gmail.com`，等价于 `executive assistant to a@gmail.com`
- 邮件渲染中 `Mailbox` 行可省略（只有一个邮箱时无歧义），也可保留（格式统一）

---

## 五、不需要改的地方

| 组件 | 原因 |
|---|---|
| JSON Repair prompt（`llm.py`） | 与邮箱无关 |
| Brief 链路所有 prompt（`phase1.py`、`judgment.py`、`handle_service.py`） | Brief 是按邮箱独立执行的，不涉及跨邮箱语义 |
| Planner 的 `_FALLBACK_PLAN_JSON` | 通用回退计划不依赖邮箱数量 |
| Executor 的 Anna sampling fallback（`pipeline.py`） | 截断策略与邮箱无关 |
