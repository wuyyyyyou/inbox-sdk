# AI 侧栏统一与 Brief 下线

状态：已实现（随 App `2.1.4` / Tool `2.2.4` 发布基线收录）。

## 目标

- **唯一智能入口**：Inbox Workspace + AI 侧栏（主路径为 Host Agent Session；`start_ai_turn` 保留为详情/兼容路径）。
- **Brief 产品面下线**：不再主路径执行 `start_mail_agent_run` / Phase1 / Phase2 / Attention Card 队列。
- **能力合并到侧栏**：
  - 问答 / 搜索：`inbox` + Ask pipeline
  - 整理：`organize` → Needs reply 分段 + Can clean up 确认批处理
- **历史**：`askHistory` 仅保留 **7 天**，每条会话独立 `conversationId`。
- **旧 Brief 数据**：启动时对各启用邮箱调用 `reset_mailbox_scan_history`（cards + cleanup + processed + scan_state）。

## 扫描性能（P0/P1/P2）

| 项 | 行为 |
|----|------|
| P0 | Ask `execute_search` 默认 `live_search_metadata_and_cache`；正文仅 top-N 详情 / 显式「全文」时 `fetch_and_cache_message` |
| P1 | 宽查询（无 from/to/主题 OR）优先本地缓存；不足再 Gmail metadata |
| P2 | 需回复本地噪声降权（noreply/OTP/newsletter 等），不跑 Phase1 LLM |

## 后端变更摘要

- 删除 `anna_inbox_executa/brief_flow.py`、`mail_agent/core/phase1.py`
- `core/pipeline.py` 仅保留 `run_custom_scan`
- describe / dispatcher 移除 `start/continue_mail_agent_run` 等 Brief 工具
- `cancel_mail_agent_run` / `_merge_partial` 迁至 `common.py`（AI turn / custom scan 仍用）
- organize：`tool_propose_inbox_actions` 分段 Needs reply + Can clean up

## 前端变更摘要

- 初始化不再 `loadActiveCards`；改为清空 Brief 扫描存储
- `startScan` / `resetAndStartScan` 不再跑 Brief
- 会话历史 7 天 TTL（`state.ts` + `persistAskHistory`）

## 验证建议

```sh
printf '%s\n' '{"jsonrpc":"2.0","method":"describe","id":1}' | uv --directory inbox-tool/src run anna-inbox-executa
cd anna-inbox && npm test && npm run build
```

## 后续（可选）

- 物理删除 `features/brief/*`、card 工具与 judgment_engine 残留
- contact memory 回填与 active cards 解耦收尾
