# indexv4 差距分析

> 当前实现：`bundle/index.html` + `bundle/app.js` + `bundle/style.css`
> 目标效果：`bundle/indexv4.html`（290KB，单文件包含 CSS/HTML/JS）
> 分析日期：2026-05-26

---

## 1. 视图体系重构（架构级）

| 当前 | indexv4 |
|------|---------|
| 3 个视图：`start`、`ask` + history/sources drawer | 7 个视图：`first-run` → `scanning` → `start` → `briefing` → `actions` → `learning` + `ask` |

indexv4 引入了完整的引导式工作流：首次扫描动画 → 逐条走查 → 审批 memo → 学习偏好。

当前 `render()` 只分发 `start` / `ask`，indexv4 分发 7 种视图：

```js
// 当前
function render() {
  if (state.view === "start") renderStart();
  if (state.view === "ask") renderAsk();
  ...
}

// indexv4
function render() {
  if (appState.view === "first-run") renderFirstRun();
  if (appState.view === "scanning") renderScanning();
  if (appState.view === "start") renderStart();
  if (appState.view === "ask") renderAskAnna();
  if (appState.view === "loading") renderLoading();
  if (appState.view === "briefing") renderBriefing();
  if (appState.view === "actions") renderActionReview();
  if (appState.view === "learning") renderLearning();
  ...
}
```

---

## 2. 场景化扫描（Scenario Briefings）

**当前**：固定 `default_secretary` 模式，用户只能选择 strategy mode。

**indexv4**：定义了 5 种预配置场景（`scenarioData`），每种自带 `timeRange`、`scope`、`prompt`、`items`、`actionMemo`、`learning`：

| 场景 | 用途 | 时间窗口 |
|------|------|---------|
| `today` | 今日简报 | 24h |
| `unread` | 清理未读堆 | 30天 |
| `person` | 按人查看（如 Christopher） | 30天 |
| `recruiting` | 招聘邮件专项 | 14天 |
| `week` | 上周遗漏回顾 | 7天 |

每个场景的数据结构：
- `proposalTitle` / `proposalTag` / `proposalCopy` — 提案卡片展示
- `opening` / `summary` — 开场概述
- `items[]` — 逐条走查项（label/line/point/recommendation/actions）
- `actionMemo` — 审批清单（safe[] / review[] / 高风险禁区）
- `learning` — 学习偏好提示

---

## 3. 逐条走查式 Briefing

**当前**：所有 attention card 一次性平铺在 `start` 页面。

**indexv4**：`stepIndex` 控制逐条展示，用户点击 "Hear next" 逐个查看。每条的结构：

```
Anna 头像 + "X of Y · Needs your decision"
├── 一句话概述（line）
├── 核心观点（point）
├── 建议（recommendation）
├── 决策按钮（actions with primary）
└── 决策反馈（status）
```

最后一步是 `renderActionPrompt()` — 进入审批 memo。

---

## 4. Action Memo / 审批工作流（全新）

**当前没有**。indexv4 在逐条走查完成后进入 `actions` 视图：

```
┌─ I can safely prepare these ─────────────────────┐
│  · Archive 18 LinkedIn/platform notifications    │
│  · Mark 7 system updates as read                 │
│  · Apply Recruiting label to 4 candidate emails  │
└──────────────────────────────────────────────────┘
┌─ I need you to review these ─────────────────────┐
│  · Review draft reply to Christopher             │
│  · Review draft reply to Jane                    │
│  · Confirm Eric follow-up timing                │
└──────────────────────────────────────────────────┘
┌─ I will never do these without explicit approval ┐
│  · Send replies                                  │
│  · Delete emails                                 │
│  · Unsubscribe                                   │
│  · Forward emails                                │
└──────────────────────────────────────────────────┘
```

- 低风险项可一键 "Approve low-risk cleanup"
- 高风险项（发送/删除/退订/转发）明确标注永不自作主张

---

## 5. 用户习惯学习（全新）

**当前没有**。indexv4 在审批完成后弹出学习卡片：

```html
"我注意到你 dismiss 了所有 LinkedIn 通知邮件"
"Should I treat these as background noise next time?"

[Yes, keep them quiet] [Ask me next time] [No, don't remember]
```

对应 `renderLearning()` + `appState.learningChoice` 状态。

---

## 6. 分类筛选标签

**当前**：用 `isMainCard()` / `lowerCards()` 二分（主卡/低优）。

