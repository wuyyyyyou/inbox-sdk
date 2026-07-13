# Google Connected Accounts 多邮箱接入设计

## 目标

让 Anna Inbox 使用 Anna 平台已授权的全部 Google 账户，而不是在 `credentials/listAccounts` 不可用时无提示地退化为默认注入账户。用户仍在 Anna 平台统一管理授权；应用不新增 OAuth 页面，也不接触 refresh token。

## 范围与约束

- App manifest 声明 Google Connected accounts Host API 能力，使平台能够展示并授予对应的应用权限。
- 邮箱账户发现继续通过 Executa 的 `credentials/listAccounts` Reverse RPC 完成；按账户换取短期 token 的逻辑保持在后端。
- 仅返回账户元数据和可安全展示的授权错误；不得返回、记录或持久化 access token、refresh token 或完整 credential context。
- 不改变 Gmail 的发送、标签、删除等已有显式用户操作 guardrail。

## 方案

1. 在 `anna-inbox/manifest.json` 的 `ui.host_api` 下声明 `credentials: ["google"]`。
2. 后端账户发现记录一次本次请求的 credentials Reverse RPC 可用性：
   - 成功时以平台返回的所有账号 metadata 作为权威来源。
   - 平台未授予 Connected accounts、协议非 2.0 或请求失败时，向前端返回安全的配置状态与处理提示。
   - 只在本地开发/旧凭据兼容场景使用单账号 token 回退；平台模式下不得把权限失败伪装成单账号发现成功。
3. 前端加载邮箱时保留已有邮箱切换模型；当发现结果表示 Connected accounts 未配置时，展示可操作提示，说明需在 Anna 授予 Google Connected accounts 后重试。

## 数据流

`Anna Authorizations` → `credentials/listAccounts` → Executa mailbox discovery → mailbox registry → Inbox account selector

选择邮箱后：

`mailbox email` → 内存中的 `account_id` → `credentials/getToken` → 后端 Gmail 请求

短期 token 只存在于当前后端调用，不进入前端或持久化存储。

## 错误处理

- `APP_NOT_GRANTED` / credentials grant 缺失：展示“为 Anna Inbox 启用 Google Connected accounts 后重试”。
- Host 不是 Executa protocol 2.0：展示运行时不支持多账号的提示，保留本地开发兼容行为。
- 单个账户的 token 或 Gmail scope 失败：只标记该账户不可用，不影响其他已发现账户。

## 验收与验证

- manifest 包含 Google credentials Host API 声明。
- 模拟 `listAccounts` 返回两个账户时，账户栏显示两个账户，且默认账户排序第一。
- 模拟 credentials grant 缺失时，前端得到明确配置错误，不再静默只显示默认账户。
- 选择第二个邮箱时，`getToken` 使用其对应的 `account_id`。
- 运行相关后端脚本测试、前端测试和生产构建；若 manifest 或 dispatcher 有变更，再运行 `describe` 和 `health` 协议检查。
