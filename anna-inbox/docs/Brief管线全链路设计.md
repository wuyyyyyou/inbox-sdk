# Brief 管线全链路设计

> 更新时间：2026-05-29 14:26（北京时间）

## 总览

```
Gmail API → 邮件缓存 (mail_adapter)
         → Phase 1 LLM (header batch 批量分类)
              ├─ user_action=reply   → Phase 2 LLM (全文评估) → main 三行卡片
              ├─ user_action=review  → Phase 2 LLM (全文评估) → 独立卡片（review tab）
              └─ user_action=ignore  → 跳过 Phase 2 → cleanup bundle 折叠卡
         → merge_cards → 持久化 (APS / Local JSON)
         → 前端渲染 (All / Needs reply / Needs review / Cleanup 四个 tab)
```

## Phase 1：批量分类 + 三路分流

### 输入

扫描窗口内所有邮件头：from_addr / subject / snippet / internal_date / label_ids / unread / starred / important / has_attachment。每条压缩为单字母 key 格式以降低 token 消耗，JSON 数组打包发送。

### LLM 调用

| provider | 批大小 | JSON 约束 |
|----------|--------|----------|
| DashScope | 全部一封 | `response_format: {"type": "json_object"}` |
| Anna Sampling | 每 8 封一批 | Prompt 文本约束 |

### LLM 输出

```json
{
  "message_id": "19e624897e9bd6ee",
  "item_type": "CandidateFollowup",
  "user_action": "reply",
  "priority_hint": "high",
  "read_depth": "thread_context",
  "confidence": 0.92,
  "reason": "Candidate following up on timeline, has another offer pending"
}
```

**`user_action` 是驱动下游分类的唯一字段**，三个值：

| user_action | 语义 | 去向 |
|-------------|------|------|
| `reply` | 有人在等回应（提问、请求、跟进、待确认） | CandidateItem → Phase 2 |
| `review` | 不需回复但值得 5 秒扫一眼（日程变更、pipeline 状态、scorecard、内部备注、候选人动态） | CandidateItem → Phase 2 |
| `ignore` | 纯通知/摘要/newsletter/自动提醒/简短感谢信 | 跳过 Phase 2 → cleanup bundle |

`item_type` 退化为信息标签（badge），展示在卡片上，不参与分类逻辑。

### 后处理（`parse_phase1_response`）

- `reply` / `review` → 生成 `CandidateItem`，`user_action` 写入 evidence 供 Phase 2 参考
- `ignore` → 收集到 `low_value_items[]`，后续直接拼 cleanup card
- LLM 未分类的邮件 → fallback 进入 `low_value_items[]`
- 兼容旧 prompt：`is_candidate=true` → `user_action="reply"`，`false` → `"ignore"`

---

## Phase 2：逐项评估

两条 LLM provider 路径，共用 `parse_judgment_output` / `_parse_compact_batch_item` 和后处理。

### DashScope 路径

| 项目 | 内容 |
|------|------|
| 入口 | `evaluate_item()` |
| Prompt | `build_judgment_prompt()` — 含 few-shot + 策略 rubric |
| 输出格式 | 嵌套 JSON：`base_judgment` / `mode_judgment` / `final_decision` |
| JSON 约束 | `response_format: {"type": "json_object"}` |
| 重试 | 最多 3 次 |

### Anna Sampling 路径

| 项目 | 内容 |
|------|------|
| 入口 | `evaluate_items_batch()` |
| Prompt | `build_anna_single_judgment_prompt()` — 极简扁平 |
| 输出格式 | 扁平 JSON：`candidate_id` + `title` + `context` + `suggestion` + `user_action` |
| JSON 约束 | Prompt 文本约束 |
| 重试 | 不重试（`max_attempts=1`） |

### LLM 输出（以 DashScope 路径为例）

```json
{
  "base_judgment": {
    "item_type": "reply_required",
    "risk_level": "medium",
    "requires_user_action": true
  },
  "final_decision": {
    "user_action": "reply",
    "priority": "medium",
    "should_show_in_main_result": true,
    "display_bucket": "需回复",
    "user_facing_summary": "Pekky Xiong needs offer timeline update",
    "user_facing_reason": "Pekky is in final conversations with another company and needs timeline clarity.",
    "user_facing_recommendation": "Reply with offer timeline before end of week.",
    "recommended_actions": [{"action_type": "create_reminder", ...}]
  }
}
```

### 后处理（两条路径共用）

1. **解析**：`parse_judgment_output()`（嵌套）或 `_parse_compact_batch_item()`（扁平）→ 统一 `JudgmentResult`
2. **一致性校验** `_enforce_consistency()`：
   - `user_action` 缺失 → 从 `item_type` 推导（`reply_required/confirmation_required` → `"reply"`，其余 → `"review"`）
   - `reply_required + priority=low` → 强制 `priority=medium` + `should_show_in_main_result=true`
   - recommendation 含 deadline 词（`before` / `by tomorrow` / `today`） + `priority=low` → 强制 `priority=medium`
