# Ask 功能重构计划

## 1. 设计原则

**Brief 和 Ask 是同一层次，两种范式：**

```
Brief: workflow — 精心编排的多步管线，追求稳定性和可预测性
Ask:   agent    — 一次 LLM 调用，追求灵活性和开放性的回答

共同底层：Gmail 读写（mail_adapter）、LLM 调用（llm.py）、用户偏好（storage_ops）
```

Ask 不复用 Brief 的 pipeline（phase1 / candidate / judgment / guards / plan）。Agent 不需要分步拆解——把邮件和任务描述一次性交给 LLM，由 LLM 理解、分析、产出答案。

---

## 2. 架构概览

```
用户 NL 请求
     │
     ▼
┌─────────────────────────────┐
│  Planner (1次LLM调用)        │
│  输出:                       │
│    gmail_queries             │  → 搜什么
│    read_depth                │  → 读多深
│    task_prompt               │  → LLM 拿到邮件后做什么 + 输出什么格式
└─────────────────────────────┘
     │
     ▼
┌─────────────────────────────┐
│  run_custom_scan()           │
│                              │
│  1. gmail_queries → 搜索邮件  │  复用 mail_adapter
│  2. read_depth → 按需读取     │  复用 mail_adapter / context
│  3. 邮件数据 + task_prompt    │
│     → 一次 LLM 调用           │  复用 call_llm_json_safe
│  4. 返回 result dict          │
└─────────────────────────────┘
     │
     ▼
  前端展示 + plan 持久化（复用）
```

---

## 3. read_depth 的三个级别

Planner 根据用户意图选择，不硬编码：

| 值 | 读取内容 | 适用场景 |
|---|---|---|
| `header_only` | 发件人、主题、时间、标签、snippet | 退订检测——不需要正文 |
| `message_detail` | 以上 + 邮件正文 | 回复判断——需要看内容才能拟 draft |
| `thread_context` | 以上 + 整线程全部消息 | 对话总结——需要完整上下文 |

---

## 4. 输出格式

一个松散的容器结构，由 Planner 在 task_prompt 里告诉 LLM 如何填充：

```json
{
  "title": "问题的简短回答标题",
  "summary": "一两句话概括发现",
  "sections": [
    {
      "heading": "这一段的小标题",
      "body": "叙述性文字（可选）",
      "items": [
        {
          "subject": "条目标题（如邮件主题、发件人）",
          "context": "关于这个条目的上下文",
          "suggestion": "建议做什么（可选）",
          "draft": "草稿文本（可选，仅回复类）"
        }
      ]
    }
  ]
}
```

**不预定义 narrative / cards / analysis 等类型。** Planner 在 task_prompt 中告诉 LLM 用这个容器输出什么内容，前端统一渲染。

### 三用例验证

**Q1 "未读邮件哪些要回复"**

```json
{
  "title": "3 unread emails need replies, 7 safe to mark read",
  "sections": [
    {
      "heading": "Needs reply",
      "items": [
        {"subject": "Re: Anna AI Partnership", "context": "Christopher asked about timeline on May 9. You replied May 6 but he followed up again.", "suggestion": "Send a brief update on UI timeline", "draft": "Hi Christopher, thanks for your patience..."}
      ]
    },
    {
      "heading": "Safe to mark as read",
      "body": "7 newsletters and notifications with no action required."
    }
  ]
}
```

**Q2 "垃圾邮件哪些可退订"**

```json
{
  "title": "8 senders worth unsubscribing from",
  "sections": [
    {
      "heading": "High frequency, no interaction",
      "items": [
        {"subject": "Marketing Weekly", "context": "3x/week since January, never opened", "suggestion": "Unsubscribe"}
      ]
    },
    {
      "heading": "Keep for now",
      "body": "2 senders you occasionally interact with."
    }
  ]
}
```

**Q3 "总结 Christopher 对话"**

