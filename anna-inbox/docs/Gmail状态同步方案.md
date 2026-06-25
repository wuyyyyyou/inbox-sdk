# 工具与 Gmail 状态同步方案

> 创建时间：2026-06-23  
> 状态：方案设计，尚未实施代码改动。  
> 目标需求：工具和 Gmail 状态应双向同步。用户在工具里完成回复/已读操作后，Gmail 也应体现；用户在 Gmail 里回复或标记已读后，下一次 Brief 扫描结束时，工具内卡片状态应与 Gmail 完全一致。

## 1. 需求拆解

用户原始需求包含两类同步：

1. 工具 -> Gmail
   - 在工具里真正发送回复后，Gmail thread 中应出现该回复。
   - 对应 Gmail 邮件应被标为已读。
   - 工具内卡片应变为 `resolved/replied`，不再作为待回复事项出现。

2. Gmail -> 工具
   - 用户在 Gmail 中手动回复后，下一次 Brief 扫描应识别该 thread 已由用户回复。
   - 用户在 Gmail 中手动标记已读、删除/归档等状态变化后，工具内卡片应随扫描同步。
   - 两边可以有时间差；不需要实时，但“再次扫描完成后”必须一致。

关键产品约束：Gmail 没有一个可任意设置的“replied=true”字段。所谓“已回复”必须以真实 Gmail thread 中出现用户本人发出的 `SENT` 邮件为准；不能通过本地状态伪造 Gmail 已回复。

## 2. 当前代码基线

已存在能力：

- `reply_now` 已能调用 Gmail `messages/send` 真正发送回复，成功后本地卡片标记为 `resolved/replied`。
- `mark_cleanup_read`、`mark_read_from_ask` 已能调用 Gmail `batchModify` 移除 `UNREAD` label。
- Brief 扫描中已有 `check_replied` 阶段，会对新候选 thread 调用 `_thread_latest_is_from_owner()`，如果最新邮件来自 mailbox owner，则过滤掉候选。
- `ScanState` 已有 `last_history_id` 字段，Gmail message cache 也保存 `history_id`，但当前短 invoke finalizing 尚未写入/消费 `last_history_id`。
- active cards 持久化在 `mailbox/<mailbox>/cards/active_*`，卡片有 `message_id`、`thread_id`、`status`、`resolution`、`details.mailbox`。

主要缺口：

- `check_replied` 只过滤“本次扫描新产生的候选”，不会 reconcile 已经持久化的 active cards。
- `record_card_decision("handled_manually")` 只改本地卡片，不验证 Gmail 中是否真的已回复。
- App 内回复后未统一刷新 Gmail thread state / cache / read label，容易出现本地 resolved 但 cache stale。
- 下一次扫描时，processed message index 可能跳过旧 message；如果不单独同步 active cards，就无法发现旧卡片对应 thread 的 Gmail 状态变化。
- `last_history_id` 尚未形成 Gmail History API 增量同步闭环。

## 3. 状态定义

以 Gmail 为跨端事实来源，本地状态只作为 UI 与历史记录缓存。

| 概念 | 判断来源 | 规则 |
|---|---|---|
| 已回复 | Gmail thread | thread 中存在 mailbox owner 发出的 message，且该 message `internalDate` 晚于卡片锚点 message |
| 已读 | Gmail labels | 卡片相关 message 或 thread 最新 inbound message 不含 `UNREAD` |
| 已删除/不可见 | Gmail API | message/thread `404`、`TRASH`、或不再符合可处理范围 |
| 待处理 | Gmail thread + 本地卡片 | 最新有效消息来自对方，且卡片未 resolved/dismissed/snoozed |

注意：

- DRAFT 不等于已回复。
- owner 匹配必须按 email address，不按 display name。
- 需要考虑 Gmail alias / send-as 地址；第一阶段可先用 mailbox email，后续补 `users.settings.sendAs.list`。
- “手动处理完成”不能自动等价为 Gmail 已回复，除非 Gmail thread 中确实出现 owner 的新 sent message。

## 4. 总体架构

新增一个后端领域服务，建议命名为 `mail_agent/sync/gmail_status.py`：

