# AI 侧边栏意图路由优化方案

> 状态：待审批。本文只记录优化方案；审批前不进行代码修改。
> 背景问题：左侧 Anna 统一输入框无法稳定区分“普通聊天/继续上一轮”和“发起邮箱检索请求”。典型误判是用户在收到 draft artifact 后输入 `Change it`，系统把它当作 custom scan，触发 focused inbox query，而不是对上一封草稿进行改写。

## 1. 当前问题

左侧 Anna 侧边栏现在同时承担三类能力：

- 普通聊天：寒暄、能力询问、解释上一轮结果。
- 邮件上下文任务：基于当前打开 thread 写 draft、改 draft、回答用户追问。
- Ask / custom scan：搜索、整理、总结 inbox 中的邮件。

当前路由策略偏向“默认扫描邮箱”：

- `shouldRouteToChat()` 只把非常明确的寒暄/帮助模式识别为 chat。
- 只要输入不命中 chat pattern，就进入 custom scan。
- 对短句、指代词和上一轮 artifact 没有上下文判断。

这会导致：

- `Change it`、`Make it shorter`、`Can you adjust this?` 被当作 inbox search。
- 用户以为自己在和 Anna 连续对话，实际系统新开了一次邮件扫描。
- UI 展示 `Searched 1 focused inbox query`，但用户本意是修改刚生成的 draft。
- 后续 Ask planner 再怎么优化，也无法弥补入口路由错误。

## 2. 设计目标

目标不是让一个 LLM “猜得更聪明”，而是让前端先做可解释的意图路由：

- 明确邮箱检索时才触发 Ask / custom scan。
- 明确承接上一轮回答、artifact 或当前打开 thread 时，优先走 chat / mail-context prompt。
- 对短指令、指代词和上下文依赖表达保持保守，不直接扫描邮箱。
- 不确定时先澄清，而不是做高成本、低相关的 inbox scan。
- 第一阶段尽量不改 Executa 工具契约，不修改 Python 后端协议。

## 3. 意图区分模型

建议把左侧输入先分成四类，而不是二分为 chat / scan：

| 路由类型 | 含义 | 典型输入 | 执行动作 |
| --- | --- | --- | --- |
| `chat` | 普通对话或解释上一轮 | `hi`、`what can you do?`、`这是什么意思？` | 调用普通 chat completion |
| `mail_context` | 针对当前 thread 或上一轮 draft artifact 的追问、改写、插入 | `Change it`、`make it warmer`、`write a first draft reply` | 调用 `submitMailContextPrompt` / `start_inbox_mail_prompt` |
| `scan` | 明确要搜索、整理、汇总邮箱 | `Find urgent emails`、`What needs my reply?`、`找一下 Stripe 发票` | 调用 `startCustomScan` |
| `clarify` | 无法可靠判断 | `Do it`、`那这个呢`、`帮我弄一下` | 展示澄清选择 UI，不扫描 |

## 4. 路由判断规则

### 4.1 明确 scan 信号

只有出现以下信号之一时，才直接进入 Ask / custom scan：

- 邮箱范围词：`email`、`mail`、`inbox`、`邮件`、`邮箱`、`收件箱`。
- 搜索/整理词：`find`、`search`、`look for`、`summarize emails`、`organize`、`scan`、`找`、`搜索`、`汇总`、`整理`。
- 邮件状态词：`unread`、`needs reply`、`urgent`、`attachment`、`invoice`、`未读`、`待回复`、`紧急`、`附件`、`发票`。
- 发件人/主题/时间等检索约束明显存在，例如 `from Sarah`、`about Demo Day`、`last week`，且不是承接上一轮 artifact 的改写表达。

### 4.2 明确 mail_context 信号

如果存在当前打开邮件 thread，或上一轮 assistant message 含 `draft_reply` artifact / `mailContext`，以下输入优先进入 `mail_context`：

- 改稿动词：`change`、`revise`、`rewrite`、`edit`、`adjust`、`make it`、`shorter`、`warmer`、`more direct`、`修改`、`改一下`、`润色`、`短一点`、`更礼貌`。
- 指代词：`it`、`this`、`that`、`这个`、`它`、`上面`、`刚才那封`。
- 上下文动作：`insert it`、`copy it`、`use this draft`、`帮我回`、`写回复`，且当前页面有 thread context。
- 短句且依赖上一轮：1-5 个词的命令式输入，如 `Change it`、`Try again`、`Too long`。

### 4.3 明确 chat 信号

以下输入继续走普通 chat：

