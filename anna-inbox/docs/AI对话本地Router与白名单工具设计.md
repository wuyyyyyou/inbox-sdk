# AI 对话本地 Router 与白名单工具设计

> 历史实现文档：App `2.1.5` / Tool `2.2.5` 起，AI 侧栏主路径改由 Host Agent Session 选型并调用显式白名单工具。本文件保留本地 Router 的安全约束、工具边界和兼容路径说明；当前主路径以 [AI 侧栏 Sampling → Sessions 改造方案](AI侧栏Sampling转Sessions改造方案.md) 为准。

## 1. 背景与结论

### 1.1 背景

历史版本的 AI 侧边栏由前端 `aiRoute.ts` 用正则将用户输入钉死为 `chat | scan | mail_context`，再分别调用：

- `chat` → Host iframe `llm.complete`
- `scan` → `start_custom_scan` / Ask 管线
- `mail_context` → `start_inbox_mail_prompt`

问题：

1. **路由规则写死在前端**：新增意图、多语言说法、多轮约束都要改正则与测试，维护成本高。
2. **选型与执行耦合在 UI**：前端既要猜意图，又要拼不同工具参数，容易与后端能力漂移。
3. **若改用 Host Agent Session 自选工具**：Host 可能带 `granted_tools:["*"]`，工具由 Host 执行，会绕过 Gmail 账号边界与显式用户操作守卫；且当前拓扑下 `rpc.stream` 常只到前端 bridge，Executa 收不到帧导致 60s 超时（已验证）。

### 1.2 结论

| 决策 | 选择 |
| --- | --- |
| 历史选型权 | **Executa 本地 Router**（用 Anna Sampling 做结构化选型） |
| 工具谁执行 | **仅本地白名单工具**（Gmail / 草稿 / Ask 搜索等） |
| Host Agent Session | **当前侧栏默认总控**；本地 Router 保留给详情/兼容路径 |
| 前端职责 | 透传用户话 + 只读 UI 上下文；**删除业务意图正则路由**（可留离线兜底） |
| LLM 通道 | 生产默认 `sampling/createMessage` + 累计 token 预算 |

一句话：**让 agent 自己选方法，但是在 Executa 里从白名单工具中选；不是让 Host Agent 自由调工具。**

