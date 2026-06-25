# Brief 分类速度与准确性优化方案

> 更新时间：2026-06-25（北京时间）  
> 状态：方案待审查。本文只做方案设计，不改代码。  
> 适用范围：Brief 的 Phase 1 粗分类、Phase 2 judgment、cleanup bundle、分类评测与用户反馈闭环。

## 1. 结论

用户提出的两个问题本质上是同一个系统问题：当前分类链路已经有 Phase 1 / Phase 2 分层，但“规则、LLM、评测、用户反馈”之间还没有形成稳定的决策体系。

对问题一，先做粗颗粒度规则分类是可行的，而且当前代码已经部分走在这条路上：

- `phase1.py` 已有 `prefilter_phase1_messages()`，会把明显低价值 bulk 邮件过滤到 cleanup，把安全、账单、星标、重要、已知联系人、清晰请求等邮件走规则候选 fast path。
- `brief_flow.py` 已在 Phase 1 progress 中统计 `phase1_prefiltered`、`phase1_rule_candidates`、`phase1_llm_messages`。
- Phase 2 已按 4 个候选一批并发评估，并在单个候选完成后立即持久化卡片。

所以优化方向不是从零加关键词，而是把现有规则升级为正式的“本地分层分类器”：高置信规则直接给结论，低置信/灰区再交给 LLM。这样能减少平台 LLM 调用次数，并让首批卡片更快出现。

对问题二，继续往 prompt 里塞关键词是治标不治本。正确方向是：

- 先定义可审计的分类标准，而不是让模型自由理解“重要/不重要”。
- 把关键词当作“信号”，不直接当作“最终分类”。
- 建立离线 golden set 和回归测试，所有规则和 prompt 调整都必须在同一批样本上验证。
- 对 false cleanup（重要邮件被扔进 cleanup）设置最高严重级别，宁可多进入 review，也不要漏掉。
- 把用户反馈接入分类校准，但分强弱信号，避免一次点击造成错误泛化。

推荐目标形态：

```text
Gmail scan
  -> normalize headers / thread status / sender features
  -> signal extractor
  -> deterministic triage router
       ├─ rule_terminal_cleanup       -> cleanup bundle, no LLM
       ├─ rule_terminal_candidate     -> CandidateItem, 可选跳过 Phase 1 LLM
       ├─ rule_templated_card         -> 少数高置信 review 卡，跳过 Phase 2 LLM
       └─ gray_zone                   -> Phase 1 LLM
  -> Phase 2 only for reply / ambiguous / high-impact candidates
  -> code-level consistency guards
  -> cards + evaluation telemetry
```

## 2. 当前链路观察

### 2.1 当前分类链路

现有 Brief 主路径是：

```text
scan -> processed filter -> thread dedupe -> Phase 1 -> check replied -> read context -> Phase 2 -> guards -> cards
```

分类相关的关键模块：

| 模块 | 现状 |
|---|---|
| `mail_agent/core/candidate.py` | 已有 `detect_signals()`、`classify_candidate_kind_by_rule()`、`generate_candidates()`，主要基于关键词和 Gmail 元数据生成候选。 |
| `mail_agent/core/phase1.py` | Phase 1 前已有低价值 prefilter 和规则候选 fast path；剩余邮件进入 LLM 分类。 |
| `anna_inbox_executa/brief_flow.py` | 短 invoke 状态机分阶段推进；Phase 1 分片；Phase 2 一批 4 个候选并发。 |
| `mail_agent/judgment_engine/service.py` | Phase 2 已改为 `action_reason` 二叉决策，并通过 `_enforce_consistency()` 做代码级约束。 |
| `mail_agent/core/guards.py` | 阻止危险动作，并把高风险项强制提升展示。 |

### 2.2 已经解决了一部分“慢”

当前代码已经不是“所有邮件都靠大模型分类”：

