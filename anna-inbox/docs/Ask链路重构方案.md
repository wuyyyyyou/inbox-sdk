# Ask 链路重构方案
2026-6-8

## 一、现状分析

### 当前链路

```
用户请求 → generate_custom_plan() (Planner LLM) → run_custom_scan() (搜索+一次LLM回答)
```

两个文件：`planning/custom.py`（生成 `CustomScanPlan`，含原始 Gmail query 字符串）、`core/pipeline.py:511`（`run_custom_scan` 执行搜索和回答）。

### 薄弱点

#### 1. Planner LLM 直接生成 Gmail Query（最致命）

LLM 被要求输出原始 Gmail 搜索语法字符串，但它不擅长这件事。

- **编造不存在的操作符。** `status:urgent`、`has:reply`——Gmail API 不报错，静默返回空。
- **OR 语法脆弱。** Gmail 用 `{term1 term2}`，LLM 输出 `from:alice OR subject:meeting` 被误解。
- **人员搜索只能用模糊匹配。** Prompt 说 "NEVER guess email addresses"，所以 `from:christopher` 是全文搜索，匹配签名里提到 "Christopher" 的无关邮件。
- **用户概念 ≠ 邮件关键词。** "候选人"不会出现在求职邮件里——出现的是 `resume`、`CV`、`interview`、`求职`、`简历`。旧 Planner 把概念翻译和语法生成压在一次输出里，既展开不充分，也容易直接把概念词当搜索词。"等我回复的"根本无法用 Gmail query 表达。
- **Query 间可能矛盾。** `in:inbox` 和 `in:sent` 是独立查询，Planner 没有意识到。
- **零验证 + 静默失败。** `_parse_queries()` 只检查字段类型；`search_gmail()` 异常返回 `[]`，上游不知道是 query 写错还是真没有。

#### 2. 一次 LLM 调用承担所有分析

Brief 链路是 phase1（分类）+ phase2（判断）+ guards（安全）+ plan（组织）。Ask 把这些全压缩成一次调用。50 封邮件全塞进 prompt，没有前处理，没有安全 guards。

#### 3. read_depth 在搜索前盲选

Planner 在没看到任何邮件时决定读多深。`header_only` 判断"要不要回复"看不到正文，`thread_context` 统计"几个未读"浪费 token。

#### 4. 没有自适应搜索

0 结果 → 直接说"没找到"，不尝试换 query。

#### 5. Contact Memory 没有接入

Brief 链路查联系人记忆（VIP、上次沟通、风格偏好），Ask 完全没有。

#### 6. Fallback 几乎等于没有

用户请求分词→去停用词→拼接。"帮我看看最近有没有YouTube合作的邮件" → `"YouTube 合作 newer_than:90d"`。分词不准，时间硬编码。

#### 7. 没有分阶段降级

每个环节失败后只能整体失败。

---

## 二、重构目标

1. **LLM 不写 Gmail 语法**——输出结构化参数 + 语义展开（用户概念→可搜索词），代码构建 query
2. **两阶段 LLM**——专用 filter（相关/不相关）+ Answer LLM（深度分析），各司其职
3. **多邮箱支持**——前端多选邮箱，Planner 共享一份 AskPlan，search + filter 按邮箱并发，Candidate 汇合标注来源，Answer LLM 一次跨邮箱分析
4. **自适应搜索**——0 结果自动放宽 query
5. **Contact memory 接入**——人员搜索用精确邮箱地址；回答阶段注入联系人上下文
6. **安全 guard**——输出前校验
7. **分阶段降级**——各阶段独立失败处理

---

## 三、新架构