- 寒暄：`hi`、`hello`、`你好`。
- 能力询问：`what can you do?`、`你能做什么？`。
- 对 Anna 输出的解释性追问，但没有要求读邮箱或改当前邮件，例如 `why did you say that?`。

### 4.4 clarify 信号

如果输入很短、含指代词，但当前没有可用 mail context / artifact，也没有明确 scan 信号，则不要扫描邮箱。返回一个可交互的澄清卡片，让用户选择或补充意图：

```text
Do you want me to revise the current draft, or search your inbox?
```

中文输入可返回：

```text
你想让我修改当前草稿，还是搜索邮箱？
```

澄清卡片建议包含：

- 快捷选择：`Revise current draft`、`Search inbox`、`Just chat`。
- 自由输入：允许用户补充更明确的说明，例如 `Search my inbox for Sarah's invite`。
- 取消入口：用户可以关闭澄清，不触发任何工具。

用户点击选择后，前端再执行对应 route：

- `Revise current draft`：如果仍没有可用 mail context，则提示用户先打开邮件或选择一封邮件；不直接调用 Ask。
- `Search inbox`：把原始输入或用户补充文本作为 Ask request，进入 `scan`。
- `Just chat`：把原始输入作为普通聊天，进入 `chat`。

## 5. 第一阶段实施方案：前端确定性路由

第一阶段只改前端路由，不改 Executa 工具契约。

### 5.1 新增路由 helper

建议新增纯函数，例如：

```ts
type AiRouteKind = "chat" | "mail_context" | "scan" | "clarify";

interface AiRouteDecision {
  kind: AiRouteKind;
  reason: string;
  confidence: "high" | "medium" | "low";
}
```

输入参数包括：

- 当前用户输入。
- 当前 `aiChatMessages`。
- 是否有最后一条 `draft_reply` artifact。
- 是否有最后一条 `mailContext`。
- 当前是否打开 mail detail drawer 及其 `mailbox/thread_id/latest_message_id`。

### 5.2 替换 `shouldRouteToChat`

现有 `shouldRouteToChat(input)` 是二分类，建议替换为：

```ts
decideAiRoute(input, context): AiRouteDecision
```

`sendAiChatMessage()` 根据结果分发：

- `chat`：继续调用 `completeAiChat()`。
- `mail_context`：调用现有 `submitMailContextPrompt()`，带上已有 `mailContext` 和 `expectedArtifact: "draft_reply"`。
- `scan`：走现有 `startCustomScan()` 分支。
- `clarify`：写入一条带交互控件的 assistant clarification message；此时不调用 LLM，不调用工具，等待用户选择。

clarification message 建议新增结构：

```ts
interface AiClarificationAction {
  id: "mail_context" | "scan" | "chat";
  label: string;
}

interface AiClarificationPayload {
  original_input: string;
  question: string;
  actions: AiClarificationAction[];
  freeform_enabled: boolean;
}
```

这个 payload 只存在前端会话状态中，不需要持久化到 storage，也不传给 Executa。

### 5.3 澄清选择交互

当用户对 clarification message 做选择时：

1. 点击 `Search inbox`：
   - 若自由输入为空，使用 `original_input`。
   - 若自由输入非空，使用自由输入作为 Ask request。
   - 进入 `scan`，并在聊天记录中追加用户选择后的可见消息。
2. 点击 `Just chat`：
   - 使用 `original_input` 或自由输入。
   - 进入 `chat`。
3. 点击 `Revise current draft`：
   - 重新检查当前是否有 `mailContext` / draft artifact / 打开的 thread。
   - 有上下文时进入 `mail_context`。
   - 无上下文时显示二级提示：`Open an email first so I know what to revise.`，不扫描邮箱。
4. 用户关闭澄清卡片：
   - 标记该 clarification 为 dismissed。
   - 不触发任何工具。

澄清卡片需要禁用重复提交：用户一旦选择某个 action，该卡片进入 resolved 状态，按钮不可再次触发。

### 5.4 上下文来源优先级

当用户输入需要 mail context 时，按以下顺序取上下文：

1. 当前最后一条 assistant message 的 `mailContext` + `draft_reply` artifact。
2. 当前打开 mail detail drawer 的 thread context。
3. 当前聊天历史中最近一个 `mailContext`。
4. 都没有时进入 `clarify`。

这样 `Change it` 会修改刚才生成的 draft，而不是搜索邮箱。

### 5.5 UI 反馈

建议轻量改变 pending / clarification 文案：

- `chat`：`thinking`
- `mail_context`：`Updating the draft...`
- `scan`：`I'll search your inbox for emails that are relevant to this question.`
- `clarify`：立即显示澄清卡片，不显示 `Thinking`，不进入 loading