- 明显 newsletter/digest/promotion/unsubscribe 可直接进入 cleanup。
- 安全、账单、星标、重要、人类回复、已知联系人、清晰请求可生成规则 candidate。
- LLM 超时或失败时会 fallback 到规则候选。
- Phase 2 已经支持候选并发和早持久化。

这说明“加粗分类逻辑提速”不是理论可行，而是已经部分实施。后续需要把它产品化、可评测化，而不是继续堆零散正则。

### 2.3 仍然存在的速度缺口

1. Phase 1 规则还是“信号 + 关键词”混合，缺少显式置信度和终止决策层。
2. 规则候选仍会进入 Phase 2 LLM；候选多时，Phase 2 仍是主要线性成本。
3. 短 invoke 路径直接调用 `_run_phase1_single_batch()`，Phase 1 分批策略和 `run_phase1_batch_classify()` 中的 Anna 8 封批大小约束存在不一致，需要正式化。
4. progress 记录了数量，但还不足以区分“规则命中节省了多少 LLM 调用”“LLM 灰区占比是多少”“false cleanup 风险来自哪类规则”。

### 2.4 仍然存在的准确率缺口

当前准确率问题不能只归因于 prompt：

- Phase 1 只看 header/snippet，天然会遇到信息不足。
- `detect_signals()` 中一些关键词过宽，例如 `account` 会触发 security，`receipt` 会触发 billing；这些只能作为弱信号。
- 低价值判断如果直接依赖 newsletter/digest/promotion 词，容易误伤“有 deadline 的 digest”“候选流程通知”“面试提醒”。
- 规则和 LLM 输出缺少统一的离线评测集，开发看到一两个邮箱样本后只能临时修 prompt 或加关键词。

## 3. 设计原则

### 3.1 分类是“路由”，不是只给标签

Brief 分类不仅要决定 tab，还要决定后续成本：

| 分类结果 | 下游行为 |
|---|---|
| `reply` | 必须进入 Phase 2，通常需要 read context / thread context。 |
| `review` | 根据置信度决定是否进入 Phase 2；高置信系统类 review 可模板化。 |
| `cleanup` | 不进入 Phase 2，只进 cleanup bundle。 |
| `gray` | 进入 LLM，让模型处理语义边界。 |

### 3.2 关键词只能是信号，不是最终规则

错误做法：

```text
subject contains "interview" -> review
subject contains "newsletter" -> cleanup
```

推荐做法：

```text
extract signals:
  source_type=bulk/system/human
  intent=event/reminder/request/status_update/promotion
  has_deadline=true/false
  protected=true/false
  relationship=known/unknown

decision:
  if protected -> never cleanup
  if bulk + no deadline + no personal ask + no protected signal -> cleanup
  if event/status/candidate signal -> review
  if direct ask/waiting/deadline from human -> reply
  else -> gray_zone LLM
```

### 3.3 false cleanup 是最高风险

分类错误应分级：

| 错误 | 严重度 | 处理原则 |
|---|---|---|
| 重要邮件进 cleanup | P0 | 必须用 protected signals 和测试集重点防住。 |
| 需要 reply 被判 review | P1 | Phase 2 和 thread reply check 继续兜底。 |
| review 被判 reply | P2 | 影响噪音，但不漏事。 |
| cleanup 被判 review | P3 | 可接受，后续通过用户反馈降权。 |

因此粗分类阶段应该遵循：不确定时进 review/LLM，不直接 cleanup。

## 4. 目标架构：本地分层分类器

### 4.1 新增概念：TriageDecision

建议在内部引入一个分类决策对象。它不一定要暴露给前端，先作为 pipeline 内部结构和 debug evidence。

```python
TriageDecision:
  message_id: str
  route: "cleanup" | "candidate" | "templated_card" | "llm"
  user_action: "reply" | "review" | "cleanup" | ""
  action_reason: str
  priority_hint: "high" | "medium" | "low"
  confidence: float
  matched_rules: list[str]
  protected_signals: list[str]
  evidence: dict
```

价值：

