# Google Connected Accounts 多邮箱接入实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 Anna Inbox 显式声明 Google Connected accounts，并在平台多账号查询失败时向用户报告配置问题，而不是静默退化为默认单邮箱。

**架构：** 前端仍只经由 `mailAgentClient.ts` 调用 bundled Executa。Executa 的 `credentials/listAccounts` 保持为账户 metadata 的权威来源，并记录无敏感数据的本次查询状态；邮箱列表接口将该状态一并返回。前端读取状态后保留已发现账户，但显示明确的授权提示。

**技术栈：** Anna App manifest、Executa JSON-RPC 2.0、Python 标准库脚本式测试、React/TypeScript、Vitest。

---

## 文件结构

- 修改：`anna-inbox/manifest.json` — 声明 iframe 可使用的 Google Connected accounts Host API 权限。
- 修改：`inbox-tool/src/anna_inbox_executa/common.py` — 记录安全的 credentials Reverse RPC 查询状态，并提供只读访问函数。
- 修改：`inbox-tool/src/anna_inbox_executa/mailbox_tools.py` — 将发现结果及 credentials 状态返回给前端；平台运行时不把授权失败伪装成成功的单账号发现。
- 修改：`inbox-tool/src/tests/test_multi_token_integration.py` — 覆盖平台两账号发现、credentials grant 缺失和兼容回退。
- 修改：`anna-inbox/src/types/mail.ts` — 定义邮箱列表返回的授权状态类型。
- 修改：`anna-inbox/src/api/mailAgentClient.ts` — 让 `listMailboxes` 返回新类型。
- 修改：`anna-inbox/src/app/useAppController.ts` — 展示可操作的 Connected accounts 配置提示，且不改变账户切换行为。
- 创建：`anna-inbox/src/app/useAppController.test.ts` — 断言授权状态到用户提示文案的映射。
- 修改：`anna-inbox/src/api/mailAgentClient.test.ts` — 断言 facade 原样返回邮箱列表的授权状态。
- 修改：`anna-inbox/docs/多邮箱接入方案.md` — 说明 Connected accounts grant 和 `credentials_status` 的可恢复错误语义。

### 任务 1：声明平台权限

**文件：**
- 修改：`anna-inbox/manifest.json`

- [ ] **步骤 1：增加 Google credentials Host API 声明**

在 `ui.host_api` 中增加 `credentials`，不修改现有 `tools`、`storage`、`llm` 或 CSP：

```json
"credentials": [
  "google"
]
```

- [ ] **步骤 2：验证 manifest JSON 可解析**

运行：

```powershell
Get-Content -Raw anna-inbox/manifest.json | ConvertFrom-Json | Out-Null
```

预期：命令退出码为 `0`，不输出 JSON 解析错误。

### 任务 2：先建立平台授权失败的后端回归用例

**文件：**
- 修改：`inbox-tool/src/tests/test_multi_token_integration.py`

- [ ] **步骤 1：编写失败测试，模拟平台 grant 被拒绝**

在 `TestMultiTokenIntegration.run()` 中调用新用例 `test_platform_credentials_error_is_not_silent_single_account_fallback()`。该用例将：

```python
with patch.object(common, "_platform_credentials_ready", True), \
     patch.object(common.platform_credentials, "list_accounts", side_effect=CredentialsError(-32061, "not granted")), \
     patch("anna_inbox_executa.mailbox_tools._is_platform", return_value=True):
    payload = _sync_list_mailboxes()

check("credentials status requires grant", payload["credentials_status"]["code"], "not_granted")
check("credentials status has action", payload["credentials_status"]["action"], "enable_connected_accounts")
```

同时断言 `payload["credentials_status"]` 不包含 `access_token`、`refresh_token`、`credentials_token` 或完整异常 data。

- [ ] **步骤 2：运行测试并确认当前实现失败**

运行：

```powershell
uv --directory inbox-tool/src run python tests/test_multi_token_integration.py
```

预期：失败，原因是当前 `list_mailboxes` payload 不含 `credentials_status`，且 `refresh_platform_google_accounts()` 将 `CredentialsError` 静默转换为空账号列表。

