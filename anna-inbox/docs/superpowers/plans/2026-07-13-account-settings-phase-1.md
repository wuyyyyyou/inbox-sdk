# 账号设置一期实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 为当前邮箱提供全页 Settings，持久化展示范围、时间分组、Important 置顶配置，并保留禁用的自定义分类 UI。

**架构：** 后端新增 mailbox-scoped 的 `InboxSettings` 记录，所有读写经 `storage/ops.py` 和两个 V2 JSON-RPC 工具完成，使用 `etag`/`if_match` 处理竞争更新。前端将设置状态置于 AppController，通过 `mailAgentClient.ts` 调用，并将 SettingsView 作为 HomeView 的全页分支；邮件列表只消费已加载的当前账号设置。

**技术栈：** React 19、TypeScript、Vitest、Python 3、Executa JSON-RPC、mail_agent storage。

---

## 文件结构

- 修改：`inbox-tool/src/mail_agent/storage/types.py` — 定义 `InboxSettings` 默认值与序列化边界。
- 修改：`inbox-tool/src/mail_agent/storage/ops.py` — 使用 `anna-inbox/mailbox/{sanitized_email}/inbox_settings` 读写、返回 etag。
- 修改：`inbox-tool/src/anna_inbox_executa/common.py` — 声明 `get_inbox_settings`、`save_inbox_settings` 工具参数。
- 修改：`inbox-tool/src/anna_inbox_executa/dispatcher.py`、`inbox-tool/src/anna_inbox_executa/v2_tools.py` — 将工具放入 V2 分发并验证邮箱、字段和 if_match。
- 创建：`inbox-tool/src/tests/test_inbox_settings_storage.py` — 默认值、邮箱隔离、etag 冲突和字段夹紧的脚本测试。
- 修改：`anna-inbox/src/types/mail.ts` — 前端设置 DTO、AppState 字段与 UI 视图类型。
- 修改：`anna-inbox/src/api/mailAgentClient.ts`、`anna-inbox/src/api/mailAgentClient.test.ts` — facade 方法和 RPC 参数断言。
- 修改：`anna-inbox/src/app/state.ts`、`anna-inbox/src/app/useAppController.ts` — 加载、乐观保存/失败回滚、切换邮箱刷新及 SettingsView 开关。
- 创建：`anna-inbox/src/features/settings/inboxSettings.ts` — 默认值、前端夹紧、时间段标签和 Important 分组的纯函数。
- 创建：`anna-inbox/src/features/settings/inboxSettings.test.ts` — 纯函数与显示顺序测试。
- 创建：`anna-inbox/src/features/settings/SettingsView.tsx` — 全页设置 UI 与禁用的分类框架。
- 修改：`anna-inbox/src/features/home/HomeView.tsx`、`anna-inbox/src/features/home/HomeView.test.ts` — 齿轮入口、SettingsView 分支、时间标签、Important 分段渲染。
- 修改：`anna-inbox/src/features/drawers/Drawers.tsx` — 删除 Sources 入口和旧设置内容，不影响 History/Memory。
- 修改：`anna-inbox/src/styles/global.css` — 设置页、账号栏齿轮、分段与窄屏样式，复用现有 token。

### 任务 1：定义前后端设置契约

**文件：**
- 修改：`inbox-tool/src/mail_agent/storage/types.py`
- 修改：`anna-inbox/src/types/mail.ts`
- 创建：`anna-inbox/src/features/settings/inboxSettings.ts`
- 测试：`anna-inbox/src/features/settings/inboxSettings.test.ts`

- [ ] **步骤 1：编写前端纯函数的失败测试**