同时保留现有 `New chat` 行为：点击 New chat 后清空 `aiChatMessages`，短指令不再继承旧 artifact。

## 6. 第二阶段：路由可观测与少量 LLM 辅助

如果第一阶段规则仍覆盖不足，再考虑增加轻量 LLM router，但不建议作为首版入口。

### 6.1 可观测字段

在开发日志或前端 debug trace 中记录：

- `route.kind`
- `route.reason`
- 是否存在 `mailContext`
- 是否存在 `draft_reply` artifact
- 是否触发 custom scan

注意不要记录邮件正文、OAuth token、完整 credential context。

### 6.2 LLM router 的边界

LLM router 只用于 `clarify` 和低置信度场景，不参与高置信度规则：

- 高置信度 scan：直接 scan。
- 高置信度 mail_context：直接 mail_context。
- 高置信度 chat：直接 chat。
- 低置信度：可以问 LLM “该输入应归类为 chat/mail_context/scan/clarify”，但结果仍需经过 allowlist guard。

## 7. 示例路由表

| 输入 | 上下文 | 期望路由 | 说明 |
| --- | --- | --- | --- |
| `Change it` | 上一轮有 draft artifact | `mail_context` | 修改刚生成的草稿 |
| `Make it shorter` | 上一轮有 draft artifact | `mail_context` | 典型改稿 |
| `Try again` | 上一轮有 mailContext | `mail_context` | 重新生成当前 thread 回复 |
| `Change it` | 无 thread、无 artifact | `clarify` | 不应扫描邮箱 |
| `Find urgent emails` | 任意 | `scan` | 明确邮箱检索 |
| `What needs my reply?` | 任意 | `scan` | 明确待回复邮件 |
| `写一封回复` | 当前打开 thread | `mail_context` | 当前邮件上下文任务 |
| `帮我找一下 Stripe 发票` | 任意 | `scan` | 明确搜索邮箱 |
| `你好` | 任意 | `chat` | 寒暄 |
| `这是什么意思？` | 上一轮有 assistant answer | `chat` | 解释上一轮，不扫描 |

## 8. Ask 结果快速跳转链接

> 新增待审批能力：AI 在回答“找邮件 / 汇总邮件 / 有哪些紧急邮件”时，可以在结果中生成可点击的邮件引用。用户点击引用后，直接跳转到对应邮件详情抽屉，而不是只能看到邮件标题文本。

### 8.1 用户体验目标

参考截图中的形态，Ask 回答可以按主题聚合，并在每个 bullet 里展示多个可点击邮件标题：

```text
I found 15 emails marked as important...

- Collaborations with content creators
  [Feature Invite: Meet the ...] [Demo Invite: A Desktop AI ...]
  Multiple outreach and follow-up emails to YouTube creators...
- Anna Hackathon invitations
  [Anna Hackathon invite for...] [Invitation: Anna Hackatho...]
```

点击任意邮件标题后：

- 右侧打开对应邮件详情抽屉。
- 抽屉展示完整 thread，并定位到对应 message / latest message。
- 左侧 Anna 对话不丢失，用户可以继续追问或让 Anna 起草回复。
- 如果该邮件不在当前 Inbox 列表可见范围内，也应能通过 `mailbox + thread_id + message_id` 打开详情，而不是要求用户先手动搜索列表。

### 8.2 数据结构建议

现有 `CustomRunResultItem` 已有 `mailbox`、`message_id`、`thread_id`，但它只适合“一个 item 对应一封邮件”。截图里的体验更像“一个分组 item 下有多封邮件链接”。建议向后兼容地增加可选 `mail_links`：

```ts
interface AskMailLink {
  label: string;        // 展示用短标题，通常来自 subject
  mailbox: string;
  thread_id: string;
  message_id: string;
  from?: string;
  date?: string;
  snippet?: string;
}

interface CustomRunResultItem {
  subject?: string;
  context?: string;
  suggestion?: string;
  draft?: string;
  reply_gaps?: ReplyGaps;
  mailbox?: string;
  message_id?: string;
  thread_id?: string;
  from?: string;
  mail_links?: AskMailLink[];
}
```

兼容规则：

- 如果 `mail_links` 存在，前端优先渲染多个链接。
- 如果没有 `mail_links`，但 item 自身有 `mailbox + thread_id/message_id`，则把 item title 渲染为单个可点击链接。
- 如果缺少可校验 ID，只渲染普通文本，不生成链接。

### 8.3 后端 / LLM 输出约束

Ask answer prompt 需要要求模型在引用邮件时输出结构化引用，而不是在自然语言里拼 URL：

