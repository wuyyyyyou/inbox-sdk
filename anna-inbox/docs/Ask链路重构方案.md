# Ask 链路重构方案
2026-6-8 9:59

## 一、现状分析

### 当前 Ask 链路

```
用户请求 → generate_custom_plan() (Planner LLM) → run_custom_scan() (搜索+一次LLM回答)
```

文件位置：
- `inbox-tool/src/mail_agent/planning/custom.py`：Planner LLM 生成 `CustomScanPlan`
- `inbox-tool/src/mail_agent/core/pipeline.py:511`：`run_custom_scan()` 执行搜索和回答

### 七个薄弱点（按严重程度排序）

#### 1. Planner LLM 直接生成 Gmail Query，完全不可控（最致命）

Planner LLM 被要求输出原始的 Gmail 搜索语法字符串，这是根本问题。

**具体表现：**

- **人员搜索只能用模糊匹配。** Prompt 明确说 "NEVER guess full email addresses"，所以查人只能用 `from:christopher` 这种写法。但在 Gmail 里这是全文搜索，不是精确地址匹配——会搜到签名里提到 "Christopher" 的无关邮件。

- **LLM 会编造不存在的 Gmail 操作符。** Prompt 里列了 `is:unread`、`category:primary` 等合法操作符，但 LLM 随时可能输出 `status:urgent`、`priority:high`、`has:reply` 这种 Gmail 不支持的语法。Gmail API **不会报错**，只是静默返回空结果。

- **OR 语法极其脆弱。** Gmail 的 OR 使用 `{term1 term2}` 格式，不是自然语言的 "OR"。`_normalize_gmail_query()` 只处理了括号包裹的 OR 和简单场景。LLM 输出 `from:alice OR subject:meeting` 大概率被 Gmail 误解。

- **Query 之间可能互相矛盾。** 比如同时生成 `in:inbox` 和 `in:sent` 两个 query，各自搜各自的，Planner 没有意识到这是独立查询。

- **零验证。** `_parse_queries()` 只检查了字段类型（是不是 list、有没有 `query` 字段），对 query 字符串本身完全不校验。`_normalize_gmail_query()` 也只修了两个问题（`in:draft→in:drafts`、OR 括号），剩下的坏 query 直接喂给 Gmail API。

- **静默失败。** `search_gmail()` 捕获异常返回 `[]`，`live_search_and_cache()` 查到空也返回 `[]`。上游只知道"没搜到"，完全不知道是 query 写错了还是确实没有相关邮件。

- **用户概念 ≠ 邮件关键词，Planner 没有语义展开。** 用户说"找找候选人的邮件"——但没有人会在邮件里自称"候选人"。真正出现在邮件里的是 `resume`、`CV`、`application`、`interview`、`求职`、`简历`、`面试`。旧 Planner 把概念翻译和 Gmail 语法生成压在一次 LLM 输出里，既做不到充分的语义展开（token 被 Gmail 语法占用），也容易把概念词直接当成搜索词用。同理："合作机会"≠邮件正文，而是 `partnership`、`proposal`、`demo`、`collaboration`；"等我回复的"根本无法用 Gmail query 表达，只能广搜 + LLM 判断。

#### 2. Fallback 机制几乎等于没有

`_fallback_plan_for_request()` 做的事是：把用户请求分词，去掉停用词，拼接成 query。

```
"帮我看看最近有没有YouTube合作的邮件" → query: "YouTube 合作 newer_than:90d"
```

- 中文分词不准确
- `newer_than:90d` 硬编码，用户问"今天"的邮件也搜 90 天
- 求职场景稍好（用关键词枚举），但也仅限于那一个场景

#### 3. 一次 LLM 调用承担所有分析任务

对比 Brief 链路的 phase1（分类）+ phase2（判断）+ guards（安全）+ plan（组织），Ask 把所有这些压缩成了一次调用。