```
用户请求 + 选中的邮箱列表 [a@x.com, b@x.com, ...]
  │
  ▼
┌──────────────────────────────────────────┐
│  planner.py · Planner LLM                 │
│                                           │
│  只调一次——同一份 AskPlan 适用所有邮箱      │
│                                           │
│  "找找候选人的未回复邮件"                   │
│    ↓ LLM 语义展开                         │
│  concept: "job candidates"                │
│  search_terms: ["resume","CV",           │
│    "application","interview",             │
│    "cover letter","求职","简历","面试"]     │
│  relevance_hint: "unknown senders         │
│    about jobs or interviews,              │
│    waiting for response"                  │
│                                           │
│  + people[], timeframe, direction,        │
│    goal, task_prompt                      │
│                                           │
│  输出：AskPlan（零 Gmail 语法）             │
└──────────────────┬───────────────────────┘
                   │
    ┌──────────────┼──────────────┐
    ▼              ▼              ▼
  a@x.com       b@x.com       c@x.com    ← 每个邮箱并发执行 search + filter
    │              │              │
  search.py     search.py     search.py
    │              │              │
  filter        filter        filter
    │              │              │
    ▼              ▼              ▼
  candidates_a  candidates_b  candidates_c
    │              │              │
    └──────────────┼──────────────┘
                   │  汇合 + 标注来源邮箱
                   ▼
┌──────────────────────────────────────────┐
│  answer.py · Context → Answer → Guard     │
│                                           │
│  a. 对所有候选读正文/线程 + contact memory  │
│                                           │
│  b. Answer LLM（一次，看全部）              │
│     候选渲染时标注来源邮箱，LLM 有跨邮箱视野  │
│                                           │
│  c. Guard：禁止操作 + ID 校验               │
│                                           │
│  输出：{title, summary, sections} → 前端   │
└──────────────────────────────────────────┘
```

---

## 四、各模块详细设计

### 4.1 planner.py — 意图解析 + 语义展开

**核心改变：** LLM 不再输出 Gmail query 字符串，而是输出结构化参数 + 语义展开。

```
旧版：gmail_queries: ["from:christopher newer_than:7d in:inbox"]
      ↑ LLM 写 Gmail 语法，不可靠

新版：topics: [{
        concept: "job seekers",
        search_terms: ["resume","CV","application","interview",
                       "cover letter","求职","简历","面试","应聘"],
        relevance_hint: "unknown senders about jobs or interviews,
                        typically with resume attachments or mentioning
                        specific roles — waiting for response"
      }]
      people: [{name_hint:"christopher", role:"sender"}]
      timeframe: "7d", direction: "inbox"
      ↑ LLM 做语义展开，代码构建 query
```

**LLM 输出 Schema：**

```json
{
  "title": "Short task title (<=12 words, English)",
  "description": "One sentence summary",
  "people": [{"name_hint": "name as mentioned", "role": "sender|recipient|either"}],
  "topics": [{
    "concept": "What the user means (e.g. 'job candidates')",
    "search_terms": ["concrete", "searchable", "terms"],
    "relevance_hint": "How to judge if an email matches — sender type, purpose, content patterns"
  }],
  "timeframe": "1d|3d|7d|14d|30d|90d|180d|365d",
  "direction": "inbox|sent|all",
  "goal": "count_items|summarize_threads|find_emails|check_reply_status|draft_replies|general_qa",
  "task_prompt": "Analysis instructions for the Answer LLM (no JSON format instructions)",
  "confidence": 0.85
}
```

**topics 三层结构：**

用户说"候选人"、"合作机会"、"安全问题"——这些都是概念标签，不是邮件中实际出现的词。Planner LLM 利用自己的世界知识，把用户概念翻译为可搜索的词汇。

| 层 | 字段 | 用途 | "等回复"的例子 |
|----|------|------|-------------|
| 1 | `concept` | 给后续阶段理解上下文 | "awaiting replies" |
| 2 | `search_terms` | 直接用于 Gmail 搜索 | `[]`（空——这类查询无法用关键词搜） |
| 3 | `relevance_hint` | 告诉 filter "怎么判断匹配" | "sender explicitly asked a question or sent a proposal, and the latest message in the thread is from them, not me" |

三层缺一不可：`search_terms` 不能直接放概念词（搜不到），`relevance_hint` 不能只复述 search_terms（要描述语义特征，如发件人类型、邮件目的），`concept` 留下可读标签方便排查。

