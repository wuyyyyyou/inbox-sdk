# Anna LLM Sampling — Brief 全链路分析

> 创建时间：2026-05-31

---

## 整体流程

```
用户请求 → parse_intent → scan → dedup → Phase 1 → read_context → Phase 2 → guards → plan → cards
                              (规则)  (Gmail) (规则)  (LLM#1)   (no LLM)     (LLM#2)  (规则) (规则) (存储)
```

LLM 调用只有两处：**Phase 1** 和 **Phase 2**。其余环节全是规则驱动。

---

## Phase 1：批量分类（`phase1.py`）

**目的**：把几十封邮件的头信息一次性喂给 LLM，粗筛出需要关注的候选项，剩下的归为低价值。

### 调用参数

```python
call_llm_json_safe(
    sampling_create_message,
    system_prompt=_PHASE1_SYSTEM,      # 45 行英文指令
    user_message=user_prompt,          # 策略 + 邮件头 JSON
    temperature=0.1,
    max_tokens=2048,
    timeout=240.0,
    allow_fallback=False,              # 不做 DashScope 降级
    allow_sampling_provider_fallback=False,
)
```

### System prompt（`_PHASE1_SYSTEM`）

```
You are Anna's batch email triage engine. Classify ALL email headers in one pass.

## Priority
- high: security incident, payment failure, direct ask from known contact, deadline today
- medium: needs reply/confirmation, collaboration opportunity, account notice worth checking
- low: newsletters, promotions, automated notifications, receipts, already-handled threads

## Read Depth
- header_only / message_detail / thread_context（含判断标准）

## Safety Rules (NEVER violate)
1-4: 安全告警→reply+high；付款失败→reply+high；权限变更→reply+high；SENT/DRAFT 判断

## User Action
reply  — 有人要求回复/跟进，包含直接问句或时间敏感决策
review — 无需回复但值得 5 秒关注（pipeline 变化、面试提醒、calendar 变更）
ignore — 删掉不打开也没损失（仅限 mass-mail newsletter、无个人行动的 digest）

## Output Constraint
Your entire response must be a single JSON object...
```

### User prompt（`build_phase1_user_prompt`）

```
## Strategy
{strategy.name}: {strategy.description}
Allowed candidate kinds: reply_required_possible, confirmation_required_possible, ...

## Mailbox Owner
{profile.mailbox_id} — match by EMAIL ADDRESS

## Headers (N emails)
Each header: i=index, id=message_id, f=from, s=subject, sn=snippet, d=date, l=labels, u=unread, st=starred, im=important, at=has_attachment.
[{"i":0,"id":"19e623...","f":"sender@example.com","s":"Subject line","sn":"Email snippet...","d":"1717171200","l":["INBOX"],"u":true,"st":false,"im":false,"at":false}, ...]

## Required Output
Return a JSON object:
{"classifications": [
  {"message_id": "...", "user_action": "reply", "priority_hint": "high", "read_depth": "message_detail", "confidence": 0.92, "reason": "..."}
]}
Every input header MUST have exactly one entry.
```

**单字母 key 压缩**：`i`=index, `id`=message_id, `f`=from, `s`=subject, `sn`=snippet, `d`=date, `l`=labels, `u`=unread, `st`=starred, `im`=important, `at`=has_attachment。每封邮件 ~200 chars。

### 输入量

| 要素 | 大小 |
|------|------|
| System prompt | ~3,000 chars |
| User prompt（策略+指令） | ~800 chars |
| 邮件头 JSON（每批8封 × 200 chars） | ~1,600 chars |
| **总输入** | **~5,400 chars** |

### Anna 特有：分批机制

```python
_ANNA_PHASE1_BATCH_SIZE = 8   # 每批最多 8 封
```

DashScope 不分批（一次全喂），Anna 每 8 封调一次 LLM。20 封邮件 = 3 次 LLM 调用。

### 输出 → 数据结构

```json
{"classifications": [
  {"message_id": "...", "user_action": "reply", "priority_hint": "high",
   "read_depth": "message_detail", "confidence": 0.92,
   "reason": "Unknown Windows login from Shanghai — user must verify"},
  {"message_id": "...", "user_action": "ignore", "priority_hint": "low",
   "read_depth": "header_only", "confidence": 0.95,
   "reason": "Weekly newsletter digest — no personal action items"}
]}
```