**后果：**
- 没有分阶段筛选——50 封邮件全塞进 prompt，token 压力巨大，不得不做 progressive truncation
- 没有 thread 感知的前处理——依赖 LLM 在 prompt 里理解 thread 关系
- **没有安全 guards**——Ask 链路零安全防护，Brief 链路有 `apply_rule_guards()` 阻止危险操作

#### 4. read_depth 在搜索前盲选

Planner 要在看到任何邮件之前就决定读多深：
- `header_only` → 判断"要不要回复"？LLM 看不到正文，只能瞎猜
- `thread_context` → 统计"几个未读"？浪费大量 token 读线程历史

#### 5. 没有结果反馈和自适应循环

搜索返回 0 结果 → 直接告诉用户"没找到"，不尝试换 query。搜索返回 500 条 → 截断到 budget，可能漏掉关键邮件。

#### 6. Contact Memory 没有接入

Brief 链路在判断阶段会查联系人记忆（VIP、上次沟通时间、风格偏好）。Ask 链路完全没有。

#### 7. 没有分阶段降级

每个环节失败后只能整体失败，不能降级返回部分结果。

---

## 二、重构目标

1. **LLM 永远不写 Gmail 语法**——它只输出结构化参数，代码构建 query
2. **两阶段 LLM**——先过滤相关性（phase1），再深度分析（answer），各司其职
3. **自适应搜索**——0 结果自动放宽，太多自动收窄
4. **Contact memory 接入**——人员搜索用联系人记忆解析为精确邮箱地址
5. **安全 guards**——输出前校验
6. **分阶段降级**——每阶段独立失败处理，尽量返回部分结果
7. **复用现有组件**——phase1、contact_memory、llm_runtime、run_mail_scan 全部复用，不造轮子

---

## 三、新架构：7 阶段管线

```
用户请求
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ 阶段 1 · Planner LLM                                        │
│ 输入：user_request, mailbox                                  │
│ 输出：AskPlan（结构化参数，无 Gmail 语法）                    │
│       people / topics / timeframe / direction / goal /       │
│       task_prompt / analysis_focus                           │
│ topics 包含三层：concept（用户原意）/ search_terms（展开的    │
│ 可搜索词）/ relevance_hint（给过滤器的判断提示）              │
│ LLM 做语义展开，把用户概念翻译成邮件中实际出现的词              │
└──────────────────────────┬──────────────────────────────────┘
                           │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ 阶段 2 · Query Builder (纯代码)                               │
│ 输入：AskPlan + contact memory lookup                        │
│ 输出：SearchParams（验证过的 Gmail query 列表）                │
│ - 人员 → contact memory 查找 → 精确邮箱地址                    │
│ - 主题 → 用 search_terms 构建 Gmail OR 组（非 concept）         │
│ - 模板引擎从结构化参数构建 query                               │
│ - 复用 _normalize_gmail_query() 做语法修正                     │
└──────────────────────────┬──────────────────────────────────┘
                           │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ 阶段 3 · Searcher (纯代码)                                    │
│ 输入：SearchParams                                            │
│ 输出：MessageLite 列表                                        │
│ - 复用 run_mail_scan() 执行搜索                                │
│ - 0 结果 → 自动放宽（去掉人员过滤→去掉主题→广撒网）             │
│ - 超过阈值 → 自动收窄（加 category 过滤）                      │
└──────────────────────────┬──────────────────────────────────┘
                           │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ 阶段 4 · Relevance Filter (LLM)                               │
│ 输入：MessageLite 列表 + analysis_focus + relevance_hint       │
│ 输出：candidates + low_value_items                            │
│ - 复用 run_phase1_batch_classify()                            │
│ - reply / review / ignore 分类                                │
│ - 每个候选给出 read_depth 建议                                 │
│ - LLM 失败 → 回退到 generate_candidates() 纯规则               │
│ - relevance_hint 告诉 LLM "什么样算相关"（如：未知发件人、     │
│   附带简历、讨论具体职位），弥补关键词匹配的语义缺口            │
└──────────────────────────┬──────────────────────────────────┘
                           │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ 阶段 5 · Context Reader (纯代码)                               │
│ 输入：candidates + mailbox                                    │
│ 输出：EnrichedCandidate 列表                                  │
│ - 按每个候选的 read_depth 读正文/线程上下文                     │
│ - 对每个唯一发件人检索 contact memory                          │
│ - 复用 get_message_detail() / get_thread_context()            │
│ - 复用 retrieve_contact_context()                             │
└──────────────────────────┬──────────────────────────────────┘
                           │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ 阶段 6 · Answer Generator (LLM)                                │
│ 输入：EnrichedCandidate 列表 + task_prompt + contact_context   │
│ 输出：{title, summary, sections[...]}                         │
│ - 只看已过滤的候选（聚焦、更小上下文）                          │
│ - 复用现有 _EXECUTION_SYSTEM_PROMPT（固定输出 schema）          │
│ - 渐进截断回退：full → compact → short → headers               │
└──────────────────────────┬──────────────────────────────────┘
                           │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ 阶段 7 · Safety Guard (纯代码)                                  │
│ 输入：LLM 输出 + 候选邮件列表                                  │
│ 输出：AskResult（校验后）                                      │
│ - 禁止操作检查（send/delete/forward/unsubscribe）              │
│ - message_id/thread_id 引用校验（防止 LLM 幻觉）               │
│ - 内容净化（剥离输入中未出现的邮箱地址）                         │
└──────────────────────────┬──────────────────────────────────┘
                           │
  ▼
                    AskResult → 前端
```

