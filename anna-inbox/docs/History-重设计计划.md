# History 改造实施计划

> 目标：从"运行日志"变为"决策记录"，支持回看 + 反悔
> 范围：纯前端 + 1 个微型后端 RPC

---

## 1. 后端（5 行）

`main.py` 新增 `restore_card` RPC：

```python
# RPC 注册（tool manifest 中加一项）
{"name": "restore_card", ...}

# 实现
async def restore_card(mailbox, card_id):
    await update_card_status(mailbox, card_id, "pending")
    return {"ok": True}
```

`update_card_status` 已有（`storage_ops.py:153`），只需包一层 RPC。

## 2. 前端 — 数据结构

不需要新 state 字段。`state.cards` 已包含所有状态的卡片，`visibleCards()` 过滤掉了非 pending。复用即可。

```js
function resolvedCards() {
  return state.cards.filter(c => c.status && c.status !== "pending");
}
```

## 3. 前端 — renderHistoryDrawer() 改版

### 3.1 布局

```
┌─ History ──────────────────────────────────────┐
│                                                 │
│  ┌─ Recently processed ────────────────────┐   │
│  │                                          │   │
│  │  Snoozed                                │   │
│  │  ┌─ Christopher · until tomorrow ────┐  │   │
│  │  │  Restore                          │  │   │
│  │  └───────────────────────────────────┘  │   │
│  │                                          │   │
│  │  Dismissed                               │   │
│  │  ┌─ LinkedIn cleanup ────────────────┐  │   │
│  │  │  Restore                          │  │   │
│  │  └───────────────────────────────────┘  │   │
│  │                                          │   │
│  │  Resolved                                │   │
│  │  ┌─ Jane assignment · handled ───────┐  │   │
│  │  │  Restore                          │  │   │
│  │  └───────────────────────────────────┘  │   │
│  └──────────────────────────────────────────┘   │
│                                                 │
│  ┌─ Past runs ─────────────────────────────┐   │
│  │  Today 09:20 · 4 items          [展开]  │   │
│  │  Yesterday 18:40 · 3 items      [展开]  │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
```

### 3.2 渲染逻辑

```
if resolvedCards().length > 0
  → 显示 "Recently processed" 区块
  → 按 status 分组（snoozed / dismissed / resolved）
  → 每条显示：卡片 title + 状态描述 + [Restore] 按钮

显示 "Past runs" 区块（现有 run 列表，轻量保留）
```

### 3.3 Restore 操作

```
点击 Restore → invokeTool("restore_card", { mailbox, card_id })
  → 重新 loadActiveCards()
  → toast "Card restored to active"
```

## 4. 前端 — CSS

新增样式（约 40 行）：

- `.resolved-group` — 状态分组容器
- `.resolved-group-title` — 分组标题（Snoozed / Dismissed / Resolved）
- `.resolved-card` — 卡片条目
- `.resolved-status` — 状态标签（不同状态不同颜色）
- `.restore-btn` — 恢复按钮（轻量，同 detail-back 风格）

## 5. 改动清单

| 文件 | 改动 |
|------|------|
| `main.py` | 新增 `restore_card` RPC（~5 行） |
| `app.js` | `resolvedCards()` 函数（3 行） |
| `app.js` | `renderHistoryDrawer()` 改版（~60 行） |
| `app.js` | 点击事件 `data-action="restore-card"`（~8 行） |
| `style.css` | 新增 history 卡片样式（~40 行） |

## 6. 估时

| 步骤 | 时间 |
|------|------|
| 后端 RPC | 3 min |
| `resolvedCards()` | 2 min |
| `renderHistoryDrawer()` 改版 | 25 min |
| Restore 事件 | 5 min |
| CSS | 15 min |
| **合计** | **~50 min** |
