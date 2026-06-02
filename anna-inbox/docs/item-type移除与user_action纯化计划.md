# item_type 移除、Phase 1/2 Prompt 修正 实施计划

> 2026-05-29（北京时间）

## 背景

当前 Phase 1 LLM 在 hr@anna.partners 邮箱的实测表现：12 封进了 cleanup，其中 10 封的 reason 字段 LLM 自己写了 "useful signal" / "requires awareness" / "needs attention"，但 `user_action` 仍输出 `ignore`。推理和决策分裂。

根因：两个独立问题叠加——
1. `item_type` 字段的 association bias：LLM 判了 `AccountNotice` 就内在地绑定低优先级
2. reason 指令写成 "explaining your classification"，LLM 理解为"描述邮件内容"而非"论证 user_action 选择"

## 改动清单

全部在 prompt 层，不依赖关键词/规则兜底。`_PHASE1_SYSTEM` 两个 provider 共用。

---

### 改动 1：删除 `item_type`

**Phase 1 prompt**

- 删除 `## Item Types` 整段（"SecurityRisk | BillingPayment | ..." 共 15 种）
- 输出格式里删除 `"item_type": "SecurityRisk",` 字段

**Phase 2 prompt（两条路径）**

- `build_judgment_prompt`：输出 schema 删除 `base_judgment.item_type`
- `build_anna_single_judgment_prompt`：输出 JSON 删除 `"item_type":"reply_required",`

**代码适配**

| 位置 | 改动 |
|------|------|
| `parse_phase1_response` | 不再读 `item_type`；`candidate_kind` 从 `user_action` 三行推导（`reply→reply_required_possible`，`review→account_notice_possible`，`ignore→safe_cleanup_bundle`） |
| `_ITEM_TYPE_TO_KIND` | 删除 15 种映射表 |
| `_build_low_value_item` | 不再需要 `item_type` 参数 |
| `_derive_item_type()` | 删除函数 |
| `parse_judgment_output` / `_parse_compact_batch_item` | `base_judgment.item_type` 从 `user_action` 推导，不读 LLM 输出 |
| `_enforce_consistency` | 规则改为 `user_action` 驱动："reply+low→medium" |
| `build_card` | `item_type` 从 `user_action` 推导 |
| `PersistentCard.item_type` | 保留字段，值由代码推导（向后兼容） |

---

### 改动 2：reason 指令改为"论证决策"

**Phase 1 prompt 输出格式说明**，当前：

```
- reason: one short English sentence explaining your classification.
```

改为：

```
- reason: One sentence explaining WHY you chose this user_action.
          If user_action=ignore: state what information is being discarded
          and why the user doesn't need to see it.
          If user_action=review: state what useful signal this email contains.
          If user_action=reply: state who is waiting and for what.
```

效果：LLM 不会再写成一句话邮件摘要，而是被迫论证 `user_action` 选择。一旦 reason 里出现 "useful" / "awareness" / "deadline" 等词，LLM 自然会意识到 `ignore` 是错的。

---

### 改动 3：user_action 定义末尾加自检规则

Phase 1 prompt `## User Action` 段末尾加：

```
Before outputting: re-read your own reason.
If the reason says the email is "useful", requires "awareness",
"attention", or contains a "signal" or "deadline",
user_action CANNOT be ignore — use review instead.
```

效果：LLM 在生成 JSON 的最后一步做 self-consistency check。不依赖代码层关键词匹配，是 prompt 内化的约束。

---

### 改动 4：ignore 定义收紧

当前（已实施，列出来确保完整）：

```
ignore — You can delete this without opening and miss nothing useful.
         ONLY: mass-mail newsletters, daily digests with no personal action
         items, one-line thank-you with zero follow-up, internal guides/FYI
         with no ask and no deadline. If in doubt between review and ignore,
         choose review.
```

`review` 定义同步列出了正向示例：interview reminders、scorecard results、pipeline updates、new applications、referral notifications、calendar reschedules、candidate notes、feedback reminders with deadlines。

---

### 改动 5：Phase 2 prompt 同步删 `item_type`

**DashScope 路径** `build_judgment_prompt` 的 Rules 段，当前有：

```
- base_judgment.risk_level: security/billing→critical/high, needs reply→medium, notifications→low/none
```

删除 `base_judgment.item_type` 相关引用。

**Anna 路径** `build_anna_single_judgment_prompt` 输出 JSON 中 `"item_type"` 替换为仅 `"user_action"`。

---

## 改动范围

| 文件 | 改动要点 |
|------|---------|
| `phase1.py` | `_PHASE1_SYSTEM`：删 Item Types + 输出删 item_type + reason 改论证式 + 自检规则；`parse_phase1_response`：删 item_type 处理，`candidate_kind` 三行推导；`classifications_to_candidates`：evidence 写 `user_action` 替代 `llm_item_type`；删除 `_ITEM_TYPE_TO_KIND` |
| `judgment.py` | `build_judgment_prompt` 输出删 item_type；`build_anna_single_judgment_prompt` 输出删 item_type；`parse_judgment_output` / `_parse_compact_batch_item`：item_type 代码推导；删除 `_derive_item_type`；`_enforce_consistency`：user_action 驱动 |
| `card_service.py` | `build_card` 的 item_type 从 user_action 推导 |

## 预期效果

1. LLM 不再被 `item_type` 暗示优先级，`user_action` 决策更纯净
2. reason 字段由"邮件摘要"变为"决策论证"，消除推理-行动分裂
3. 自检规则内化到 prompt，LLM 自己拦截矛盾输出
4. prompt token 减少（Phase 1 少传 Item Types 列表 + 输出少字段）