- 所有规则都输出统一结构。
- LLM prompt 可以收到规则证据，而不是只看原始 header。
- 测试和日志能精确定位是哪条规则导致误判。
- 后续用户反馈可以调整 rule weight，而不是继续改 prompt。

### 4.2 信号层：从关键词升级为结构化信号

当前 `detect_signals()` 可以保留，但建议升级为多维 signal extractor：

| 信号维度 | 示例 |
|---|---|
| `source_type` | `human_sender`、`automated_sender`、`bulk_sender`、`internal_sender` |
| `intent` | `direct_question`、`request_review`、`follow_up`、`calendar_change`、`candidate_update`、`scorecard`、`receipt`、`newsletter` |
| `risk` | `security_alert`、`payment_failed`、`permission_change`、`account_recovery` |
| `time` | `has_deadline`、`event_within_48h`、`relative_deadline_text` |
| `relationship` | `known_contact`、`important_contact`、`same_domain_colleague` |
| `gmail_state` | `unread`、`starred`、`important`、`has_attachment`、`draft`、`sent` |
| `bulk_features` | `list_unsubscribe`、`marketing_language`、`digest_format` |
| `user_memory` | `dont_prioritize_sender`、`no_action_needed_sender_count` |

关键词仍然可以用于提取这些信号，但最终分类必须由规则组合决定。

### 4.3 路由层：四级决策

#### Level 0：身份和状态守护

先处理不应交给 LLM 自由判断的事实：

- 发件人是 mailbox owner 且是 `SENT`：通常不生成卡片。
- 发件人是 mailbox owner 且是 `DRAFT`：`reply / unsent_draft`。
- thread 最新一封来自用户本人：通常不再提醒 reply。
- `starred` / `important`：禁止进入 cleanup。

#### Level 1：高置信 cleanup 终止规则

满足全部条件才允许直接 cleanup：

- `source_type` 是 bulk 或 automated。
- 有 `list_unsubscribe` 或强 bulk 格式。
- 没有 direct question / request / deadline。
- 没有 security / billing / permission / account recovery。
- 没有 candidate / interview / scorecard / referral / pipeline 相关信号。
- 没有 attachment / starred / important / known contact。

输出：

```text
route=cleanup
user_action=cleanup
action_reason=cleanup
confidence>=0.85
```

#### Level 2：高置信 candidate 规则

这些邮件可以跳过 Phase 1 LLM，直接成为 CandidateItem：

| 条件 | user_action | action_reason |
|---|---|---|
| 明确问题、请求、审批、确认 | reply | `question_asked` / `waiting_for_you` |
| 人类 follow-up，等待用户输入 | reply | `waiting_for_you` |
| draft 未发送 | reply | `unsent_draft` |
| 安全、付款失败、账号恢复、权限变更 | review | `security_or_billing` |
| 面试提醒、日程变化、deadline reminder | review | `upcoming_event` |
| 候选人申请、referral、scorecard、pipeline stage change | review | `deal_or_pipeline` |

注意：Level 2 只负责“是否值得关注”和“粗 action_reason”，具体卡片文案仍可交给 Phase 2，除非满足 Level 3。

#### Level 3：模板卡片规则

少数稳定、低语义复杂度的 review 可以跳过 Phase 2 LLM，直接生成模板卡片：

- 普通 receipt / subscription notice。
- 明确 calendar reschedule。
- 低风险账号通知。
- 明确候选 pipeline 状态通知。

模板卡片必须满足：

- 标题、摘要、建议都能从 sender/subject/snippet/date 确定。
- 不需要读正文才能判断。
- 不需要生成 draft。
- 不会建议危险动作。

这一步收益很大，因为 Phase 2 是每个候选一次 LLM。建议第一阶段只对 `review` 做模板卡，不对 `reply` 做模板卡。

#### Level 4：灰区交给 LLM

进入 LLM 的邮件应该是：

- 信号冲突，例如 bulk sender 但有 deadline。
- 语义需要正文，例如合作邀请是真人还是平台通知。
- 需要判断是否 courtesy_due。
- header/snippet 不足以判断。

