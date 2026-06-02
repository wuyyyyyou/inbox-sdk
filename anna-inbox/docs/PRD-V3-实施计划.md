# PRD-V3 + V3 前端改造实施计划

## 文档依据

- **PRD-V3**: `new/prd-v3-daily-brief-scan-scope.md`（扫描范围自适应策略）
- **V3 前端**: `new/bundle/indexv3.html`（视觉重设计 + Next Scan 配置 UI）
- **V2 设计**: `new/mail_agent_mvp_design.md`（当前架构）

---

## 一、改造前现状

### 1.1 扫描范围：写死的时间窗口

`strategies.py:67-78`，`default_secretary` 的 5 条查询全部硬编码：

| 查询 | 时间范围 | 上限 |
|------|---------|------|
| `in:inbox newer_than:3d` | 固定 3 天 | 100 |
| `in:inbox newer_than:14d (from:* OR to:*)` | 固定 14 天 | 100 |
| `in:inbox (is:important OR is:starred)` | 不限 | 50 |
| `in:inbox newer_than:30d (security OR...)` | 固定 30 天 | 50 |
| `in:draft newer_than:7d` | 固定 7 天 | 20 |

加上 `pipeline.py:131` 动态追加的 `category:primary -in:sent`（30 条）。

问题：
- 首次使用扫 3 天，可能漏掉历史积压
- 每天使用扫 3+14 天，大量重复
- 不区分用户状态（首次 / 日常 / 断档回归）

### 1.2 扫描计划：无用户配置

用户无法调整扫描参数。前端 `app.js` 硬编码 `primary_count: 30, max_messages: 100`。没有持久化的扫描偏好。

### 1.3 扫描状态：仅基础追踪

`ScanState`（`storage_types.py:30`）只有 4 个字段：
- `last_scan_ts` — 上次扫描时间
- `last_message_internal_date` — 上次最新消息时间
- `last_history_id` — Gmail historyId
- `total_scans` / `total_processed` — 计数

缺少：
- 用户状态判断（首次/日常/断档）
- 触顶检测和继续提示
- 扫描配置持久化

### 1.4 扫描历史：原始 JSON

Run 记录存为 `run/{run_id}.json`，仅含原始 `ActionPlan`。V3 前端期望的结构化摘要（扫描配置、发现、动作、跟进状态）不存在。

### 1.5 查询优先级：无排序

当前 6 条查询按追加顺序执行。如果 `category:primary` 先返回 30 条 newsletter，后续高优先级查询（安全账单）可能因 `max_messages=100` 被截断。PRD-V3 要求的"按优先级建立候选池"未实现。

---

## 二、改造后目标

### 2.1 自适应扫描范围

`pipeline.py` 新增 `_determine_scan_window()`，根据用户状态动态选择扫描范围：

| 用户状态 | 判断条件 | 扫描范围 | 上限 | 触顶 |
|---------|---------|---------|------|------|
| 首次使用 | `total_scans == 0` | 最近 7 天 | 100 | 提示继续 |
| 每天使用 | 距上次 ≤1 天 | 上次简报以来 + 6h 重叠 | 100 | 无 |
| 断档 2-7 天 | 距上次 2-7 天 | 上次简报以来 | 100 | 提示继续 |
| 断档 >7 天 | 距上次 >7 天 | 最近 7 天 + 旧未读重点 | 100 | 提示积压 |

实现方式：
- "上次简报以来" → 已有 `stop_at_internal_date` 机制（`_apply_incremental_window`）
- "6h 重叠回溯" → `stop_at_internal_date` 减去 6 小时的毫秒偏移
- "旧未读重点" → 追加 `is:unread` 查询

### 2.2 查询优先级排序

当前查询顺序 → 改为按优先级分组：

```
优先级 1: in:inbox is:unread newer_than:Nd     ← 最高
优先级 2: in:inbox (is:important OR is:starred)
优先级 3: 已知联系人查询（从 MailboxProfile 构建）
优先级 4: 安全/账单关键词 newer_than:30d
优先级 5: in:inbox 其余邮件 newer_than:Nd
优先级 6: newsletter/promotions（轻量扫描，降权）
```

按此顺序执行查询，高优先级邮件先进入候选池，低优先级的不挤占高优。

### 2.3 扫描计划持久化

新增 `ScanPlan` 数据结构，持久化存储用户偏好：

```python
@dataclass
class ScanPlan:
    mailbox: str
    schedule: str = "manual"         # manual | every_morning | every_afternoon | twice_daily | workdays
    time_range: str = "auto"         # auto(自适应) | since_last | last_24h | last_7d | unread_backlog
    max_messages: int = 100
    priorities: list[str]            # ["unread_first", "inbox_first", ...]
    include_newsletters: bool = False
    include_promotions: bool = False
    include_archived: bool = False
    batch_behavior: str = "ask"      # ask | auto_300 | never_older
    active: bool = True
    updated_at: str = ""
```

存储路径：`.local_storage/mailbox/{mailbox}/scan_plan.json`

提供 `get_scan_plan` / `set_scan_plan` API，前端可读写。

### 2.4 扫描计划生效

