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