LLM prompt 中应加入结构化信号和 matched_rules，让模型只解决灰区，而不是从零分类。

## 5. 速度优化方案

### 5.1 Phase 1 前置全量路由

当前 prefilter 发生在每个 Phase 1 batch 内。建议把它提升为 Phase 1 的正式前置步骤：

```text
new_messages
  -> triage_router(messages)
  -> terminal_cleanup
  -> terminal_candidates
  -> templated_cards
  -> llm_messages
```

收益：

- 可以一次性统计全局 rule/LLM 比例。
- 可以按优先级排序 `llm_messages`。
- 可以控制每次 invoke 的 LLM 调用预算。
- 可以避免 private `_run_phase1_single_batch()` 和 public `run_phase1_batch_classify()` 分批策略不一致。

### 5.2 控制 Phase 1 LLM 调用预算

建议设置每个 continue invoke 的预算：

```text
phase1_llm_max_calls_per_invoke = 4
phase1_llm_batch_size = 8 或 12
```

规则已经确定的邮件不占预算。若灰区邮件超过预算，保存 cursor 下次 continue。

验收指标：

- `phase1_llm_messages / phase1_input`。
- `phase1_llm_calls`。
- `phase1_rule_terminal_cleanup`。
- `phase1_rule_terminal_candidates`。
- `phase1_gray_zone`.

### 5.3 Phase 2 只处理真正需要深读的候选

建议将 candidates 分为三类：

| 类型 | Phase 2 行为 |
|---|---|
| `reply` | 保持进入 Phase 2。 |
| `review_high_impact` | 进入 Phase 2，例如 security/billing、关键 pipeline、复杂日程变化。 |
| `review_template_safe` | 跳过 Phase 2，用模板卡。 |

第一阶段不要让 `reply` 跳过 Phase 2，因为 reply 需要判断 reply gaps、草稿方向和具体建议。

### 5.4 Phase 2 优先队列

Phase 2 当前按 candidates 顺序每批 4 个。建议排序：

1. `reply` + high/medium。
2. `security_or_billing`。
3. `important/starred/known_contact`。
4. `upcoming_event` 且 48 小时内。
5. 其他 review。

这样即使总扫描未完成，用户先看到最重要卡片。

### 5.5 Judgment 缓存

对未变化的 thread，不要重复 Phase 2：

```text
cache_key = mailbox + thread_id + latest_message_id + latest_internal_date + body_hash
```

如果 cache hit，直接复用 JudgmentResult 或 PersistentCard 的可复用字段。注意不能缓存用户状态字段，如 snoozed/resolved。

## 6. 准确性优化方案

### 6.1 先写分类标准，再写规则和 prompt

建议新增一份“Brief 分类标注规范”，核心定义：

| user_action | 判定问题 |
|---|---|
| `reply` | 是否需要用户发送回复？ |
| `review` | 不需要回复，但是否值得用户 5 秒扫一眼？ |
| `cleanup` | 用户不打开也不会错过有用信息吗？ |

并明确业务域例子：

- recruiting：new application、interview reminder、scorecard、referral、stage changed 默认至少 review。
- security/billing：安全、付款失败、权限变更默认 review/high，不进 cleanup。
- marketing/newsletter：只有无个人 ask、无 deadline、无业务状态变化时 cleanup。
- thank-you：一行客套可 cleanup；候选人表达持续兴趣或包含后续上下文则 review/reply。

### 6.2 建立 golden set

建议在仓库内新增可脱敏测试样本，例如：

```text
inbox-tool/src/tests/fixtures/brief_triage_golden.jsonl
```

每条样本包含：

```json
{
  "id": "hr_001",
  "from": "lever@example.com",
  "subject": "Scorecard: Maya Chen Strong yes",
  "snippet": "Interview feedback was submitted...",
  "labels": ["INBOX"],
  "expected_user_action": "review",
  "expected_action_reason": "deal_or_pipeline",
  "must_not_cleanup": true,
  "notes": "scorecard result is useful recruiting signal"
}
```