**字段规则：**
- `people[].name_hint`：用户提到的名字原文，绝不猜邮箱域名。查询时由 search.py 通过 contact memory 解析为精确地址
- `topics[].search_terms`：5-15 个具体可搜索词，覆盖中英文和同义变体。不要把 concept 当成 search_term（除非它本身就是邮件里会出现的关键词，如 "invoice"、"password"）。如果请求无法用关键词搜（如"等我回复的"），设为空数组
- `topics[].relevance_hint`：1-2 句自然语言，描述匹配这类邮件的典型特征。不要复述 search_terms，描述"怎么判断"
- `direction`：inbox="查收件箱"；sent="我发了什么"；all="完整对话"、"回复状态"
- `timeframe`：中文映射——"最近"→7d，"本月"→30d，"今年"→365d
- `goal`："多少个"→count_items，"要回复吗"→check_reply_status，"帮我写回复"→draft_replies
- `task_prompt`：告诉 Answer LLM 怎么分析和分组。不包含 JSON 格式指令

**回退：** LLM 失败→规则提取关键词，构建保守 AskPlan（direction=inbox, timeframe=30d, goal=general_qa）。

---

### 4.2 search.py — Query 构建 + 自适应搜索

纯代码，两个函数。

**`build_queries(plan, mailbox)`：**

```
输入：AskPlan

1. 人员解析
   - contact_memory store 查找 name_hint → email
   - 找到 → from:email@domain.com（精确地址匹配）
   - 未找到 → from:name_hint（Gmail 部分名称匹配）
   - 多人 → OR 组 {from:a@x.com from:b@y.com}

2. 方向 → in:inbox | in:sent | （不加）

3. 时间 → newer_than:{timeframe}

4. 主题搜索词 → 遍历 plan.topics[].search_terms
   → 全部展开为 OR 组：{resume CV application interview 求职 简历}
   → 绝不使用 concept 字段

5. _normalize_gmail_query() 语法修正

6. 兜底：所有 query 构建失败 → newer_than:30d -in:sent -in:draft
```

**`execute_search(mailbox, queries)`：**

```
尝试 1：原样搜索
  ↓ 0 结果
尝试 2：去掉人员过滤（保留主题+时间+方向）
  ↓ 0 结果
尝试 3：只保留方向+时间（广撒网）
  ↓ 0 结果
返回 []，上游处理
```

复用 `core/scan.py::run_mail_scan()` 执行搜索（内置 Gmail API 失败→回退本地缓存）。

---

### 4.3 answer.py — Filter → Context → Answer → Guard

`run_ask_pipeline()` 编排以下四步。

#### 4.3.1 Ask Filter（专用 prompt，非 Phase 1）

**为什么不用 Phase 1：**

Phase 1 的 prompt 问的是"这封邮件需要回复吗？"（reply/review/ignore），这是 Brief 管线的语义框架。Ask 用户问的是"这封邮件和我的问题相关吗？"——两个维度。比如用户问"这个月有多少发票"，一封 Stripe 月结邮件在 Phase 1 被标成 review（不需要回复），但用户需要它被标成 relevant（这是发票）。反之，内部 HR 讨论"候选人池太小"在 Phase 1 可能被标成 review（值得关注），但对用户来说 not_relevant（不是候选人本人）。

**设计：**

写一个轻量 filter prompt，只问一件事：**"这封邮件和用户的问题相关吗？"**

系统 prompt：

```
You are Anna's relevance filter. For each email, answer one question:
"Is this email relevant to the user's request?"

Relevance means the email helps answer the user's question. If in doubt,
mark it relevant — the next stage will do deeper analysis.

Output a single JSON object: {"items":[{"i":<index>,"relevant":true|false,"reason":"..."}]}
```

用户 prompt 复用 Phase 1 的基础设施：
- **紧凑头格式**：复用 `_compact_header()`，单字母 key 省 token（i/id/f/s/sn/d/l/u/st/im/at）
- **批量分批**：Anna sampling 下每批 8 封，与 Phase 1 一致
- **LLM 调用**：复用 `call_llm_json_safe()`，temperature=0.1，失败回退到规则分类

**relevance_hint 注入点：** 用户 prompt 中标注 `relevance_hint`：

```
## User request
{plan.user_request}

## What to look for
{merged relevance_hints from plan.topics}

## Emails
{compact headers}
```

