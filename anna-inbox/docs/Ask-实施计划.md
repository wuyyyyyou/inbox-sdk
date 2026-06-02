# Ask 功能实施计划

## Context

Anna Inbox Agent 当前只有 Brief（每日简报）视图，使用三个预设策略（`default_secretary`、`creator_opportunity`、`security_billing`）做固定流程的邮件扫描。Ask 视图目前是一个占位符（策略下拉框 + 按钮），需要改造为：用户输入自然语言 → LLM 动态生成扫描计划 → 管道执行 → 计划持久化供复用。

Ask 与 Brief **完全解耦**：Brief 继续走 `strategies.py` 预设策略体系，Ask 走独立的 plan-and-execute 路径。

## 架构概要

```
用户 NL 请求
     │
     ▼
┌─────────────────────────────┐
│  Planner (1次LLM调用)        │  ← planner.py (新增)
│  输出 CustomScanPlan:        │
│  - gmail_queries             │
│  - candidate_kinds           │
│  - llm_candidate_hints       │
│  - read_depth_by_kind        │
│  - evaluation_rubric         │
│  - evaluation_buckets        │
│  - output_mode               │
└─────────────────────────────┘
     │
     ▼
┌─────────────────────────────┐
│  run_custom_scan()           │  ← pipeline.py (新增函数)
│  - 构建 synthetic MailStrategy│
│  - scan → phase1 → judgment  │
│  - 复用现有 pipeline 函数     │
└─────────────────────────────┘
     │
     ▼
  结果持久化 → cards 展示 / plan 存储
```

**复用**：加载存储的 plan → 跳过 LLM 规划 → 直接用 plan 中的 gmail_queries 重新执行扫描。

**关键决策 — Synthetic MailStrategy 桥接**: 为避免修改 `run_phase1_batch_classify`、`evaluate_items_batch` 等函数的签名（它们接受 `MailStrategy`），在 `run_custom_scan()` 内部从 `CustomScanPlan` 构建一个临时的 `MailStrategy`。这是纯数据映射，不含业务逻辑。Ask 路径不依赖 `strategies.py` 的注册表。

## 实施步骤

### 步骤 1：新增 CustomScanPlan 类型（types.py）

在 `types.py` 末尾添加：

```python
@dataclass
class CustomScanPlan:
    plan_id: str                        # "cplan_" + 12 hex
    user_request: str                   # 原始 NL 输入
    title: str                          # 人类可读的 plan 标题
    description: str                    # 一句话描述

    # Scan
    gmail_queries: list[dict[str, Any]]  # 直接作为 scan_plan["queries"]
    scan_budget: dict[str, int]          # {max_messages, max_threads, ...}

    # Phase 1
    candidate_kinds: list[str]
    llm_candidate_hints: str
    read_depth_by_candidate_kind: dict[str, str]

    # Judgment
    evaluation_rubric: str
    evaluation_buckets: list[str]

    # Output
    output_mode: str = "cards"           # "cards" | "summary"(v2)

    # Metadata
    created_at: str = ""
    last_used_at: str = ""
    use_count: int = 0
    last_result_summary: str = ""        # 执行后回写
```

### 步骤 2：新增 planner.py

**新文件** `src/mail_agent/planner.py`，核心函数：

```python
async def generate_custom_plan(
    user_request: str,
    sampling_create_message: Any = None,
) -> CustomScanPlan:
```

**Prompt 设计要点**：
- System prompt 包含 Gmail 搜索语法参考、CandidateKind 列表、ReadDepth 说明
- 指导 LLM 如何根据用户意图写 evaluation_rubric
- 3 个 few-shot 示例覆盖 PM 的三个测试用例
- 输出格式严格匹配 CustomScanPlan JSON schema
- 使用 `call_llm_json_safe` 调用 LLM
- Fallback：LLM 失败时返回一个安全默认 plan（扫描最近 2 天 Primary 收件箱）

### 步骤 3：新增 run_custom_scan()（pipeline.py）

在 `pipeline.py` 中新增，不修改 `run_mail_task()`。

```python
async def run_custom_scan(
    plan: CustomScanPlan,
    mailbox: str,
    *,
    sampling_create_message: Any,
    primary_count: int = 20,
    progress_callback: ProgressCallback | None = None,
) -> ActionPlan:
```

**内部流程**：
1. `_build_synthetic_strategy(plan)` → `MailStrategy`，将 plan 字段映射到 5 个子策略
2. 构建 scan_plan（queries 直接来自 plan.gmail_queries）
3. 复用 `_apply_incremental_window` 实现增量扫描
4. 复用 `run_mail_scan()` 执行 Gmail 搜索
5. 复用 `filter_unprocessed()` 过滤已处理邮件
6. 提取 `_dedupe_by_thread()` 公共函数（从 `run_mail_task` 中提取，避免代码重复）
7. 复用 `run_phase1_batch_classify()` — synthetic strategy 提供 candidate_kinds
8. 复用 `read_candidate_context()`
9. 复用 `evaluate_items_batch()` — synthetic strategy 提供 name/description；`task_plan.raw_user_request` 携带用户意图
10. 复用 `apply_rule_guards()` — synthetic strategy 提供 action_policy
11. 复用 `generate_action_plan()`，然后覆盖 title 为 plan.title
12. 复用 `_persist_run_results()`，传入 `plan_id=plan.plan_id`

**_build_synthetic_strategy 映射表**：

