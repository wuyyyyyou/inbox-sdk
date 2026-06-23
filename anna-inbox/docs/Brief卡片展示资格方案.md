# Brief 卡片展示资格方案

> 状态：方案待实施。
> 背景：首次扫描已经改为覆盖扫描窗口内的已读和未读邮件，避免漏掉用户打开过但尚未处理的邮件。新的问题是：扫描范围变宽后，最终展示给用户的 Attention Cards 也可能包含已经不需要处理的邮件。

## 一、目标

Brief 扫描可以尽量完整，但卡片列表只展示当前仍需要用户处理的事项。

最终用户应该只看到两类卡片：

1. 应该回复，但用户还没有回复的邮件。
2. 不需要回复，但需要阅读且用户还没有阅读的邮件。

其他情况不应该出现在卡片中，包括：

- 已经回复过的 reply 邮件。
- 已经读过的 review 邮件。
- ignore、safe cleanup、routine record、low value 邮件。
- Gmail 中已经删除、移入垃圾箱或无法找到的线程。

核心边界是：扫描负责发现，卡片负责未完成的注意力任务。

## 二、判定模型

引入一个统一的卡片展示资格概念，可以命名为 `card eligibility` 或 `attention_state`。

| Phase 2 / 卡片类型 | Gmail 当前状态 | 是否展示 | 原因 |
| --- | --- | --- | --- |
| `user_action == "reply"` | 线程最新一封不是 mailbox owner 发出 | 展示 | 对方仍在等待用户回复 |
| `user_action == "reply"` | 线程最新一封来自 mailbox owner | 不展示 | 用户已经回复 |
| `user_action == "review"` | 线程仍有未读邮件 | 展示 | 需要阅读且尚未阅读 |
| `user_action == "review"` | 线程没有未读邮件 | 不展示 | 用户已经阅读 |
| `ignore` / low value / cleanup | 任意 | 不展示为普通卡片 | 不是未完成注意力任务 |
| 任意类型 | Gmail 线程 missing / trashed | 不展示 | 外部状态已经移除 |

注意：`reply` 的展示资格与已读/未读无关。用户可能已经读过一封需要回复的邮件，但还没有回复，它仍然应该展示。

## 三、当前代码现状

当前实现已经有部分能力：

- `mail_agent/core/scan.py::_build_thread_scan_query` 首次扫描不加 `is:unread`，可以覆盖已读和未读邮件。
- `mail_agent/core/pipeline.py::_thread_latest_is_from_owner` 已经在候选阶段检查 reply 线程是否由用户最后回复，并过滤一部分已回复候选。
- `mail_agent/sync/gmail_status.py::fetch_gmail_thread_state` 已经可以获取线程级状态：
  - `latest_from_owner`
  - `unread_message_ids`
  - `missing`
  - `trashed`
- `mail_agent/sync/gmail_status.py::reconcile_active_cards_with_gmail` 已经会把 `latest_from_owner=True` 的卡片标记为 `resolved/replied_in_gmail`。

缺口：

1. review 卡片已读后，目前只计数 `marked_read`，没有真正 resolve 或从展示中移除。
2. 新生成卡片时，没有统一根据 Gmail state 做最终展示资格过滤。
3. `cards_to_frontend()` 只过滤 dismissed 和一部分 replied resolved 卡，没有表达 `reply` / `review` 的业务资格。
4. cleanup bundle 仍可能作为 lower 卡片出现，但本方案要求普通最终卡片只呈现 reply/review 未完成事项。

## 四、推荐实现

### 4.1 保持扫描范围不变

不要把扫描 query 改回 `is:unread`。

首次扫描继续覆盖已读和未读：

```text
-in:chats newer_than:{days}d
```

增量扫描继续叠加 `after:{last_scan_ts}` 和 processed index。

原因：一封需要回复的邮件可能已读但未回复，如果扫描层只扫未读会漏掉。

### 4.2 在卡片层增加统一资格判断

建议在 `mail_agent/cards/service.py` 中新增纯函数：

```python
def is_card_actionable(card: PersistentCard) -> bool:
    if card.status in ("dismissed", "resolved"):
        return False
    if card.card_type == "cleanup_bundle":
        return False

    state = card.gmail_state or {}
    if state.get("missing"):
        return False

    if card.user_action == "reply":
        return not bool(state.get("latest_from_owner"))

    if card.user_action == "review":
        return bool(state.get("unread"))

    return False
```

然后在两个地方复用：

1. `cards_to_frontend()`：最终返回前过滤，保证 UI 不展示不合格卡片。
2. `merge_cards()` 或持久化前：避免新卡片一生成就进入 active cards。

