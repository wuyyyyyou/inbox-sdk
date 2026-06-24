# Draft 质量优化方案

> 状态：第一阶段已实施；后续优化继续记录在本文档。
> 目标：提升 Draft 的“直接可用”比例，减少用户因为 AI 感、模板化、语气不自然或信息不完整而需要大幅手动改写的情况。

本文档用于持续记录 Draft 生成质量相关的问题、判断和解决方案。后续新的 Draft 优化需求也追加到这里，避免方案分散在实现细节或临时讨论中。

## 设计原则

- Draft 默认应像用户自己会写的邮件，而不是泛化的 AI 模板。
- 生成结果必须保留原邮件事实和用户已输入内容，不补造未提供的信息。
- UI 控制项使用英文，便于直接转成 prompt 约束和用户可见选项。
- 控制项应帮助用户快速表达偏好，但保留自由输入的 `Ask Anna to revise` 能力。
- 所有 Draft 仍然是用户可编辑、用户确认后才发送；质量优化不改变安全边界。

## 问题 1：Draft AI 感强，直接可用率不足

### 用户痛点

当前生成的 Draft 有时存在以下问题：

- 语气不自然，像 AI 写作或商务模板。
- 内容不够完整，需要用户补充上下文或关键表态。
- 表达过于固定，容易出现类似开头、类似结尾和套话。
- 用户需要手动修改较多，削弱“生成后基本可直接发送”的体验。

### 当前交互限制

Handle 面板里的 Draft 调节入口较少，目前主要是少量快捷按钮，例如：

- `Shorter`
- `Warmer`
- `More direct`

这些按钮适合轻量 revision，但不足以覆盖用户真实写信时常见的偏好维度，例如长度、写作风格、语气、情绪强度、细节程度和回复意图。

## 方案：将快捷按钮升级为 Draft Controls

把当前少量 chip 改造成更丰富的英文下拉菜单，让用户在生成或改写 Draft 前选择偏好。推荐保留自由输入框，同时新增结构化控制项。

### 建议控制项

| 控制项 | 英文 UI Label | 选项建议 |
| --- | --- | --- |
| 长度 | `Length` | `Brief`, `Standard`, `Detailed` |
| 写作风格 | `Writing style` | `Natural`, `Polished`, `Plain-spoken`, `Executive`, `Persuasive` |
| 语气 | `Tone` | `Warm`, `Direct`, `Diplomatic`, `Enthusiastic`, `Calm`, `Apologetic` |
| 心情/能量 | `Mood` | `Confident`, `Grateful`, `Supportive`, `Neutral`, `Urgent` |
| 细节程度 | `Detail level` | `High-level`, `Specific`, `Step-by-step` |
| 回复意图 | `Intent` | `Accept`, `Decline`, `Follow up`, `Ask for info`, `Confirm`, `Schedule` |

第一阶段可以先实现核心四项：

- `Length`
- `Writing style`
- `Tone`
- `Mood`

`Detail level` 和 `Intent` 更接近内容控制，适合在第二阶段加入，避免首版 UI 过重。

### 推荐默认值

默认组合建议偏向“自然、可直接发送”：

- `Length`: `Standard`
- `Writing style`: `Natural`
- `Tone`: `Warm`
- `Mood`: `Confident`

这样可以避免默认生成过短、过冷或过度正式，同时压低 AI 模板感。

### 交互草图

```text
Draft reply

[Reply to sender] [Reply all]

Subject input

[Length: Standard] [Writing style: Natural] [Tone: Warm] [Mood: Confident]
[Ask Anna to revise...] [Revise]

Textarea draft body
```

也可以在移动端将控制项收进一个 `Controls` 菜单，避免横向空间不足。

### Prompt 映射

结构化控制项不应该只作为 UI 状态存在，而应统一映射为 Draft 生成/改写约束。例如：

```text
Rewrite the current draft using these preferences:
- Length: Standard
- Writing style: Natural
- Tone: Warm
- Mood: Confident

Keep all factual commitments from the current draft and original email.
Avoid generic AI-like openings, excessive praise, and template-like closings.
Do not invent missing details.
```

如果用户同时输入自由修改要求，则拼接在结构化偏好之后，优先级建议为：

1. 安全与事实约束
2. 用户自由输入
3. 下拉菜单偏好
4. 默认风格约束

### 质量约束

无论选择什么选项，Draft 生成都应遵守：

- 不编造时间、价格、承诺、附件、会议安排或用户身份信息。
- 不使用过度模板化表达，例如过多的 “I hope this email finds you well”。
- 不为了显得热情而改变用户立场。
- 不删除原邮件中必须回应的问题。
- 如果关键信息缺失，应优先提示需要用户补充，而不是猜测。