**优化：跳过过滤。** 搜索结果 ≤ 10 封时，过滤的价值为负（多一次 LLM 调用但不减少任何 token）。直接全部标记为 relevant。

**回退：** LLM 失败→全部标记为 relevant（保守策略：不筛掉任何可能的候选），交给 Answer LLM 自行判断。

#### 4.3.2 Context Reader

对每个 relevant 候选：
- 读正文（`get_message_detail`，上限 4000 字符）
- 读线程上下文（`get_thread_context`，上限 10 条，每条 2000 字符）
- 收集唯一发件人→检索 contact memory（`retrieve_contact_context`）→渲染为内联文本（`format_contact_context_for_prompt`）

读取失败→留空，继续。

#### 4.3.3 Answer LLM

系统 prompt 复用现有 `_EXECUTION_SYSTEM_PROMPT`（pipeline.py:464-508）。

用户 prompt 结构：

```
## Your Identity
You are Anna, executive assistant to {mailbox}.

## User request
{plan.user_request}

## Task
{plan.task_prompt}

## Contact context
{formatted contact memory}

## Relevant emails ({n} from {total} scanned)
{rendered candidates}

## Important
- Answer ONLY from the emails provided
- If nothing matches, say so honestly
```

渐进截断回退：full(4000) → compact(1600) → short(800) → headers(0)。

#### 4.3.4 Safety Guard

三道防线，不阻断流程——替换/剥离后继续返回：

1. **禁止操作**："send"、"delete"、"forward"、"unsubscribe" 模式→替换为 `[BLOCKED]`
2. **ID 校验**：输出中所有 `message_id`/`thread_id` 在候选邮件中验证→剥离不存在的引用
3. **内容净化**：`draft` 文本中不在输入中的邮箱地址→剥离

---

## 五、错误处理

两个硬阻断点，其他全部降级：

| 环节 | 错误 | 处理 |
|------|------|------|
| Planner | LLM 不可用/无效 JSON | 规则回退→保守 AskPlan（call_llm_json_safe 自动修复+重试）|
| Query Builder | Contact lookup 失败 | name_hint 直接用于 Gmail 部分名称匹配 |
| Query Builder | 无法构建有效 query | 兜底：`newer_than:30d -in:sent -in:draft` |
| Searcher | Gmail API 失败 | run_mail_scan 已内置回退本地缓存 |
| Searcher | 0 结果（含放宽后） | **阻断**：返回 "No matching emails found" |
| Filter | LLM 失败 | 全部标为 relevant（保守策略） |
| Filter | 无候选 | **阻断**：返回 "Scanned N emails but none matched" |
| Context | Body/thread 读取失败 | 留空，继续 |
| Context | Contact memory 失败 | 留空，继续 |
| Answer | LLM 全部截断失败 | 返回 fallback：标题 + 错误信息 |
| Guard | 检测到问题 | 替换/剥离，记录日志，继续返回 |

---

## 六、文件规划

### 新建（3 个）

```
inbox-tool/src/mail_agent/ask/
├── planner.py    # Planner LLM + AskPlan 类型 + 规则回退
├── search.py     # build_queries() + execute_search() + 自适应放宽
└── answer.py     # run_ask_pipeline(): filter → context → answer LLM → guard
                   # 含 Ask filter 的专用 prompt（非 Phase 1）
```

### 修改（4 个）

| 文件 | 改动 |
|------|------|
| `core/pipeline.py` | 删除 `run_custom_scan()` 中旧逻辑，改为调用 `run_ask_pipeline()` |
| `anna_inbox_executa/main.py` | `_start_custom_scan_async()` 中旧 Planner 替换为 `plan_ask_request()`；接受 `mailboxes` 数组参数 |
| `features/ask/AskView.tsx` | `ask-composer-footer` 左侧 "Custom mailbox scan" 替换为多选邮箱下拉（复用 Brief `MailboxFilter` 交互模式） |
| `app/state.ts` + `useAppController.ts` | 新增 `customScanMailboxes: string[]` 状态字段，`startCustomScan()` 传递 `mailboxes` 数组 |

### 复用基础设施（不改动）