样本来源：

- 已知误判邮箱的脱敏样本。
- HR/recruiting 合成样本。
- 安全/账单样本。
- newsletter / digest / promotion 样本。
- 人类请求、自动通知混淆样本。

### 6.3 指标按错误成本加权

不要只看总体准确率。建议使用：

| 指标 | 目标 |
|---|---|
| false cleanup rate | 必须接近 0。 |
| reply recall | 高于 precision，优先不漏回复。 |
| review recall | recruiting / security / billing 域必须高。 |
| cleanup precision | 直接 cleanup 的必须非常准。 |
| LLM bypass rate | 衡量速度收益。 |
| card noise rate | 用户标记 no action needed 的比例。 |

### 6.4 Prompt 改动必须过评测

以后不建议“发现错一封就往 prompt 加关键词”。流程应改为：

1. 把误判样本加入 golden set。
2. 判断错误发生层：signal、rule、Phase 1 LLM、Phase 2 LLM、postprocess。
3. 如果是规则缺陷，改规则组合。
4. 如果是标准不清，改标注规范。
5. 如果是 LLM 灰区，改 prompt 或 few-shot。
6. 跑 golden set，确认没有引入新的 false cleanup。

### 6.5 用户反馈闭环

已有 `用户偏好信号.md` 中区分了强信号和弱信号，建议分类优化沿用：

| 用户行为 | 信号强度 | 分类影响 |
|---|---|---|
| `dont_prioritize` | 强 | sender/thread 降权，但 security/reply/deadline 仍可进入 main。 |
| `no_action_needed` 1 次 | 弱 | 只记录，不立即泛化。 |
| `no_action_needed` 2 次以上 | 中 | 同 sender 类似邮件优先 lower priority，不直接 cleanup。 |
| 用户从 cleanup 找回或恢复 | 强负反馈 | 对相似规则加 protected，降低 direct cleanup。 |

重点：用户反馈不应该直接变成“以后这个 sender 全部忽略”，否则会制造新的漏判。

### 6.6 优先级稳定性：避免同一封邮件多次扫描等级抖动

当前还有一个独立问题：同一封邮件或同一 thread 在多次扫描中可能 priority 不稳定，例如这次是 `medium`，下一次又变成 `critical`。这会削弱用户信任，也会让前端卡片顺序和注意力等级反复跳。

这类问题通常有三种来源：

| 来源 | 例子 | 处理方式 |
|---|---|---|
| LLM 非确定性 | 同样的正文，这次判断 medium，下次判断 high。 | 同一内容版本复用旧 judgment，不重复覆盖。 |
| 上下文变化 | thread 里新来一封催促、deadline 或安全告警。 | 允许升级，但必须记录升级证据。 |
| 规则/prompt 版本变化 | 代码发布后同一旧邮件被新规则重判。 | 保留已有 active card 的稳定等级，只对新内容或人工要求重算。 |

建议引入“优先级稳定策略”，核心不是让 priority 永远不变，而是要求变化必须有可解释原因。

#### 稳定身份：区分同一内容和新内容

对每张普通卡片记录一个轻量分类版本：

```text
classification_key = mailbox + thread_id + latest_message_id + latest_internal_date
classification_fingerprint = hash(subject + snippet + latest_body_excerpt + matched_signals)
```

如果下一次扫描的 key/fingerprint 没变，说明邮件内容没有新信息。此时不应该让新的 LLM 输出直接覆盖旧 priority。

#### 同一内容版本：默认复用旧等级

同一内容版本重复扫描时：

- 保留旧 `priority`、`user_action`、`display_section`。
- 可更新 Gmail 状态，例如 unread、latest_from_owner、missing、sync_failed。
- 可补全旧卡缺失字段，例如旧卡没有 `reply_gaps`，新结果有。
- 不允许仅凭一次新 LLM 输出把 `medium` 改成 `critical`，也不允许把 `high/medium` 降到 `low/cleanup`。