```text
Brief scan start
  -> reconcile_active_cards_with_gmail(mailbox)
  -> run_mail_scan / Phase 1 / check_replied / Phase 2
  -> persist cards
  -> save ScanState(last_message_internal_date, last_history_id)

Tool action(reply_now / mark_read / handled)
  -> perform Gmail write
  -> refresh affected thread state
  -> update local card + cache + history
```

核心服务职责：

- 读取某个 mailbox 的 active cards。
- 批量或分页获取对应 Gmail thread 的轻量状态。
- 比较 Gmail state 与卡片锚点状态。
- 更新本地卡片 `status/resolution/resolved_at/updated_at`。
- 更新 Gmail cache 中相关 message 的 label/thread metadata，避免 UI 读到旧 cache。
- 返回同步摘要给 Brief progress 和 run history。

建议返回结构：

```json
{
  "ok": true,
  "mailbox": "user@example.com",
  "checked_threads": 24,
  "resolved_replied": 3,
  "marked_read": 8,
  "removed_missing": 1,
  "reopened_new_inbound": 2,
  "last_history_id": "123456",
  "warnings": []
}
```

## 5. 工具 -> Gmail 同步

### 5.1 Reply now

当前 `reply_now` 已经真实发送 Gmail 回复。建议补齐为原子流程：

1. 校验 `mailbox + card_id`，必须在该 mailbox active cards 中存在。
2. 发送前 fetch thread state，确认卡片未过期：
   - 如果最新消息已经来自 owner，则直接将本地卡片标记为 `resolved/replied`，不重复发送。
   - 如果 thread 缺失或 latest inbound 与卡片不匹配，返回可解释错误。
3. 调用 Gmail `messages/send`。
4. 发送成功后，对该 thread 的 inbound messages 执行 `batch_mark_read`。
5. 刷新该 thread cache，并重新判断 `latest_from_owner=true`。
6. 本地卡片标记为：
   - `status="resolved"`
   - `resolution="replied"`
7. 写 run history 和 contact memory。

### 5.2 “已回复 / Handled manually”

如果 UI 保留 “Handled manually” 或新增 “I replied in Gmail”：

- 不能直接写 Gmail “已回复”。
- 点击后应先执行 thread reconcile：
  - 若 Gmail thread 最新 owner sent message 晚于卡片锚点，则本地标记 `resolved/replied`。
  - 若没有找到 owner sent message，则提示用户“Gmail 中还没有检测到这封回复”，可选择仅本地标记 `handled_manually`，但不能显示为 Gmail 已回复。

建议产品语义：

- `Reply now`：工具发送，必然同步 Gmail 已回复。
- `I replied in Gmail`：验证 Gmail 后标记已回复。
- `Handled manually`：只表示本地不再提醒，不承诺 Gmail 已回复。

### 5.3 Mark read / cleanup

已有 `batch_mark_read` 能移除 `UNREAD`。建议统一走同一个 Gmail write helper：

- 成功后更新 cache label_ids，移除 `UNREAD`。
- 对 cleanup bundle 同步更新 bundled item 的 read state。
- 对 review 卡的显式 `Read` 动作，按卡片锚点 `message_id` 标记 Gmail 已读，并将该卡片本地 resolve 为 `read`。
- `mark_card_read` 只允许 `user_action="review"` 的普通卡片使用；`reply` 卡不能因为“已读”而被错误清理。
- run history 中记录 Gmail write 结果，而不是只记录本地 UI 动作。

## 6. Gmail -> 工具同步

### 6.1 第一阶段：扫描前 active-card reconcile

这是满足当前用户需求的最小可靠方案。

在 `_brief_prepare_scan()` 开始拉取新邮件前，先调用：

```python
await reconcile_active_cards_with_gmail(mailbox, reason="scan_start")
```

它必须检查 active cards，而不是只检查新扫描 message。原因是：用户可能在 Gmail 中回复了旧卡片对应 thread，该 thread 没有新 inbound message；processed index 会跳过旧 message，普通扫描无法发现状态变化。

每张 active card 的判断：

1. Gmail thread 最新消息来自 owner，且时间晚于 card anchor
   - 本地卡片设为 `resolved/replied`。
   - 写 contact memory `owner_replied` observation。