`cards_to_frontend()` 是最后一道防线；持久化前过滤是数据清洁。两层都做更稳。

### 4.3 新卡生成前补 Gmail state

在 `_persist_run_results_locked()` 中，`build_card()` 后、`merge_cards()` 前，对 `new_cards` 做状态补齐和过滤：

```python
for card in new_cards:
    if card.thread_id and card.user_action in ("reply", "review"):
        state = await asyncio.to_thread(fetch_gmail_thread_state, mailbox, card.thread_id)
        card.gmail_state = state.to_card_state()

new_cards = [card for card in new_cards if is_card_actionable(card)]
```

优化点：

- reply 候选已经经过 `_thread_latest_is_from_owner`，这里是兜底。
- review 候选不能只看单封 `MessageLite.unread`，最好看线程级 `unread_message_ids`，避免同一 thread 多封邮件状态不一致。
- 如果 Gmail 状态读取失败，可以保守保留卡片，避免漏掉需要处理的邮件；同时在 `gmail_state` 写入 `sync_failed` 供调试。

### 4.4 同步器真正 resolve 已读 review 卡

修改 `mail_agent/sync/gmail_status.py::reconcile_active_cards_with_gmail`：

```python
if card.user_action == "reply" and state.latest_from_owner:
    card.status = "resolved"
    card.resolution = "replied_in_gmail"

if card.user_action == "review" and not state.unread_message_ids:
    card.status = "resolved"
    card.resolution = "read_in_gmail"
```

建议补充 action history：

- `replied_in_gmail`
- `read_in_gmail`
- `gmail_removed`

这样用户在 Gmail 里读了 review 邮件，下一次打开或下一次扫描同步后，卡片自动消失。

### 4.5 Active cards 获取时可选同步

当前扫描开始时已经调用 Gmail state sync。为了让用户在不重新扫描时也能看到正确列表，可以考虑：

- `get_active_cards` 工具默认不做全量同步，避免每次打开都打 Gmail API。
- 增加前端显式 refresh 或短节流同步：
  - 距离上次 sync 超过 2-5 分钟时同步 active cards。
  - 只同步当前页卡片或最多 50-200 张。

这属于体验优化，不是第一步必须项。

## 五、实施步骤

### Phase 1：后端规则落地

1. 在 `cards/service.py` 增加 `is_card_actionable()`。
2. 在 `cards_to_frontend()` 中调用该函数过滤。
3. 在 `sync/gmail_status.py` 中把已读 review 卡 resolve 为 `read_in_gmail`。
4. 保持 reply 卡 `latest_from_owner=True` resolve 为 `replied_in_gmail`。

### Phase 2：新卡生成兜底

1. 在 `_persist_run_results_locked()` 中，`build_card()` 后补 Gmail state。
2. 对 `new_cards` 调用 `is_card_actionable()`。
3. 对 Gmail state 获取失败做保守保留，并记录 warning。

### Phase 3：cleanup 语义确认

本方案要求最终普通卡片只包含未完成 reply/review 事项。

如果仍需要 cleanup bundle，建议把它作为独立的 cleanup 入口，不混在 Attention Cards 主列表中。否则用户会把 cleanup 误解为“需要处理的卡片”。

## 六、测试用例

至少覆盖以下场景：

1. 已读但未回复的邮件：
   - 输入：`user_action=reply`，`latest_from_owner=False`，`unread=False`
   - 预期：展示。

2. 未读且需要阅读的邮件：
   - 输入：`user_action=review`，`unread=True`
   - 预期：展示。

3. 已读且只需阅读的邮件：
   - 输入：`user_action=review`，`unread=False`
   - 预期：不展示，或同步后 `resolved/read_in_gmail`。

4. 已回复的 reply 邮件：
   - 输入：`user_action=reply`，`latest_from_owner=True`
   - 预期：不展示，或同步后 `resolved/replied_in_gmail`。

5. ignore / cleanup：
   - 输入：`user_action=cleanup` 或 `priority=ignore`
   - 预期：不进入普通卡片列表。

6. Gmail 线程删除或丢失：
   - 输入：`missing=True` 或 `trashed=True`
   - 预期：dismissed，不展示。

## 七、验收标准

- 首次扫描仍能扫描到已读和未读邮件。
- 已读但未回复的 reply 邮件仍会出现在卡片中。
- 已读的 review 邮件不会出现在卡片中。
- 用户在 Gmail 外部回复后，reply 卡片会在同步后消失。
- 用户在 Gmail 外部阅读后，review 卡片会在同步后消失。
- 前端不需要理解复杂规则，只消费后端已经过滤后的 cards。