## 实施建议

第一阶段建议只改前端交互和 revision prompt 拼装：

- 将现有 `Shorter` / `Warmer` / `More direct` chips 替换或升级为下拉控制项。
- 继续复用现有 `revise_draft` 能力，把菜单选择转换成 `revision_input`。
- 不先修改 Executa 工具契约，降低协议改动风险。
- 保留自由输入框，允许用户输入更具体的修改要求。

### 第一阶段实施结果

- 已将 `Shorter` / `Warmer` / `More direct` chips 升级为英文下拉控制项。
- 已上线四个核心控制项：`Length`、`Writing style`、`Tone`、`Mood`。
- 已复用现有 `revise_draft` / `start_generate_draft` 调用链，通过前端拼接 `revision_input` 传给后端。
- 当前仍为前端会话内状态，不做偏好持久化，也不修改 Executa 工具契约。

第二阶段再考虑后端结构化参数：

- `draft_preferences.length`
- `draft_preferences.writing_style`
- `draft_preferences.tone`
- `draft_preferences.mood`
- `draft_preferences.detail_level`
- `draft_preferences.intent`

当偏好需要持久化、跨卡片复用或用于首次生成 Draft 时，再扩展工具参数会更合理。

## 验收标准

- 用户可以通过英文下拉菜单表达至少 `Length`、`Writing style`、`Tone`、`Mood` 四类偏好。
- 生成/改写后的 Draft 比当前 chip 方案更少出现模板化套话。
- 用户手动补改幅度下降，尤其是语气和表达风格方面。
- 自由输入修改仍然可用，并且与下拉偏好可以同时生效。
- 不引入自动发送、凭据或 Gmail 状态变更。

## 问题 2：Draft 不知道“我想怎么回”

### 用户痛点

用户反馈的核心不是“AI 不会写”，而是“AI 不知道我这个人想怎么回”：

- 当前 Draft 能根据邮件线程写出一封合理回复，但经常只是“通用合理”。
- 系统知道邮件上下文，却不知道用户此刻的立场、真实想法、想推进的结果。
- 系统也缺少对用户长期回复偏好的理解，例如常用语气、边界、签名习惯、对不同关系的沟通方式。
- 结果是 Draft 看起来没错，但不像用户自己会发的邮件，用户需要重写关键表达。

这类问题不能只靠更多风格下拉菜单解决。`Length`、`Tone`、`Mood` 只能控制表达形式，不能替用户决定“要答应、拒绝、拖延、反问、推进、保留空间，还是释放某个信号”。

### 现状判断

项目里已经有三块可复用基础：

- `reply_gaps`：Phase 2 会识别对方明确问了什么，并在 Draft 前让用户补充答案。
- `contact memory`：Draft 生成会按 `draft_generation` 目的召回同联系人相关历史，用于关系、历史上下文和 open loop。
- `revision_input` + Draft Controls：前端已把自由输入和结构化偏好拼成 prompt，传给 `start_generate_draft`。

但它们还没有覆盖这次需求：

- `reply_gaps` 主要解决“缺少事实答案”，例如报价、档期、确认信息；它不负责询问用户的主观立场。
- `contact memory` 主要是联系人/线程记忆，不等于用户个人回复画像，也不能代表用户当前意图。
- Draft Controls 主要是风格控制，不表达业务目标或回复策略。
- `revision_input` 是一次性的自由输入，当前没有沉淀成可复用偏好，也没有从用户最终编辑结果中学习。
- 后端 `_DRAFT_SYSTEM` 已经规定 source priority，但缺少一个独立的“User reply intent / personal reply profile”输入层。

因此，Draft 质量的下一阶段核心应是：在写 Draft 前，让系统拿到“这封邮件我想怎么回”；在多次使用后，让系统逐渐知道“我通常怎么回”。

### 方案总览：增加 Reply Intent + Personal Reply Profile

把 Draft 生成输入从“邮件上下文 + 风格偏好”升级为：

```text
Draft = 原邮件事实
      + 当前线程/联系人上下文
      + 用户对这封邮件的回复意图
      + 用户长期回复偏好
      + 安全与事实约束
```

其中：

- `Reply Intent` 解决当前这封邮件“我想怎么样去回复”。
- `Personal Reply Profile` 解决长期“AI 对我这个人的了解”。
- `contact memory` 继续解决“我和这个联系人之前发生过什么”。
- `reply_gaps` 继续解决“对方问了哪些必须由用户回答的事实问题”。