---

## 四、各阶段详细设计

### 4.1 Planner LLM

**新文件：** `ask/planner.py`

**核心改变：** LLM 输出结构化参数 + 语义展开，不写 Gmail 语法。

```
旧 Planner 输出：  gmail_queries: ["from:christopher newer_than:7d in:inbox"]
                   ↑ LLM 写的原始 Gmail 语法，不可靠
                   用户说"候选人"，旧 Planner 可能直接把"候选人"塞进 query
                   → Gmail 搜不到，因为邮件里不会出现"候选人"

新 Planner 输出：  people: [{name_hint: "christopher", role: "sender"}]
                   topics: [{
                     concept: "job seekers",           ← 用户的原始概念
                     search_terms: ["resume","CV",     ← 展开的可搜索词
                       "application","interview",
                       "cover letter","求职","简历"],
                     relevance_hint: "unknown senders,  ← 给过滤器的判断标准
                       mentions specific roles,
                       has resume attachments"
                   }]
                   timeframe: "7d"
                   direction: "inbox"
                   ↑ 结构化参数 + 语义展开，代码构建 query
```

**LLM 输出 Schema：**

```json
{
  "title": "Short task title (<=12 words, English)",
  "description": "One sentence summary",
  "people": [
    {
      "name_hint": "The name exactly as the user mentioned it",
      "role": "sender | recipient | either"
    }
  ],
  "topics": [
    {
      "concept": "What the user means (e.g. 'job candidates')",
      "search_terms": ["term1", "term2", "..."],
      "relevance_hint": "How to judge if an email matches this concept"
    }
  ],
  "timeframe": "1d | 3d | 7d | 14d | 30d | 90d | 180d | 365d",
  "direction": "inbox | sent | all",
  "goal": "count_items | summarize_threads | find_emails | check_reply_status | draft_replies | general_qa",
  "task_prompt": "Analysis instructions for the Answer LLM",
  "analysis_focus": "Short hint for the relevance filter (1-5 words)",
  "confidence": 0.85
}
```

**topics 三层结构（核心设计）：**

用户说"候选人"、"合作机会"、"安全问题"——这些都是**概念标签**，不是邮件里实际出现的词。Planner LLM 的工作是利用自己的世界知识做语义展开：把用户的概念翻译成邮件中可能实际出现的具体词汇。