```ts
import { describe, expect, it } from "vitest";
import { DEFAULT_INBOX_SETTINGS, clampInboxSettings, splitImportantMessages } from "./inboxSettings";

it("uses the agreed defaults and clamps invalid values", () => {
  expect(DEFAULT_INBOX_SETTINGS).toMatchObject({ display_range_days: 30, time_section_mode: "detailed", stars_enabled: true, stars_limit: 10, todos_enabled: true, todos_limit: 10 });
  expect(clampInboxSettings({ display_range_days: 9, stars_limit: 1000 })).toMatchObject({ display_range_days: 30, stars_limit: 50 });
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`cd anna-inbox && npm test -- inboxSettings.test.ts`

预期：FAIL，提示找不到 `./inboxSettings`。

- [ ] **步骤 3：定义一致的 DTO 和默认值**

在 `types.py` 添加：

```py
@dataclass
class InboxSettings:
    mailbox: str
    display_range_days: int = 30
    time_section_mode: str = "detailed"  # detailed | recent_then_months | months_only
    stars_enabled: bool = True
    stars_limit: int = 10
    todos_enabled: bool = True
    todos_limit: int = 10
    updated_at: str = field(default_factory=_now)

    @classmethod
    def empty(cls, mailbox: str) -> "InboxSettings":
        return cls(mailbox=mailbox)
```

在 `mail.ts` 镜像该字段，额外定义 `InboxSettingsPayload { settings; etag }`；在 `inboxSettings.ts` 导出 `DEFAULT_INBOX_SETTINGS`、显示范围白名单 `[7, 30, 60]`、数量夹紧区间 `1..50` 和时间分组枚举。`splitImportantMessages` 必须先去重、再按 `stars -> todos -> remainder` 返回分段。

- [ ] **步骤 4：运行前端纯函数测试验证通过**

运行：`cd anna-inbox && npm test -- inboxSettings.test.ts`

预期：PASS。

### 任务 2：实现 mailbox-scoped 存储与 Executa 工具

**文件：**
- 修改：`inbox-tool/src/mail_agent/storage/ops.py`
- 修改：`inbox-tool/src/anna_inbox_executa/common.py`
- 修改：`inbox-tool/src/anna_inbox_executa/dispatcher.py`
- 修改：`inbox-tool/src/anna_inbox_executa/v2_tools.py`
- 创建：`inbox-tool/src/tests/test_inbox_settings_storage.py`

- [ ] **步骤 1：编写后端存储和工具边界的失败测试**

```py
async def test_inbox_settings_are_mailbox_scoped_and_etag_protected():
    first = await get_inbox_settings("one@example.com")
    assert first["settings"].display_range_days == 30
    saved = await set_inbox_settings("one@example.com", {"display_range_days": 60}, if_match=first["etag"])
    assert saved["settings"].display_range_days == 60
    other = await get_inbox_settings("two@example.com")
    assert other["settings"].display_range_days == 30
```

- [ ] **步骤 2：运行脚本验证失败**

运行：`uv --directory inbox-tool/src run python tests/test_inbox_settings_storage.py`

预期：FAIL，提示 `get_inbox_settings` 未定义。

- [ ] **步骤 3：通过 ops、manifest 和 V2 dispatcher 实现最小读写路径**

在 `ops.py` 使用：

```py
def _inbox_settings_key(mailbox: str) -> str:
    return f"{_mailbox_prefix(mailbox)}/inbox_settings"

async def get_inbox_settings(mailbox: str) -> dict[str, Any]:
    result = await get_storage().get(_inbox_settings_key(mailbox), scope=default_scope())
    raw = result.get("value") if result.get("exists") and isinstance(result.get("value"), dict) else {}
    return {"settings": InboxSettings(mailbox=mailbox, **allowed_fields(raw)), "etag": str(result.get("etag") or "")}
