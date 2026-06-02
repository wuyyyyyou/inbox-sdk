# Handle 详情页改造计划

> 目标：对齐 indexv4 的详情页设计
> 后端：无需改动

---

## 1. 差异对比

### 1.1 整体结构

| 区块 | 当前实现 | indexv4 |
|------|---------|---------|
| 顶部 | From/To/CC/Thread/Latest 五行元数据网格 | 卡片标题 + 发送者·主题 + 状态标签 |
| Anna 分析 | **无** | `review-block is-summary`：bullet 概述 + Open loop + Why this matters |
| 邮件原文 | 独立 section，展开显示 raw body | **不单独显示**，合并到折叠的线程上下文中 |
| 草稿区 | textarea + 文本输入 + Revise 按钮 | textarea + **预设芯片**（Shorter/Warmer/More direct）+ 文本输入 + Ask Anna to revise |
| 线程上下文 | 散落在顶部网格中 | `review-block is-quiet`：**可折叠**，默认收起，含 From/To/CC + 邮件原文 |
| 底部按钮 | Reply now / No action / Handled + Next → | Mark ready / Send this reply（两步）+ Next reply |
| 视觉层次 | 统一 `.drawer-section` 样式 | 三种 `.review-block`：summary（渐变紫）/ composer（白底阴影）/ context（淡灰） |

### 1.2 关键差距

| # | 差距 | 影响 |
|---|------|------|
| 1 | 缺少 Anna 分析区块 | 用户看不到 Anna 的判断依据，只能自己读原文 |
| 2 | 草稿编辑无预设修改词 | 用户必须手动输入修改指令，不如点一下芯片快 |
| 3 | 线程上下文占顶部大量空间 | 用户每次都要滑过 From/To/CC 才能看到草稿区 |
| 4 | 视觉无分层 | 分析、编辑、上下文混在一起，无优先级引导 |

## 2. 实施计划

### 2.1 现状：已有可用数据

`summarize_thread` RPC 返回结构化 JSON：

```json
{
  "core_ask": "he is asking for a build update",
  "current_progress": "Christopher replied yesterday, waiting on Kate",
  "open_questions": "whether the upgraded UI is ready",
  "user_action_needed": "send a short update confirming build timing",
  "tone": "warm"
}
```

但当前 `summarizeSelectedThread()` 把这些字段 **flatten 成了字符串数组**，丢了结构。只需改前端解析即可。

### 2.2 新结构

```
┌─ ← Back to brief ──────────────────────────────────────────┐
│                                                              │
│  Christopher is waiting on the new build                     │
│  Christopher Lee · Video collaboration follow-up              │
│                                                              │
│  ┌─ Anna noticed ────────────────────────────────────────┐  │
│  │  · He is asking for a build update                    │  │
│  │  · Christopher replied yesterday, waiting on Kate      │  │
│  │                                                       │  │
│  │  Open loop: send a short update confirming timing     │  │
│  │  Why this matters: keeps a warm creator relationship  │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌─ Draft reply ────────────────────────────────────────┐  │
│  │  [textarea]                                           │  │
│  │  [Shorter] [Warmer] [More direct]                     │  │
│  │  [___________] [Ask Anna to revise]                   │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌─ Thread context ▾ ───────────────────────────────────┐  │
│  │  From / To / CC / Thread / Latest                     │  │
│  │  ──                                                   │  │
│  │  Latest email body...                                 │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                              │
│  [Reply now]  [No action]  [Handled manually]    [Next →]  │
└──────────────────────────────────────────────────────────────┘
```

### 2.3 改动清单

#### JS — `summarizeSelectedThread()`

```js
// 改前：flatten 为字符串数组
const lines = [summary.core_ask, summary.current_progress, ...].filter(Boolean);

// 改后：存原始对象
state.threadSummaryById[cardId] = summary;
```

#### JS — `renderHandleView()` 重写

| 序号 | 区块 | 数据来源 | CSS 类 |
|------|------|---------|--------|
| 1 | 标题行 | `card.title` + `context.from` + `context.subject` | `.reply-review-head` |
| 2 | Anna noticed | `state.threadSummaryById[card.id]` | `.review-block.is-summary` |
| 3 | Draft reply | `state.draftById[card.id]` | `.review-block.is-composer` |
| 4 | Thread context | `context` + `original` | `.review-block.is-quiet` |
| 5 | 操作栏 | 不变 | `.decision-row.drawer-action-row` |

#### JS — 预设修改芯片

三个芯片 `[Shorter]` `[Warmer]` `[More direct]`，`data-revise-preset` 属性，点击后：
1. 从 textarea 取当前草稿
2. 调 `invokeTool("revise_draft", {..., revision_input: "Make it shorter"})`
3. 更新 `state.draftById`

复用现有 `reviseDraft()` 逻辑，只改 `revision_input` 来源。

#### JS — 线程上下文折叠

- `state` 加 `threadContextExpanded: {}`
- 点击 `data-toggle-context` → 切换 `state.threadContextExpanded[card.id]`
- 默认收起（不展开）

#### JS — 新 state 字段

```js
threadContextExpanded: {},  // cardId → bool
```

#### CSS — 新增样式

| 类名 | 用途 | 参考 indexv4 行 |
|------|------|----------------|
| `.reply-review-head` | 标题行 flex 布局 | 3985 |
| `.reply-review-title` | 卡片标题 | 3992 |
| `.review-block` | 通用区块容器 | 4020 |
| `.review-block.is-summary` | Anna 分析（渐变紫底） | 4029 |
| `.review-block.is-composer` | 草稿区（白底阴影） | 4036 |
| `.review-block.is-quiet` | 上下文区（淡灰） | 4042 |
| `.review-block-title` | 区块标题 | 4048 |
| `.review-block-kicker` | "Anna noticed" 标签 | 4055 |
| `.review-block-kicker::before` | 小 "A" 头像 | 4065 |
| `.thread-context-toggle` | 折叠按钮 | - |

### 2.4 不变的部分

- 后端所有 RPC（`summarize_thread` / `generate_draft_reply` / `revise_draft` / `reply_now` / `record_card_decision`）
- `detail-shell` / `detail-back` / `detail-card` 容器
- 底部操作按钮（Reply now / No action / Handled / Next →）
- `openCard()` / `closeDrawers()` / `nextCardId()` 导航逻辑

### 2.5 估时

| 步骤 | 时间 |
|------|------|
| `summarizeSelectedThread()` 存储格式 | 5 min |
| `renderHandleView()` 重写 | 40 min |
| 预设芯片点击逻辑 | 10 min |
| 线程上下文折叠 | 10 min |
| CSS 样式 | 25 min |
| **合计** | **~90 min** |