### Reply Intent：让用户低成本表达当前想法

在 Handle Panel 中增加一个独立的意图输入层，目标不是让用户写完整回信，而是让用户给 AI 一个方向。

关键交互决策：**AI 需要用户做决策时，不应该把问题写进 reply 文本框。**

- reply 文本框只展示最终可编辑 Draft，不承载系统追问。
- 用户决策问题应放在 Draft 文本框上方的独立模块里，例如 `Decision needed` / `Reply intent`。
- 该模块提交后，系统再生成或改写 Draft。
- 用户跳过该模块时，系统可以生成低承诺 Draft，但不能在正文里伪装成用户已经做了决定。

建议 UI 保持英文，与现有 Draft Controls 一致：

```text
Anna needs your decision

Reply goal
[Accept] [Decline] [Ask for info] [Follow up] [Schedule] [Negotiate] [Hold off]

Your take
[e.g. "Politely decline, but keep the door open for next month."]

[Skip] [Generate draft]

Draft reply
[Textarea: final editable draft only]
```

可选结构化字段：

| 字段 | UI Label | 用途 |
| --- | --- | --- |
| 回复目标 | `Reply goal` | 表达要推进的结果，例如 accept、decline、ask for info |
| 用户立场 | `My take` | 用户对此事的真实态度或判断 |
| 必须包含 | `Must include` | 必须提到的事实、条件、时间、链接 |
| 必须避免 | `Avoid saying` | 不要承诺、不要报价、不要透露某类信息 |
| 下一步 | `Next step` | 希望对方做什么，例如 book a call、send details |

第一步可以只做一个自由输入框的产品强化：

- 当前 placeholder 是 `Tell Anna how to write the reply (optional)`。
- 可以升级为独立模块中的 `Your take` 输入框，例如 `Tell Anna your take before drafting...`。
- 这个输入在首次生成 Draft 时应被视为最高优先级的用户意图，而不是普通 revision。
- 不建议把这个问题作为 textarea placeholder，因为 placeholder 会在 Draft 生成后消失，也容易让用户误以为要自己从零写回复。

### 自适应追问：缺意图时不要硬写

当系统判断“邮件需要用户主观决策”，但用户没有输入 `Reply Intent`，应优先追问一个短问题，而不是直接生成看似完整但用户不喜欢的 Draft。

触发场景：

- 对方邀请合作、询价、约时间、请求确认、要求评价或做决定。
- `reply_gaps.needs_user_input=false`，但 `recommendation` 暗示需要用户选择立场。
- contact memory 显示有 open loop，但当前用户态度不明确。

追问形态：

```text
Decision needed
Before I draft: do you want to move forward, ask for more details, or decline?

[Move forward] [Ask for details] [Decline] [Write my own direction...]
```

原则：

- 最多问 1 个高价值问题，避免把 Draft 变成问卷。
- 已有 `reply_gaps` 时优先复用 GapForm，不重复问。
- 追问显示在独立 decision block 中，不写入 Draft textarea，也不混入邮件正文。
- 用户跳过时可以生成低承诺 Draft，但必须避免编造态度或承诺。

### Personal Reply Profile：沉淀用户长期偏好

新增一个 mailbox-scoped 的个人回复画像，用来记录用户稳定偏好，而不是记录完整邮件正文。

建议存储形态：

```json
{
  "mailbox": "user@example.com",
  "default_style": {
    "length": "Standard",
    "tone": "Warm",
    "writing_style": "Natural",
    "signature": "Best,\nKate"
  },
  "reply_principles": [
    "Prefer concise replies with concrete next steps.",
    "Do not overpromise timeline unless user explicitly provides one.",
    "For partnership emails, ask for budget and scope before agreeing."
  ],
  "avoid_patterns": [
    "Avoid 'I hope this email finds you well'.",
    "Avoid sounding overly enthusiastic for cold outreach."
  ],
  "updated_at": "..."
}
```

存储边界：

- 按 mailbox 隔离，遵守现有 `mailbox/<sanitized-mailbox>/...` key 设计。
- 只保存压缩后的偏好、原则和可删除的学习记录，不保存完整敏感邮件正文。
- 用户应能查看、编辑、删除该 profile。
- profile 只能作为风格和策略参考，不能当作当前事实来源。

### 学习闭环：从用户最终编辑结果里学习

目前 Draft 生成后，如果用户大幅编辑，系统不会知道“哪里不喜欢”。后续可加入轻量学习：