`run_mail_task()` 启动时：
1. 读取 `ScanPlan`
2. `time_range=auto` → 调用 `_determine_scan_window()` 自适应判断
3. `time_range=last_24h` → 查询改为 `newer_than:1d`
4. `max_messages` 覆盖 budget
5. `priorities` 调整查询顺序和 `label_weights`
6. `include_newsletters/include_promotions` 覆盖 `candidate_policy.exclude_signals`

策略层（`strategies.py`）的默认查询保留不变，作为未配置 `ScanPlan` 时的 fallback。

### 2.5 触顶提示

`ActionPlan` 增加 `ScanContinuation` 字段：

```python
@dataclass
class ScanContinuation:
    should_prompt: bool           # 是否提示继续
    prompt_text: str              # 提示文案
    remaining_estimate: int       # 预估剩余数量
    continuation_key: str         # 下次继续扫描的上下文
```

触顶条件：收集到的消息数 ≥ `max_messages`，且未扫的查询仍有结果。

前端 V3 已有对应 UI 状态（`scanStatus` 卡片 + toast + 按钮）。

### 2.6 扫描历史增强

`get_run_history` 返回结构调整为 V3 前端期望的格式：

```json
{
  "id": "today-briefing",
  "kind": "Daily briefs",
  "title": "Today's briefing",
  "time": "Today 09:20",
  "mailboxes": ["kate@anna.partners"],
  "range": "Since last brief + 6h overlap",
  "reviewed": "86 emails reviewed",
  "conclusion": "Christopher follow-up needs review",
  "found": "4 attention items · 2 actions prepared",
  "status": "3 pending · 1 handled",
  "details": {
    "scanRange": [...],
    "findings": "...",
    "attentionItems": [...],
    "preparedActions": [...],
    "followUpStatus": [...]
  }
}
```

其中 `details` 从原始 `ActionPlan` 字段映射：`attentionItems` ← `main_items`，`preparedActions` ← `proposed_actions`，`followUpStatus` ← cards 的 `status` 字段。

### 2.7 V3 前端对接

`app.js` 新增 4 个渲染函数，复用 V3 CSS 类名：

| 函数 | 对应 V3 组件 | 功能 |
|------|------------|------|
| `renderScanPlanDrawer()` | `#scanPlanDrawer` | 展示/编辑扫描计划 |
| `renderRunBriefModal()` | `#runBriefModal` | 配置并触发扫描 |
| `renderScanStatus()` | `#brandSubtitle` | "Last scan · Next scan" |
| `renderContinuationPrompt()` | `.scan-status-card` | 触顶提示 |

现有函数改造：
- `renderStart()` → 使用 V3 `attention-item-card` 结构映射
- `renderHistoryDrawer()` → 展开时展示结构化 `details`
- 调度逻辑 → 读取 `scan_plan.schedule`，`setInterval` 驱动定时扫描

**CSS 不在此阶段替换** — V3 的 `indexv3.html` CSS 视觉重设计独立进行，避免混入功能改动。

---

## 三、文件变更清单

| # | 文件 | 改动 | 阶段 |
|---|------|------|------|
| 1 | `pipeline.py` | 新增 `_determine_scan_window()`；`run_mail_task` 读取 `ScanPlan`；触顶检测 | A + C + D |
| 2 | `strategies.py` | `default_secretary` 查询按优先级排序 | A |
| 3 | `storage_types.py` | 新增 `ScanPlan`、`ScanContinuation` 数据类 | B + D |
| 4 | `types.py` | `ActionPlan` 增加 `scan_continuation` 字段 | D |
| 5 | `storage_ops.py` | 新增 `get_scan_plan` / `set_scan_plan` | B |
| 6 | `main.py` | 注册 `get_scan_plan` / `set_scan_plan` tool；`get_run_history` 返回结构增强 | B + E |
| 7 | `app.js` | 新增 scan plan 配置 UI；card/history 结构映射；调度逻辑 | E |

---

## 四、不做

- 后端 cron 调度 — 扫描由前端或 Anna 平台触发
- V3 CSS 替换 — 纯视觉改动，独立进行
- 多邮箱联合扫描 — 需要更大重构
- `creator_opportunity` / `security_billing` 改造 — PRD-V3 仅针对日报场景
- DRAFT 查询优先级调整 — 草稿数量极少，保持现有位置

---

## 五、验证计划

| 场景 | 操作 | 预期结果 |
|------|------|---------|
| 首次使用 | 清空 `scan_state`，运行扫描 | 扫描 7 天窗口，上限 100 |
| 每天使用 | 当天再次运行 | 仅扫描增量 + 6h 重叠 |
| 断档 5 天 | `last_scan_ts` 改为 5 天前 | 扫描 5 天窗口 + 触顶提示 |
| 断档 14 天 | `last_scan_ts` 改为 14 天前 | 扫描 7 天 + 未读重点 + 提示 |
| 自定义范围 | `set_scan_plan(time_range=last_24h)` | 仅扫描最近 24h |
| 修改优先级 | `set_scan_plan(priorities=[...])` | 下次扫描按新优先级排序 |
| 触顶继续 | 扫描触达上限 | ActionPlan 包含 `scan_continuation` |
| 扫描历史 | `get_run_history` | 返回结构化 details |