| plan 字段 | → MailStrategy 子策略字段 |
|-----------|--------------------------|
| title, description | name, description |
| candidate_kinds | candidate_policy.candidate_kinds |
| llm_candidate_hints | candidate_policy.llm_candidate_hints |
| read_depth_by_candidate_kind | context_policy.read_depth_by_candidate_kind |
| evaluation_rubric | judgment_policy.rubric |
| evaluation_buckets | judgment_policy.output_buckets |
| scan_budget | scan_policy.budget |
| —（硬编码） | action_policy: 禁止 send/delete/unsubscribe |

**提取 `_dedupe_by_thread()`**：`run_mail_task` L140-157 的线程去重逻辑提取为独立函数，两个 pipeline 路径共享。

### 步骤 4：存储层（storage_ops.py + storage_types.py）

**storage_types.py**：`RunRecord` 和 `RunHistoryEntry` 各增加 `plan_id: str = ""`（向后兼容）。

**storage_ops.py**：新增 4 个函数，使用 Pattern C（单 key 包装列表）：

```
Key: custom/scan_plans → {"plans": [...]}
```

```python
async def save_custom_plan(plan: CustomScanPlan) -> None:
    """保存或更新一个 plan。如果 plan_id 已存在则覆盖。最多保留 20 条。"""

async def get_custom_plan(plan_id: str) -> CustomScanPlan | None:
    """按 ID 加载单个 plan。"""

async def list_custom_plans() -> list[dict]:
    """返回所有 plan 的轻量元数据列表（供前端展示）。"""

async def update_plan_result(plan_id: str, summary: str) -> None:
    """执行完成后更新 last_used_at、use_count、last_result_summary。"""
```

### 步骤 5：JSON-RPC 工具方法（main.py）

新增 4 个 tool manifest 条目和对应的 handler：

| Tool | 说明 | 类型 |
|------|------|------|
| `start_custom_scan` | 启动定制扫描：规划 + 后台执行 | async（后台） |
| `re_run_custom_scan` | 用已存储的 plan 重新执行 | async（后台） |
| `get_custom_plans` | 列出所有已保存的 plan 元数据 | sync |
| `get_custom_plan_detail` | 获取单个 plan 的完整详情 | sync |

**start_custom_scan 流程**：
1. 创建 run entry（stage: "planning"）
2. `asyncio.run_coroutine_threadsafe(run_custom_scan_background(...), loop)`
3. 后台：planner 生成 plan → 保存 plan → 执行 pipeline → 更新 run status
4. 前端用 `get_mail_agent_run` 轮询状态（复用现有轮询机制）

**re_run_custom_scan 流程**：
1. `get_custom_plan(plan_id)` 加载 plan
2. 跳过 planner，直接创建 run 并后台执行 `run_custom_scan(plan, ...)`
3. 执行完成后 `update_plan_result()`

**进度阶段扩展**：`scanStageLabel` 新增 `"planning"` 和 `"planning_done"` 标签。

### 步骤 6：前端（app.js）

**状态新增**：
```javascript
customPlans: [],        // get_custom_plans 返回
customScanInput: "",    // NL 输入框内容
isCustomScanning: false,
```

**renderAsk() 重写**（替换 L293-316）：
- 移除策略下拉框
- 新增 NL textarea + "Run custom scan" 按钮
- 新增 "Past custom scans" 区域，列出历史 plan
- 每个 plan 条目：标题、原始请求摘要、使用次数、最后使用时间、Re-run 按钮

**新函数**：
- `startCustomScan()` — 读取输入 → 调用 `start_custom_scan` → 轮询 → 刷新 cards
- `reRunCustomPlan(planId)` — 调用 `re_run_custom_scan` → 轮询 → 刷新
- `loadCustomPlans()` — 调用 `get_custom_plans` → 更新 state

**init() 中**：添加 `await loadCustomPlans()`。

## 不改动的文件

`strategies.py`、`intent.py`、`scan.py`、`phase1.py`、`candidate.py`、`context.py`、`judgment.py`、`guards.py`、`plan.py`、`card_service.py`、`llm.py`、`mail_adapter.py`、`local_storage.py`、`storage_client.py` — **零改动**。

## 关键风险

1. **Batch judgment 不包含自定义 rubric**：`build_batch_judgment_prompt` 不使用 `judgment_policy.rubric`，只用了 `strategy.name/description`。MVP 依赖 `task_plan.raw_user_request`（已在 prompt 中）来传递用户意图。v2 可新增 `build_custom_batch_judgment_prompt` 注入完整 rubric。

2. **output_mode="summary"** 推迟到 v2。MVP 所有 plan 使用 `output_mode="cards"`。Q3 类请求（"总结对话"）通过 evaluation_rubric 引导 LLM 生成单张综合 card 来实现。

3. **Planner LLM 质量问题**：可能生成无效的 Gmail 查询或不适用的 rubric。通过 prompt 中的 Gmail 语法参考 + few-shot 示例 + fallback 机制来缓解。

## 验证方案

1. **Planner 独立测试**：用三个 PM 用例调用 `generate_custom_plan()`，检查输出的 JSON 结构和字段合理性
2. **Pipeline 集成测试**：用人工构造的 `CustomScanPlan` 调用 `run_custom_scan()`，验证完整管道输出
3. **存储 CRUD 测试**：调用 save/get/list/update，验证数据持久化和读取
4. **端到端测试**：启动 dev server，在浏览器中：
   - 输入 NL 请求 → 点击 Run → 看到扫描进度 → 出现 cards
   - 切换到 Ask 视图 → 看到过去的定制运行列表 → 点击 Re-run → 出现新结果
5. **回归测试**：确认 Brief 视图的每日简报功能完全不受影响
