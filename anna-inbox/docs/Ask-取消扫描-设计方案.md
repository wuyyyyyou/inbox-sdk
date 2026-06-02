# Ask 取消扫描 — 设计方案

## 核心思路

在 `MAIL_AGENT_RUNS` 字典上加取消标记，后台协程在关键节点检查标记后提前退出。**不是硬杀进程**，而是**优雅提前退出**。

## 改动范围

| 文件 | 改动 |
|------|------|
| `main.py` | 新增 `cancel_mail_agent_run` tool；`_start_custom_scan_async` 和 `run_custom_scan_background` 在步骤间检查取消标记 |
| `pipeline.py` | `run_custom_scan()` 接收 `is_cancelled` 回调，在搜索后、读取后、LLM 调用前检查 |
| `app.js` | 轮询中检测 `cancelled` 状态；进度卡片加 "Cancel" 按钮 |

## 后端流程

```
cancel_mail_agent_run(run_id)
  → MAIL_AGENT_RUNS[run_id]["cancelled"] = True

_start_custom_scan_async:
  Plan LLM → 检查 cancelled? → return
  Save plan → 检查 cancelled? → return
  Execute → run_custom_scan_background

run_custom_scan_background:
  Search Gmail → 检查 cancelled? → return
  Read emails  → 检查 cancelled? → return
  LLM call     → 调用前检查 cancelled? → skip

前端轮询:
  get_mail_agent_run → status="cancelled" → 停止轮询
```

## 注意事项

- **LLM 调用进行中无法中断**：`sampling.create_message()` 一旦发出就无法取消。但可以在调用前检查，跳过 LLM 调用。最坏情况等当前 LLM 调用完成后才响应取消。
- **Gmail 搜索同理**：使用 ThreadPoolExecutor，无法中途中断线程。但可以在搜索完成后跳过后续步骤。
- **最终一致性**：取消后状态持久化，刷新页面不会恢复。
- **Brief 扫描同理可复用**：取消逻辑和 Ask 完全一致，后续可以给 Brief 也加上。

## 前端交互

```
Ask 页面，custom scan 进行中:
  ┌─────────────────────────────────────┐
  │  Custom scan running                │
  │  Planning your scan                 │
  │  [======○──────] Plan · Search · ... │
  │                                     │
  │  [Cancel scan]                      │  ← 新增
  └─────────────────────────────────────┘
```

点击后按钮变为 "Cancelling…"，轮询检测到 `cancelled` 后显示 "Scan cancelled."

## 轮询逻辑变更

```javascript
// 之前：只在 status === "done" 或 "failed" 时停止
if (status.status === "done") break;
if (status.status === "failed") throw new Error(...);

// 之后：增加 cancelled 状态
if (status.status === "cancelled") {
  state.customRunProgress = null;
  showToast("Scan cancelled.");
  break;
}
```