### 任务 3：实现安全的 Reverse RPC 状态边界

**文件：**
- 修改：`inbox-tool/src/anna_inbox_executa/common.py`
- 修改：`inbox-tool/src/anna_inbox_executa/mailbox_tools.py`

- [ ] **步骤 1：在 `common.py` 创建只保存安全字段的状态函数**

在 `platform_credentials` 初始化位置附近新增锁保护状态和访问器；状态只包含 `available`、`code`、`message`、`action`：

```python
_platform_credentials_status_lock = threading.RLock()
_platform_credentials_status = {
    "available": False,
    "code": "not_checked",
    "message": "Google Connected accounts have not been checked.",
    "action": "retry",
}

def _set_platform_credentials_status(*, available: bool, code: str, message: str, action: str) -> None:
    with _platform_credentials_status_lock:
        _platform_credentials_status.update({
            "available": available,
            "code": code,
            "message": message,
            "action": action,
        })

def get_platform_credentials_status() -> dict[str, Any]:
    with _platform_credentials_status_lock:
        return dict(_platform_credentials_status)
```

- [ ] **步骤 2：让 `refresh_platform_google_accounts()` 分类结果**

成功列出账户后设为 `available=True`、`code="ok"`、`action="none"`。协议非 2.0 时设为 `code="protocol_unsupported"`、`action="upgrade_runtime"`；`CredentialsError` 的 `-32061` 映射为 `code="not_granted"`、`action="enable_connected_accounts"`；其它错误只返回类别化的 `code="unavailable"`，不得暴露 `exc.data`。

```python
except CredentialsError as exc:
    if exc.code == -32061:
        _set_platform_credentials_status(
            available=False,
            code="not_granted",
            message="Enable Google Connected accounts for Anna Inbox, then retry.",
            action="enable_connected_accounts",
        )
    else:
        _set_platform_credentials_status(
            available=False,
            code="unavailable",
            message="Google connected-account discovery is temporarily unavailable.",
            action="retry",
        )
    log(f"platform Google account listing unavailable: {exc.code}")
    return []
```

- [ ] **步骤 3：把状态加入 `list_mailboxes` payload**

在 `_sync_list_mailboxes()` 内，在 `_discover_mailboxes()` 后读取状态并返回：

```python
return {
    "mailboxes": mailboxes,
    "selected": [item["email"] for item in mailboxes if item.get("selected")],
    "discovered": discovered,
    "credentials_status": get_platform_credentials_status(),
}
```

在 `_discover_mailboxes()` 中只有当 status 为 `not_checked` 或 `ok` 时才进入 legacy 默认 token 分支；当平台运行时的状态为 `not_granted`、`protocol_unsupported` 或 `unavailable` 时保留已有 registry 由调用层展示，但不合成默认平台邮箱。

- [ ] **步骤 4：运行后端回归测试并确认通过**

运行：

```powershell
uv --directory inbox-tool/src run python tests/test_platform_credentials_client.py
uv --directory inbox-tool/src run python tests/test_multi_token_integration.py
```

预期：两条命令均输出成功摘要；双账号账户按默认账号优先排列，grant 缺失返回 `enable_connected_accounts` 状态且没有敏感字段。

### 任务 4：把授权状态接入前端而不改动切换模型

**文件：**
- 修改：`anna-inbox/src/types/mail.ts`
- 修改：`anna-inbox/src/api/mailAgentClient.ts`
- 修改：`anna-inbox/src/app/useAppController.ts`
- 创建：`anna-inbox/src/app/useAppController.test.ts`
- 测试：`anna-inbox/src/api/mailAgentClient.test.ts`
- 修改：`anna-inbox/docs/多邮箱接入方案.md`

- [ ] **步骤 1：编写前端失败测试**

在 `useAppController.ts` 中先声明将由 `loadMailboxes()` 调用的纯函数 `connectedAccountsStatusMessage(status)`；在新建的 `useAppController.test.ts` 中为以下状态编写断言：

