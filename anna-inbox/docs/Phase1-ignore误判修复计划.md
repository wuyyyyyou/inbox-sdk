# Phase 1 ignore 误判修复计划

> 2026-05-29 14:50（北京时间）

## 问题报告

### 现象

HR 邮箱 21 封邮件，Phase 1 把 11 封判为 `ignore` 进了 cleanup bundle。人工复查发现其中仅 2 封真正不重要，其余 9 封都需要用户至少扫一眼（review），1 封甚至需要行动（reply）。

### 误判明细

| # | 邮件 | Phase 1 判 | 正确应为 | 差距 |
|---|------|-----------|---------|------|
| 2 | New application: Jason Miller | ignore | review | 新候选人值得知道 |
| 3 | Reminder: Priya Nair interview tomorrow | ignore | review | 明天有面试 |
| 5 | Candidate note: Priya "worth moving quickly" | ignore | review | 面试官内部备注 |
| 9 | Scorecard: Maya Chen "Strong yes" | ignore | review | 评分卡结果有用 |
| 10 | New referral: Nina Patel | ignore | review | 内推候选人 |
| 11 | New event: Emily Rogers interview Thursday | ignore | review | 周四有新面试 |
| 15 | Interview feedback reminder: Daniel Park | ignore | **reply** | 有 deadline，明天前需交 |
| 17 | Interview rescheduled: Jason Miller | ignore | review | 日程变更 |
| 18 | Candidate stage changed: Leo Martinez | ignore | review | pipeline 进展 |
| 6 | Daily recruiting digest | ignore | ignore ✅ | — |
| 8 | Guide: improve response times | ignore | ignore ✅ | — |

11 封中 9 封误判，准确率仅 18%。

### 根因

Phase 1 LLM 只读邮件头（from / subject / 100 字符 snippet），不读正文。但 snippet 已经包含足够信息——例如 Lever feedback reminder 的 snippet 里就有 "feedback is pending" 和 "tomorrow's hiring sync"。

**真正的问题在 Phase 1 prompt 的 `ignore` 定义**：

```
ignore — Purely automated: newsletter, digest, interview reminder,
         new-application notification, mass-mail guide with no personal ask,
         simple thank-you with no follow-up context.
```

把 `interview reminder` 和 `new-application notification` 写进了 ignore 的**示例**里，LLM 看到这两个词就直接归为 ignore，不再思考这封邮件是否有用。

## 修复方案

### 把 `ignore` 收紧为"看完标题就不需要任何后续动作"

```
ignore — You can delete this without opening it and miss nothing.
         ONLY these types: mass-mail newsletter, digest with no personal
         action items, one-line thank-you with zero follow-up,
         internal guide/fyi with no ask and no deadline.
         If in doubt between review and ignore, choose review.
```

关键改动：
1. 删除 `interview reminder` 和 `new-application notification` 示例
2. 加 "If in doubt, choose review" 兜底规则
3. `review` 的示例里明确列出被误判的类型

### 同步收紧 `review` 定义

```
review — No reply needed, but contains useful signal worth 5 seconds.
         Examples: interview reminders, new applications, scorecard results,
         pipeline stage changes, calendar reschedules, internal candidate notes,
         referral notifications, feedback reminders with deadlines.
```

把被误判的 8 种类型全部列进 review 的正面示例。

### 改动范围

单文件单段落：`phase1.py` 第 57-84 行的 `_PHASE1_SYSTEM` 中 User Action 部分。

```diff
- ignore — Purely automated: newsletter, digest, interview reminder,
-         new-application notification, mass-mail guide with no personal ask,
-         simple thank-you with no follow-up context.
+ ignore — You can delete this without opening and miss nothing.
+         ONLY: mass-mail newsletter, daily digest with no personal action
+         items, one-line thank-you with zero follow-up, internal guide/FYI
+         with no ask and no deadline. If in doubt, choose review.

+ review — No reply needed, but contains useful signal worth 5 seconds.
+         Pipeline changes, scorecard results, interview reminders,
+         new applications, referral notifications, calendar reschedules,
+         internal candidate notes, feedback reminders with deadlines.
```

### 预期效果

以 HR 邮箱 21 封为例，改动后预期：

| 分类 | 改动前 | 改动后 |
|------|--------|--------|
| reply | 7 | 8（Lever feedback reminder 升为 reply） |
| review | 2 | 10（8 封从 cleanup 升为 review） |
| ignore | 11 | 2（仅 digest + guide） |
| 误判 | 9/11 | ~1-2/13 |