```

`set_inbox_settings` 只能接受六个公开字段、夹紧展示范围和数量，并以 `if_match` 写入。`common.py` 声明 `mailbox`、可选 `if_match` 和可选字段；`dispatcher.py` 将两个名称加入 V2 allowlist；`v2_tools.py` 在空邮箱时返回错误，读取时返回 `{ mailbox, settings, etag }`，保存时返回同一结构及 `ok: true`。后端新增的函数、夹紧规则和异常分支必须附中文注释。

- [ ] **步骤 4：运行后端脚本和协议冒烟测试**

运行：

```sh
uv --directory inbox-tool/src run python tests/test_inbox_settings_storage.py
printf '%s\n' '{"jsonrpc":"2.0","method":"describe","id":1}' | uv --directory inbox-tool/src run anna-inbox-executa
printf '%s\n' '{"jsonrpc":"2.0","method":"health","id":1}' | uv --directory inbox-tool/src run anna-inbox-executa
```

预期：测试 PASS；`describe` 包含两个工具；`health` 返回 `healthy`，且 stdout 只有 JSON-RPC 响应。

### 任务 3：连接 facade、控制器和账户切换

**文件：**
- 修改：`anna-inbox/src/api/mailAgentClient.ts`
- 修改：`anna-inbox/src/api/mailAgentClient.test.ts`
- 修改：`anna-inbox/src/app/state.ts`
- 修改：`anna-inbox/src/app/useAppController.ts`

- [ ] **步骤 1：编写 facade RPC 参数的失败测试**

```ts
it("gets and saves inbox settings through the facade", async () => {
  await client.saveInboxSettings("one@example.com", { display_range_days: 60 }, "etag-1", "local");
  expect(invoke).toHaveBeenLastCalledWith("save_inbox_settings", expect.objectContaining({ mailbox: "one@example.com", display_range_days: 60, if_match: "etag-1", storage_provider: "local" }), expect.anything());
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`cd anna-inbox && npm test -- mailAgentClient.test.ts`

预期：FAIL，提示 `saveInboxSettings` 不存在。

- [ ] **步骤 3：实现 facade 与 controller 状态机**

新增 facade 方法 `loadInboxSettings(mailbox, storageProvider)` 与 `saveInboxSettings(mailbox, patch, ifMatch, storageProvider)`。在 AppState 增加 `settingsOpen`、`inboxSettings`、`inboxSettingsEtag`、`inboxSettingsLoading`、`inboxSettingsError`；在 AppActions 增加 `openSettings`、`closeSettings`、`loadInboxSettings`、`saveInboxSettings`。

保存流程必须遵循：先记录 previous settings/etag -> 乐观应用 patch -> 调用 facade -> 成功后更新 etag -> 失败后恢复 previous 并 `showToast(error)`。`switchMailbox` 成功切换后调用 `loadInboxSettings(nextMailbox)`；`loadInboxSettings` 以请求邮箱与最新 state.mailbox 比较，丢弃过期响应。

- [ ] **步骤 4：运行 facade 与既有账户选择测试**

运行：`cd anna-inbox && npm test -- mailAgentClient.test.ts src/app/mailboxSelection.test.ts`

预期：PASS，且现有账户切换测试不回归。

### 任务 4：实现全页 Settings 和移除 Sources

**文件：**
- 创建：`anna-inbox/src/features/settings/SettingsView.tsx`
- 修改：`anna-inbox/src/features/home/HomeView.tsx`
- 修改：`anna-inbox/src/features/drawers/Drawers.tsx`
- 修改：`anna-inbox/src/styles/global.css`

- [ ] **步骤 1：编写 Settings 视图的失败测试**

在 `HomeView.test.ts` 覆盖：齿轮按钮存在且点击打开 Settings；30 days 默认被选中；自定义分类控件 disabled 并显示 `Coming soon`；返回按钮关闭页面。

- [ ] **步骤 2：运行测试验证失败**

运行：`cd anna-inbox && npm test -- HomeView.test.tsx`

预期：FAIL，提示缺少 Settings 入口或预期文案。

- [ ] **步骤 3：实现页面、账号栏入口和样式**

在 `AccountRail` 的头像按钮后加入具有 `aria-label="Open settings"` 的齿轮按钮，调用 `actions.openSettings()`。当 `settingsOpen` 为真时，HomeView 渲染 SettingsView 并继续渲染 AccountRail。

SettingsView 仅使用 props `settings`、`loading`、`error`、`onChange`、`onBack`：展示三个展示范围按钮、三种 radio 时间分组、Stars/Todos switch 和 `1..50` 数量选择；所有事件调用 `onChange` 提交最小 patch。自定义分类使用禁用的 name/query/创建按钮和 `Coming soon` 提示。删除 `SourcesDrawer`、`sourcesOpen` 及 `openSourcesWithConfig` 的调用点，保留 History 与 Memory drawer 行为。

CSS 使用现有颜色/圆角 token：Settings 主内容为 max-width 容器，桌面双列仅用于 Stars/Todos，窄屏单列；Important 分段和可横向滚动 tab 的样式留给任务 5。

- [ ] **步骤 4：运行页面测试验证通过**

运行：`cd anna-inbox && npm test -- HomeView.test.tsx`

预期：PASS。

### 任务 5：将配置应用到邮件主页

**文件：**
- 修改：`anna-inbox/src/features/home/HomeView.tsx`
- 修改：`anna-inbox/src/features/home/HomeView.test.ts`
- 修改：`anna-inbox/src/styles/global.css`

- [ ] **步骤 1：编写显示范围、时间分段、Important 排序的失败测试**

```ts
it("renders starred then todos before remaining important messages", () => {
  const groups = splitImportantMessages(messages, { ...DEFAULT_INBOX_SETTINGS, stars_limit: 1, todos_limit: 1 });
  expect(groups.map((group) => group.kind)).toEqual(["stars", "todos", "important"]);
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`cd anna-inbox && npm test -- HomeView.test.tsx inboxSettings.test.ts`

预期：FAIL，提示 Important 分段尚未渲染。

- [ ] **步骤 3：以设置值驱动加载与渲染**

将 HomeView 的 `DEFAULT_INBOX_FEED_WINDOW.days` 初始化、`syncInbox(days)`、首次加载和 “Show older” 文案统一改为 `inboxSettings.display_range_days`；设置范围变化时重置 feed 并以新天数重新加载当前账号邮件。

把现有 `groupLabel` 改为接收 `time_section_mode`：`detailed` 产生 Today/Yesterday/Last 7 Days/months，`recent_then_months` 产生 Last 7 Days/months，`months_only` 只产生月份。Important tab 使用 `splitImportantMessages` 先渲染 Stars、Todos 分段（只在启用且有邮件时），再渲染剩余 Important；其余 Inbox 分组继续遵从时间分组。不得改变 Gmail star/todo 写操作或其 guardrail。

- [ ] **步骤 4：运行针对性测试并执行完整前端验证**

运行：

```sh
cd anna-inbox
npm test -- HomeView.test.tsx inboxSettings.test.ts mailAgentClient.test.ts
npm test
npm run build
```

预期：所有 Vitest 测试 PASS；TypeScript typecheck 与 Vite build 成功。

### 任务 6：同步文档与提交前检查

**文件：**
- 修改：`anna-inbox/docs/2.0.15架构与发布基线.md` — 仅在任务要求“准备提交”时同步版本、进度和设置架构。
- 修改：`anna-inbox/docs/README.md` — 仅在需要目录索引时增加本设计/实现文档链接。

- [ ] **步骤 1：检查变更范围和既有工作树冲突**

运行：`git status --short && git diff --check`

预期：无空白错误；任何 `useAppController.ts`、多账号文件的既有变更均保留并在实现前人工合并。

- [ ] **步骤 2：执行最终协议与前端验证**

运行：

```sh
uv --directory inbox-tool/src run python tests/test_inbox_settings_storage.py
printf '%s\n' '{"jsonrpc":"2.0","method":"describe","id":1}' | uv --directory inbox-tool/src run anna-inbox-executa
printf '%s\n' '{"jsonrpc":"2.0","method":"health","id":1}' | uv --directory inbox-tool/src run anna-inbox-executa
cd anna-inbox && npm test && npm run build
```

预期：全部成功。未收到“准备提交”指令前，不更新版本、不暂存、不提交。