- 每个链接必须来自候选邮件集合。
- 每个链接必须包含 `mailbox`、`thread_id`、`message_id`。
- `label` 只能来自邮件 subject 的简短截断，不得编造。
- 同一 thread 在同一分组内只保留一个链接，优先 latest / most relevant message。
- 每组最多展示 3-5 个链接，避免结果变成一整页标题堆叠。

Ask guard 需要延续现有 ID 校验思路：

- `mail_links[].message_id` / `thread_id` 必须能在本次候选邮件或可读 thread 中找到。
- `mail_links[].mailbox` 必须等于候选邮件所属 mailbox。
- 校验失败的 link 直接剥离，不让前端渲染。
- 不允许 LLM 输出 `href`、外部 URL 或任意 deep link 字符串；前端只接受结构化对象。

### 8.4 前端渲染与跳转

`AiAssistantMessage` 渲染 Ask result 时：

- `mail_links` 渲染成紧凑的 link/chip/button，文本使用 `label`。
- hover tooltip 可展示 sender/date/snippet。
- 点击 link 调用统一的本地导航函数，例如：

```ts
openMailDetailFromAskLink({
  mailbox,
  threadId,
  messageId,
});
```

跳转行为建议：

1. 如果当前 mailbox 不同，先切到 link 指定 mailbox。
2. 如果对应 message 已在 `inboxMessages` / `inboxSnapshotMessages` / saved flags 中，复用现有 `openMessageDetail()`。
3. 如果列表里没有该 message，使用 `get_inbox_thread_page(mailbox, thread_id, { anchorMessageId: message_id })` 拉取 thread，并用轻量 placeholder 打开 Drawer。
4. Drawer 打开后由现有 thread page 逻辑补齐完整正文、附件和 AI thread assist。

### 8.5 与意图路由的关系

这个能力属于 `scan` 路由的输出增强，不改变前面的路由决策：

- `Find urgent emails` 仍然走 `scan`。
- scan 结果中的邮件标题可以点击打开详情。
- 打开详情后，用户再输入 `Draft a reply to the second one`、`Change it`、`帮我回这封` 时，应进入 `mail_context`，引用当前打开的 thread。

也就是说：快速跳转链接把 Ask 发现结果和邮件详情上下文接起来，避免用户在列表里二次寻找邮件。

## 9. 需要修改的文件草案

第一阶段预计只改前端：

- `anna-inbox/src/app/useAppController.ts`
  - 新增/替换 AI route helper。
  - `sendAiChatMessage()` 从二分逻辑改成四类分发。
  - `clarify` 分支写入带 actions 的 clarification message，等待用户选择后再执行。
- `anna-inbox/src/types/mail.ts`
  - 如需要，补充 route decision、clarification payload 类型。
- `anna-inbox/src/features/home/HomeView.tsx`
  - 渲染 clarification card：快捷选择、自由输入、取消/关闭。
  - 处理澄清 action 点击后的二次路由。
  - 如果需要把当前打开 thread context 显式传给 controller，再做小范围 props/state 调整。
- `anna-inbox/src/app/useAppController.test.ts` 或新增 `aiRoute.test.ts`
  - 覆盖路由纯函数。

快速跳转链接功能预计涉及：

- `anna-inbox/src/types/mail.ts`
  - 新增 `AskMailLink`，扩展 `CustomRunResultItem.mail_links`。
- `anna-inbox/src/features/home/HomeView.tsx`
  - Ask result item 渲染 mail links。
  - 新增从 Ask link 打开邮件详情抽屉的本地导航 helper。
- `anna-inbox/src/features/mail-detail/MailDetailDrawer.tsx`
  - 确认支持从外部传入 `mailbox + thread_id + anchor_message_id` 打开 thread。
- `inbox-tool/src/mail_agent/ask/answer.py`
  - Prompt 要求输出结构化 `mail_links`。
  - Guard 校验并剥离非法 link。
- `inbox-tool/src/mail_agent/ask/search.py`
  - 确保候选邮件保留 `mailbox/message_id/thread_id/subject/from/date`，供 answer 阶段引用。

暂不修改：

- Executa manifest / JSON-RPC 工具契约

## 10. 测试与验收

### 10.1 单元测试

新增纯函数测试：

- `Change it` + draft artifact => `mail_context`
- `Make it shorter` + mailContext => `mail_context`
- `Change it` + no context => `clarify`
- `Find urgent emails` => `scan`
- `What needs my reply?` => `scan`
- `你好` => `chat`
- `这是什么意思？` + previous assistant chat => `chat`
- `帮我找一下 Stripe 发票` => `scan`
- `写一封回复` + current thread context => `mail_context`
- `Do it` + no context => `clarify`，且不调用 LLM / tool
- clarification 点击 `Search inbox` => 使用原始输入或自由输入进入 `scan`
- clarification 点击 `Just chat` => 进入 `chat`
- clarification 点击 `Revise current draft` + no context => 显示打开邮件提示，不进入 `scan`
- clarification resolved 后重复点击不会重复触发工具