解析后生成两类结果：

| 输出 | user_action | 转化为 |
|------|-----------|--------|
| `candidates[]` | reply / review | `CandidateItem`（进入 Phase 2） |
| `low_value_items[]` | ignore | 聚合为 `cleanup_bundle` 卡片 |

---

## Phase 2：逐项评估（`judgment.py`）

**目的**：对每个候选邮件做深度评估，生成 PRD 三行卡片（title / context / suggestion）。

### 调用参数

```python
call_llm_json_safe(
    sampling_create_message,
    system_prompt="You are a strict JSON generator. Output ONLY valid JSON...",
    user_message=prompt,              # build_anna_single_judgment_prompt()
    temperature=0.1,
    max_tokens=2048,                  # 刚改（原 1200）
    timeout=120.0,                    # 刚改（原 60s）
    allow_fallback=True,
    allow_sampling_provider_fallback=False,
    max_attempts=2,                   # 刚改（原 1）
)
```

### System prompt

```
You are a strict JSON generator. Output ONLY valid JSON — no explanation, no markdown, no code fences.
```

一行，纯粹约束输出格式。

### User prompt（`build_anna_single_judgment_prompt`）

```
You are Anna, an executive email assistant. Evaluate exactly ONE email candidate against the strategy below.
Output ONLY a single JSON object.

## Strategy
Default 秘书模式: 找出真正需要用户注意的邮件事项。

## 判断维度（按优先级，冲突时以高优先级为准）
1. **安全/风险** (权重:最高): 登录异常、安全提醒、付款失败、账号风险 → priority ≥ high
2. **回复等待** (权重:高): 有人等待回复、含问句或请求、对方最近发来 → priority ≥ medium
3. **用户行动** (权重:高): 用户需确认、付款、回复、审批 → should_show_in_main_result=true
4. **业务影响** (权重:中): 客户、合作方、业务机会相关 → priority ≥ medium
5. **可代理** (权重:中): agent 可准备草稿/标签/提醒 → can_agent_prepare=true
6. **低价值** (权重:低): newsletter、促销、自动通知、已处理完成 → priority=low

## Few-Shot Examples

### Example 1: Google security alert: new device login
Email: From: Google, Subject: New sign-in notification, Snippet: Your Google Account was signed into on an unknown Windows device...
Output: {"base_judgment":{"requires_user_action":true,...},"mode_judgment":{"bucket":"must_review",...},"final_decision":{...},"confidence":0.9}

### Example 2: Colleague asks for proposal review
Email: From: Colleague, Subject: Please review the updated Q2 proposal, Snippet: I updated the proposal based on your feedback...
Output: {"base_judgment":{...},"mode_judgment":{"bucket":"needs_reply",...},"final_decision":{...},"confidence":0.85}

## Mailbox Owner
You are evaluating mail for: hr@anna.partners
- Sender IS mailbox owner → OUTGOING. SENT: surface=false, priority=low. DRAFT: surface as unsent draft.

## Email
candidate_id: cand_abc123
kind: reply_required_possible
priority_hint: high
from: Google <no-reply@accounts.google.com>
subject: New sign-in notification
snippet: Your Google Account was signed into on an unknown Windows device...
date: 1717171200
context_type: message_detail
Body: [邮件正文 800 chars]

## User request
Brief this mailbox and surface only emails that need attention.

## Output format
Return exactly this JSON shape:
{
  "candidate_id": "cand_abc123",
  "priority": "medium",
  "surface": true,
  "user_action": "reply",
  "title": "Short card title (English ≤12 words)",
  "context": "WHAT happened: who did what, when. Verifiable facts only.",
  "suggestion": "Specific next action. Be concrete, not generic.",
  "action": "create_draft",
  "needs": "≤4 words label",
  "latest_action": "≤8 words. What recently happened.",
  "latest_actor": "English name or service.",
  "confidence": 0.85
}

## Rules（10 条完整规则）
- user_action: "reply" if waiting; "review" if worth awareness. No "ignore".
- priority: security/risk→critical/high. reply→medium/high. routine→low.
- user_action=reply + priority=low is INVALID.
- Deadline in suggestion → priority≥medium.
- title: specific person/project/risk. If sender=mailbox owner, start with "Unsent draft".
- context: verifiable facts only. Do NOT explain why it matters.
- suggestion: one specific action. Never "evaluate and respond."
- NEVER suggest send/delete/unsubscribe.
- needs: concise like "Reply to Kate", "Timing decision".
- Re-read suggestion. Time constraint → priority NOT low.
```

