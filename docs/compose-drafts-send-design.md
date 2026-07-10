# Compose、Drafts 与 AI 发送设计

## 目标与范围

为 Anna Inbox 2.0 增加新建邮件（Compose）能力，并基于 Anna 应用内草稿提供保存草稿、延迟 10 秒的可撤销发送、草稿批量发送，以及由 AI 对话驱动的发送任务。草稿不创建、更新或同步至 Gmail Drafts；其持久化沿用当前 `storage_provider`，可存于 local 或 APS。

本设计保留现有 Inbox、邮件详情和 AI 侧栏的布局与视觉语言。前端工具调用继续统一经过 `anna-inbox/src/api/mailAgentClient.ts`，后端继续使用 Gmail adapter 和 `mail_agent/storage/ops.py` 的高层入口。

不在本期范围内：Cc/Bcc、附件和富文本工具栏功能、任意时间的 Scheduled send、在 App/Agent 未运行时补发邮件、AI 未经最终用户操作直接发送邮件。

## 已确认的产品规则

### Compose

- 顶栏在 Refresh 右侧加入 Compose；必要时左移搜索框和 Refresh，复用现有按钮尺寸、颜色、图标和间距。
- 点击 Compose 打开独立的新建邮件编辑页，包含 `To`、`Subject` 和 `Content`。
- 支持一个或多个收件人；不提供 Cc/Bcc。
- `To` 支持按姓名或邮箱搜索 Google Contacts。选中联系人后显示头像、名称和移除按钮；有效的非联系人邮箱可点击候选项或按 Enter 加入；失焦后收起候选列表。
- 联系人来源包含“我的联系人”和 Google 自动保存的“其他联系人”。联系人数据仅用于搜索与展示名称、邮箱和头像。
- 顶栏 Close 只在用户点击时保存。To、Subject、Content 都为空时不保存；其他情况保存 Anna 应用内草稿，成功后关闭编辑页。
- Discard 清空内容、删除当前本地草稿（若已创建）并关闭编辑页。

### Save to drafts

- Send 右侧菜单中的原 Scheduled send 改为 `Save to drafts`，不再提供时间选择。
- 点击后保存当前非空邮件至 Anna 应用内 Drafts 并关闭编辑页。
- 操作完成后显示包含 `View` 的 Toast；`View` 跳转 Drafts 分类。
- 不提供 Undo，也不显示倒计时。用户可在 Drafts 中自行删除草稿。

### 普通发送与 Undo

- Send 仅在所有校验通过后可执行：至少一个有效收件人、非空 Subject、非空 Content。
- 点击 Send 后，应用先将邮件保存为 Anna 本地草稿，并创建本地内存中的 10 秒待发送任务；编辑页关闭，Toast 显示倒计时和 Undo。
- 倒计时结束后才调用 Gmail 发送，发送成功后刷新并跳转/展示 Sent。
- 点击 Undo 会取消待发送任务、删除刚创建或更新的待发送本地草稿，并恢复原编辑页及其内容。
- 10 秒期间不会把邮件乐观地同步为 Gmail Sent。
- 若 App 或 Agent 在这 10 秒内退出、挂起或重启，内存任务不会补发；本地草稿保留在 Drafts，用户可重新编辑或手动发送。
- 若倒计时后的 Gmail send 失败，草稿保留在 Drafts，并向用户展示错误；不得自动重试或误报成功。

### Drafts 批量一键发送

- Drafts 分类支持多选，并显示批量发送操作。
- 每封本地草稿使用其已有的收件人、标题和正文；批量操作不会增加、覆盖或合并收件人，也不会把多封草稿合成一封邮件。
- 启动发送前必须重新校验全部已选 Draft。任意草稿缺少有效收件人、标题或正文时，阻止整个批次，并列出不完整草稿；不发送任何一封。
- 校验通过后，显示二次确认，至少说明草稿数量、每封主题及其收件人。用户确认是唯一触发实际发送流程的操作。
- 确认后将所有选中 Draft 归为同一个 10 秒待发送批次，并显示一个带倒计时和 Undo 的 Toast。
- 批次 Undo 取消整个批次，所有草稿保持在 Drafts，并恢复原有多选状态。
- 倒计时结束后逐封执行 Gmail 发送。运行期个别失败不阻断其余发送尝试；成功项从本地 Drafts 移除并进入 Sent，失败项保留在 Drafts，并在结果中逐项报告。

### AI 一键发送

- AI 侧栏接受自然语言发送任务，可完成：创建单封邮件、保存草稿、定位并批量发送指定 Drafts、为不同收件人生成不同正文。
- AI 可使用联系人搜索结果和 Draft 元数据理解指令；例如用户可按草稿主题、收件人或内容描述指出要发送的 Draft。
- AI 不得直接调用 Gmail send。它必须先呈现可审阅的发送任务卡：收件人、主题、正文或各 Draft、个性化差异、发送数量与预计操作。
- 用户点击任务卡的最终确认按钮后，才进入与手动操作相同的验证、二次确认（批量）和 10 秒待发送流程。
- 对个性化群发，AI 生成一封独立本地草稿对应每位收件人，并在任务卡中可逐项审阅；不允许把不同正文混入同一封多收件人邮件。