例外：

- 旧结果是 fallback judgment，且新结果是正常高置信 judgment。
- 用户手动要求重新扫描/重新判断。
- 代码规则发现硬风险信号，例如 unauthorized login、payment failed、permission change。

#### 新内容版本：允许升级，降级要保守

如果 thread 有新邮件、latest message 变了、Gmail label 状态发生关键变化，则允许重新评估。

升级规则：

- `medium -> high/critical` 必须有新增证据，例如安全/付款失败/权限变更/明确 deadline/对方催促。
- 升级可以立即生效，因为漏掉高风险比噪音更严重。
- 升级时在 evidence/debug 中记录 `priority_changed_reason`。

降级规则：

- `critical/high -> medium/low` 需要更保守。
- 不建议把 active card 自动降到 cleanup；最多先降为 review/lower priority。
- 降级最好满足至少一个条件：
  - 用户标记 `no_action_needed` 或 `dont_prioritize`。
  - Gmail 状态显示用户已回复或邮件已读且无待处理信号。
  - 连续两次新内容版本都被判为低优先级。
  - 新邮件明确关闭事项，例如 “resolved / cancelled / no action needed”。

#### merge_cards 应改为“稳定合并”，不是新卡直接覆盖旧卡

当前 `merge_cards()` 对同一 `thread_id` 的普通卡会偏向新卡覆盖旧卡。建议后续改为：

```text
if same thread_id:
  compare old.classification_key with new.classification_key
  if same content version:
      keep old priority/user_action/display_section
      merge newer metadata and missing fields
  else:
      apply priority transition policy
      keep old card_id/status/snooze/resolution fields
```

这样用户看到的是“同一个事项在持续更新”，而不是同一邮件每扫一次就像新卡一样被重判。

#### MVP 实现建议

项目周期紧张时，第一轮可以先不新增完整 fingerprint，只做最小稳定策略：

- 以 `thread_id + message_id + internal_date` 作为内容版本。
- 同一版本命中已有 active card 时，保留旧 `priority/user_action/display_section`。
- 只有新 message 或硬风险规则命中时允许升级。
- 不允许自动把已有 active card 降级到 cleanup。
- 在卡片 `details` 或 debug evidence 中记录 `priority_source=stable_existing|new_evidence|hard_guard|llm`。

这个改动能快速解决“同一封邮件反复扫描等级不同”的体感问题，且不需要先完成完整 triage router。

## 7. 落地顺序

### 7.1 项目周期紧张版 MVP 顺序

如果当前周期只能做一轮优化，建议不要先做大重构，也不要先做模板卡和 judgment cache。先把“少调 LLM + 不漏重要邮件 + 可验证”落下来。

#### 第 1 步：先补最小评测集和标注标准

目标：避免继续用“看到一封错一封就改 prompt”的方式修问题。

改动范围：

- 新增一份简短分类标注规范，先覆盖 `reply / review / cleanup` 三类。
- 从当前已知误判邮箱里抽 20-40 条脱敏样本，放入 golden set。
- 样本重点覆盖：
  - HR/recruiting：new application、interview reminder、scorecard、referral、stage changed。
  - 明显 bulk：newsletter、digest、promotion、unsubscribe。
  - 安全/账单：login、payment failed、invoice、permission change。
  - 人类请求：direct question、please review、let me know。

验收：

- 每个样本有 `expected_user_action` 和 `must_not_cleanup`。
- false cleanup 单独统计。
- 不要求一次性覆盖所有邮箱场景，先覆盖当前真实痛点。

#### 第 2 步：收紧 Phase 1 cleanup 规则，优先保护不漏判

目标：解决“正常人觉得不该放 cleanup”的准确率问题。

改动范围：