**indexv4**：结果页面顶部有三分类 + "All" 标签栏：

| 分类 | 含义 | 示例 |
|------|------|------|
| Waiting for reply | 需要回复 | Christopher、Eric、Jane |
| Internal updates | 需要知晓 | 安全告警 |
| System noise | 可忽略 | LinkedIn 通知 |

每种分类有独立的批量操作按钮。

---

## 7. 回复队列 + 批量回复（全新）

**当前**：单个卡片 → drawer → 生成草稿 → 发送（单线程）。

**indexv4** 新增：

- **回复队列** (`replyQueueStatuses`, `replyWorkflow`)：多个待回复项排队处理
- **批量回复视图** (`renderBatchReplyDetail`)：同时审核多条回复
- **发送成功模态** (`renderSendSuccessModal`)：显示发送结果和计数
- **已发送历史** (`renderSentRepliesList`)：追踪已发送回复
- **自定义指令** (`replyQueueCustomInstruction`)：一次性修改所有回复

---

## 8. 多邮箱源管理（全新）

**当前**：单一邮箱 `state.mailbox`，sources drawer 仅显示基本信息。

**indexv4**：完整的 `sources[]` 数组，每个邮箱包含三层信息：

### Profile
- `primaryRole` — 主角色（Founder / business inbox）
- `secondaryRoles` — 次要用途
- `highPrioritySignals` — 高优先级信号
- `lowPrioritySignals` — 低优先级信号

### Strategy
- `operatingMode` — 运行模式
- `defaultScanRange` — 默认扫描范围
- `scanFocus` — 扫描关注点
- `readDepth` — 读取深度

### 状态追踪
- `lastScanned`、`backlog`、`nextIncluded`、`status`

Sources drawer 有三个 Tab：**Mailboxes** / **Profile** / **Strategy**，支持单邮箱重扫、排除/加入下次扫描。

---

## 9. 首次使用体验

**当前**：简单的 `isFirstScan` 状态 + 欢迎文案 + "Start my first scan" 按钮。

**indexv4**：完整的 onboarding 动画流程（`first-run` → `scanning` → `start`）：

```
1. Connecting mailboxes    — 检查已授权的邮箱
2. Reading recent activity — 审查最近线程和发件人上下文
3. Identifying what matters — 找出 open loops，分离噪音
4. Preparing your first brief — 按行动类型分组
```

每步有标题、microcopy、已审核邮件计数（动态变化），扫描完成自动进入 start。

---

## 10. Run Brief 模态框（全新）

**当前**：底部栏 "Scan now" 按钮直接触发扫描。

**indexv4**：`runBriefModal` 弹窗（`scan-modal`），两步流程：

**Setup 模式**：
- 选择邮箱（多选）
- 时间范围（since-last / last-24h / last-7d / unread-backlog / custom）
- 消息上限（50/100/200/custom）
- 优先级（多选）

**Progress 模式**：
- 显示扫描进度步骤
- "Stop here" 中止按钮

---

## 11. 提案卡片系统（全新）

**当前没有**。indexv4 在 Ask 视图中展示预定义场景提案卡片：

- 5 张提案卡片，分为 **attention**（person/recruiting/today）和 **cleanup**（unread）
- 每张卡片包含场景描述、能力说明、CTA
- Handle / Ignore 操作，忽略时可输入原因

---

## 12. 历史记录增强

**当前**：简单的 run 列表，仅显示时间和结果。

**indexv4**：
- 10 条 mock 历史记录，每条的 `details` 包含完整信息：
  - `scanRange` — 扫描范围配置
  - `findings` — 发现概要
  - `attentionItems` — 关注项
  - `preparedActions` — 已准备操作
  - `followUpStatus` — 跟进状态
- 分类筛选：Daily briefs / Catch-up / Manual scans
- 展开/折叠详情
- Rerun / Catch up / View actions 操作按钮

---

## 13. CSS 组件差异

当前 `style.css` 与 indexv4 `<style>` 共享相同的基础变量和全局样式，但 indexv4 新增约 **15 个组件样式**：

