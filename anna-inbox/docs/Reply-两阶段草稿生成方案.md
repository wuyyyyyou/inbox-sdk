# Reply 两阶段草稿生成方案

> 2026-06-07

## 问题

LLM 替用户写回信时不知道定价、档期、态度，只能编。让它改风格（更短/更正式）能做到，让它回答对方的具体问题就只能猜，导致擅自承诺和虚构回答。

## 核心思路

把决策权还给用户，LLM 只负责分析"哪里需要用户决策"和按用户回答组装语言。

## 数据流

```
Phase 2 Judgment（已读完全文，零额外 LLM 调用）
  └→ 新增输出字段 reply_gaps: { needs_user_input, summary, questions[] }
       └→ 存入 PersistentCard.reply_gaps
            └→ 用户打开 Handle Panel
                 ├── needs_user_input=true → 显示 GapForm 问答
                 │     └→ 用户填完 → Generate Draft(user_answers)
                 │          └→ LLM 基于 answers 生成回信
                 └── needs_user_input=false → 一键生成草稿（现有流程）
```

## 阶段一：Gap Analysis（Phase 2 内嵌，零成本）

Phase 2 judgment 已读完全文，在现有输出上追加 `reply_gaps` 字段。不增加 LLM 调用次数。

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

**判定规则：**
- `needs_user_input=true` 仅当对方明确问了问题（报价/档期/意见/确认具体事实）
- `needs_user_input=false` 适用于感谢信/FYI/纯通知/无需实质性回复
- `questions` 最多 3 个，合并同类问题，用中文

## 阶段二：User Response → Draft

用户在 GapForm 填写答案后：

```
你的回答：
- q1: "标准报价 ¥5000/月，首月 8 折"
- q2: "周三下午 3 点可以"
```

→ LLM 根据原邮件 + 用户回答组装回信。严格约束：草稿只能基于用户回答和原邮件内容，不得编造用户没有提供的信息。

## 前端 UI

GapForm 组件，嵌在 Handle Panel 草稿区：

```
📋 需要你确认几个问题：

Q1 · 对方问报价，你想报多少？
[________________________________]  标准报价 ¥5000/月，首月 8 折

Q2 · 对方约下周三 call，你方便吗？
[________________________________]  周三下午 3 点

[跳过，直接生成]              [生成草稿]
```

- "跳过" → 用空 answers 调 generate_draft，LLM 在 note 里标注信息不足
- 提交后 → `actions.generateDraft({ user_answers })` → 正常 draft 编辑/发送流程
- `needs_user_input=false` 时不显示表单，保持现有 Generate Draft 一键体验
- Gap 表单期间不显示 revision chips（Shorter/Warmer/More direct）

## 改动文件清单

### Backend

| 文件 | 改动 |
|---|---|
| `judgment_engine/service.py` | 三处 prompt builder 加 gap 分析指令；`parse_judgment_output` / `_parse_compact_batch_item` 提取 `reply_gaps` 存入 `mode_judgment` |
| `storage/types.py` | `PersistentCard` 加 `reply_gaps: dict`（`{needs_user_input, summary, questions}`） |
| `cards/service.py` | `build_card` 从 `mode_judgment` 提取 `reply_gaps` 写入 card |
| `actions/service.py` | `generate_draft_reply` 加 `user_answers` 参数；`_build_draft_prompt` 插入用户回答；重写 `_DRAFT_SYSTEM`（禁止编造） |
| `main.py` | `start_generate_draft` / `generate_draft_reply` handler 透传 `user_answers` |

### Frontend

| 文件 | 改动 |
|---|---|
| `types/mail.ts` | 新增 `ReplyGaps`、`GapQuestion` 类型；`FrontendCard` 加 `reply_gaps` |
| `features/handle/HandleView.tsx` | 新增 `GapForm` 组件；草稿区条件渲染 |
| `app/useAppController.ts` | `generateDraft` 支持 `user_answers` 参数 |
| `app/state.ts` | 新增 `replyGapsByCard: Record<string, ReplyGaps>` |

## 设计决策

1. **Gap 分析在 Phase 2 做而非单独调 LLM** — Phase 2 已读完全文，多输出一个字段零额外成本
2. **`reply_gaps` 缓存到 card 持久化存储** — Phase 2 输出直接落盘，打开 Handle Panel 立即可用
3. **Gap 表单不显示 revision chips** — 用户必须先完成 gap → 生成草稿 → 然后才能 revise
4. **revision_input 回归纯粹风格调整** — 不再承载补充信息的职责
