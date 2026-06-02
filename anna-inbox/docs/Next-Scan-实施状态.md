# Next Scan 实现现状与 V3 差距分析

## 一、当前实现逻辑链路

```
┌─ 前端 ─────────────────────────────────────────────────────────┐
│                                                                  │
│  1. 页面加载                                                     │
│     init() → loadScanPlan()                                     │
│       → invokeTool("get_scan_plan", {mailbox})                  │
│       → state.scanPlan = {time_range, max_messages, ...}        │
│                                                                  │
│  2. 用户打开配置                                                 │
│     点击底部 [Next Scan] 按钮                                     │
│       → state.scanPlanOpen = true                               │
│       → renderScanPlanDrawer()                                  │
│       → 显示 chip 按钮组（schedule / time_range / max / include）│
│                                                                  │
│  3. 用户修改配置                                                 │
│     点击 chip（如 "Last 24 hours"）                               │
│       → data-set-scan-plan="time_range:last_24h"                │
│       → saveScanPlanField("time_range", "last_24h")             │
│         → invokeTool("set_scan_plan", {mailbox, time_range})    │
│         → state.scanPlan.time_range = "last_24h"                │
│         → render() 刷新 UI，chip 高亮变化                        │
│                                                                  │
└──────────────────┬─────────────────────────────────────────────┘
                   │ JSON-RPC (Anna Runtime / stdio)
                   ▼
┌─ 后端 ─────────────────────────────────────────────────────────┐
│                                                                  │
│  4. API 路由                                                     │
│     main.py handle_invoke()                                     │
│       → tool in ("get_scan_plan", "set_scan_plan")              │
│       → dispatch to _handle_v2_tool()                           │
│                                                                  │
│  5. 存储读写                                                     │
│     _handle_v2_tool()                                           │
│       → storage_ops.get_scan_plan(mailbox)                      │
│       → storage_ops.set_scan_plan(mailbox, plan)                │
│         → .local_storage/mailbox/{mailbox}/scan_plan.json       │
│                                                                  │
└──────────────────┬─────────────────────────────────────────────┘
                   │ 下次扫描触发时
                   ▼
┌─ Pipeline ─────────────────────────────────────────────────────┐
│                                                                  │
│  6. 扫描时读取配置                                               │
│     run_mail_task()                                             │
│       → _determine_scan_window() → 自适应窗口 7天                │
│       → _get_scan_plan_config(mailbox)                          │
│         → storage_ops.get_scan_plan()                           │
│       → _time_range_to_days("last_24h", 7) → 覆盖为 1天         │
│       → _apply_scan_window(plan, 1) → 查询变为 newer_than:1d    │
│       → plan.max_messages 覆盖 budget                           │
│                                                                  │
│  7. 执行扫描                                                     │
│     run_mail_scan() → Gmail API                                 │
│       查询: in:inbox newer_than:1d  (用配置的1天，不是自适应7天)  │
│       上限: 50 封 (用配置的50，不是默认100)                       │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

关键点：配置在步骤 3 保存，步骤 6 读取，步骤 7 生效。配置保存后不立即触发扫描，而是在下一次 `run_mail_agent`（定时或手动）时生效。

---

## 二、与 V3 设计的差距

### 2.1 信息架构

| | V3 设计 | 当前实现 |
|------|---------|---------|
| 入口位置 | Topbar `[Brief] [Ask]` 右侧 + Sources 抽屉内的 mailbox 卡片 | Bottom bar 一个 `[Next Scan]` 按钮 |
| 配置载体 | 两个独立组件：`#scanPlanDrawer`（日常查看）+ `#runBriefModal`（执行扫描） | 一个 `#scanPlanDrawer`（混合配置+查看） |
| 状态可见性 | Topbar 头像旁直接显示 `"Last scan 09:20 · Next scan in 3h"` | 仅 `brandSubtitle` 小字显示 plan 摘要 |

**差距**：V3 把 Next Scan 当作一等公民，和 Brief/Ask 同级。当前把它塞在 bottom bar 里，用户不容易发现。

### 2.2 配置维度

V3 的 `runBriefModal` 是一个完整的扫描配置向导，5 个 section：