| 层 | 字段 | 用途 | 例子 |
|----|------|------|------|
| 1 | `concept` | 用户说的是什么概念，给后续阶段理解上下文 | "job candidates"、"partnership opportunities" |
| 2 | `search_terms` | 邮件里实际出现的词，直接用于 Gmail 关键词搜索 | `["resume","CV","interview","求职","简历"]` |
| 3 | `relevance_hint` | 告诉 Phase 1 过滤器"什么样的邮件匹配这个概念"，弥补关键词匹配的语义缺口 | "unknown senders mentioning specific roles, typically with resume attachments" |

**为什么需要三层：**

- `search_terms` 只能做**关键词初筛**——含有 "resume" 的邮件不一定是候选人（可能是内部讨论招聘流程），不含 "resume" 的也不一定不是（可能写的是 "I'm interested in the role"）
- `relevance_hint` 让 Phase 1 过滤器做**语义判断**——在关键词搜出来的结果中，判断每个邮件是否真的匹配用户的概念。例如"等我回复的邮件"根本无法用 Gmail query 表达，只能广搜 + 靠 LLM 判断"这封邮件是不是在等我回复"
- `concept` 作为**可读标签**——在 trace 和日志里解释 Planner 当时的理解，方便排查"为什么搜到这些结果"

**字段规则：**
- `people[].name_hint`：用户提到的名字原文，绝不猜邮箱域名
- `topics[].search_terms`：5-15 个具体的、可搜索的词。展开缩写、覆盖中英文、覆盖同义变体。不要把 concept 本身直接当成 search_term，除非它本身就是邮件中会出现的关键词（如 "invoice"、"password"）
- `topics[].relevance_hint`：1-2 句自然语言，描述这类邮件的典型特征（发件人类型、邮件目的、典型内容模式）。不要复述 search_terms，而要描述"怎么判断"
- `direction`：
  - `inbox`："查收件箱"、"有什么需要处理的"
  - `sent`："我发了什么"、"我的提案"
  - `all`："完整对话"、"回复状态"、"追上讨论"
- `timeframe`：中文时间映射——"最近"→7d，"本月"→30d，"今年"→365d
- `goal`：从请求语义推导——"多少个"→count_items，"要回复吗"→check_reply_status，"帮我写回复"→draft_replies
- `task_prompt`：自然语言，告诉 Answer LLM 怎么分析、怎么分组。不包含 JSON 格式指令
- `analysis_focus`：传给 relevance filter，如 "job applications"、"partnership discussions"、"security alerts"

**回退：** LLM 失败→从请求中提取关键词，构建保守的 AskPlan（direction=inbox, timeframe=30d, goal=general_qa）。

### 4.2 Query Builder

**新文件：** `ask/query_builder.py`

**核心原理：** 纯代码，无 LLM 参与。

```python
async def build_search_params(
    plan: AskPlan,
    mailbox: str,
) -> SearchParams:
```

**构建逻辑：**

1. **人员解析**（`_resolve_people`）：
   - 遍历 `plan.people`，在 contact_memory store 中查找 `name_hint` → `email`
   - 找到：用 `from:email@domain.com`（精确地址匹配）
   - 未找到：用 `from:name_hint`（Gmail 部分名称匹配，仍然是搜索语法级别但比全文搜索好）
   - 多人：用 OR 组 `{from:a@x.com from:b@y.com}`

2. **方向过滤**：
   - `inbox`：`in:inbox`
   - `sent`：`in:sent`
   - `all`：不加

3. **时间范围**：`newer_than:{timeframe}`

4. **主题搜索词**：遍历 `plan.topics[]`，取每个 topic 的 `search_terms`，全部展开为一个 OR 组 `{resume CV application interview 求职 简历}`。注意：**不用 `concept` 字段**——"候选人"本身在邮件中不会出现。如果有多个 topic，各自构建独立的 OR 组后用 AND 连接。

5. **类别过滤**（基于 goal）：
   - `draft_replies`→不加类别过滤
   - `general_qa`→加 `category:primary`
   - 其他→不加（让 relevance filter 来决定）

6. **语法规范化**：调用复用 `_normalize_gmail_query()` 修正 `in:draft→in:drafts`、OR 括号→`{}` 等问题