3. **安全守护** `apply_rule_guards()` → 禁止 `send_email / delete_email / unsubscribe`；高风险升级

---

## 卡片构建

### 高价值卡片（`build_card`）

```
Phase 2 JudgmentResult
  user_action:     fd.user_action                    → 驱动前端 tab
  title:           fd.user_facing_summary            → 卡片 Line 1（≤12 words）
  summary:         fd.user_facing_reason             → 卡片 Line 2（≤30 words）
  recommendation:  fd.user_facing_recommendation     → 卡片 Line 3（≤15 words）
  display_section: should_show_in_main_result → "main" / "lower"
  item_type:       base_judgment.item_type           → 信息标签（badge）
```

### Cleanup bundle（`build_cleanup_bundle`）

```
Phase 1 low_value_items（user_action=ignore）
  → 一张折叠卡片，零 LLM 成本
  card_type: "cleanup_bundle"
  user_action: "cleanup"
  title: "Cleanup · {N} low-priority emails"
  bundled_messages: [{from_addr, subject, snippet, date, item_type, reason}, ...]
  display_section: "lower"
```

### 合并（`merge_cards`）

- 普通卡：按 `thread_id` 合并，新扫描的覆盖旧的
- Cleanup bundle：旧的全部丢弃，每轮扫描生成一张新的
- Snoozed 到期自动恢复
- Resolved / dismissed 丢弃

---

## 持久化

两条存储路径共用 `storage_ops` 接口层：

| 路径 | 实现 | 数据位置 |
|------|------|---------|
| APS | `StorageClient` → host RPC | Anna 平台 APS 服务 |
| Local | `LocalStorageClient` | `.local_storage/{mailbox}/cards/active.json` |

`PersistentCard` 关键字段：

| 字段 | 用途 |
|------|------|
| `user_action` | 驱动前端分类 tab（reply/review/cleanup） |
| `card_type` | `"cleanup_bundle"` 标识折叠卡片 |
| `bundled_messages` | cleanup bundle 内的低价值邮件列表 |
| `display_section` | main / lower |
| `title / summary / recommendation` | 三行卡片内容 |
| `item_type` | 信息标签 |
| `status` | pending / snoozed / resolved / dismissed |

---

## 前端

### 四个分类 Tab

| Tab | 包含的卡片 | 展示方式 |
|-----|-----------|---------|
| **All** | main 三行卡 + lower 折叠 "Quiet for now" + cleanup bundle | 默认视图 |
| **Needs reply** | `userAction="reply"` 的卡（含 lower） | 独立卡片列表 |
| **Needs review** | `userAction="review"` 的卡（含 lower） | 独立卡片列表 |
| **Cleanup** | cleanup bundle + `userAction="cleanup"` 的卡 | 折叠卡 + 批量已读 |

### 分类逻辑（`cardCategory`）

```
card.userAction === "reply"    → "reply"
card.userAction === "review"   → "review"
card.userAction === "cleanup"  → "cleanup"
旧卡无 userAction → 回退到 ITEM_TYPE_CATEGORY 映射表
```

### Cleanup bundle 交互

- 默认展开，点击折叠头切换
- `[Mark all read]`：逐行 50ms 间隔，淡蓝色背景 + ✓ 弹出 + 文字降透明度
- 全部标完后按钮 disabled，标题加删除线，计数器变 ✓

---

## 改动文件清单

| 文件 | 职责 |
|------|------|
| `phase1.py` | Phase 1 系统提示词；`parse_phase1_response` 三路分流；`run_phase1_batch_classify` |
| `judgment.py` | 两条路径的 prompt 构建；`parse_judgment_output` / `_parse_compact_batch_item`；`_enforce_consistency` |
| `types.py` | `FinalDecision` / `BaseJudgment` / `JudgmentResult` / `CandidateItem` 等数据类 |
| `storage_types.py` | `PersistentCard`（含 `user_action`/`card_type`/`bundled_messages`）；`ProcessedMessage`；`ActiveCards` |
| `card_service.py` | `build_card` / `build_cleanup_bundle`；`merge_cards`；`cards_to_frontend` |
| `pipeline.py` | 主流水线编排；`_persist_run_results_locked` |
| `llm.py` | `call_llm_json` / `call_llm_json_safe`；DashScope HTTP 调用 |
| `storage_ops.py` | 统一存储操作（get/set/list/delete/cards/run/prefs） |
| `storage_client.py` | 存储单例 |
| `local_storage.py` | 本地 JSON 文件存储实现 |
| `mail_adapter.py` | Gmail API 适配 + OAuth token 管理 |
| `guards.py` | 安全策略守护 |
| `main.py` | JSON-RPC 入口；Executa tool 清单；storage/LLM provider 切换 |
| `app.js` | 前端 SPA：渲染、交互、状态管理 |
| `style.css` | 前端样式 |