2. Gmail message/thread 不含 `UNREAD`
   - 更新卡片外部状态为 `read`。
   - 对 cleanup bundle 可标为已读或从 cleanup bundle 中移除。
   - 对 `review` 卡，可在同步器中 resolve 为 `read_in_gmail`，表示 Gmail 侧已经读过、Brief 不再继续提醒。
   - 对 `reply` 卡，仅“已读”不等于“已处理”，不应自动 resolved。

3. Gmail thread 出现新的对方 inbound message，且晚于当前 card anchor
   - 旧卡片应被新扫描结果覆盖或重新打开为 pending。
   - 这类 thread 必须进入本轮扫描候选，即使旧 message_id 已 processed。

4. Gmail thread/message 已删除、进 Trash 或不可访问
   - 本地卡片标记 `dismissed` 或 `resolved/gmail_removed`。

### 6.2 第二阶段：Gmail History API 增量同步

第一阶段逐个 active card 拉 thread 状态，简单可靠，但 active cards 多时会消耗 API quota。第二阶段接入 Gmail History API：

- 在每轮扫描 finalizing 时保存 mailbox 的 `last_history_id`。
- 下一轮扫描前调用 `users.history.list(startHistoryId=last_history_id)`。
- 处理：
  - `messagesAdded`：发现新 inbound 或 owner sent reply。
  - `labelsAdded/labelsRemoved`：发现 `UNREAD`、`TRASH` 等变化。
  - `messagesDeleted`：移除或 dismiss 本地卡片。
- 如果 Gmail 返回 `404 historyId too old`，回退到第一阶段 full active-card reconcile。

History API 只作为优化入口，不应成为唯一同步机制。active-card reconcile 是兜底。

### 6.3 不建议第一阶段做 push/watch

`users.watch` + Pub/Sub 能实现近实时，但需要外部 webhook / Pub/Sub 配置，不适合当前 Executa stdio 本地开发和 Anna App 运行模型。当前需求允许时间差，所以先用“再次扫描时同步”即可。

## 7. 数据模型建议

第一阶段尽量少改 schema，但建议给 `PersistentCard` 增加轻量外部状态字段：

```python
gmail_state: dict = {
  "last_synced_at": "",
  "latest_message_id": "",
  "latest_internal_date": "",
  "latest_from_owner": false,
  "unread": false,
  "history_id": "",
  "missing": false
}
```

也可以拆为 dataclass `ExternalMailState`。用途：

- 判断卡片是否因 Gmail 外部状态变化而 resolved。
- 显示“Replied in Gmail”“Read in Gmail”等历史原因。
- 避免只靠 `status/resolution` 丢失同步证据。

`ScanState.last_history_id` 应正式写入：

- scan/reconcile 获取到 thread/message 的最大 `historyId`。
- finalizing 时保存。
- History API 失败时不覆盖旧值，避免丢增量窗口。

## 8. 与现有 Brief 管线的关系

需要保留现有 `check_replied`，但应抽象复用同一个 thread-state helper：

- `reconcile_active_cards_with_gmail()`：处理已有卡片。
- `check_replied_after_phase1()`：处理新候选。

共同依赖：

```text
fetch_gmail_thread_state(mailbox, thread_id)
  -> latest_message_id
  -> latest_internal_date
  -> latest_from_addr
  -> latest_from_owner
  -> unread_message_ids
  -> sent_message_ids
  -> trashed
  -> history_id
```

最终扫描顺序建议：

```text
queued
  -> sync_gmail_state
  -> scan
  -> storage_filter
  -> phase1
  -> check_replied
  -> phase2
  -> finalizing
```

`sync_gmail_state` 阶段应进入前端进度文案，例如 “Syncing Gmail state.”。

## 9. 多邮箱边界

- 同步必须按 mailbox 独立执行。
- 卡片操作继续以 `card.details.mailbox` 为准。
- `mailbox="all"` 只做聚合读取，不直接执行 Gmail write。
- 多邮箱 Brief 扫描时，每个 mailbox 开始扫描前先 reconcile 自己的 active cards。
- Ask result 的 mark read / reply / trash 继续使用 item mailbox。

## 10. 验收标准

核心用户路径：

1. 工具内 `Reply now` 成功后：
   - Gmail thread 出现新 sent reply。
   - 原 thread 相关 inbound messages 不再 unread。
   - 工具卡片显示 `Replied` 或从待处理列表消失。