```text
生成 Draft
  -> 用户编辑
  -> 用户发送/记录已处理
  -> 比较 generated_draft 与 final_draft
  -> 提取 compact learning
  -> 更新 Personal Reply Profile 或 contact memory
```

学习内容示例：

- 用户总是删掉模板化开头。
- 用户常把 “happy to” 改成更克制的 “open to”。
- 用户对合作邮件通常先问预算和时间线，不直接答应。
- 用户对某联系人喜欢更直接，少寒暄。

注意事项：

- 不要把一次编辑立刻泛化成全局规则；至少需要多次一致信号或用户显式确认。
- 联系人相关偏好应进入 contact-scoped memory；全局写作偏好进入 Personal Reply Profile。
- 对涉及价格、法律、雇佣、商业承诺的内容，只学习“不要擅自承诺”等原则，不学习具体数值作为默认事实。

### Prompt 映射

Draft prompt 的优先级建议调整为：

1. 安全与事实约束
2. 用户对当前邮件的显式回答和 `Reply Intent`
3. 用户正在编辑的现有 Draft
4. 原始邮件线程事实
5. Personal Reply Profile
6. Contact memory
7. Draft Controls 默认风格偏好

示例 prompt block：

```text
User reply intent (AUTHORITATIVE for this draft):
- Reply goal: Decline
- My take: Not a fit for this month, but keep the door open.
- Must include: Ask them to reconnect in July.
- Avoid saying: Do not mention budget.

Personal reply profile:
- Prefer concise replies with concrete next steps.
- Avoid generic AI-like openings.
- Do not promise timelines unless the user provided one.

Relevant contact memory:
- Previous thread: They asked about a creator partnership in May.
- Open loop: They were waiting for campaign details.

Write the draft using the user's current intent first. Use profile and contact memory only for style, relationship, and context. Do not infer missing commitments.
```

### 分阶段实施建议

#### Phase 2A：不改工具契约，先强化意图输入

- 保持 `start_generate_draft` / `revision_input` 现有协议不变。
- 前端新增独立 `Decision needed` / `Reply intent` 模块，放在 Draft textarea 上方。
- reply textarea 只显示最终 Draft；系统追问、用户决策和意图输入都不进入 textarea。
- 生成 Draft 前，把 `Reply goal` / `What should Anna say?` 拼入 `revision_input`。
- 修改 `buildDraftPreferencesInstruction` 文案，让后端更明确地区分 `User intent` 和 `Draft preferences`。
- 对缺少用户立场的卡片，在独立 decision block 中提示用户先选方向或写一句想法。
- 可加 `Clear local draft` 按钮，只清当前前端会话里的 draft/revision 状态，不修改后端已持久化的 `draft_reply`。

优点：改动小，不碰后端协议；能快速验证用户是否愿意提供意图。

#### Phase 2B：结构化 `reply_intent`

当 2A 验证有效后，扩展工具参数：

```json
{
  "reply_intent": {
    "goal": "decline",
    "my_take": "Not a fit right now.",
    "must_include": ["Reconnect in July"],
    "avoid_saying": ["budget"],
    "next_step": "Ask them to follow up next month"
  }
}
```

后端改动方向：

- `anna_inbox_executa/common.py`：声明 `start_generate_draft` / `generate_draft_reply` 可选参数。
- `card_tools.py` / `v2_tools.py`：透传 `reply_intent`。
- `mail_agent/actions/service.py`：`generate_draft_reply` 和 `_build_draft_prompt` 注入 `reply_intent`。
- 测试：覆盖 `reply_intent` 优先于 Draft Controls 和 contact memory。

#### Phase 2C：Personal Reply Profile 持久化与学习

新增或扩展 storage：

- `mail_agent/storage/types.py`：新增 `ReplyProfile` / `ReplyPrinciple` dataclass。
- `mail_agent/storage/ops.py`：新增 `get_reply_profile` / `set_reply_profile` / `append_reply_learning`。
- key 建议：`mailbox/<sanitized-mailbox>/prefs/reply_profile`。
- 前端 Memory 或 Settings 中提供查看、编辑、清空入口。
- Draft 生成时读取 profile，并作为低于当前意图、高于默认风格的 prompt context。

学习触发：

- 用户点击 `Reply now` 且 final draft 与 generated draft 差异明显。
- 用户点击 `Handled manually` 时可选输入“我实际怎么处理了”。
- 用户显式点击 `Remember this preference`。

### 时间紧张下的开发优先级

如果当前时间紧张，建议不要从完整 `Personal Reply Profile` 或结构化协议改造开始，而是按以下优先级推进。

#### P0：先做 Phase 2A 的独立 Decision Block

优先开发内容：