```json
{
  "title": "Christopher partnership discussion since March",
  "summary": "You reached out in March about Anna AI collaboration, signed a contract, and he's now waiting for platform UI updates before recording his video review.",
  "sections": [
    {
      "heading": "Timeline",
      "body": "Mar 16: First outreach from Kate\nMar 26: Contract signed and payment arranged\nApr 9-10: Kate asked Christopher to hold recording for new UI\nMay 5: Christopher checked in asking for update\nMay 6: Kate replied — UI upgrade still in progress\nMay 9: Christopher confirmed he's waiting to see upgrades, needs ~2 weeks lead time, away early June"
    },
    {
      "heading": "Current status",
      "body": "Christopher is waiting for Anna AI platform updates before recording. Kate's side is still in development. No missed deadline — Christopher said he'll be flexible as long as he has ~2 weeks notice."
    },
    {
      "heading": "Suggested next step",
      "items": [
        {"subject": "Send timeline update", "context": "Give Christopher a brief update before end of May so he can plan his video schedule. He'll be away the first week of June.", "suggestion": "Send a quick email with expected UI release window"}
      ]
    }
  ]
}
```

---

## 5. 前端展示

### Ask 视图

```
┌─ 输入 ───────────────────────────┐
│ [textarea] [Run custom scan]     │
└──────────────────────────────────┘

┌─ 本次结果 ───────────────────────┐
│ title                            │
│ summary                           │
│                                   │
│ ┌─ section ──────────────────────┐│
│ │ heading                        ││
│ │ body (可选文本)                  ││
│ │ item · item · item             ││
│ └────────────────────────────────┘│
└──────────────────────────────────┘

┌─ Past custom runs ───────────────┐
│ 时间 · 标题 · Re-run 按钮         │
│ 可展开 last_result_summary         │
└──────────────────────────────────┘
```

### 渲染规则

通用渲染，不区分 "卡片模式 / 摘要模式"：
- `body` → Markdown 文本段落
- `items` → 紧凑条目列表，`suggestion` 显示为弱操作提示，有 `draft` 的条目附 "Copy draft" 按钮

无 Handle、Snooze 等 Brief triage 概念——Ask 是问答，不是工作流。

---

## 6. 改动范围

### 修改文件

| 文件 | 改动 |
|------|------|
| **`types.py`** | `CustomScanPlan` 重定义：去掉 `candidate_kinds`、`llm_candidate_hints`、`evaluation_rubric`、`evaluation_buckets`、`output_mode`。新增 `read_depth`、`task_prompt` |
| **`planner.py`** | System prompt 重写：指导 LLM 根据用户意图选择 `read_depth`，写 `task_prompt`（包含输出格式指令），生成 `gmail_queries` |
| **`pipeline.py`** | `run_custom_scan` 重写为极简版（搜→读→一次 LLM→返回）。删掉 `_build_synthetic_strategy`，去掉 phase1/judgment/guard 等 Brief 管线环节的调用 |
| **`main.py`** | `run_custom_scan_background` 适配新 result 格式。`start_custom_scan` / `re_run_custom_scan` handler 无需改动 |
| **`app.js`** | `renderCustomRunResult` 改为通用容器渲染（sections / body / items） |

### 不改动的文件

`strategies.py`、`intent.py`、`phase1.py`、`candidate.py`、`context.py`、`judgment.py`、`guards.py`、`plan.py`、`card_service.py`、`scan.py`、`llm.py`、`mail_adapter.py`、`local_storage.py`、`storage_ops.py`、`storage_types.py`

---

## 7. Past custom runs（保留）

不变。Plan 持久化到 `custom/scan_plans.json`，前端展示历史列表，支持 Re-run（加载存储的 plan → 跳过 Planner → 直接执行）。Plan 结构变了，老数据不兼容，清掉重建。

---

## 8. 与旧版的关键差异

| | 旧版 Ask | 新版 Ask |
|---|---|---|
| 执行方式 | 复用 Brief 管线（phase1→judgment→cards） | 独立 agent（搜→读→一次 LLM） |
| Planner 输出 | 管线参数（candidate_kinds, rubric） | 任务指令（task_prompt） |
| 去重 | 强制线程去重 | 不自动去重（由 read_depth 和 Planner 决定） |
| 输出形态 | 只能出 attention cards | 任意结构（sections + body + items） |
| 前端 | 卡片列表 + Handle/Snooze 按钮 | 通用容器渲染 |