| V3 section | 选项 | 当前实现 |
|-----------|------|---------|
| Mailboxes | 多邮箱勾选 | 不支持，只有单邮箱 |
| Scan range | `since-last` / `last-24h` / `last-7d` / `unread-backlog` / `custom`，每个带说明文字 | 有 5 个 chip，无说明文字，无 `custom` |
| Message limit | `50` / `100` / `200` / `Custom` | 有 3 个 chip（50/100/200），无 Custom |
| Priority | `Unread first` / `Inbox first` / `Active threads` / `Important contacts` / `Include newsletters` / `Include promotions` / `Include archived` | 只有 newsletters + promotions 两个 include toggle，优先级概念缺失 |
| Batch behavior | `Ask before continuing` / `Auto continue up to 300` / `Never scan older` | 完全没有 |

**差距**：V3 的配置是完整的扫描策略定义——选邮箱、选范围、选优先级、选批量策略。当前只有范围 + 数量 + 两个 include toggle，相当于 V3 的 1/3。

### 2.3 交互模式

| | V3 | 当前 |
|------|-----|------|
| 配置生效 | 在 modal 里配完点 `[Run brief]`，当场执行扫描 | 在 drawer 里点 chip，即时保存但不扫描 |
| 进度反馈 | Modal 切换到 progress 视图：`Preparing → Reading → Finding → Preparing → Brief ready` | 无，扫描进度只在 bottom bar 文字变化 |
| 扫描后 | 自动跳到 history 查看结果 | 无引导，用户需手动关 drawer 回主页 |
| Schedule 配置 | 在 Sources drawer 的 mailbox 展开卡片里 | 在 Next Scan drawer 里，但无 mailbox 上下文 |

**差距**：V3 是一个"配置→执行→查看"的完整闭环，当前是"配置→保存→自己触发扫描→自己回主页看结果"。

### 2.4 视觉呈现

| | V3 | 当前 |
|------|-----|------|
| 配置卡片 | `.panel-card` + `.scan-option`（大卡片，标题+说明，勾选态高亮） | `.preset-chip`（小圆角 chip，与 Brief/Ask 的 preset 混用） |
| 信息层级 | 每个 option 有 `strong` 标题 + `span` 说明文字 | 只有 label，无说明 |
| Schedule 显示 | Sources drawer 内 mailbox 展开卡片中有 `Scan strategy` 自然语言描述段落 | chip 高亮，无自然语言摘要 |

### 2.5 数据模型

V3 demo 的 `manualScan` 对象：

```js
manualScan = {
  mailboxes: ["kate@anna.partners"],
  range: "since-last",
  limit: "100",
  priorities: ["Unread first", "Inbox first", "Active threads"],
  schedule: "Every morning",
  batch: "Ask before continuing"
}
```

当前后端 `ScanPlan`：

```python
ScanPlan:
  mailbox: str          # 单邮箱
  schedule: str         # 已有 ✅
  time_range: str       # 已有，对应 V3 range ✅
  max_messages: int     # 已有，对应 V3 limit ✅
  priorities: list[str] # 已有字段，pipeline 未消费 ⚠️
  include_newsletters: bool  # 已有 ✅
  include_promotions: bool   # 已有 ✅
  include_archived: bool     # 已有字段，pipeline 未消费 ⚠️
  batch_behavior: str   # 已有字段，pipeline 未消费 ⚠️
```

后端字段基本齐全，`priorities`、`include_archived`、`batch_behavior` 有存储但 pipeline 还没消费。前端缺了 priority / batch / custom range 的 UI。

---

## 三、差距优先级

| 优先级 | 差距 | 涉及 |
|--------|------|------|
| P0 | Next Scan 入口不在 topbar，发现性差 | 前端：按钮位置 |
| P0 | 缺 `runBriefModal`（配置→执行→查看闭环） | 前端：新增 modal 渲染 |
| P1 | 配置 UI 缺 priority / batch 选项 | 前端：扩充 chip 组 |
| P1 | 缺自然语言说明文字 | 前端：每个 option 加 note |
| P1 | `priorities` / `include_archived` / `batch_behavior` pipeline 未消费 | 后端：pipeline 读取并生效 |
| P2 | 缺进度反馈（scan progress steps） | 前端：新增 progress 视图 |
| P2 | 缺多邮箱支持 | 前后端：ScanPlan + UI 支持多邮箱 |