**对比旧版：**

| 场景 | 旧版（LLM 写 query） | 新版（代码构建） |
|------|---------------------|-----------------|
| 查人的邮件 | `from:christopher`（全文搜索） | `from:christopher@company.com`（精确地址，如果 contact memory 有） |
| 用户概念→搜索词 | "候选人"直接塞进 query→搜不到 | Planner 展开为 `{resume CV application interview 求职 简历}` |
| OR 语法 | `from:alice OR subject:meeting`（可能被误解） | `{from:alice subject:meeting}`（Gmail 标准格式） |
| 矛盾 query | 可能同时搜 inbox 和 sent | direction 字段统一控制 |
| 验证 | 无 | 语法规范化 + 日志记录 |

### 4.3 Searcher

**新文件：** `ask/searcher.py`

**核心原理：** 包装现有 `run_mail_scan()`，添加自适应行为。

```python
async def execute_search(
    mailbox: str,
    search_params: SearchParams,
    *,
    progress_callback=None,
) -> tuple[list[MessageLite], StageTrace]:
```

**自适应策略：**

```
尝试 1：原样搜索
  ↓ 0 结果
尝试 2：去掉人员过滤（只保留主题+时间+方向）
  ↓ 0 结果
尝试 3：广撒网（只保留方向+时间，不加任何内容过滤）
  ↓ 0 结果
返回空 + trace 中的警告信息
```

每次尝试都通过 `run_mail_scan()` 执行，该函数已内置 Gmail API 失败→回退本地缓存的逻辑。

**若无需自适应：** 只执行一次尝试，不浪费时间。

### 4.4 Relevance Filter

**新文件：** `ask/filter.py`

**核心原理：** 复用 Brief 管线的 Phase 1 批量分类。

```python
async def filter_candidates(
    messages: list[MessageLite],
    plan: AskPlan,
    mailbox: str,
    *,
    sampling_create_message=None,
) -> tuple[list[CandidateItem], list[dict]]:
```

**实现：**
1. 构建一个轻量 `MailStrategy`，其中 `llm_candidate_hints` 包含 `plan.analysis_focus`
2. 调用现有 `run_phase1_batch_classify()`
3. LLM 失败→回退到 `generate_candidates()`（纯规则分类）
4. 返回 `(candidates, low_value_items)`

**两层提示机制：**

Phase 1 过滤器的 prompt 中注入两类提示信息：

1. **`analysis_focus`**（来自 Planner，2-5 词）：告诉 LLM "用户关心什么大类"。如 `"job applications"`、`"security alerts"`、`"partnership inquiries"`。
2. **`relevance_hints`**（来自每个 topic 的 `relevance_hint`，1-2 句自然语言）：告诉 LLM "什么样的邮件算匹配"。如 `"unknown senders mentioning specific roles, typically with resume attachments"`。多个 topic 的 `relevance_hint` 合并注入。

**两者协作：** `analysis_focus` 设定整体方向，`relevance_hints` 提供具体的判断标准。例如用户问"帮我找找候选人的未回复邮件"：

- `analysis_focus`: `"unreplied job candidates"`
- `relevance_hints`: `"unknown senders discussing job applications, interviews, or position inquiries — especially those where the sender appears to be waiting for a response"`

Phase 1 LLM 看到这两者 + 邮件列表，判断每封邮件：(a) 是否匹配候选人概念，(b) 是否未回复，(c) 是否需要用户处理。

**为什么需要两层而不是只靠 search_terms：** 关键词初筛有天然的精度天花板。有些邮件包含 "resume" 但不是候选人（内部 HR 讨论），有些邮件是候选人但不含 "resume"（写的是 "I'm interested in this position"）。`relevance_hints` 弥补这个语义缺口——让 LLM 在关键词初筛的基础上做第二道语义把关。

**具体例子：**