- 在现有 `prefilter_phase1_messages()` / `_is_obvious_bulk_low_value()` 基础上小改，不先抽象完整新模块。
- 增加 protected signals：候选人、面试、scorecard、referral、stage changed、feedback reminder、deadline、calendar reschedule。
- cleanup 必须满足更严格条件：bulk/退订 + 无 protected signal + 无 deadline + 无个人请求 + 非 starred/important/attachment/known contact。
- 不确定的一律进入 LLM 或 review，不直接 cleanup。

验收：

- golden set 中 `must_not_cleanup=true` 的样本 0 个进入 cleanup。
- 明显 newsletter/digest 仍能进入 cleanup。
- Phase 1 prompt 不再靠新增一堆个案关键词兜底。

#### 第 3 步：扩大规则 fast path，但只跳过 Phase 1，不跳过 Phase 2

目标：先提速，风险可控。

改动范围：

- 继续复用现有 rule candidate fast path。
- 对高置信邮件直接生成 CandidateItem，跳过 Phase 1 LLM：
  - security/billing。
  - starred/important。
  - known contact + direct request。
  - HR/recruiting 状态类 review。
- 这些 candidate 仍进入 Phase 2，让 LLM 负责卡片内容和最终 action_reason。

验收：

- `phase1_llm_messages / phase1_input` 明显下降。
- 新增 progress 字段或复用现有字段展示 `phase1_rule_candidates`、`phase1_prefiltered`、`phase1_llm_messages`。
- 不改变前端字段，不改变卡片 schema。

#### 第 4 步：Phase 2 先做排序，不先做跳过

目标：让用户更早看到重要卡片，同时避免模板卡带来的新准确率风险。

改动范围：

- Phase 2 candidates 排序：
  1. `reply` / direct request。
  2. security/billing。
  3. starred/important/known contact。
  4. deadline/upcoming event。
  5. 其他 review。
- 保持现有一批 4 个并发和完成即持久化逻辑。

验收：

- 首张重要卡片出现时间下降。
- Phase 2 总调用数不一定下降，但体感更快。
- 不改变 Phase 2 输出协议。

#### 第 5 步：加最小优先级稳定策略

目标：解决同一封邮件多次扫描 priority 抖动的问题。

改动范围：

- 同一 `thread_id + message_id + internal_date` 命中已有 active card 时，保留旧 `priority/user_action/display_section`。
- 新结果只补充缺失字段，不直接覆盖已有等级。
- 有新 message 或硬风险信号时允许升级。
- 不自动把已有 active card 降级到 cleanup。

验收：

- 同一封邮件重复扫描 3 次，priority 不应在 `medium/high/critical` 之间来回跳。
- 有新 deadline/security/payment failed 证据时，可以升级。
- 用户已处理或已回复的卡片按现有 lifecycle 退出，而不是靠降级到 cleanup 消失。

#### 第 6 步：补观测，不做大而全 telemetry

目标：上线后能知道优化有没有效果。

最低观测字段：

```text
phase1_input
phase1_prefiltered
phase1_rule_candidates
phase1_llm_messages
phase1_llm_calls
phase2_candidates
phase2_evaluated
first_card_elapsed_ms
```

验收：

- 用户反馈“慢”时能判断慢在 Phase 1 还是 Phase 2。
- 用户反馈“分类错”时能看到该邮件是 rule cleanup、rule candidate 还是 LLM 判定。

### 7.2 本轮暂缓项

这些方向有价值，但不建议放进紧张周期第一轮：

| 暂缓项 | 原因 |
|---|---|
| 完整 `TriageDecision` 大重构 | 架构更干净，但会扩大改动面。第一轮可先用现有 dict/evidence 承载 matched rule。 |
| review 模板卡跳过 Phase 2 | 能省调用，但容易引入卡片质量和边界问题。等 golden set 稳定后再做。 |
| judgment cache | 对重复扫描有效，但需要处理 cache key、失效和用户状态字段。 |
| 用户反馈自动学习 | 需要产品闭环和存储策略，第一轮先不做自动泛化。 |
| Gmail History API 增量 | 性能收益大，但不是分类准确率主线。 |