本设计与 [AI 侧边栏意图路由](AI侧边栏意图路由优化方案.md)、[Ask 链路](Ask链路重构方案.md)、Anna 官方 Sampling 文档对齐；产品体验对标 [Shortwave AI Assistant](https://www.shortwave.com/docs/guides/ai-assistant/)（见 §1.3）。Host Agent Session 已成为侧栏默认路径，本地 Router 仅保留详情/兼容路径。实现以代码与 manifest 为最终事实来源。

### 1.3 对标 Shortwave：产品原则与差距

Shortwave 文档中的 AI Assistant 是「邮箱内嵌的对话式行政助理」，能力簇为：整理收件箱、写作与改稿、自然语言搜索与即时回答、理解/分析邮件、日历、Saved prompts、AI Memories、Integrations/MCP、AI Filters、以及后台自动化（Tasklet）。

对本方案**直接采纳**的体验原则：

| Shortwave 原则 | 本方案落地 |
| --- | --- |
| **Anywhere assistant**（侧栏/快捷键，全端一致） | 统一 `start_ai_turn`；Web 侧栏先做，不按意图拆入口 |
| **理解屏上上下文**（this email / these contacts） | 前端只传只读 `ui_context`（当前线程、多选、Compose 快照）；Router 解析指代，**不写前端意图正则** |
| **自然语言一条入口**（不背 Gmail 语法） | Router 选型 + 本地 `search_mail`；结果可带来源线程与「打开搜索/筛选」类 affordance |
| **写与改稿多轮 refinement** | `draft_reply` / `revise_draft` + 上轮 draft 注入；不依赖 Host Session 工具循环 |
| **整理收件箱：建议动作，用户逐步确认** | 新增 `propose_inbox_actions`：**只产出建议包**，归档/标已读/删除须用户确认后走既有 mutation API |
| **复合请求**（搜信 + 起草 + …） | Router `steps[]` 串本地工具；v1 不做自动发日历邀请 |
| **Saved prompts** | 已有/规划中的 Prompt 保存；提交时作为完整 user 文本或 `template_id` 注入，不单独路由 |
| **Memories（风格与行为偏好）** | v1：注入已有联系人记忆/用户偏好摘要到 Generator；v1.5：显式「记住…」写入本地/APS 记忆项 |
| **Integrations / MCP / Tasklet** | **非 v1**；预留 tool 扩展位，不把 Host MCP 工具直接挂进默认 Router |

**明确不对齐或延后**（避免假对标）：

| Shortwave 能力 | 本阶段 |
| --- | --- |
| Ghostwriter（从历史发信学文风） | 延后；可用 Memories + 可选 sent 样例摘要近似 |
| 日历创建/查忙闲 | 非 v1；可 `clarify` 或返回「暂不支持」 |
| 外链 Integrations / 自定义 MCP | 非 v1 |
| AI Filters 自动化规则 | 非本对话 Router 范围（属设置/规则引擎） |
| Tasklet 级 24/7 无人值守 | 非本侧栏范围；Brief/后台任务另案 |
| 搜索栏 `about:` 语法 | 侧栏用自然语言；Inbox 搜索栏可后续复用同一 `search_mail` |

**与 Shortwave「像 agent 一样做事」的关键差异（安全）：**

- Shortwave 产品侧可在自家栈内执行整理/集成；我们跑在 **Anna Executa + 用户 Gmail**，mutation 必须 **建议 → 用户确认 → 既有工具**，禁止 Router/Host 静默改邮箱。
- 选型在 **本地白名单**，不是 Host `granted_tools:["*"]` 自由工具环。

---

## 2. 目标与非目标

### 2.1 目标

1. 前端 AI 侧栏**不再维护** scan/chat/mail_context 业务路由表。
2. 用户一句话进入统一后端入口；由 **Router Sampling** 输出结构化 tool 计划（对标 Shortwave「直接说自然语言」）。
3. 所有邮件读写、搜索、候选排序、source guard **仍由本地代码执行**。
4. **屏上上下文优先**：打开邮件/多选/Compose 时，「这封」「这些」可解析；无上下文则 `clarify` 或全箱搜索（由 Router 判断）。
5. 覆盖 AI 对话测试用例中的理解 / 草稿 / 搜索 / 批量 / 多轮改写；并对齐 Shortwave 主路径：整理建议、写/改稿、搜索回答、分析提取。
6. 失败时**不回退成「本地邮件列表伪装 AI 回答」**。
7. 保留 60s 初等等待 + `run_id` 轮询、Sampling 60s 超时与每 invoke token 预算。
8. 日志仅含阶段、tool 名、耗时、token 数字、错误码；**禁止**记录 prompt、邮件正文、地址、凭据、UUID 全文。

### 2.2 非目标

1. 不把 Gmail 搜索/发送/删除注册为 Host Agent 可调用工具。
2. 不改变 Brief 状态机、联系人记忆 backfill 的既有短 slice 协议（可复用同一 budgeted sampler）。
3. 不在 v1 做真 token 流式 UI；进度仍用 run stage + 轮询。
4. 不把 `app_session_uuid` 或完整聊天历史写入 APS/KV。
5. **不自动**发送邮件或改变 Gmail 状态；状态变更须用户显式确认（对标 Shortwave「organize 时逐步可控」）。
6. v1 不做日历、外链 MCP、AI Filters 规则引擎、后台 Tasklet 式无人自动化。

---

## 3. 与 Sampling / Agent Session 的选型（文档对照）

| 维度 | Sampling | Host Agent Session (L2) | 本方案 |
| --- | --- | --- | --- |
| 语义 | 一次请求一次响应 | 多轮 + Host 可执行工具 | Router 与生成均用 **Sampling** |
| 协议 | v2 `sampling/createMessage` | v2.1 `agent/session.*` | 生产默认 v2 Sampling |
| 工具执行方 | 无 | Host | **Executa 本地** |
| 结构化输出 | `responseFormat` 可用 | frames / stream | Router 用 JSON object |
| 稳定性（现状） | Brief/Ask 已验证 | stream 常不到 Executa；工具上下文膨胀 | 避免 Session 总控 |

官方文档建议：仅需单轮文本时优先 Sampling；Agent 用于 stateful tool-using 负载。本产品「选方法」是**选本地工具枚举**，不是 Host 工具循环，故与 Sampling 一致。

---

## 4. 架构

### 4.1 总览

```text
AI Sidebar
  │  user_text + conversation_id + ui_context（只读）
  ▼
handle_ai_turn / start_ai_turn   （统一入口，可轮询 run）
  │
  ├─ 1. Router Sampling
  │     输出：{ tools[], params, use_current_thread, language, clarify? }
  │
  ├─ 2. 本地 Tool Runner（白名单，可串行 1～N 步）
  │     search_mail | read_current_thread | draft_reply | …
  │
  ├─ 3. Answer / Draft Sampling（按 tool 结果生成用户可见文本）
  │
  └─ 4. Guard + 结果物化（mail_links、禁止自动 mutation）
        │
        ▼
  前端：展示 summary / sections / draft / 步骤进度
```

### 4.2 分层职责

| 层 | 职责 |
| --- | --- |
| 前端 | 会话 UI、conversation_id、**只读** `ui_context`、轮询、插入草稿等显式动作 |
| Router | 将自然语言映射到**固定 tool 枚举** + 参数；可输出 `clarify` |
| Tool Runner | 执行 Gmail/缓存/Ask 搜索/读线程；写 run progress |
| Generator | 基于 tool 证据生成总结/草稿/分类等 |
| Guard | 校验 message_id/thread_id、剥离禁止动作文案、不发明来源 |

### 4.3 前端 `ui_context`（不是路由）

前端只传**硬事实**（对标 Shortwave「AI 理解当前屏幕」），不传「我认为是 scan」：

```json
{
  "conversation_id": "前端匿名会话 id",
  "mailbox": "当前主邮箱",
  "selected_mailboxes": ["..."],
  "display_range_days": 30,
  "max_messages": 100,
  "screen": {
    "view": "inbox|thread|compose|search|brief|other",
    "focus": "list|detail|composer"
  },
  "current_thread": {
    "kind": "thread|compose|none",
    "mailbox": "...",
    "message_id": "...",
    "thread_id": "...",
    "subject": "可选短主题",
    "snippet": "可选短摘要"
  },
  "selected_threads": [
    { "mailbox": "...", "message_id": "...", "thread_id": "...", "subject": "可选" }
  ],
  "last_draft": {
    "body": "若上一轮有 draft 或 Compose 正文需改写时附带",
    "source": "assistant_artifact|compose_box"
  },
  "saved_prompt_id": "可选，用户点了已保存 prompt",
  "language_hint": "可选"
}
```

约束：

- `current_thread` / `selected_threads` 仅当用户**确实打开或勾选**时填充；不得猜测（对应 “this email / these emails”）。
- 正文默认不进 Router prompt；需要读正文时由 tool 在本地拉取并截断。
- `last_draft` 仅作引用包裹，不可当作系统指令覆盖安全策略。
- `selected_threads` 上限建议 ≤ 20，避免 Router/证据爆炸；超出由后端截断并说明。

### 4.4 与现有入口的关系

| 现状 | 迁移后 |
| --- | --- |
| `decideAiRoute` → 三分支 | 删除业务路由；统一 `handle_ai_turn` |
| `start_custom_scan` | 变为内部 tool `search_mail` / `rank_inbox` 的实现，或 Router 选中后调用同一 Ask 管线 |
| `start_inbox_mail_prompt` | 变为 `summarize_thread` / `draft_reply` 等 tool |
| iframe `llm.complete` 闲聊 | tool `chat_general` 走 budgeted Sampling（或极轻量本地模板） |
| 保存/复用 Prompt | 仍为产品存储能力，与 Router 正交；调用时作为 user 文本前缀或 template_id |

过渡期允许：前端仍调旧方法名，后端内部转发到 `handle_ai_turn`，避免一次大爆炸。

---

## 5. 白名单工具

### 5.1 能力簇（对标 Shortwave 文档结构）

| Shortwave 能力簇 | 本方案 tool / 产品面 | v1 |
| --- | --- | --- |
| Organize inbox | `search_mail` + `propose_inbox_actions` + `rank_answer` | 是（建议包，不自动执行） |
| Write / improve draft | `draft_reply`、`revise_draft`、`summarize_then_draft`、`compose_new` | 是 |
| Search & answers | `search_mail`、`rank_answer`、`chat_general`（非邮箱常识） | 是 |
| Analyze | `summarize_thread`、`extract_*`、`classify_thread`、`judge_need_reply`、`translate_thread` | 是 |
| Calendar | — | 否 |
| Saved prompts | 设置页管理 + 输入建议 + `saved_prompt_id`/全文注入 | **v1 必做** |
| AI Memories | 设置/对话「记住…」+ Generator 注入偏好摘要 | **v1 必做** |
| Integrations / MCP | — | 否 |
| AI Filters / Tasklet | — | 否 |

### 5.2 枚举（v1）

| tool | 说明 | 主要测试用例 / Shortwave 话术 |
| --- | --- | --- |
| `chat_general` | 寒暄、能力说明、与邮箱无关的解释 | “Explain dark matter…” 类非邮箱问答 |
| `clarify` | 缺上下文时反问 | 指代不清、未打开邮件却说「这封」 |
| `summarize_thread` | 单封/线程/多选总结 | AI-001, 014, 020, 021；“action items in this thread” |
| `judge_need_reply` | 是否需要回复 | AI-002 |
| `extract_actions` | 待办、责任人、截止 | AI-003 |
| `extract_dates` | 日期/会议/deadline 及含义 | AI-004 |
| `classify_thread` | 单封分类 | AI-005 |
| `search_mail` | 收件箱级检索（本地 Gmail 查询） | AI-006, 007, 024, 025；“find emails from customers in December…” |
| `rank_answer` | 对检索结果排序/汇总/摘录回答 | “most important emails today”；可附带 open-search affordance |
| `propose_inbox_actions` | **仅建议** Mark done / archive / trash 等分组动作（确认卡片） | “organize my inbox”；**不执行 mutation** |
| `draft_reply` | 基于来信起草回复 | AI-008～011, 018 |
| `compose_new` | 基于主题/检索结果写新邮件大纲或正文 | “Search … and create a blog outline / FYI email” |
| `revise_draft` | 改写已有 draft / Compose 正文 | AI-013；“Shorten and make friendlier” |
| `summarize_then_draft` | 先总结再回复 | AI-012 |
| `batch_draft` | 多封简短 draft（一封一证据） | AI-015 |
| `batch_outreach` | 个性化 DM（变量隔离） | AI-016 |
| `translate_thread` | 翻译 | AI-019 |
| `remember_preference` | 将用户显式「记住…」写入本地/APS 偏好，供后续 turn 注入 | Shortwave AI Memories（**v1**） |

v1 **不**注册为 Router 可执行 mutation：`send_mail`、`delete_mail`、`archive_mail`、`mark_read`、`create_calendar_event`。  
`propose_inbox_actions` 只返回确认卡片载荷（§16.1.1）；用户 **Mark done** 后再调既有 Gmail 工具，**Skip** 无副作用。

### 5.3 工具契约原则

1. **输入**：结构化 params + 可选 `run_id` 进度回调；禁止把完整聊天历史当工具参数。
2. **输出**：`evidence`（可引用的 message/thread id 列表）+ `payload`（供 Generator 使用的截断文本）+ `metrics`（数量、耗时，无内容）+ 可选 `ui_affordances`（如 `open_search`、`apply_proposed_actions` 需确认）。
3. **截断**：候选默认 ≤ 8；正文摘录默认 ≤ 1200 字符；批量 draft 必须按封隔离 evidence。
4. **搜索时间范围**：默认 `ui_context.display_range_days` / Scan Plan；用户话术中明确时间时可覆盖，且不超过 `max_messages`。
5. **失败**：tool 失败返回结构化 error；最终用户可见文案为错误摘要，**禁止**用「本地找到的 N 封相关邮件」列表伪装成功回答。
6. **整理类**：任何 archive/done/trash 只能出现在确认卡片字段中，默认 `requires_user_confirmation=true`；支持 **Skip** 进入下一步叙事而不 mutation（见 §16.1.1）。

### 5.4 结果形态（对标 Shortwave 搜索/整理，`example/1–2.png`）

成功响应除自然语言 `summary` / `sections` 外，可携带：

| 字段 | 用途 |
| --- | --- |
| `mail_links` / sources | 可打开线程（现有 Ask 能力；图 2 重点未读列表） |
| `search_affordance` | 可选：检索范围说明 / 打开 Inbox 筛选（对标 “Search query limited by plan”） |
| `proposed_actions` | **Step 卡片**：step 标题、rationale、可勾选 items、primary_action、Skip 语义 |
| `artifacts` | `draft_reply` 等可插入 Compose 的正文 |

### 5.5 Router 输出 schema（示例）

```json
{
  "language": "zh",
  "use_current_thread": true,
  "clarify": null,
  "steps": [
    {
      "tool": "summarize_thread",
      "params": {
        "focus": ["purpose", "key_facts", "user_actions"]
      }
    }
  ]
}
```

批量搜索示例：

```json
{
  "language": "zh",
  "use_current_thread": false,
  "clarify": null,
  "steps": [
    {
      "tool": "search_mail",
      "params": {
        "goal": "find_emails",
        "topics": ["invoice", "receipt", "payment"],
        "timeframe_days": 30,
        "direction": "inbox"
      }
    },
    {
      "tool": "rank_answer",
      "params": {
        "goal": "priority_top_n",
        "n": 5
      }
    }
  ]
}
```

校验规则：

- `tool` 必须在白名单内，否则整单失败或降级 `clarify`。
- `steps` 长度 v1 建议 ≤ 3，防止单 invoke 耗尽 Sampling 调用次数（Host 每 invoke 通常有 maxCalls 上限）。
- `use_current_thread=true` 但 `ui_context.current_thread` 为空 → 强制 `clarify`，不得瞎搜。
- 复合话术（搜 + 写 + 提议整理）拆成多 step，但 **mutation 永不自动执行**。

**复合请求示例**（对标 Shortwave “Find emails … and draft an FYI”）：

```json
{
  "language": "en",
  "use_current_thread": false,
  "steps": [
    { "tool": "search_mail", "params": { "topics": ["downtime"], "timeframe_days": 7 } },
    { "tool": "compose_new", "params": { "goal": "fyi_summary_email" } }
  ]
}
```

日历类 step 在 v1 应被 Router 避免；若模型误输出，Runner 拒绝并返回「日历能力尚未接入」。

---

## 6. 单轮与多轮流程

### 6.1 单轮

1. 前端生成/复用 `conversation_id`，附带 `ui_context`（屏上状态），调用 `start_ai_turn`（`wait_timeout_seconds=60`）。
2. 后端写 run：`stage=routing`。
3. Router Sampling（小 max_tokens，JSON only）；可注入短 Memories 摘要（风格偏好，无邮件正文）。
4. 若 `clarify` 非空 → `done`，返回反问，不跑 Gmail。
5. 依次执行 steps，更新 `stage=search|read|draft|propose|answer` 等。
6. Generator Sampling（如需要）→ Guard → 附带 `mail_links` / `proposed_actions` / `artifacts` → `done`。
7. 超过 60s 未完成 → 返回 `running` + `run_id`，前端轮询（与现 Ask 一致）。

### 6.2 多轮（对标 Shortwave 改稿 follow-up）

- Registry：进程内 `conversation_id` → 最近 N 条**非敏感摘要**（上一 tool 名、是否成功、候选数量、上一 draft 是否存在、上一 search 意图摘要），**不**存邮件正文。
- 改写类：前端或上一轮结果带 `last_draft`；Router 倾向 `revise_draft`（“Shorten… / more professional…”）。
- 搜索追问（「只要最近五天」）：Router 选 `search_mail` 并合并约束；**不**在前端拼字符串规则。
- 整理建议确认：用户在 UI 点「应用」后走 mutation API，**不**再进 Router 当「默认发送」。

### 6.3 与 Ask 管线复用

`search_mail` + `rank_answer` 应复用现有 `run_ask_pipeline` / planner-search-answer 实现，避免两套 Gmail 查询。Router 只决定「要不要搜、搜什么意图」，查询构建与 broaden 策略仍属本地代码。

---

## 7. 超时、预算与稳定性

| 项 | 约定 |
| --- | --- |
| 前端 invoke | 统一入口约 120s（wait 60 + 余量） |
| `wait_timeout_seconds` | 默认 60，超时返回可轮询状态 |
| Sampling 单次 | 60s；每 invoke 累计 token 预算与现 Brief/Ask 一致（如 6000 总 / 4096 单次） |
| Router 调用 | 计 1 次 Sampling；steps 内生成再计 |
| Agent Session | 默认关闭；若实验开启须 `granted_tools` 为空，否则拒绝 |
| 取消 | 前端 abort 应调用 cancel run；停止后续 tool 与 Sampling |
| Ask 邮箱范围 | 仅使用 `ui_context.mailbox` 当前活动邮箱；`selected_mailboxes` 不参与检索 |
| 诊断 | 后台 `run_id` 轮询返回同一安全 diagnostics trace；失败 UI 仅展示阶段、耗时和错误类型 |

---

## 8. 安全边界

1. **事实边界**：回答中的 message_id / thread_id 必须来自本轮 tool evidence，经 source guard。
2. **禁止自动 mutation**：Router 不得选择发送/删除/已读等 tool（v1 不存在这些 tool）。
3. **Draft 引用**：上一版 draft 作为引用内容，防 prompt 注入覆盖系统策略。
4. **多邮箱**：Ask 仅使用 `ui_context.mailbox` 当前活动邮箱；其他邮箱的选择状态不得扩大检索范围。
5. **日志**：仅 tool 名、阶段、耗时、token、错误类型、fallback 原因码。

---

## 9. 前端变更要点

1. AI 发送路径改为单一 `client.startAiTurn` / `getMailAgentRun`。
2. `aiRoute.ts`：删除或降级为「无后端时的离线提示」；**不以**其结果选择工具。
3. 每次发送组装 **屏上 `ui_context`**（当前线程、多选、Compose、saved prompt），对标 Shortwave 上下文感知。
4. 进度文案映射后端 stage（理解请求 / 检索 / 阅读 / 生成 / 建议整理），与现 Ask 步骤风格一致。
5. 结果渲染：sections、mail_links、draft artifact；**新增**整理确认卡片（勾选列表 + `Skip` + `Mark done` 下拉，见 §16.1.1）。
6. Compose「插入草稿 / Improve draft」：可走同一 `start_ai_turn` + `revise_draft`；**发送**仍须用户点击。
7. Saved prompts：输入框 **↑** 打开列表（图 3、4）；设置页 AI Personalization（图 5）管理 prompts 与 Memory。
8. Memory：支持对话内 `Remember to ...` / 「记住…」；设置页可查看；每 turn 注入摘要。

---

## 10. 后端变更要点

| 区域 | 内容 |
| --- | --- |
| manifest | 新增 `start_ai_turn` / `continue_ai_turn`（或扩展现有 custom scan 协议）；声明 `llm.sample` |
| `sampling_tools` | 统一 budgeted Sampling；Ask 默认不走 Agent Session |
| 新模块建议 | `mail_agent/ai_turn/router.py`、`tools.py`、`runner.py` |
| Ask | `search_mail` 复用 `run_ask_pipeline` |
| 邮件上下文 | 复用 inbox mail prompt / thread 读取逻辑 |
| 测试 | Router schema 校验、白名单拒绝、无 thread 时 clarify、搜索不串证据、失败不本地列表伪装 |

---

## 11. 与测试用例 & Shortwave 话术的映射

| 用例 / 话术簇 | Router 倾向 steps |
| --- | --- |
| AI-001～005, 014, 020, 021；“action items in this thread” | `summarize_thread` / `classify_thread` / `extract_*` / `judge_need_reply` |
| AI-006, 007, 024, 025；“most important emails today” | `search_mail` → `rank_answer` |
| “organize my inbox / archive low-priority…” | `search_mail` → `propose_inbox_actions`（**仅建议**） |
| AI-008～011, 018；“Reply with … bullets” | `draft_reply` |
| AI-012 | `summarize_then_draft` |
| AI-013；“Shorten / friendlier” | `revise_draft` |
| “Search product updates and draft outline” | `search_mail` → `compose_new` |
| AI-015, 016 | `batch_draft` / `batch_outreach`（按封隔离） |
| AI-019；“Translate this thread” | `translate_thread` |
| AI-022, 023；Saved prompts | 存储 + 注入，不新增 Host 能力 |
| “Remember I prefer short replies…” | `remember_preference` + 设置页 Memories（v1） |
| 日历 / “schedule a meeting…” | v1 明确不支持或 clarify |

验收共性：

- 不编造邮件中不存在的事实。
- 搜索类结果可打开真实线程。
- 批量不串上下文。
- 长邮件不崩溃、不 frame 溢出（截断证据）。
- **整理类必须二次确认**，不自动 archive/delete。

---

## 12. 实施阶段

### 阶段 A — 协议与 Router（优先，对齐 Shortwave「一条入口 + 屏上上下文」）

1. 定义 `start_ai_turn` 参数（含 `ui_context`）与 run 状态字段。
2. 实现 Router Sampling + schema 校验 + `clarify`。
3. 先接入：`chat_general`、`search_mail`（复用 Ask）、`summarize_thread`、`rank_answer`。
4. 前端单一发送路径 + 组装 `ui_context`；旧 `aiRoute` 旁路开关。

**阶段 A 实现状态（2026-07-14）：已落地**

| 项 | 位置 |
| --- | --- |
| 工具协议 | `inbox-tool/manifest.json`、`common.py` DEFAULT_MANIFEST、`dispatcher.py` |
| Router / Runner | `mail_agent/ai_turn/router.py`、`runner.py` |
| 可轮询入口 | `anna_inbox_executa/ai_turn_flow.py` → `start_ai_turn` |
| 前端 facade | `mailAgentClient.startAiTurn` |
| 侧栏默认路径 | `useAppController.sendAiChatMessage` 优先 `startAiTurn`；`localStorage anna-inbox-use-ai-turn=0` 回退 `aiRoute` |
| 测试 | `tests/test_ai_turn_router.py`、前端 client/controller 静态用例 |

### 阶段 B — 写稿、多轮、整理建议、Saved prompts / Memories

1. `draft_reply`、`revise_draft`、`summarize_then_draft`、`compose_new`。
2. `propose_inbox_actions` + 前端确认条（见 §16.1 已确认动作集）。
3. conversation 摘要 registry。
4. **Saved prompts（v1）**：设置页增删改 + 输入框建议 + 提交注入。
5. **AI Memories（v1）**：设置页/对话写入 + 每 turn 注入短偏好摘要（无邮件正文）。
6. 去掉本地邮件列表成功伪装；统一错误摘要。

**阶段 B 实现状态（2026-07-15）：已落地**

| 项 | 位置 |
| --- | --- |
| 白名单扩展 | `mail_agent/ai_turn/router.py`（含 draft / revise / propose / remember） |
| 写稿 / 整理 / 记忆工具 | `mail_agent/ai_turn/tools.py` |
| 多轮 registry | `mail_agent/ai_turn/registry.py`（进程内，无正文） |
| Saved prompts / Memory 存储 | `mail_agent/ai_turn/personalization.py`（APS/local KV） |
| 确认执行 | `apply_proposed_actions`（manifest + dispatcher）；`mark_done`/`archive` → mark_read + 前端本地 Done |
| 前端确认卡 / ↑ prompts / 设置 AI Personalization | `HomeView`、`SettingsView`、`useAppController`、`mailAgentClient` |
| 测试 | `tests/test_ai_turn_phase_b.py`、既有 `test_ai_turn_router.py` |

`mark_done` 与 Inbox Done 对齐：Gmail `batch_mark_read` + 前端 workflow `done` 标记；**不**做 Gmail archive 标签流水线。

### 阶段 C — 批量与清理

1. `batch_draft` / `batch_outreach`。
2. 删除前端业务路由与死代码；回写旧路由文档。
3. 回归：内部测试用例 + Shortwave 风格话术清单。
4. 对外文案按 §16.1：称「AI 助理」，并写明无日历/无自动删除/整理须确认。

**阶段 C 实现状态（2026-07-15）：已落地**

| 项 | 位置 |
| --- | --- |
| `batch_draft` / `batch_outreach` | `mail_agent/ai_turn/tools.py` + Router 白名单 |
| 多选 `selected_threads` | `buildAiTurnUiContext` + 收件箱列表勾选 |
| 多 artifact 结果 | `ai_turn_flow` / `useAppController` / `HomeView` |
| 删除前端业务路由 | 移除 `decideAiRoute` 旁路与 feature flag |
| 旧路由文档 | `AI侧边栏意图路由优化方案.md` 标注「已由本地 Router 取代」 |
| 测试 | `tests/test_ai_turn_phase_c.py` |
| 文案 | 侧栏空态 / chat_general 能力边界（AI 助理） |

### 明确不做（本设计周期）

- Host Agent Session 作为默认总控。
- 前端继续扩充正则意图表。
- 日历、MCP 集成、Tasklet 级后台自动化、静默 mutation。

---

## 13. 验收标准

1. 用户仅输入自然语言 + 打开/多选邮件上下文时，**无需**前端判断 scan/chat 即可完成理解/搜索/草稿类 P0 用例。
2. 「这封邮件」在打开详情时生效；未打开时 `clarify` 或明确改为全箱搜索，不静默猜错线程。
3. Router 输出非法 tool 名时被拒绝，不执行 Gmail。
4. 搜索与回答中的邮件链接均通过 source guard。
5. `propose_inbox_actions` 不改变 Gmail 状态，直至用户确认。
6. Sampling 失败返回明确失败摘要，不展示「本地找到的 N 封邮件」充数。
7. 初等 60s + 轮询行为与现 Ask 一致；单次 Sampling 60s 预算仍生效。
8. 日志无邮件正文、地址、完整 prompt、凭据。
9. 与 [AI 侧边栏意图路由](AI侧边栏意图路由优化方案.md) 冲突处以**本文档迁移完成后的实现**为准，并回写旧文档「已由本地 Router 取代」。
10. 产品话术上可对标 Shortwave 主路径（整理建议、写/改、搜、分析），且文档中延后能力不在 UI 中虚假宣传。

---

## 14. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| Router 选错 tool | 小枚举 + JSON schema；代码校验「要 thread / 要搜索 / 禁止 mutation tool」 |
| 对标 Shortwave 期望过高 | 文档与 UI 能力边界写清；日历/集成标 Coming later |
| 整理建议被当成已执行 | 文案 + 确认条；`requires_user_confirmation` 强制 |
| 单 invoke Sampling 次数超 cap | steps ≤ 3；批量 draft 可拆 run 或短 continue |
| 延迟变长（Router + 执行 + 生成） | Router max_tokens 小；搜索复用 Ask；60s 早返回轮询 |
| 与旧入口双轨 | 特性开关；过渡期后端转发；阶段 C 删前端路由 |
| 误开 Session | 默认 Sampling；Session 需显式 flag 且 granted_tools 为空 |

---

## 15. 文档关系

| 文档 | 关系 |
| --- | --- |
| [Shortwave AI Assistant](https://www.shortwave.com/docs/guides/ai-assistant/) | 产品对标与话术来源（非协议实现） |
| [AI 侧边栏意图路由](AI侧边栏意图路由优化方案.md) | 描述**当前**前端路由；迁移完成后标注废弃业务路由部分 |
| [Ask 链路](Ask链路重构方案.md) | `search_mail` / 回答 guard 的实现基线 |
| [Anna Sampling 与 Brief](Anna-Sampling-Brief全链路分析.md) | 预算、超时、invoke 绑定 |
| [联系人记忆](联系人记忆架构设计.md) | Memories / 偏好注入可复用存储思路 |

---

## 16. 实现前对齐清单

### 16.1 已确认（2026-07-14，产品；对照仓库 `example/1.png`–`5.png` Shortwave 截图）

| # | 议题 | 结论 |
| --- | --- | --- |
| 6 | 整理确认 UI（图 1、2） | 见下方 **§16.1.1** |
| 7 | Saved prompts / Memory（图 3、4、5） | **v1 必做**，见 **§16.1.2** |
| 8 | 对外文案 | **是**：称「AI 助理」；边界：自然语言整理/搜索/写改稿/分析；**无**日历自动约、**无**静默 mutation、**无** Host 自由工具环 |

#### 16.1.1 整理收件箱（对标 `example/1.png`、`2.png`）

用户话术示例：`Organize my inbox` / 「帮我整理收件箱」。

**交互流（逐步、可跳过）：**

1. 助理先说明范围（如当前 split / 未读），并执行检索；可展示「找到 N 个线程」与检索范围提示（对标 “Search query limited by plan” → 我方用 Scan Plan / `display_range_days`）。
2. 按 **Step N** 给出一类整理建议（例：Step 1 Remove low-quality / 信息类邮件），自然语言说明**将要**做什么，**尚未**改 Gmail。
3. 结果区出现 **确认卡片**：
   - 标题如 `Mark N threads as done?` / 「将 N 封标为已处理？」
   - **可勾选线程列表**（默认全选，用户可取消）
   - 每行可点击打开线程（蓝链 + 图标）
   - 底部：`Skip` | 主按钮 **`Mark done`（可下拉换动作）**
4. 用户点 **Mark done** → 仅对勾选项调用既有 mutation（我方映射见下表）；点 **Skip** → **不** mutation，助理进入下一焦点（图 2：保留收件箱、改讲 key unread / 行动项，并可追问 Next step）。
5. 多轮：Skip 后继续对话，不重开整次 organize，除非用户新开请求。

**主按钮动作集合（v1，对齐截图「Mark done」语义）：**

| 建议 action id | 用户可见（EN 参考） | 本地执行（须已有或补齐 API） |
| --- | --- | --- |
| `mark_done` | Mark done | 归档和/或移出收件箱（与产品「Done」语义一致；Gmail 侧可用 archive / 自定义 label，实现时与 Inbox Done 行为对齐） |
| `archive` | Archive | 归档 |
| `trash` | Move to trash | 进垃圾箱（下拉次要项，需确认文案） |

v1 **不做**截图未强调的自动打复杂 label 流水线；若 Router 建议 label，可先文案建议，mutation 仍走用户确认卡片。  
**禁止**：未点主按钮就 archive/trash；Skip 不得产生副作用。

**`propose_inbox_actions` 结果字段（对齐 UI）：**

```json
{
  "step_index": 1,
  "step_title": "Remove low-quality emails",
  "rationale": "These are informational trial/registration notices.",
  "primary_action": "mark_done",
  "allowed_actions": ["mark_done", "archive", "trash"],
  "items": [
    {
      "mailbox": "...",
      "message_id": "...",
      "thread_id": "...",
      "subject": "...",
      "default_selected": true
    }
  ],
  "requires_user_confirmation": true
}
```

前端：勾选变更只改本地 UI state；提交时 `apply_proposed_actions({ action, items[] })` 调既有工具。

#### 16.1.2 Saved prompts 与 Memory（对标 `example/3.png`、`4.png`、`5.png`）

| 截图 | 产品要求 | 本方案 |
| --- | --- | --- |
| 图 3 输入框 | 占位「Press ↑ for saved prompts」；附件/工具图标；模型档位可选 | v1：输入区支持 **↑ 打开 Saved prompts**；模型档位可后续接 Host；附件非本 Router 必做 |
| 图 4 面板 | 「Saved prompts」列表、搜索过滤、「No saved prompts」、设置齿轮、**+** 新建 | v1：侧栏/浮层列表 + 过滤 + 空态 + 跳转管理；选中后填入输入框（可 autosubmit 配置项二期） |
| 图 5 设置 | **AI Personalization**：Saved prompts（+ Add）；**Memory** 说明可通过助理输入 `Remember to ...` / 「记住…」创建，链到对话 | v1：设置页同构两块；`remember_preference` tool + 设置页 CRUD；每 turn 注入短 Memory 摘要 |

存储：Saved prompts 与 Memory 条目走 APS/local（与现 storage 约定一致），**不**写邮件正文；Memory 仅偏好/行为短句。

### 16.2 仍待确认（实现前）

1. 统一入口工具名：`start_ai_turn` 是否与现 `start_custom_scan` 合并或并存一个版本周期（**默认并存**）。
2. v1 是否裁剪 `batch_outreach`（`batch_draft` 可先做）。
3. 闲聊是否必须走 Sampling，或允许极短本地模板以省 token。
4. 前端删除 `aiRoute` 的时间点（阶段 A 旁路 vs 阶段 C 删除）。
5. 失败文案与 i18n 是否与现侧栏 toast 统一。
6. `mark_done` 与现 Inbox「Done」标签/归档行为的精确映射（工程实现时与 `HomeView` Done 逻辑对齐一次）。

### 16.3 开工条件

- §16.1（含 16.1.1 / 16.1.2）已按 `example/*.png` 锁定。
- §16.2 未全部确认前，可先开 **阶段 A**（`start_ai_turn` + Router + search/summarize）。
- 入口名默认：**新建 `start_ai_turn`，旧入口并存一个版本**。

按阶段 A → B → C 实现；阶段 B 必须交付确认卡片（Skip / Mark done）与 Saved prompts + Memory。