| 文件 | 用途 |
|------|------|
| `core/phase1.py::_compact_header()` | Ask filter 的紧凑头格式 |
| `core/phase1.py::_ANNA_PHASE1_BATCH_SIZE` | Ask filter 的批次大小（8） |
| `core/scan.py::run_mail_scan()` | 搜索执行 |
| `core/candidate.py::generate_candidates()` | filter 失败时的规则回退 |
| `core/pipeline.py::_EXECUTION_SYSTEM_PROMPT` | Answer LLM 输出 schema |
| `llm_runtime/service.py::call_llm_json_safe()` | 所有 LLM 调用 |
| `contact_memory/` | 人员查找 + 上下文注入 |
| `mail_providers/gmail/adapter.py` | Gmail API + 缓存 + 正文读取 |

### 删除

`planning/custom.py` 中的 `generate_custom_plan()` 和 `_PLANNER_SYSTEM_PROMPT`（由 planner.py 替代）。`_fallback_plan_for_request()` 的关键词提取逻辑迁移到 planner.py 的规则回退中。

---

## 七、与 Brief 管线的对比

| 维度 | Brief | Ask 新版 |
|------|-------|---------|
| 搜索 | 策略预设 query + 用户关键词追加 | Planner 语义展开→代码构建 query→自适应放宽 |
| 过滤 | Phase 1：reply/review/ignore | **专用 filter**：relevant/not_relevant |
| 分析 | Phase 2 逐候选判断 | Answer LLM 一次分析（只看已过滤的 5-15 封） |
| 人员 | 无特殊处理 | Contact memory 解析名字→精确邮箱 |
| 联系人 | 判断阶段检索 | 过滤后、回答前检索 |
| 防线 | apply_rule_guards() | 内联 guard：禁止操作 + ID 校验 |
| 邮箱 | 遍历选中的邮箱各自跑 | Planner 共享 + search/filter 并发 + 候选汇合标注→一次 Answer |
| 输出 | ActionPlan + Cards | {title, summary, sections} |

核心差异就一个：**Brief 的 filter 问"需要回复吗"，Ask 的 filter 问"相关吗"。**

---

### 4.4 多邮箱编排

**前端：** AskView 的 `ask-composer-footer` 中，左侧 "Custom mailbox scan" 替换为多选邮箱下拉。

```
当前：  [ Custom mailbox scan ]              [ Run ]
改为：  [ 📧 a@x.com, b@x.com... ▾ ]         [ Run ]
```

复用 Brief `MailboxFilter` 的交互模式——紧凑 dropdown，列出所有已启用邮箱，多选 checkbox。默认选中 `state.selectedMailboxes`。用户改选后更新 `state.customScanMailboxes`。

**后端：** Planner 共享一份 AskPlan，search + filter 按邮箱并发，汇合后一次 Answer LLM。

```
run_ask_pipeline(question, mailboxes=[a@x.com, b@x.com, c@x.com]):

  1. Planner LLM 调一次 → AskPlan
     同一份 plan 适用所有邮箱，因为用户的问题（"找找候选人的未回复邮件"）
     和邮箱无关——不管搜哪个邮箱，搜索策略是一样的。

  2. 对每个邮箱并发执行 search + filter：
     a@x.com: build_queries(plan, a@x.com) → execute_search → filter → candidates_a
     b@x.com: build_queries(plan, b@x.com) → execute_search → filter → candidates_b
     c@x.com: build_queries(plan, c@x.com) → execute_search → filter → candidates_c

  3. 汇合：
     - 每封 candidate 标注 source_mailbox
     - 按 internalDate 统一排序
     - 去重：同一 thread_id 出现在多个邮箱时保留一份，标记 "出现在多个邮箱"

  4. Context Reader + Answer LLM + Guard：
     - 候选渲染时标注 [mailbox: a@x.com]
     - Answer LLM 一次调用，有跨邮箱全局视野
     - 输出不强制按邮箱分组——LLM 自然地提到"你的 a@x.com 邮箱里有..."
```

**降级：** 单个邮箱搜索失败→该邮箱返回空候选，不阻断其他邮箱。所有邮箱都失败→阻断返回错误。