| 用户请求 | analysis_focus | relevance_hint | 效果 |
|---------|---------------|----------------|------|
| "找找候选人的未回复邮件" | "unreplied job candidates" | "unknown senders about jobs or interviews, waiting for response" | 关键词 `{resume interview...}` 广搜 → LLM 从结果中筛出真正是候选人且未回复的 |
| "有没有合作相关的邮件" | "partnership discussions" | "collaboration proposals, demo offers, channel partnerships — sender is reaching out first" | 关键词 `{partnership proposal demo...}` 广搜 → LLM 过滤掉内部讨论、保留外部主动联络 |
| "等我回复的邮件" | "awaiting reply" | "someone explicitly asked a question, sent a proposal, or followed up — and the last message is from them, not me" | 无法用关键词搜 → 广搜 inbox → LLM 逐封判断"是不是在等我回复" |

**类比 Brief：** Brief 管线的 Phase 1 把 100 封邮件筛成 20 个候选，ASk 在此阶段实现同样的效果。原本 Answer LLM 要一次性处理 50 封，现在只需要处理 5-15 个候选。

### 4.5 Context Reader

**新文件：** `ask/context.py`

**核心原理：** 按需读取，按需查联系人。

```python
async def enrich_candidates_with_context(
    candidates: list[CandidateItem],
    messages: list[MessageLite],
    mailbox: str,
    *,
    sampling_create_message=None,
) -> list[EnrichedCandidate]:
```

**读取逻辑：**
1. 遍历候选，按其 `candidate.read_depth_required` 决定读多深
   - `header_only`：只用已有 headers（不需要额外读取）
   - `message_detail`：调用 `get_message_detail()` 读正文（上限 4000 字符）
   - `thread_context`：调用 `get_thread_context()` 读整个线程（上限 10 条，每条 2000 字符）
2. 收集所有候选中的唯一发件人地址
3. 对每个发件人调用 `retrieve_contact_context()` 查询联系人记忆
4. 调用 `format_contact_context_for_prompt()` 渲染为内联文本

**关键改变：** read_depth 不再由 Planner 盲选，而是由 Phase 1 分类器**按候选邮件**决定。例如：求职邮件标记为 `message_detail`（需要看正文判断质量），而促销邮件标记为 `header_only`（标题就够）。

### 4.6 Answer Generator

**新文件：** `ask/answer.py`

**核心原理：** 复用现有 `_EXECUTION_SYSTEM_PROMPT`，但只看到已过滤的候选。

```python
async def generate_answer(
    plan: AskPlan,
    candidates: list[EnrichedCandidate],
    mailbox: str,
    *,
    sampling_create_message=None,
) -> dict[str, Any]:
```

**系统 Prompt：** 复用现有 `_EXECUTION_SYSTEM_PROMPT`（pipeline.py:464-508）——固定输出 schema。

**用户 Prompt 结构：**

```
## Your Identity
You are Anna, executive assistant to {mailbox}.

## User request
{plan.user_request}

## Task
{plan.task_prompt}

## Contact context (when available)
{formatted contact memory context}

## Relevant emails ({len(candidates)} candidates from {total_scanned} scanned)
{rendered candidates}

## Important
- Base your answer ONLY on the emails provided
- If nothing matches, say so honestly
```

**渐进截断回退：** 复用现有 variants 机制。
```
尝试 1：full    — body 4000 字符，thread 2000 字符
尝试 2：compact — body 1600 字符，thread 900 字符
尝试 3：short   — body 800 字符，thread 500 字符
尝试 4：headers — 只看 headers，无正文
```

**对比旧版：**
- 旧版：Answer LLM 看到 50 封原始搜索结果
- 新版：Answer LLM 看到 5-15 封已过滤候选 + contact memory 上下文
- 结果：更少幻觉、更聚焦、更有上下文

### 4.7 Safety Guard

**新文件：** `ask/guard.py`

```python
def apply_ask_guards(
    result: dict[str, Any],
    candidates: list[EnrichedCandidate],
) -> AskResult:
```

**三道防线：**