### 输入量

| 要素 | 大小 |
|------|------|
| System prompt | ~120 chars |
| Strategy 描述 | ~100 chars |
| Rubric（6 条判断维度） | ~500 chars |
| Few-shot（2 个示例 + 完整 JSON） | ~2,000 chars |
| Mailbox owner 规则 | ~180 chars |
| 邮件上下文（Email + Body + Thread） | ~2,000 chars |
| User request | ~80 chars |
| Output format 模板 | ~600 chars |
| Rules（10 条） | ~1,500 chars |
| **总输入** | **~7,100 chars** |

### 输出 → 数据结构

```json
{
  "candidate_id": "cand_abc123",
  "priority": "high",
  "surface": true,
  "user_action": "reply",
  "title": "Google login alert from unknown device",
  "context": "An unknown Windows device signed into Google account today in Shanghai. No prior verification.",
  "suggestion": "Check recent login activity and change password immediately.",
  "action": "create_reminder",
  "needs": "Security check",
  "latest_action": "signed in from unknown device",
  "latest_actor": "Unknown device",
  "confidence": 0.92
}
```

由 `_parse_compact_batch_item()` 解析成 `JudgmentResult`：

```python
base_judgment:
  item_type="reply_required"
  requires_user_action=True
  risk_level="high"
  should_surface=True

final_decision:
  user_action="reply"
  priority="high"
  should_show_in_main_result=True
  user_facing_summary="Google login alert from unknown device"
  user_facing_reason="An unknown Windows device signed into Google account..."
  user_facing_recommendation="Check recent login activity and change password..."

mode_judgment:
  needs="Security check"
  latest_action="signed in from unknown device"
  latest_actor="Unknown device"
```

---

## 全链路数据流

```
用户请求: "Brief this mailbox and surface only emails that need attention."
  │
  ▼ parse_intent (规则)
MailTaskPlan(strategy_mode="default_secretary")
  │
  ▼ scan (Gmail API)
25 封 MessageLite
  │
  ▼ dedup (规则，按 thread_id)
20 封去重后
  │
  ▼ Phase 1 (LLM#1，3 批 × 8 封)
  ├─ 6 个 candidates (user_action=reply/review)
  └─ 14 个 low_value_items (user_action=ignore)
  │
  ▼ read_context (Gmail API)
6 个 CandidateContext（含 body 或 thread）
  │
  ▼ Phase 2 (LLM#2，6 次 × 1 候选)
6 个 JudgmentResult
  │
  ▼ guards (规则：禁止 send/delete，标记需审批)
6 个 JudgmentResult（可能被修改 priority/actions）
  │
  ▼ plan (规则聚合)
ActionPlan(main_items=4, lower_priority_items=2)
  │
  ▼ cards (存储)
4 张主卡片 + 2 张低优先级卡片 + 1 张 cleanup_bundle
```

## Anna vs DashScope 路径差异速查

| | Anna Sampling | DashScope |
|------|-------------|-----------|
| Phase 1 分批 | 每 8 封一批 | 全部一批 |
| Phase 1 JSON修复 | 二次 LLM 调用 | regex + `json_object` |
| Phase 2 prompt | 紧凑（rubric+fewshot → 扁平输出） | 完整（三层嵌套 schema） |
| Phase 2 max_tokens | 2048 | 1024 |
| Phase 2 timeout | 120s | 240s |
| 降级到另一方 | ❌ | ❌ (DashScope path 本来就直连) |
| JSON 修复 | Anna sampling 二次调用 | regex `_repair_json` |
