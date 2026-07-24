# AI 侧栏本地测试开关

状态：已实现（local 与 host **共用同一套细粒度工具**）  
用途：本地调试侧栏时绕过 Host Agent Session，但取证 / 写改稿 / 整理 / 记忆执行逻辑与生产一致。

## 背景

生产侧栏主路径为 `anna.agent.session`（Host 选型 + 白名单工具）。本地每次测需上传 Tool，且易受平台 Agent 行为影响。

**local 不是旧 Router 旁路**：选型在本地用 Anna Sampling 多步 JSON 环完成，**工具执行仍走** `handle_ai_agent_tool` / `AI_AGENT_TOOL_NAMES`（与 Host 相同，以 `query_mail_evidence` 为主只读入口）。

## 开关

| 层级 | 键 | 取值 | 说明 |
| --- | --- | --- | --- |
| 后端 env | `ANNA_INBOX_AI_SIDEBAR_MODE` | `host`（默认）/ `local` | 后端默认；写入 `health` 与 `check_sampling_status.ai_sidebar_mode` |
| 前端覆盖 | `localStorage["anna-inbox-ai-sidebar-mode"]` | `host` / `local` | **优先于**后端默认 |

解析：`resolveAiSidebarMode(backendMode)`（`anna-inbox/src/app/aiSidebarMode.ts`）。

## 行为对照

| 模式 | 选型 | 工具执行 | LLM |
| --- | --- | --- | --- |
| `host` | Host Agent Session | `query_mail_evidence` / `ai_*` / `propose_inbox_actions` 等 | Host 模型环 |
| `local` | 本地 Sampling JSON 多步环（`local_agent_session`） | **同一套** `handle_ai_agent_tool` | `ai_provider: anna-llm` Sampling |

```text
host:  侧栏 → anna.agent.session → Host 选型 → handle_ai_agent_tool
local: 侧栏 → start_ai_turn(source=sidebar_local) → Sampling 选型 → handle_ai_agent_tool
```

- local **无真流式**（轮询 run）；无 UI 提示。
- stderr：`ai_sidebar path=local_agent_session ...` / `path=local_agent_session tools=host_whitelist`。
- 详情页协助仍走旧 `start_ai_turn` Router（`source` 非 `sidebar_local`），与侧栏 local 无关。
- mutation 仍须用户确认（`apply_proposed_actions` 不进白名单）。
- `display_range_days` 不得作为检索边界传入选型上下文。

## 使用示例

```sh
set ANNA_INBOX_AI_SIDEBAR_MODE=local
```

```js
localStorage.setItem("anna-inbox-ai-sidebar-mode", "local");
// 或强制 host / 清除覆盖
localStorage.setItem("anna-inbox-ai-sidebar-mode", "host");
localStorage.removeItem("anna-inbox-ai-sidebar-mode");
```

## 相关代码

- `anna_inbox_executa/local_agent_session.py`：本地选型环
- `anna_inbox_executa/ai_agent_tools_flow.py`：host/local 共用工具实现
- `anna_inbox_executa/ai_turn_flow.py`：`source=sidebar_local` 分支
- 前端：`aiSidebarMode.ts`、`useAppController.sendAiChatMessage`