2. 用户在 Gmail 中回复某张 active card：
   - 不打开工具操作。
   - 回到工具点 Brief 扫描。
   - 扫描结束后，该卡片不再作为 `Needs reply` pending card 出现。
   - History/状态显示可追溯为 `replied_in_gmail` 或 `replied`。

3. 用户在 Gmail 中标记 cleanup 邮件为已读：
   - 下一次扫描后 cleanup bundle 不再把这些邮件显示为 unread 待处理。

4. 用户在 Gmail 中收到同一 thread 的新回复：
   - 即使旧卡片之前 resolved，该新 inbound message 应重新进入 Brief 判断。

## 11. 测试计划

Python focused tests：

- `test_gmail_status_sync.py`
  - fake Gmail thread latest owner -> active card resolved/replied。
  - latest owner 早于 card anchor -> 不 resolved。
  - latest inbound 晚于 card anchor -> card 保持/重开 pending。
  - `UNREAD` label removed -> read state 更新，但 reply card 不自动 resolved。
  - missing/trash thread -> card dismissed 或 resolved/gmail_removed。
  - historyId 过期 -> fallback full reconcile。

- `test_reply_now_sync.py`
  - `reply_now` 调用 send 后调用 mark-read。
  - send 成功但 mark-read 失败时，reply 仍 resolved，同时 warnings 记录 mark-read 失败。
  - send 前发现已在 Gmail 回复，不重复发送。

- `test_storage_integration.py`
  - resolved/replied card 不被旧扫描结果重新覆盖为 pending。
  - 同 thread 新 inbound 可重新生成 pending card。
  - 多邮箱 active cards 互不影响。

Smoke：

```sh
printf '%s\n' '{"jsonrpc":"2.0","method":"describe","id":1}' \
  | uv --directory inbox-tool/src run anna-inbox-executa

printf '%s\n' '{"jsonrpc":"2.0","method":"health","id":1}' \
  | uv --directory inbox-tool/src run anna-inbox-executa
```

真实 Gmail 验证依赖有效 OAuth token、网络和测试邮箱，不能当作无条件可用测试。

## 12. 实施路线

### Phase 1：无 History API 的可靠同步

目标：满足“再次扫描后完全同步”。

- 新增 Gmail thread state helper。
- 新增 active-card reconcile service。
- 在 Brief scan 开始前调用 reconcile。
- `reply_now` 成功后刷新 thread state、mark read、更新 cache。
- `handled manually / I replied in Gmail` 改为先验证 Gmail thread。
- 增加 focused tests。

### Phase 2：接入 Gmail History API

目标：减少 active cards 较多时的 API 调用。

- 保存 `ScanState.last_history_id`。
- 新增 `list_gmail_history_since(mailbox, history_id)`。
- 将 history events 映射到 thread/message state changes。
- 404/过期时 fallback active-card reconcile。

### Phase 3：体验与边界完善

目标：让用户理解状态来源。

- 前端区分 “Replied by Anna” / “Replied in Gmail” / “Handled manually”。
- History 抽屉展示同步来源。
- 支持 mailbox aliases/send-as。
- 对大量 active cards 做分页 reconcile 和 quota warning。

## 13. 关键风险

- Gmail “已回复”只能通过真实 sent message 判断，不能伪造。
- Gmail aliases 会影响 owner 判断；第一阶段需保守，避免误把别人当 owner。
- API quota：逐 thread reconcile 简单但有成本，需限制每轮检查数量或尽快进入 History API。
- 本地 cache stale 会造成 UI 与 Gmail 不一致；所有 Gmail write 后必须刷新或 patch cache。
- resolved card 的保留策略要统一：可以短期保留用于 UI 反馈和去重，但不能让它长期污染 active cards 展示。

## 14. 推荐决策

推荐先实施 Phase 1。

原因：

- 不依赖外部 Pub/Sub/watch。
- 对当前架构改动集中在 Gmail adapter、sync service、Brief scan start 和少量工具动作。
- 能直接满足用户“我在 Gmail 里操作后，点再次扫描，工具状态应同步”的核心诉求。
- 后续接 History API 时，只是替换 reconcile 的事件来源，不改变前端和卡片主流程。
