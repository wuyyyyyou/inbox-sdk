# Streaming 与任务 System Prompt 拆分

状态：已实现（CLI `@anna-ai/cli@0.1.38`；不 bump App/Tool 版本号）。

## 目标

1. **体验**：App 侧使用 Host `anna.llm.stream`（与 `llm.complete` 同请求形）做闲聊流式输出。
2. **治本**：各任务 **独立、尽量短** 的 system prompt；邮件证据只进 user。

## Streaming（路径 B）

| 项 | 约定 |
|----|------|
| API | `anna.llm.stream({ messages, maxTokens })` → `model_token` 帧 + `complete` 帧 |
| 入口 | 侧栏 `routingIntent === "chat"` 时优先 stream |
| 实现 | `anna-inbox/src/api/llmClient.ts` |
| 回退 | stream 不可用 / `-32601` → `llm.complete` → 再失败则 `start_ai_turn`（Executa Sampling） |
| 不迁移 | 搜邮 / 整理 / 草稿 / Router 仍走 Executa（Gmail + 结构化 JSON + guardrail） |
| Manifest | App `host_capabilities` 增加 `llm.complete`（stream 与 complete 同 L1 能力族） |

参考：[Anna beta.71 Streaming LLM](https://forum.anna.partners/t/anna-1-1-0-beta-55-beta-96-web-superpowers-streaming-llm-multi-account-credentials-a-rock-solid-executa-pipeline/176)

## System Prompt 按任务拆分

集中文件：`inbox-tool/src/mail_agent/ai_turn/prompts.py`

| 任务 | 函数 |
|------|------|
| Router | `router_system_prompt` |
| 闲聊 | `chat_general_system_prompt` / 前端 `chatStreamSystemPrompt` |
| 线程问答 | `thread_answer_system_prompt` |
| 草稿 / 改写 / 撰写 / 批量 | `draft_*` / `compose_*` / `batch_*` |
| Ask 规划 | `ask_planner_system_prompt` |
| Ask 回答 | `ask_answer_system_prompt(item_limit)` |
| Custom scan | `custom_scan_system_prompt` |
| Ask 补草稿 | `ask_item_draft_system_prompt` |

规则：

- **system**：规则 + schema，短
- **user**：本轮请求 + 证据（可截断）
- 禁止把 Router 规则拼进 Answer/Draft

## 验证

```sh
npm i -g @anna-ai/cli@latest   # 当前 0.1.38
cd anna-inbox && npm test && npm run typecheck
uv --directory inbox-tool/src run python -c "from mail_agent.ai_turn.prompts import router_system_prompt, ask_answer_system_prompt; print(len(router_system_prompt()), len(ask_answer_system_prompt(3)))"
```

侧栏点 **Chat** 类 starter（`routingIntent: chat`）应看到逐 token 更新；Host 过旧时自动回退。