| 组件 | CSS 类名 | 用途 |
|------|---------|------|
| 走查卡片 | `.brief-item`, `.brief-body`, `.brief-topline`, `.speaker`, `.human-label` | 逐条 briefing 流 |
| 审批 memo | `.memo-section`, `.memo-stack`, `.review-card` | 审批工作流 |
| 学习卡片 | `.habit-card`, `.habit-result` | 用户偏好学习 |
| 扫描弹窗 | `.scan-modal`, `.scan-modal-card`, `.scan-modal-head/body/foot` | Run brief 配置 |
| 回复审核 | `.reply-review-panel`, `.reply-review-main`, `.reply-review-head` | 回复队列审核 |
| 发送成功 | `.send-success-card`, `.state-chip` | 发送确认 |
| 分类标签 | `.category-tab`, `.category-segment`, `.result-filter-row` | 结果筛选 |
| 扫描动画 | `.scanning-layout`, `.scanning-panel`, `.scan-flow`, `.scan-flow-step` | 首次扫描 |
| 首次使用 | `.first-run-layout`, `.scan-launch-btn`, `.scan-orb` | onboarding |
| 决策按钮 | `.decision-btn`, `.decision-row`, `.item-status` | briefing 交互 |
| 会话布局 | `.session`, `.session-header`, `.session-meta`, `.session-summary` | 对话式 UI |
| 邮箱管理 | `.source-tabs`, `.source-tab`, `.mailbox-expanded`, `.mailbox-detail-grid` | 多邮箱 |
| 迷你头像 | `.mini-avatar` | 对话流中的小 Anna 头像 |
| 结果分区 | `.result-section`, `.attention-queue` | 分类结果展示 |
| 提案卡片 | `.proposal-grid`, `.proposal-card`, `.proposal-top/title/tag` | 场景提案 |

---

## 14. 状态管理差异

当前 `state` 约 **25 个字段**，indexv4 `appState` 约 **55 个字段**，新增主要围绕：

| 类别 | 新增字段 |
|------|---------|
| 多步骤流程 | `stepIndex`, `itemDecisions`, `selectedScenario` |
| 回复工作流 | `replyQueueOpen`, `replyQueueStatuses`, `replyWorkflow`, `activeReplyId` |
| 审批状态 | `lowRiskApproved`, `learningChoice`, `ignoredProposals` |
| 分类筛选 | `resultFilter`, `updatesReviewed`, `noiseHidden` |
| 多邮箱 | `sources[]`, `selectedMailboxId`, `sourcesTab`, `expandedMailboxIds` |
| 扫描弹窗 | `runBriefOpen`, `runBriefMode`, `manualScan`, `scanProgressIndex` |
| 首次体验 | `firstScanStepIndex`, `firstScanReviewed`, `hasScanResults` |
| 详情视图 | `detailMode`, `detailItemId`, `successMode`, `successOverlayOpen` |
| 历史 | `historyFilter`, `expandedHistoryId` |
| 发送追踪 | `replyWorkflow.sentCount`, `replyWorkflow.sentIds`, `replyWorkflow.sentRecipients` |

---

## 实施优先级建议

| 优先级 | 模块 | 复杂度 | 依赖 |
|--------|------|--------|------|
| **P0** | 视图体系（7 views 框架 + render 分发） | 架构级 | 无 |
| **P0** | CSS 组件补齐（~15 个新组件样式） | 中 | 无 |
| **P1** | 分类筛选标签（3 分类 + All） | 中 | P0 |
| **P1** | 回复队列 + 批量回复 | 高 | P0 |
| **P2** | 场景化扫描 + 提案卡片 | 中 | P1 |
| **P2** | Action Memo 审批工作流 | 中 | P0 |
| **P2** | 多邮箱源管理（Profile/Strategy） | 高 | P1 |
| **P3** | 逐条走查 Briefing（step-by-step） | 中 | P2 |
| **P3** | 用户习惯学习 | 低 | P2 |
| **P3** | Run Brief 模态框 | 中 | P2 |
| **P3** | 历史记录增强 | 低 | P0 |
| **P3** | 首次使用体验（onboarding 动画） | 低 | P0 |

### 备注

- indexv4 是纯前端原型，所有数据均为 mock（`scenarioData`、`homeAttentionItems`、`scanningSteps` 等），实施时需对接后端 API
- 当前后端 RPC 接口（`start_mail_agent_run`、`get_active_cards` 等）可能需要扩展以支持多邮箱、场景化扫描、回复队列等新功能
- indexv4 缺少与 Anna Runtime SDK 的集成代码（`connectAnnaRuntime` 仅为 stub），实施时需保留当前 `connectRuntime()` + `invokeTool()` 逻辑