- 在 Handle Panel 的 Draft 区上方增加独立 `Decision needed` / `Reply intent` 模块。
- 支持一个 `Your take` 自由输入，以及少量快捷 goal，例如 `Accept`、`Decline`、`Ask for info`、`Schedule`。
- 把用户输入拼进现有 `revision_input`，继续调用 `start_generate_draft`。
- 调整 `buildDraftPreferencesInstruction`，让 prompt 明确区分：
  - `User reply intent`
  - `Draft preferences`
  - `Freeform revision`

为什么优先：

- 这是对用户痛点最直接的修复：AI 不知道用户想怎么回，就先让用户用一句话告诉 AI。
- 不改 Executa 工具契约，不需要同步 manifest / frontend DTO / backend handler，风险最低。
- 现有前端已经有 Draft Controls、GapForm、`revision_input` 拼装和 `start_generate_draft` 调用链，改动范围集中。
- 独立 decision block 能马上解决“问题不要写在 reply 文本框里”的交互担忧。

#### P1：小幅调整后端 prompt 语义

优先开发内容：

- 在 `_DRAFT_SYSTEM` 中补充：`revision_input` 可能包含用户当前回复意图，应优先于风格偏好。
- 在 `_build_draft_prompt` 中把 `User instruction` 文案改得更明确，例如 `User reply intent and instructions`。
- 增加小测试，确保用户意图优先于 Draft Controls 和 contact memory。

为什么排第二：

- 改动仍然很小，不需要新协议。
- 能减少模型把用户输入当“风格修改”而不是“当前立场”的概率。
- 对 Phase 2A 的收益有放大作用。

#### P2：暂缓 Phase 2B 的结构化 `reply_intent`

暂缓原因：

- 需要改 Executa manifest / `common.py` tool schema / `card_tools.py` / `v2_tools.py` / 前端 API 类型与调用参数。
- 会扩大测试面，必须做 JSON-RPC smoke test 和前后端契约验证。
- 当前目标是快速改善 Draft 质量，`revision_input` 已足够承载第一版意图信息。

适合启动条件：

- Phase 2A 已验证用户愿意填写意图。
- 需要跨组件复用 intent，或需要把 intent 持久化/分析。
- Prompt 拼接开始变复杂，继续塞进 `revision_input` 可维护性下降。

#### P3：暂缓 Phase 2C 的 Personal Reply Profile

暂缓原因：

- 涉及新 storage schema、隐私边界、用户查看/删除入口、学习策略和误学习纠偏。
- 对“这封邮件我想怎么回”的即时问题帮助不如 P0 直接。
- 如果仓促做，容易把历史偏好误当当前事实，反而增加 Draft 风险。

适合启动条件：

- P0/P1 已经让单次 Draft 可用率提升，但用户仍反复做同类编辑。
- 产品上已经有明确的 memory/settings 管理入口。
- 能设计好学习确认、删除、回滚和 mailbox 隔离。

最小可交付建议：

```text
第一轮只做 P0 + P1：
1. 独立 Decision Block，不占用 reply textarea。
2. 用户一句话意图进入 revision_input。
3. Prompt 明确把这句话当当前回复意图。
4. 不做新协议、不做长期记忆、不做自动学习。
5. `Clear local draft` 仅用于本地清空和重新生成，不承诺跨刷新或跨设备删除已持久化草稿。
```

### 质量与安全约束

- 用户当前意图永远高于历史 profile 和 contact memory。
- profile 不得把历史价格、时间、承诺当成当前默认事实。
- contact memory 只能解释关系和历史，不能替用户决定当前立场。
- 如果缺少关键事实或立场，应追问或生成低承诺草稿，不要擅自补全。
- 所有学习记录必须可删除，且按 mailbox 隔离。
- Draft 仍然只生成可编辑草稿，不改变发送前用户确认的安全边界。

### 验收标准

- 用户输入一句“我想委婉拒绝，但保留以后合作可能”，Draft 能准确围绕该立场生成，而不是泛化寒暄。
- 当邮件需要用户主观决策但没有提供想法时，系统会提出一个简短追问或生成低承诺 Draft。
- 用户多次删除或改写的表达方式，后续 Draft 中明显减少。
- 对同一联系人，Draft 能利用历史 open loop，但不会把历史事实误当成当前承诺。
- 不修改自动发送边界，不记录完整敏感邮件正文到偏好 profile。

## 后续问题记录

后续 Draft 质量问题统一按以下格式追加：

```text
## 问题 N：标题

### 用户痛点

### 现状判断

### 方案

### 实施建议

### 验收标准
```
