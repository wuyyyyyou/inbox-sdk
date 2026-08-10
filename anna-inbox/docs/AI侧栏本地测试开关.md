# AI 侧栏本地测试开关

状态：已实现（产品默认走 local Sampling；host 仅保留为开发调试路径）
用途：说明 AI 侧栏的默认路径，以及开发调试时临时验证 Host Agent Session 的方式。

## 背景

产品侧栏默认强制使用本地 Sampling。平台 `host.agent.session` 路径仍保留但默认隐藏，不删除，仅供开发调试时临时启用。

**local 不是旧 Router 旁路**：选型在本地用 Anna Sampling 多步 JSON 环完成，**工具执行仍走** `handle_ai_agent_tool` / `AI_AGENT_TOOL_NAMES`（与 Host 相同，以 `query_mail_evidence` 为主只读入口）。

## 开关

| 层级 | 键 | 取值 | 说明 |
| --- | --- | --- | --- |
| 产品默认 | — | `local` | 强制走本地 Sampling；用户界面不提供路径切换入口 |
| 前端调试覆盖 | `localStorage["anna-inbox-ai-sidebar-mode"]` | `host` / `local` | 仅开发调试使用；临时覆盖当前侧栏路径 |

解析：`resolveAiSidebarMode(backendMode)`（`anna-inbox/src/app/aiSidebarMode.ts`）。后端 `ai_sidebar_mode` 仅作为后端状态信息，不会把产品默认路径切回 `host`。

## 行为对照

| 模式 | 选型 | 工具执行 | LLM |
| --- | --- | --- | --- |
| `host` | Host Agent Session（仅开发调试临时启用） | `query_mail_evidence` / `ai_*` / `propose_inbox_actions` 等 | Host 模型环 |
| `local` | 本地 Sampling JSON 多步环（`local_agent_session`）；`query_mail_evidence` 的 QueryPlan 与 route 合并 | **同一套** `handle_ai_agent_tool` | `ai_provider: anna-llm` Sampling |

```text
默认: 侧栏 → start_ai_turn(source=sidebar_local) → Sampling 选型 → handle_ai_agent_tool
调试: 侧栏 → anna.agent.session → Host 选型 → handle_ai_agent_tool
```

- local **无真流式**（轮询 run）；无 UI 提示。
- stderr：`ai_sidebar path=local_agent_session ...` / `path=local_agent_session tools=host_whitelist`。
- 详情页协助仍走旧 `start_ai_turn` Router（`source` 非 `sidebar_local`），与侧栏 local 无关。
- mutation 仍须用户确认（`apply_proposed_actions` 不进白名单）。
- `display_range_days` 不得作为检索边界传入选型上下文。

## 开发调试使用示例

```sh
# 强制使用本地 Sampling
localStorage.setItem("anna-inbox-ai-sidebar-mode", "local");
```

```js
// 临时启用平台 Host Agent Session
localStorage.setItem("anna-inbox-ai-sidebar-mode", "host");

// 删除覆盖后恢复产品默认：local Sampling
localStorage.removeItem("anna-inbox-ai-sidebar-mode");
```

`local` 用于明确强制 Sampling，`host` 只用于开发调试验证 Session。删除该键即可恢复默认路径；不要将该调试覆盖作为用户功能或产品配置暴露。

## 相关代码

- `anna_inbox_executa/local_agent_session.py`：本地选型环
- `anna_inbox_executa/ai_agent_tools_flow.py`：host/local 共用工具实现
- `anna_inbox_executa/ai_turn_flow.py`：`source=sidebar_local` 分支
- 前端：`aiSidebarMode.ts`、`useAppController.sendAiChatMessage`