1. **禁止操作检查：**
   - 扫描所有 `suggestion` 和 `draft` 文本
   - 检测模式："send"、"delete"、"forward"、"unsubscribe"、"archive all"
   - 替换为："[BLOCKED: manual review required]"
   - 记录到 `guard_actions_taken`

2. **ID 引用校验：**
   - 提取输出中所有 `message_id` 和 `thread_id`
   - 验证每个 ID 都在输入的候选邮件中存在
   - 剥离不存在的引用（LLM 幻觉的 ID）
   - 记录被剥离的 ID 数量

3. **内容净化：**
   - 扫描 `draft` 文本中的邮箱地址
   - 剥离输入邮件中未出现的邮箱地址（防止 LLM 编造联系人）
   - 警告但不过滤 suggestion 中的新邮箱（可能是合理的引用）

**对比 Brief guards：**
- Brief guards 操作的是 `JudgmentResult` 对象（类型化），修改 `recommended_actions`
- Ask guards 操作的是自由格式 dict（LLM 输出），进行文本扫描和替换

---

## 五、错误处理与降级策略

| 阶段 | 错误 | 处理方式 | 是否阻断 |
|------|------|---------|---------|
| 1. Planner | LLM 超时/不可用 | 规则回退：关键词提取→保守 AskPlan | 否 |
| 1. Planner | 无效 JSON | call_llm_json_safe 自动修复+重试 | 否（有回退） |
| 2. Query Builder | Contact lookup 失败 | 保留 name_hint→Gmail 部分名称匹配 | 否 |
| 2. Query Builder | 无法构建有效 query | 使用最宽泛的 query：`newer_than:30d -in:sent -in:draft` | 否 |
| 3. Searcher | Gmail API 失败 | run_mail_scan 已内置回退本地缓存 | 否 |
| 3. Searcher | 0 结果（含放宽后） | 返回 AskResult："No matching emails found" | **是**（无邮件） |
| 4. Filter | Phase 1 LLM 失败 | 回退到 generate_candidates() 纯规则 | 否 |
| 4. Filter | 无候选 | 返回 AskResult："Scanned N emails but none matched" | **是**（无可分析） |
| 5. Context | Body 读取失败 | 留空 body，继续 | 否 |
| 5. Context | Contact memory 失败 | 留空 contact_context，继续 | 否 |
| 6. Answer | LLM 全部截断失败 | 返回 fallback：标题 + 错误信息 | 否（返回可用结果） |
| 7. Guard | 检测到禁止操作 | 替换文本，记录 action | 否 |
| 7. Guard | 检测到无效 ID | 剥离引用，记录警告 | 否 |
| 编排器 | 任何未预期的异常 | 捕获、记录到 trace、返回部分结果（如有） | 否 |

**核心原则：** 只有两个"硬阻断"点——搜不到邮件（阶段 3 返回空）和没有相关候选（阶段 4 返回空）。其他所有错误都降级处理，尽量返回部分结果。

---

## 六、文件规划

### 新建文件（10 个）

```
inbox-tool/src/mail_agent/ask/
├── __init__.py          # 公开 API：run_ask_pipeline + 所有类型
├── types.py             # AskPlan, AskResult, SearchParams, EnrichedCandidate 等
├── planner.py           # 阶段 1：LLM 意图解析器（结构化输出）
├── query_builder.py     # 阶段 2：代码构建 Gmail query + contact lookup
├── searcher.py          # 阶段 3：自适应搜索（包装 run_mail_scan）
├── filter.py            # 阶段 4：相关性过滤（复用 phase1）
├── context.py           # 阶段 5：上下文读取 + contact memory
├── answer.py            # 阶段 6：答案生成 LLM
├── guard.py             # 阶段 7：安全守卫
└── pipeline.py          # 编排器：串联 7 个阶段
```

### 修改文件（4 个）