## 状态模型

```text
editing
  ├─ Close / Save to drafts → local_draft_saved → closed
  ├─ Discard → discarded → closed
  └─ Send (valid) → local_draft_saved + pending_send(10s)
                              ├─ Undo → local_draft_deleted + editing
                              ├─ timer expires → gmail_sent + local_draft_deleted → sent
                              ├─ send error → local_draft_saved + error
                              └─ App/Agent stops → local_draft_saved

drafts_selected
  ├─ validation error → selection_preserved + error
  └─ confirmation → pending_batch_send(10s)
                        ├─ Undo → selection_preserved
                        └─ timer expires → per-draft send results
```

待发送计时器只存在于正在运行的前端/Agent 会话。它不是后台调度服务，因此不会违反“挂起后不补发”的产品规则。

## 技术设计

### Gmail 与 OAuth

- 新建、更新、删除应用内草稿通过 `mail_agent/storage/ops.py` 的高层入口完成，并传递当前 `storage_provider`；草稿内容不写入 Gmail Drafts，也不与 Gmail Drafts 双向同步。`storage_provider=local` 时保存在本机，`storage_provider=aps` 时保存在 APS 的适当存储介质。
- 倒计时结束后，前端才调用 Gmail 发送接口。发送成功后删除对应本地草稿并刷新 Sent；发送失败时保留本地草稿。
- 联系人搜索接入 Google People API：
  - 我的联系人：`contacts.readonly`；
  - 自动保存的其他联系人：`contacts.other.readonly`。
- 更新 OAuth 授权申请、manifest 和相关开发文档，并引导已有用户重新授权。不得把 access token、refresh token、authorization header 或完整 credential context 写入日志、状态或结果。
- 仅请求姓名、邮箱和头像等必要字段。搜索结果应限制条数、按输入防抖，并在 UI 中使用短时内存缓存；不将大批联系人写入 APS KV。

### 前端边界

- 新建 Compose 视图/组件，复用邮件详情编辑器的 Send 与 AI Draft 视觉样式、已有 Toast、Snooze 时间风格和按钮体系。
- 扩展邮件 DTO 与前端状态以表达收件人、本地 Draft ID、编辑状态、批次选择、待发送倒计时和 AI 发送任务卡。
- 所有后端工具调用只能由 `mailAgentClient.ts` 暴露；组件中不得散落工具名称。
- To 候选列表应支持键盘上下选择、Enter 确认和点击外部关闭；移除收件人不会删除 Google Contact。

### 后端边界

- Gmail adapter 提供 MIME 编码后的 Compose 发送及联系人搜索的适配层；状态改变均要求前端明确用户动作。
- 本地草稿通过 `mail_agent/storage/ops.py` 的高层入口保存、读取、更新和删除；不绕过该入口，不将大内容放入 APS KV。
- Executa 增加与 facade 一一对应的工具边界：搜索联系人、创建/更新/删除/列出本地 Compose Draft、发送单封 Compose 邮件、发送多封 Compose 邮件。发送多个邮件返回逐项结果，不在工具层创建后台定时器。
- 10 秒计时和 Undo 是前端工作流：计时结束后才请求发送工具。后端不存储可延迟执行的发送指令，不保留凭据，也不在进程恢复后重放发送。
- AI 只生成结构化“发送计划”或 Draft 内容；其计划需由前端展示并由用户明确确认，再调用实际状态改变工具。

## 错误处理与可访问性

- 本地 Draft 保存失败时留在编辑页，不清空用户输入，并提示可重试错误。
- 发送、批量确认、Undo、View、收件人移除和联系人候选均应具备可访问名称、键盘操作及禁用/加载状态。
- 校验错误靠近对应字段显示，并在批量场景汇总到确认入口；不以静默跳过替代明确报错。
- Toast 明确说明“将在 X 秒后发送”或“Draft saved”；发送完成/失败后以单独结果反馈。

## 验收与测试

### 前端

- Compose 打开、布局适配、联系人搜索/键盘/失焦、原始邮箱收件人、Close、Discard、Save to drafts。
- 单封 Send 的必填校验、10 秒计时、Undo、正常成功、App/Agent 中断后保留 Draft、Gmail 失败。
- Draft 多选、全量校验阻断、确认弹窗、批次倒计时、批次 Undo、部分发送失败。
- AI 单封、指定 Draft 批量、个性化群发任务卡和“未确认不发送”守卫。

### 后端

- 本地 Compose Draft 的 DTO、存储边界、MIME 发送与错误映射。
- People API 两类联系人授权不足、搜索结果和头像回退。
- 协议与 dispatcher 变更必须执行：

```sh
printf '%s\n' '{"jsonrpc":"2.0","method":"describe","id":1}' | uv --directory inbox-tool/src run anna-inbox-executa
printf '%s\n' '{"jsonrpc":"2.0","method":"health","id":1}' | uv --directory inbox-tool/src run anna-inbox-executa
```

### 全量验证

```sh
cd anna-inbox
npm test
npm run build
```

并按实际后端改动范围运行 `inbox-tool/src/tests/` 中对应脚本。