```ts
expect(connectedAccountsStatusMessage({
  available: false,
  code: "not_granted",
  message: "Enable Google Connected accounts for Anna Inbox, then retry.",
  action: "enable_connected_accounts",
})).toBe("Enable Google Connected accounts for Anna Inbox, then retry.");
```

在 `mailAgentClient.test.ts` 中 mock tools invoke 返回两个邮箱和同一 `credentials_status`，再断言 facade 不丢弃该字段：

```ts
await expect(client.listMailboxes("aps")).resolves.toMatchObject({
  mailboxes: [{ email: "personal@example.com" }, { email: "work@example.com" }],
  credentials_status: { action: "enable_connected_accounts" },
});
```

- [ ] **步骤 2：运行前端测试确认失败**

运行：

```powershell
cd anna-inbox
npm test -- --run useAppController mailAgentClient
```

预期：失败，因为状态提示函数和 `MailboxListPayload` 均不存在，facade 也未声明 `credentials_status`。

- [ ] **步骤 3：定义返回类型并读取状态**

在 `mail.ts` 新增：

```ts
export interface MailboxCredentialsStatus {
  available: boolean;
  code: "ok" | "not_checked" | "not_granted" | "protocol_unsupported" | "unavailable";
  message: string;
  action: "none" | "retry" | "enable_connected_accounts" | "upgrade_runtime";
}

export interface MailboxListPayload {
  mailboxes: MailboxInfo[];
  selected: string[];
  discovered?: MailboxInfo[];
  credentials_status?: MailboxCredentialsStatus;
}
```

使 `MailAgentClient.listMailboxes()` 返回 `MailboxListPayload`。在 `loadMailboxes()` 中，仅当 `credentials_status.action === "enable_connected_accounts"` 时调用已有 `showToast(credentialsStatus.message)`；邮箱数组和 `switchMailbox()` 的单活动账户逻辑保持不变。

新增纯函数时，只返回 `enable_connected_accounts` 或 `upgrade_runtime` 的安全提示，`ok`、`not_checked`、`retry` 返回空字符串：

```ts
export function connectedAccountsStatusMessage(status?: MailboxCredentialsStatus) {
  if (!status || (status.action !== "enable_connected_accounts" && status.action !== "upgrade_runtime")) return "";
  return status.message;
}
```

- [ ] **步骤 4：更新多邮箱接入文档**

在 `anna-inbox/docs/多邮箱接入方案.md` 的“发现来源”后补充：App manifest 必须声明 `ui.host_api.credentials: ["google"]`；生产环境还需授予该 App 的 Google Connected accounts。说明 `list_mailboxes.credentials_status` 只包含安全的 `code`、`message`、`action`，其中 `not_granted` 要求重新授予权限，`protocol_unsupported` 要求升级运行时，token 不会被返回。

- [ ] **步骤 5：运行前端定向测试确认通过**

运行：

```powershell
cd anna-inbox
npm test -- --run useAppController mailAgentClient
```

预期：目标测试通过，两个邮箱继续可见，提示不包含 token 或账户内部 ID。

### 任务 5：集成验证

**文件：**
- 修改：无

- [ ] **步骤 1：执行协议 smoke checks**

运行：

```powershell
printf '%s\n' '{"jsonrpc":"2.0","method":"describe","id":1}' | uv --directory inbox-tool/src run anna-inbox-executa
printf '%s\n' '{"jsonrpc":"2.0","method":"health","id":1}' | uv --directory inbox-tool/src run anna-inbox-executa
```

预期：stdout 每条仅一行 JSON-RPC 2.0 成功响应；`describe` 中 credentials 仍只声明 `GOOGLE_ACCESS_TOKEN`，不出现 token 值。

- [ ] **步骤 2：执行前端全量验证**

运行：

```powershell
cd anna-inbox
npm test
npm run build
```

预期：Vitest 全部通过，TypeScript typecheck 与 production build 均成功。

- [ ] **步骤 3：人工平台验收**

在 Anna 中重新加载开发 App 后，确认权限界面出现 Google Connected accounts；授权两个 Google 账户后，打开 Inbox，账户栏显示两个邮箱。切换第二个邮箱并刷新邮件，确认其 Gmail 请求只影响第二个账户。