| 文件 | 改动 |
|------|------|
| `core/pipeline.py` | 新增 `run_ask_pipeline()`；`run_custom_scan()` 改为向后兼容包装 |
| `planning/custom.py` | 保留 `generate_custom_plan()` 作为旧版兼容 + 回退参考 |
| `domain/types.py` | `CustomScanPlan` 新增可选字段：`people`, `topics`, `timeframe`, `direction`, `goal`, `analysis_focus`（默认 None，向后兼容） |
| `anna_inbox_executa/main.py` | `_start_custom_scan_async()` 路由到新版 pipeline；旧版通过 feature flag 保留 |

### 复用文件（不改动）

| 文件 | 复用方式 |
|------|---------|
| `core/scan.py::run_mail_scan()` | 阶段 3 搜索执行 |
| `core/phase1.py::run_phase1_batch_classify()` | 阶段 4 相关性过滤 |
| `core/candidate.py::generate_candidates()` | 阶段 4 LLM 失败时的规则回退 |
| `core/guards.py` | 阶段 7 的守卫模式参考 |
| `llm_runtime/service.py::call_llm_json_safe()` | 阶段 1/4/6 的 LLM 调用 |
| `contact_memory/` | 阶段 2 人员查找 + 阶段 5 上下文检索 |
| `mail_providers/gmail/adapter.py` | 全部 Gmail API + 缓存操作 |
| `pipeline.py::_EXECUTION_SYSTEM_PROMPT` | 阶段 6 答案生成 schema |

---

## 七、向后兼容

### API 兼容

外部调用者（`start_custom_scan` 工具）不变：

```python
# main.py 入口
async def _start_custom_scan_async(...):
    if _use_ask_v2():  # 初期通过 feature flag 控制
        plan = await plan_ask_request(user_request, mailbox, ...)
        result = await run_ask_pipeline(user_request, mailbox, ...)
    else:
        plan = await generate_custom_plan(user_request, mailbox, ...)  # 旧版
        result = await run_custom_scan(plan, mailbox, ...)              # 旧版
```

`run_custom_scan()` 的签名不变，兼容已保存的 `CustomScanPlan`。

### 迁移路径

```
Phase 1：新代码侧挂 → feature flag 控制 → 默认走旧版
Phase 2：内部验证 → 对比新旧结果质量
Phase 3：切到新版默认 → 旧版保留为 ANNA_USE_ASK_LEGACY=true
Phase 4：稳定 2 周后 → 删除旧版路径
```

---

## 八、与 Brief 管线的对比

| 维度 | Brief（工作流范式） | Ask 新版（agent 范式） |
|------|-------------------|----------------------|
| 搜索 | 策略预设 query + 用户关键词追加 | Planner 提取参数→代码构建 query→自适应放宽 |
| 分类 | Phase 1 批量 LLM 分类 | Phase 1 批量 LLM 分类（复用同一个函数） |
| 判断 | Phase 2 逐候选 LLM 判断 | Answer LLM 统一分析（只针对已过滤候选） |
| 人员 | 无特殊处理 | Contact memory 解析名字→精确邮箱 |
| 防线 | apply_rule_guards() | apply_ask_guards()（ID 校验 + 禁止操作检测） |
| 联系人 | 判断阶段检索 contact memory | 过滤后、回答前检索 contact memory |
| 输出 | ActionPlan + Cards（结构化的可操作项） | AskResult（title + summary + sections） |
| 持久化 | 完整持久化（消息、卡片、扫描状态） | 运行记录持久化 |

---

## 九、验证计划

1. **单元测试**（`tests/test_ask_pipeline.py`）：
   - Query builder 对各种 AskPlan 参数组合的输出验证
   - Guard 的禁止操作检测验证
   - Guard 的 ID 校验验证

2. **集成测试**（`tests/test_storage_integration.py`）：
   - 端到端：Mock 用户请求 → 完整 7 阶段 → 验证输出结构
   - 0 结果场景：验证诚实输出和放宽行为
   - 降级场景：Mock Gmail API 失败→验证仍能返回可用结果

3. **人工验证**：
   - 用真实邮箱跑 5-10 个典型 Ask 请求
   - 对比新旧链路结果质量
   - 确认 contact memory 解析正确
