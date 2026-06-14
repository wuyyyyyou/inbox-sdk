# Phase 2 Judgment — 二叉决策改造

> 2026-06-04
> **状态：已实施。** `action_reason` 已替代 bucket 分类，`_enforce_consistency` 已包含 6 条硬约束。

## 改造目的

Phase 2 judgment 是 Brief 流程中读完全文后对邮件做最终判断的环节。原先使用 7 个语义重叠的 bucket（`must_review`、`needs_reply`、`needs_confirmation`、`agent_can_prepare`、`safe_cleanup`、`lower_priority`、`ignore`），LLM 需要在边界模糊的分类中强行选择。

核心问题：
- `needs_reply`（含问句）和 `needs_confirmation`（需确认）语义重叠——对方问了问题，是 reply 还是 confirmation？LLM 只能随机
- 需要回复的邮件被误判为 `needs_confirmation` 甚至 `safe_cleanup`
- 没有可审计的原因字段，错了无法追溯

改造目标：
1. 将多分类简化为二叉判断：先回答「是否需要发送回复」，再选原因
2. 代码层对 `action_reason` 做硬约束，兜底 LLM 不一致
3. 宁多勿少——漏判的代价远高于多一个 card

## 改造前后差异

### Prompt 结构

| | 改造前 | 改造后 |
|---|---|---|
| 判断方式 | 7-bucket 多分类，LLM 直接选一个 | 二叉决策：先判断「是否需要回复」，再选具体原因 |
| bucket 定义 | `must_review / needs_reply / needs_confirmation / agent_can_prepare / safe_cleanup / lower_priority / ignore`（7 个语义重叠的桶） | 去掉 bucket，新增 `action_reason`（9 个互斥原因，每个有明确的 reply/review 归属） |
| 输出字段 | `mode_judgment.bucket` | `final_decision.action_reason` + `final_decision.user_action` |
| 优先级规则 | LLM 自行判断，仅少数规则约束 | `action_reason` → priority 有明确映射表，LLM 必须遵守 |

### action_reason 定义

**需要回复（user_action = "reply"）：**

| action_reason | 触发条件 | priority 约束 |
|---|---|---|
| `question_asked` | 对方明确问了问题或提了请求 | ≥ medium, surface |
| `waiting_for_you` | 对方明确在等你的输入/审批/决定 | ≥ medium, surface |
| `unsent_draft` | 用户写了草稿但没发送 | ≥ medium, surface |
| `courtesy_due` | 对方投入实质性努力（3+ 句正文、分享文档、明确征求想法），不触发自动通知/newsletter/收据/一行状态更新 | ≥ medium, surface |

**不需要回复（user_action = "review"）：**

| action_reason | 触发条件 | priority 约束 |
|---|---|---|
| `upcoming_event` | 面试/会议/截止日期提醒 | ≥ medium |
| `deal_or_pipeline` | 项目/合作/交易状态更新，值得跟踪 | ≥ medium |
| `security_or_billing` | 安全告警、账单异常、订阅变更 | ≥ high, surface |
| `receipt_or_notice` | 收据、订阅确认、普通账号通知，仅记录 | low |
| `cleanup` | 新闻通讯、促销、自动摘要，可归档 | low |

### 代码层硬约束（`_enforce_consistency`）

改造前仅有 3 条软性校验。改造后增加 6 条硬约束，**代码优先级高于 LLM 输出**：

1. `action_reason` ∈ `REPLY_REASONS` → `user_action` 强制 `"reply"`
2. `action_reason` ∈ `REVIEW_REASONS` → `user_action` 强制 `"review"`
3. reply reasons → priority ≥ medium，强制 surface
4. `security_or_billing` → priority ≥ high，强制 surface
5. `cleanup` → priority = low
6. deadline 关键词 → priority ≥ medium（保留旧规则）

`REPLY_REASONS` = `{question_asked, waiting_for_you, unsent_draft, courtesy_due}`
`REVIEW_REASONS` = `{upcoming_event, deal_or_pipeline, security_or_billing, receipt_or_notice, cleanup}`

### 涉及文件

| 文件 | 改动 |
|---|---|
| `judgment_engine/service.py` | `_secretary_schema` 去 bucket；`_final_decision_schema` 加 action_reason；三个 prompt builder 全部替换 rubric；`_enforce_consistency` 重写；`parse_judgment_output` / `_parse_compact_batch_item` 存 action_reason 到 mode_judgment；新增 `_extract_action_reason` |
| `planning/strategies.py` | rubric 字段不再被 prompt 引用（prompt 改为内联二叉决策 rubric），可后续清理 |

### 生产路径

生产环境始终走 **Anna 单候选**路径（`build_anna_single_judgment_prompt`），批量路径（`build_batch_judgment_prompt`）仅在 DashScope 回退时使用。