新增 Ask link 测试：

- `mail_links` 有合法 `mailbox + thread_id + message_id` 时渲染为可点击 link。
- 缺少 ID 的 item 只渲染普通文本。
- Guard 剥离不存在于候选集合的 `mail_links`。
- 点击 Ask link 能调用邮件详情打开 helper，并传入正确 mailbox/thread/message。
- 当前列表没有该 message 时，仍能通过 thread page fallback 打开详情。

### 10.2 手动验收

复现截图场景：

1. 打开一个邮件 thread。
2. 点击 AI draft，生成 draft artifact。
3. 在左侧 Anna 输入 `Change it`。
4. 期望：不出现 `Searched 1 focused inbox query`，不触发 `Custom scan complete`。
5. 期望：assistant 返回改写后的 draft artifact，按钮仍是 `Insert draft reply` / `Copy draft`。

Ask 场景回归：

1. 输入 `Find urgent emails`。
2. 期望：仍触发 custom scan。
3. 输入 `What needs my reply?`。
4. 期望：仍触发 custom scan。

无上下文短指令：

1. 点击 `New chat`。
2. 输入 `Change it`。
3. 期望：Anna 展示澄清卡片，包含 `Revise current draft`、`Search inbox`、`Just chat`，不触发 custom scan。
4. 点击 `Search inbox`。
5. 期望：此时才触发 custom scan。
6. 再次输入 `Change it`，点击 `Revise current draft`。
7. 如果当前没有打开邮件，期望：提示先打开邮件，不触发 custom scan。

Ask 快速跳转链接：

1. 输入 `Find urgent emails`。
2. 期望：Anna 返回按主题聚合的结果，其中具体邮件标题是可点击链接。
3. 点击任意邮件标题。
4. 期望：右侧打开对应邮件详情抽屉，展示正确 mailbox/thread/message。
5. 期望：返回或继续使用左侧 Anna 时，原 Ask 回答仍保留。
6. 对于非法/缺失 ID 的结果，期望：不显示可点击链接，只显示文本。

### 10.3 构建验证

前端修改后运行：

```sh
cd anna-inbox
npm run build
```

如果只新增 route helper 测试，则同时运行：

```sh
cd anna-inbox
npm run test
```

后端 Ask 输出/guard 修改后运行：

```sh
cd inbox-tool/src
uv run python tests/test_ask_answer.py
uv run python tests/test_ask_planner.py
```

如果修改 JSON-RPC 行为，再补充 describe smoke；仅扩展 result 内部字段且保持工具契约兼容时，可不改 manifest。

## 11. 风险与取舍

- 规则路由比 LLM router 更可解释，适合第一阶段修正明显误判。
- 规则会有边界情况，所以必须有可交互 `clarify` 兜底，避免“默认扫描”。
- `mail_context` 依赖可用的 thread context / artifact；没有上下文时不能假装知道用户说的 `it` 是什么。
- 不改后端契约可以降低风险，但如果后续需要真正多轮改稿记忆，可能需要扩展 `start_inbox_mail_prompt` 输入，例如传入上一版 draft body 或 artifact id。
- 快速跳转链接必须是结构化内部导航，不允许模型生成任意 URL；否则会有错误跳转和安全风险。
- Ask link 会让用户更自然地从 scan 进入 mail detail，因此后续 `mail_context` 路由要优先使用当前打开详情，而不是继续引用旧 Ask 结果。
- Clarification card 会增加一点前端状态复杂度，但能显著降低误触发高成本 scan 的概率。

## 12. 推荐审批结论

建议先批准第一阶段：

- 前端新增 `decideAiRoute()` 纯函数。
- 将左侧输入从 chat/scan 二分改为 chat/mail_context/scan/clarify 四分。
- 保持 Executa 工具契约不变。
- 用单元测试覆盖截图中的 `Change it` 误路由。

快速跳转链接建议作为同一轮或紧随其后的体验增强：

- Ask result 增加 `mail_links` 结构化引用。
- 前端把合法邮件引用渲染为可点击链接。
- 点击后打开对应邮件详情抽屉。
- Guard 剥离所有无法校验的链接，避免 LLM 伪造跳转。

第二阶段的 LLM router、日志可观测和后端上下文扩展，等第一阶段上线后根据误判样本再决定。
