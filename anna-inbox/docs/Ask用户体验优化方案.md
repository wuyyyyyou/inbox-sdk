# Ask 用户体验优化方案

## 问题

Ask 完成一次定制化扫描后，切到 Brief 再切回 Ask，结果丢失，无法回溯本次会话中多次扫描的内容。

## 设计原则

- **不是多轮对话**：每次 Ask 独立，不跨请求传递上下文
- **会话级别存储**：从打开 App 到关闭/刷新，切换 tab 不丢失
- **不持久化**：关闭 App 即清空，不留存过期快照

## 数据结构

```js
state.askHistory = [
  { query: "...", result: {...}, timestamp: "2026-06-03 14:30" },
  // ...
]
// state.askResult → 删除，统一用 askHistory
```

每条目存完整内容：`query`（用户输入）、`result`（LLM 返回的完整 JSON，含计划、卡片数、策略、send reply 按钮数据等）、`timestamp`。

最新一条即为当前结果，全部展开显示；历史条目折叠，点击展开。

## 页面布局

```
┌─ Ask ──────────────────────────────────────────┐
│                                                  │
│  ┌──────────────────────────────────┐ [发送]     │
│  │ 输入框（自定义扫描需求）           │            │
│  └──────────────────────────────────┘            │
│                                                  │
│  ── History ──────────────────────────────────── │
│  ▸ May 28, 14:30   合作提案扫描 · 12封             │  ← 折叠
│  ▸ May 28, 10:15   安全审查 · 8封                  │  ← 折叠
│                                                  │
│  ── Latest Result ────────────────────────────── │
│  账单风险扫描 · 2026-06-03 16:00                   │
│                                                   │
│  发现 3 张卡片，2 条 lower priority                 │
│  ...（当前结果完整展开，含 send reply 按钮等）        │
│                                                   │
│  [切换到 Brief 查看]                               │
│                                                  │
└──────────────────────────────────────────────────┘
```

## 交互行为

| 操作 | 行为 |
|------|------|
| 完成一次 Ask | 旧 result 推入 history 头部，新 result 设为最新一条（展开） |
| 切到 Brief | 状态保留 |
| 切回 Ask | 恢复最后一次的视图（history + 最新结果展开） |
| 点击折叠的历史条目 | 展开查看完整详情（含 send reply 等按钮） |
| 点击"切换到 Brief" | 切到 Brief tab，不丢失 Ask 状态 |
| 刷新/关闭 App | askHistory 清空 |

## 生命周期

```
打开 App → askHistory = []
  → Ask #1 完成 → history = [{问1, 答1}]
  → 切 Brief → 切回 Ask → 状态保留
  → Ask #2 完成 → history = [{问2, 答2}, {问1, 答1}]
  → 刷新 → askHistory = []   ← 会话级别，重新开始
```

## 改动范围

仅前端 `app.js`，不改扫描链路：

| 改动 | 说明 |
|------|------|
| `state.askHistory` | 替代 `state.askResult` |
| `renderAsk()` | 渲染历史列表 + 最新结果 |
| Ask 发送完成回调 | 推入 history |
| Tab 切换逻辑 | 不清空 askHistory |
| 历史条目折叠/展开 | 点击切换 |

### state 变更

```js
// 删除
state.askResult = null;
state.planSummary = "";
state.scannedCount = 0;

// 新增
state.askHistory = [];          // [{ query, result, timestamp }]
state.askHistoryExpanded = {};  // { [index]: true/false }
```

### renderAsk 结构

```js
function renderAsk() {
  // 输入框 + 发送按钮（不变）

  // 历史列表
  if (state.askHistory.length > 1) {
    // 渲染折叠的历史条目（第 2 条到最后）
    // 点击条目 → 切换展开/折叠
  }

  // 最新结果（第 1 条，始终展开）
  if (state.askHistory.length > 0) {
    // 渲染最新结果，含完整 LLM 回复、卡片列表、send reply 按钮等
  }
}
```

## send reply 按钮兼容性

历史条目中的 send reply 按钮依赖 `card_id` 和 `mailbox`，这些数据在 `result` 中完整保存。重新渲染历史条目时：
- 按钮可正常点击
- 若对应卡片仍存于 Brief → send 正常工作
- 若卡片已被处理 → send 报错提示（不会 UI 崩溃）