### 7.3 完整版后续顺序

#### Phase A：方案审查后先做评测基础

1. 写 `Brief 分类标注规范`。
2. 建 `brief_triage_golden.jsonl` 脱敏样本。
3. 建离线脚本，输出规则路由、Phase 1 结果、Phase 2 结果和 confusion matrix。
4. 把 `test_phase1_prefilter.py` 扩展为 golden set 回归。

验收：

- 至少覆盖 80-120 条样本。
- HR/recruiting、安全/账单、newsletter、人类请求各不少于 15 条。
- false cleanup 单独统计。

#### Phase B：正式化本地 triage router

1. 抽出 signal extractor，保留当前 `detect_signals()` 兼容入口。
2. 新增 `TriageDecision` 内部结构。
3. 把现有 `_is_obvious_bulk_low_value()`、`_should_use_rule_fast_path()` 收敛到 router。
4. 所有 rule 输出 `matched_rules` 和 `confidence`。
5. progress 增加 rule/LLM 分布。

验收：

- 直接 cleanup precision 在 golden set 上达到高置信目标。
- false cleanup 为 0 或必须有明确人工接受的例外。
- Phase 1 LLM 输入量明显下降。

#### Phase C：Phase 2 减负

1. 增加 `review_template_safe` 白名单。
2. 模板卡只覆盖低风险 review，不覆盖 reply。
3. Phase 2 candidates 排序，高优先级先评估。
4. 加 judgment cache，避免重复评估未变化 thread。

验收：

- Phase 2 LLM 调用数下降。
- 首张重要卡片出现时间下降。
- 模板卡误分类不引入 false cleanup。

#### Phase D：用户反馈校准

1. 接入 `dont_prioritize` 到 triage router 的降权逻辑。
2. 接入 `no_action_needed` 频次弱信号。
3. 增加“从 cleanup 找回”的负反馈记录。
4. 反馈只影响优先级和是否进入 lower priority，不直接绕过 protected signals。

## 8. 需要新增的观测字段

建议在 run progress / debug 中加入非敏感指标：

```text
phase1_input
rule_terminal_cleanup
rule_terminal_candidates
rule_templated_cards
gray_zone_messages
phase1_llm_messages
phase1_llm_calls
phase1_llm_elapsed_ms
phase2_input_candidates
phase2_template_skipped
phase2_llm_calls
phase2_llm_elapsed_ms
first_card_elapsed_ms
false_cleanup_feedback_count
```

注意不要记录邮件正文、OAuth token、authorization header、完整 credential context。

## 9. 方案风险

| 风险 | 应对 |
|---|---|
| 规则过强导致漏判 | cleanup 规则必须保守；protected signals 优先级最高；golden set 强制测 false cleanup。 |
| 规则越来越散 | 所有规则集中到 triage router，必须输出 matched_rules。 |
| 模板卡文案质量低 | 第一阶段只覆盖稳定 review，不覆盖 reply；模板字段来自可验证事实。 |
| 用户反馈过度泛化 | 区分强弱信号；弱信号只降权，不直接 cleanup。 |
| LLM 调用减少但噪音增加 | 同时看 card noise rate 和 no_action_needed 反馈。 |

## 10. 本方案回答用户问题

### 问题一：先写粗颗粒度逻辑能不能提速？

能，而且当前已经部分实现。下一步应升级为正式的本地 triage router，让明显 cleanup 和明显 candidate 不再消耗 Phase 1 LLM；再让少数高置信 review 用模板卡跳过 Phase 2 LLM。这样比单纯缩短 timeout 或压低 max_tokens 更稳。

### 问题二：准确率到底该怎么弄？

不要继续用“加提示词 + 关键词过滤”处理个案。应该建立分类标准、结构化信号、可解释规则、LLM 灰区处理、代码级 guard、golden set 回归、用户反馈校准这一整套闭环。关键词可以保留，但只能作为信号，最终分类必须由规则组合和评测结果负责。
