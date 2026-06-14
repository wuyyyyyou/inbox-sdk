# Anna Inbox — Gmail 邮件助手

Anna Inbox 是一个 Anna App 平台的邮件代理应用，通过 AI（DashScope / Anna LLM Sampling）自动扫描 Gmail 收件箱，生成可操作的**注意力卡片**，帮助用户管理邮件注意力。

## 目录

- [项目结构](#项目结构)
- [Brief 全链路设计](#brief-全链路设计)
  - [管线流程](#管线流程)
  - [短 Invoke 状态机](#短-invoke-状态机)
  - [Phase 1：批量分类](#phase-1批量分类)
  - [Phase 2：二叉决策](#phase-2二叉决策)
  - [线程去重](#线程去重)
  - [三层防线](#三层防线)
  - [卡片构建](#卡片构建)
  - [持久化](#持久化-1)
- [联系人记忆系统](#联系人记忆系统)
  - [隔离边界](#隔离边界)
  - [记忆结构](#记忆结构)
  - [写入来源](#写入来源)
  - [召回](#召回)
- [Ask 链路设计](#ask-链路设计)
  - [与 Brief 的本质区别](#与-brief-的本质区别)
  - [链路流程](#链路流程)
  - [Planner 的语义展开](#planner-的语义展开)
  - [输出格式](#输出格式)
- [Draft 生成设计](#draft-生成设计)
  - [两阶段草稿生成](#两阶段草稿生成)
  - [草稿修改](#草稿修改)
  - [持久化](#持久化-2)
- [Snooze 详细设计](#snooze-详细设计)
  - [Tomorrow](#tomorrow)
  - [Next week](#next-week)
  - [Don't prioritize threads like this](#dont-prioritize-threads-like-this)
- [多邮箱接入](#多邮箱接入)
  - [邮箱发现与注册](#邮箱发现与注册)
  - [勾选模型](#勾选模型)
  - [Brief 多邮箱扫描](#brief-多邮箱扫描)
  - [卡片聚合与展示](#卡片聚合与展示)
  - [卡片操作安全](#卡片操作安全)
  - [Ask 多邮箱（⚠️ 待完善）](#ask-多邮箱️-待完善)
  - [联系人记忆多邮箱隔离](#联系人记忆多邮箱隔离)
  - [Run History 多邮箱](#run-history-多邮箱)
  - [用户偏好多邮箱](#用户偏好多邮箱)
- [未完成内容](#未完成内容)
  - [1. 已持久化卡片在外部处理后 App 内无相应变化](#1-已持久化卡片在外部处理后-app-内无相应变化)
  - [2. Outlook 邮箱接入](#2-outlook-邮箱接入)
  - [3. Ask 链路 Embedding 语义筛选](#3-ask-链路-embedding-语义筛选)
  - [4. 其他建议](#4-其他建议)
- [开发指南](#开发指南)
  - [安装](#安装)
  - [Smoke Test](#smoke-test)
  - [运行测试](#运行测试)
  - [前端构建](#前端构建)
  - [代码风格](#代码风格)
  - [关键约束](#关键约束)

---

## 项目结构

```
anna-inbox/          # 前端 — React + TypeScript + Vite
  src/
    api/             # Executa 工具调用封装（mailAgentClient.ts）
    app/             # 应用入口、状态管理、控制器
    features/        # 视图组件（brief / ask / handle / drawers）
    runtime/         # Anna Runtime SDK 适配层
    types/           # TypeScript 类型定义

inbox-tool/          # 后端 — Python Executa（JSON-RPC over stdio）
  src/
    anna_inbox_executa/   # RPC 入口、工具分发
    mail_agent/
      core/               # 管线编排、扫描、Phase1、安全守护
      cards/              # 卡片构建、合并、前端格式化
      judgment_engine/    # Phase 2 LLM 判断
      llm_runtime/        # LLM 调用（DashScope + Anna Sampling）
      storage/            # 持久化（APS KV / 本地 JSON）
      mail_providers/     # Gmail 适配器（OAuth、缓存）
      planning/           # Ask 查询规划、意图解析
      contact_memory/     # 联系人记忆（索引、检索、管理）
      ask/                # Ask 链路（Planner / Search / Answer）
```

---

## Brief 全链路设计

Brief 是**可预测的工作流管线**，目标是从收件箱中产出用户当天需要关注的注意力卡片。

### 管线流程

```
用户点击 Brief 扫描
    │
    ▼
start_mail_agent_run          ← 创建 run_id，初始化状态，不调用 LLM
    │
    ▼
┌─ 前端循环 continue_mail_agent_run ──────────────────────────────┐
│                                                                   │
│  1. Scanning      — Gmail API 分页拉取线程（每页 50 条）          │
│  2. Filtering     — 过滤已处理消息、线程去重、已回复检查           │
│  3. Phase 1       — 批量 LLM 分类（头信息 → reply / review / ignore）│
│  4. Phase 2       — 逐候选深度判断（二叉决策 → 生成卡片内容）       │
│  5. Finalizing    — 持久化卡片、更新扫描状态、写运行历史           │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
    │
    ▼
前端刷新 get_active_cards → 展示注意力卡片
    │
    ▼
fire-and-forget: generate_contact_memories  ← 不阻塞主流程
```

### 短 Invoke 状态机

每次 `continue_mail_agent_run` 只推进一小段工作，控制在 45-55 秒内返回。关键约束：

- 单次 invoke 最多 6 次 sampling 调用（平台 maxCalls=8，保留余量）
- Phase 1 每批 20 封邮件，每 invoke 并行 4 批
- Phase 2 每批评估 3-5 个候选，完成即持久化卡片
- 前端看到 `cards_added > 0` 立即刷新展示

### Phase 1：批量分类

对所有邮件头做轻量 LLM 分类，输出三路分流：

| user_action | 语义 | 去向 |
|-------------|------|------|
| `reply` | 有人在等回应 | CandidateItem → Phase 2 |
| `review` | 不需回复但值得关注 | CandidateItem → Phase 2 |
| `ignore` | 纯通知/newsletter/自动提醒 | 聚合为 cleanup bundle 卡片 |

邮件头使用单字母 key 压缩（`i`=index, `f`=from, `s`=subject, `sn`=snippet, `d`=date, `l`=labels 等），降低 token 消耗。

### Phase 2：二叉决策

Phase 2 对每个候选邮件做深度判断，核心设计是**二叉决策**：

1. 先判断「是否需要回复」→ 确定 `user_action`（reply / review）
2. 再选择具体原因 `action_reason`（9 个互斥原因）

**需要回复（reply）：**
- `question_asked` — 对方明确问了问题
- `waiting_for_you` — 对方在等你的输入/审批
- `unsent_draft` — 用户写了草稿但没发送
- `courtesy_due` — 对方投入实质性努力，礼貌性回复

**不需要回复（review）：**
- `upcoming_event` — 面试/会议/截止日期提醒
- `deal_or_pipeline` — 项目/合作/交易状态更新
- `security_or_billing` — 安全告警、账单异常
- `receipt_or_notice` — 收据、订阅确认
- `cleanup` — 新闻通讯、促销

代码层硬约束（`_enforce_consistency`）优先级高于 LLM 输出——reply reasons 强制 `user_action="reply"` + `priority ≥ medium`; `security_or_billing` 强制 `priority ≥ high`。

### 线程去重

按 Gmail thread 去重，优先级：最新 INBOX > 最新 DRAFT > 丢弃纯 SENT。用户自己发出的邮件不会生成"回复自己"的卡片。

### 三层防线

```
L1 查询层：策略查询区分方向（in:inbox / -in:sent -in:draft）
L2 去重层：方向感知的线程去重（INBOX > DRAFT > 丢弃 SENT）
L3 LLM 层：prompt 中告知用户邮箱身份，按邮箱精确匹配
```

### 卡片构建

每张 Attention Card 包含三行内容：
1. **Title**：一句话说明事项是什么（≤12 words）
2. **Context**：压缩说明发生了什么（≤30 words，纯事实）
3. **Suggestion**：建议的下一步动作（≤15 words，建议语气）

另外 Cleanup Bundle 将低价值邮件聚合成一张可折叠卡片，零 LLM 成本。

### 持久化

- 卡片分片存储（50 张/片），支持分页读取
- 已处理消息标记防重复扫描
- 增量扫描：每邮箱独立维护 `processed_message_ids` 和 `scan_state`
- 支持 APS KV 和本地 JSON 两种后端

---

## 联系人记忆系统

联系人记忆是在当前 Gmail thread 之外的**长期上下文**，用于补足 LLM 对邮件关系的理解。

### 隔离边界

- 按 mailbox 隔离：同一联系人在不同邮箱下不共享记忆
- 每个联系人一个记忆文件
- 每个 thread 一个记忆条目（含 thread 总结 + 逐封邮件摘要）
- 存储 key：`mailbox/{normalized_mailbox}/contacts/{normalized_contact}/memory`

### 记忆结构

```json
{
  "mailbox": "hr@anna.partners",
  "contact_email": "alice@example.com",
  "threads": [{
    "thread_id": "...",
    "thread_summary": {
      "summary": "讨论 Priya 是否进入下一轮面试",
      "current_state": "等待用户决策",
      "open_loop": "决定是否推进 Priya",
      "status": "open",
      "importance": "medium"
    },
    "message_summaries": [{
      "message_id": "...",
      "from": "alice@example.com",
      "direction": "inbound",
      "summary": "Alice 询问 Priya 是否可以更快推进",
      "action_signal": "asks_for_decision"
    }]
  }]
}
```

线程状态仅四种：`open`（对方在等用户）、`waiting_for_them`（用户已回复）、`closed`（已处理完）、`unknown`。

### 写入来源

- Brief 扫描到的重要 thread
- 最终生成 card 的 thread
- 最新邮件来自用户本人、被过滤不生成 card 的 thread（记录 `owner_replied` 观察）
- 用户在 app 内发送的回复内容

写入方式：LLM 仅生成当前 thread entry 的摘要，代码负责 upsert、去重、限长和落库。

### 召回

LLM Selector 判断相关性，不做规则打分。硬约束：
- 同 mailbox + 同 contact + 排除当前 thread
- 不确定就返回空

召回结果以 compact context 形式注入下游 prompt（Brief Phase 2 判断、Draft 生成、Thread Summarize）。

---

## Ask 链路设计

Ask 是**开放式 agent 查询**，用户自然语言提问 → 搜索邮件 → 一次 LLM 综合分析 → 展示结果。

### 与 Brief 的本质区别

| | Brief | Ask |
|------|-------|------|
| 范式 | Workflow（多步编排管线） | Agent（搜→读→一次 LLM） |
| 过滤 | Phase 1：reply/review/ignore | 不经过 Phase 1/2 |
| 输出 | Attention Cards | 结构化答案（title + summary + sections） |
| 持久化 | 产生卡片存储 | 仅 session 级 history |

### 链路流程

```
用户自然语言请求
    │
    ▼
Planner LLM（1 次调用）
  → 语义展开：用户概念 → 可搜索词
  → 输出 AskPlan：topics、people、timeframe、direction、goal、task_prompt
    │
    ▼
Search（纯代码）
  → 从 AskPlan 构建 Gmail 查询（非 LLM 写语法）
  → 自适应搜索：0 结果自动放宽查询
    │
    ▼
Read（按需读取）
  → read_depth: header_only / message_detail / thread_context
    │
    ▼
Answer LLM（1 次调用，看全部相关邮件）
  → 渐进截断回退：full(4000) → compact(1600) → short(800) → headers(0)
  → 输出 {title, summary, sections[]}
    │
    ▼
前端展示 + Plan 持久化（支持 Re-run）
```

### Planner 的语义展开

Planner 不再直接输出 Gmail 查询语法，而是输出结构化参数，由代码构建查询：

```json
{
  "topics": [{
    "concept": "候选人",
    "search_terms": ["resume", "CV", "application", "interview", "求职", "简历", "面试"],
    "relevance_hint": "unknown senders about jobs, with resume attachments or interview mentions"
  }],
  "people": [{"name_hint": "christopher", "role": "sender"}],
  "timeframe": "7d",
  "direction": "inbox",
  "goal": "find_emails",
  "task_prompt": "查找候选人相关的未回复邮件..."
}
```

三层结构：`concept`（给后续阶段理解上下文）→ `search_terms`（直接用于 Gmail 搜索）→ `relevance_hint`（描述匹配特征，告诉 filter 怎么判断）。

### 输出格式

统一容器结构，前端通用渲染：

```json
{
  "title": "简短回答标题",
  "summary": "一两句话概括发现",
  "sections": [
    {
      "heading": "段落标题",
      "body": "叙述性文字（可选）",
      "items": [
        {
          "subject": "条目标题",
          "from": "发件人",
          "context": "事实描述",
          "suggestion": "建议动作（可选）",
          "draft": "草稿文本（可选）",
          "message_id": "...",
          "thread_id": "..."
        }
      ]
    }
  ]
}
```

---

## Draft 生成设计

### 两阶段草稿生成

核心问题：LLM 替用户写回信时不知道定价、档期、态度，只能编造。

解决方案：把决策权还给用户，LLM 只负责**分析"哪里需要用户决策"**和**按用户回答组装语言**。

#### 阶段一：Gap Analysis（Phase 2 内嵌，零额外 LLM 调用）

Phase 2 judgment 已读完全文，在现有输出上追加 `reply_gaps` 字段：

```json
{
  "reply_gaps": {
    "needs_user_input": true,
    "summary": "对方需要报价和确认时间",
    "questions": [
      {
        "id": "q1",
        "question": "对方问项目报价。你想报多少？",
        "hint": "可以给具体数字、范围，或说'先了解需求再报价'",
        "required": true
      },
      {
        "id": "q2",
        "question": "对方约下周三 call。你哪个时间段方便？",
        "hint": "比如'周三 3-4pm'",
        "required": false
      }
    ]
  }
}
```

判定规则：
- `needs_user_input=true` 仅当对方明确问了问题（报价/档期/意见/确认具体事实）
- 感谢信/FYI/纯通知 → `needs_user_input=false`
- `questions` 最多 3 个，合并同类问题

#### 阶段二：User Response → Draft

用户填写答案后：

```
你的回答：
- q1: "标准报价 ¥5000/月，首月 8 折"
- q2: "周三下午 3 点可以"
```

→ LLM 根据原邮件 + 用户回答组装回信。严格约束：草稿只能基于用户回答和原邮件内容，**不得编造用户没有提供的信息**。

### 草稿修改

- 预设芯片：`[Shorter]` `[Warmer]` `[More direct]`，一键风格调整
- 自由输入：用户可输入任意修改指令
- Revise 基于当前草稿（含用户已编辑内容），不覆盖未确认的修改
- Gap 表单期间不显示 revision chips，用户必须先完成 gap → 生成草稿 → 然后才能 revise

### 持久化

草稿和摘要自动持久化到 `PersistentCard.draft_reply` / `thread_summary`，下次打开卡片时直接加载，避免重复消耗 LLM token。

---

## Snooze 详细设计

Snooze 让用户暂时隐藏卡片，到期自动恢复。三个选项：

### Tomorrow

设置 `snooze_until` = 明天 9:00 AM 北京时间。当前卡片从 Brief 中移除，第二天重新进入。

### Next week

设置 `snooze_until` = 下周一 9:00 AM 北京时间。一周后重新允许进入 Brief。

### Don't prioritize threads like this

记录用户偏好，**降低相似 sender/thread 未来进入 Brief 的概率**（不是完全屏蔽）。

#### 两级偏好信号模型

| 信号 | 来源 | 强度 | Prompt 中的效果 |
|------|------|------|----------------|
| `dont_prioritize` | Snooze 菜单主动选择 | 强 | 严格标准：只保留必须用户亲自处理的事项 |
| `no_action_needed` ×2+ | 抽屉处理决策累积 | 弱 | 降为 lower_priority，紧急时仍可进入 main |
| `no_action_needed` ×1 | 单次标记 | 无 | 不提示 |

强信号写入 `SnoozePrefs.senders` / `SnoozePrefs.threads`，在 Phase 2 prompt 中注入，告知 LLM 对降权 sender/thread 应用更严格标准但不完全屏蔽。

关键设计决策：`no_action_needed` 不能直接写 SnoozePrefs——用户点"无需操作"可能是"这次不用处理"而非"以后都降权"。

#### 过期恢复

`merge_cards()` 在每次扫描合并时检查 `snooze_until` 是否已过，过期自动恢复为 `pending` 状态。

---

## 多邮箱接入

系统支持多个 Gmail 邮箱同时接入，通过**勾选模型**管理参与扫描的邮箱范围。

### 邮箱发现与注册

- **本地 dev**：扫描 `scripts/google_token/.secrets/gmail_tokens/` 目录，每个 token 文件对应一个邮箱，调用 Gmail profile API 获取真实地址
- **Anna 平台**：通过平台注入的 OAuth token 获取授权邮箱
- 发现后写入 `MailboxRegistry`（APS / local JSON），作为邮箱列表的唯一数据源
- Registry 不保存 token，只保存状态和元数据（email、provider、authorized、selected、last_scan_at 等）

### 勾选模型

Sources 抽屉中每个邮箱一个 checkbox：

```
┌─ Mailboxes ───────────────────────────┐
│  ☑ a@gmail.com          ✓ connected   │
│  ☑ b@company.com        ✓ connected   │
│  ☐ c@gmail.com          ⚠ re-auth    │
└────────────────────────────────────────┘
```

- 默认全选，未勾选的邮箱不扫描、不展示卡片、不消耗 API 配额
- 勾选状态持久化到后端 registry，App 重启后保持
- 取消勾选 → 该邮箱卡片立即从列表隐藏；重新勾选 → 已有卡片立即恢复显示
- 全部取消勾选时，Brief 扫描按钮禁用

### Brief 多邮箱扫描

只扫描 `selectedMailboxes` 中的邮箱，串行执行：

**第一个邮箱：** 正常全屏扫描进度动画（scanning stage、judging、完成百分比），与单邮箱体验一致。

**第一个邮箱完成后：**
- 立即展示该邮箱产出的卡片（调用 `loadActiveCards`）
- 后续邮箱不再显示全屏进度动画

**后续邮箱：** 进度仅在底部状态栏显示：
```
Scanning b@company.com · 2/3 mailboxes done
```

用户可以在第一个邮箱的卡片上操作，同时底部栏显示后续邮箱的扫描进度。

**全部完成：** 底部栏汇总显示：
```
3/3 mailboxes scanned · a@gmail.com (5 cards) · b@company.com (3 cards)
```

**失败处理：** 某邮箱扫描失败不影响其他邮箱。失败邮箱在 Sources 抽屉中标记错误状态，可单独重试（不需要重扫所有邮箱）。

**增量扫描：** 每个邮箱独立维护 `processed_message_ids` 和 `scan_state`，互不干扰。

### 卡片聚合与展示

- `get_active_cards(mailbox="all")` 聚合所有已注册邮箱的 active cards
- 前端按 `selectedMailboxes` 筛选可见卡片
- 按 priority 降序 + 最近邮件时间排序
- 多邮箱混合展示时，每张卡片显示来源邮箱标签（chip）；单邮箱时隐藏
- 分类筛选（All / Needs reply / Needs review / Cleanup）跨邮箱生效

### 卡片操作安全

**所有卡片操作以 `card.details.mailbox` 为准**，不使用全局 `state.mailbox`：

| 操作 | 使用字段 |
|------|---------|
| `openCard` | `card.details.mailbox` |
| `generateDraft` | `card.details.mailbox` |
| `recordDecision` | `card.details.mailbox` |
| `replyNow` | `card.details.mailbox` |
| `markCleanupAsRead` | `card.details.mailbox` |
| `snoozeCard` | `card.details.mailbox` |

前端卡片相关状态 map 使用 `${mailbox}::${cardId}` 作为 key，避免不同邮箱卡片 ID 碰撞。

### Ask 多邮箱（⚠️ 待完善）

**当前实现：** Ask 使用 `selectedMailboxes[0]`（取第一个勾选邮箱）作为扫描目标，不支持用户选择 Ask 的目标邮箱。

**存在问题：**
- 用户有多个邮箱时，无法指定"只在工作邮箱中查找"
- 用户不清楚 Ask 实际会在哪个邮箱中搜索
- 显示文案 "N selected mailboxes" 但实际只用第一个

**缺失的 UI：** Ask 输入框旁需要邮箱选择器，支持：
- **多选下拉框**：列出所有已授权邮箱，默认选中 `selectedMailboxes`
- **会话内记忆**：同一次会话中记住用户上次的选择
- **仅本次生效**：Ask 的邮箱选择不写回 Sources 持久化状态，不影响 Brief 扫描范围
- **App 重启时**：从 Sources 同步默认值

**后端需配套升级：** Planner 和 Executor 需接受 `mailboxes: list[str]` 参数，在多个邮箱上分别执行 search 后汇合结果，每封邮件标注来源邮箱。

### 联系人记忆多邮箱隔离

- 联系人记忆按 mailbox 严格隔离：同一 `contact_email` 在不同 mailbox 下不共享记忆
- 存储 key：`mailbox/{normalized_mailbox}/contacts/{normalized_contact}/memory`
- 召回时只在当前卡片所属 mailbox 下检索

### Run History 多邮箱

- Run history 保持全局 key（`runs/history`），每条记录携带 `mailbox` 字段
- 前端 History 抽屉中每条记录显示 mailbox tag
- 未来可按 mailbox 筛选

### 用户偏好多邮箱

- snooze / learning 偏好暂为全局
- 建议后续升级为按邮箱隔离（用户在 A 邮箱点了"不要优先这个 sender"，不应影响 B 邮箱）
- 详见 `docs/多邮箱接入方案.md` 第十三节

---

## 未完成内容

### 1. 已持久化卡片在外部处理后 App 内无相应变化

**问题：** 用户已经在 Gmail 中回复了某封邮件，但 App 内的 Attention Card 仍然显示"需要回复"。当前系统缺少对 Gmail 状态变更的感知机制。

**建议方案：**
- 方案 A：增量扫描时通过 Gmail history API 检测变更（labels 变化、新回复），自动更新或 dismiss 对应卡片
- 方案 B：每次 Brief 扫描前做一次轻量 check——遍历所有 active cards 的 thread_id，调用 Gmail API 检查最新消息是否来自用户本人
- 方案 A 更可靠但需要接入 Gmail history API；方案 B 实现成本低但会消耗 API 配额

### 2. Outlook 邮箱接入

**当前状态：** 仅有 Gmail 适配器。管线核心层（Phase 1/2、卡片构建、存储）操作的是 provider 无关的抽象类型（`MessageLite` / `MessageDetail` / `ThreadContext`），理论上接入新 provider 不影响这些模块。

**设计方案：** 详见 `docs/Outlook邮箱接入方案.md`。

核心思路是适配器模式：
```
pipeline / scan / context（不改）
    ↓ 依赖
mail_adapter.py facade
    ↓ 分发
GmailAdapter ←→ OutlookAdapter（新实现）
```

OutlookAdapter 需要实现：Microsoft Graph API 认证、邮件搜索（Gmail 语法 → Graph `$filter` / `$search` 翻译）、消息归一化（Outlook 字段 → 统一 dict 格式）、写操作（send/mark read/trash）。Planner 需按 provider 加载不同的 system prompt。

**实施顺序建议：** 先重构 GmailAdapter + facade，再实现 OutlookAdapter 搜索 + 归一化，最后补写操作和前端 UI。

### 3. Ask 链路 Embedding 语义筛选

**当前状态：** Ask 链路使用 Gmail 关键词搜索 + Ask Filter（LLM 判断 relevant/not_relevant）来筛选邮件。Filter 是 LLM 调用，有 token 成本和延迟。

**升级方向：** 使用 Embedding 模型（如 text-embedding-3-small 或本地模型）计算邮件语义向量，通过余弦相似度筛选与用户问题最相关的邮件。

**收益：**
- 筛选阶段零 LLM 调用成本
- 对"语义相关但关键词不匹配"的邮件召回率更高
- 可预先对历史邮件做 embedding 索引，Ask 查询时直接检索

**建议实现路径：**
- 阶段 1：引入 embedding SDK（如 executa_sdk 中已有的 embeddings 模块），对 Ask 搜索结果做 post-filter（计算每封邮件与 query 的相似度，取 top-K）
- 阶段 2：对全量邮件做预索引（在 Brief 扫描时异步计算 embedding，存入 APS），Ask 时直接向量检索替代关键词搜索
- 注意：embedding 模型调用有成本，需要评估收益/成本比

### 4. 其他建议

#### 4.1 Scan Plan 剩余字段消费

后端 `ScanPlan` 数据结构中 `priorities`、`include_archived`、`batch_behavior` 三个字段已有存储但 pipeline 未消费。建议在 `run_mail_task()` 中消费这些字段：
- `priorities`：调整查询优先级排序（unread first / inbox first / active threads / important contacts）
- `include_archived`：控制是否扫描归档邮件
- `batch_behavior`：控制触顶时的行为（ask / auto_300 / never_older）

#### 4.2 触顶提示与继续扫描

当扫描结果达到 `max_messages` 上限时，当前无任何提示。建议在 `ActionPlan` 中增加 `scan_continuation` 字段，告知用户还有未处理的邮件，可一键继续扫描。

#### 4.3 多邮件 Thread 关注点聚焦

详见 `docs/多邮件thread-LLM关注点修复.md`。当前已实施第一轮修复（取最新消息），但方案 C（从用户最后回复截断 + UNREPLIED/LATEST 标注）尚未实施。这能显著减少 LLM 被历史噪音干扰的问题。

#### 4.4 Gmail 状态变更感知

在第 1 条之外，还可以：
- 检测 Gmail 中已被删除的邮件，自动移除对应卡片
- 检测已标记为 spam 的邮件

#### 4.5 邮件规则学习

当前 `LearningRecord` 弱信号机制仅记录 `no_action_needed` 频率。可以扩展为：
- 从用户 dismiss 的模式中学习（如"总是 dismiss 来自某个 domain 的 newsletter"）
- 自动建议 `dont_prioritize` 规则（"你已经连续 3 次 dismiss 了来自 @linkedin.com 的通知，要自动降权吗？"）
- 学习用户的回复时间偏好

#### 4.6 HTML 邮件正文提取

详见 `docs/HTML邮件处理方案.md`。当前 HTML 邮件可能以原始标签形式进入 LLM，浪费大量 token。建议在 `_decode_body()` 层面实现 `text/plain` 优先 → `html2text` 转换 → regex fallback 的渐进式处理。

#### 4.7 测试覆盖

当前测试仅有 `test_llm_json_repair.py` 和 `test_storage_integration.py` 两个脚本式测试。建议：
- 增加 Phase 1/2 分类准确率的离线评估脚本
- 增加卡片构建和合并的单元测试
- 增加前端 `mailAgentClient.test.ts`、`cardHelpers.test.ts`、`runHelpers.test.ts` 的覆盖范围

---

## 开发指南

### 安装

```bash
cd inbox-tool/src
uv sync
```

### Smoke Test

```bash
# Executa manifest
printf '%s\n' '{"jsonrpc":"2.0","method":"describe","id":1}' \
  | uv --directory inbox-tool/src run anna-inbox-executa

# Executa health
printf '%s\n' '{"jsonrpc":"2.0","method":"health","id":1}' \
  | uv --directory inbox-tool/src run anna-inbox-executa
```

### 运行测试

```bash
cd inbox-tool/src
uv run python tests/test_llm_json_repair.py
uv run python tests/test_storage_integration.py
```

### 前端构建

```bash
cd anna-inbox
npm run build   # tsc --noEmit && vite build
```

### 代码风格

- Python：类型化、async I/O、dataclass 持久化
- 存储统一走 `storage/ops.py`，不直接散落访问 APS/local client
- 协议响应 JSON 可序列化、紧凑
- 诊断信息写 `stderr`，仅 JSON-RPC 响应写 `stdout`
- 前端组件不直接写 Executa tool 名称，工具调用集中在 `api/mailAgentClient.ts`

### 关键约束

- 单次 invoke 约 65 秒超时，maxCalls=8
- 大字节内容不能通过 JSON-RPC result 返回，使用 APS files/object storage
- APS KV 仅用于小型 JSON 状态
- 不记录 OAuth token / API key / 完整 credential context
