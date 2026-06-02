# Draft & Thread Summary 持久化 — 实施计划

> 目标：草稿和摘要生成后自动持久化，下次打开卡片时直接加载，避免重复消耗 token
> 约束：本地 JSON 和 Anna APS 两种存储路径必须保持同步兼容

---

## 1. 存储架构分析

当前两种存储后端共享同一个数据模型：

```
storage_ops.py  ← 统一接口层
    ├── get_storage().get(key)   → ActiveCards
    └── get_storage().set(key, data)
         ├── LocalJSON 后端（默认）
         └── Anna APS 后端（ANNA_STORAGE_BACKEND=aps）
```

序列化/反序列化：
- **写**：`set_active_cards()` → `_dataclass_to_dict()` → `dataclasses.asdict()` → JSON → 写入后端
- **读**：`get_active_cards()` → 后端读取 JSON → `_dict_to_persistent_card()` 手动构造 → `ActiveCards`

关键点：`dataclasses.asdict()` 自动序列化所有字段，所以 **PersistentCard 加字段 = 自动写入**。只需在反序列化 `_dict_to_persistent_card()` 中补字段即可兼容旧数据。

## 2. 改动清单

### 2.1 `storage_types.py` — PersistentCard 加字段（2 行）

```python
class PersistentCard:
    ...
    item_type: str = ""
    draft_reply: str = ""         # 新增
    thread_summary: str = ""      # 新增（JSON string）
    display_section: str = "main"
```

### 2.2 `storage_ops.py` — 反序列化兼容（2 行）

`_dict_to_persistent_card()` 中加：

```python
draft_reply=d.get("draft_reply", ""),
thread_summary=d.get("thread_summary", ""),
```

旧数据没有这两个字段 → 默认 `""` → 不影响。

### 2.3 `card_service.py` — 前端数据透传（3 行）

`cards_to_frontend()` 中加三个字段：

```python
"item_type": card.item_type,
"draft_reply": card.draft_reply,
"thread_summary": card.thread_summary,
```

**修 bug**：`item_type` 之前没传到前端，分类筛选一直靠启发式 fallback。

### 2.4 `main.py` — handler 中自动保存（6 行 × 3 处）

三个 handler 在返回结果前自动写回存储：

**`summarize_thread`**：
```python
if tool == "summarize_thread":
    ...
    result = await summarize_thread(card, mailbox)
    summary = result.get("summary")
    if isinstance(summary, dict):
        card.thread_summary = json.dumps(summary, ensure_ascii=False)
        await set_active_cards(mailbox, cards)
    return result
```

**`generate_draft_reply`**：
```python
if tool == "generate_draft_reply":
    ...
    result = await generate_draft_reply(card, mailbox, reply_mode)
    draft_body = (result.get("draft") or {}).get("body", "")
    if draft_body:
        card.draft_reply = draft_body
        await set_active_cards(mailbox, cards)
    return result
```

**`revise_draft`**：
```python
if tool == "revise_draft":
    ...
    result = await revise_draft(card, mailbox, current_draft, revision_input)
    revised_body = (result.get("revised") or {}).get("body", "")
    if revised_body:
        card.draft_reply = revised_body
        await set_active_cards(mailbox, cards)
    return result
```

### 2.5 `app.js` — 初始化时恢复持久化数据（~15 行）

`loadActiveCards()` 中，加载卡片后预填 state：

```js
state.cards.forEach(card => {
  // 草稿恢复（不覆盖已在内存中更新过的）
  if (card.draft_reply && !state.draftById[card.id]) {
    state.draftById[card.id] = card.draft_reply;
  }
  // 摘要恢复（不覆盖已在内存中更新过的）
  if (card.thread_summary && !state.threadSummaryById[card.id]) {
    try {
      state.threadSummaryById[card.id] = JSON.parse(card.thread_summary);
    } catch {
      // 解析失败则忽略
    }
  }
});
```

`!state.draftById[card.id]` 保证内存中更新的草稿不会被旧的持久化版本覆盖。

### 2.6 不需要改的

- `renderHandleView()` — 已从 `state.draftById` / `state.threadSummaryById` 读取，自动生效
- `cardCategory()` — `item_type` 透传后，精确映射优先于启发式 fallback
- 前端 `summarizeSelectedThread()` / `generateDraft()` / `reviseDraft()` — RPC 调用逻辑不变

## 3. 两种存储路径兼容性

| 路径 | 写入 | 读取 | 兼容 |
|------|------|------|------|
| 本地 JSON | `asdict()` → JSON 文件 | `_dict_to_persistent_card()` | ✅ 新字段自动写入，旧数据无字段 → `""` |
| Anna APS | `asdict()` → API | 同上 | ✅ 同上 |

`dataclasses.asdict()` 和手动 `_dict_to_persistent_card()` 两个序列化入口都已覆盖，两种后端无需分别处理。

## 4. 用户可见变化

| 场景 | 改前 | 改后 |
|------|------|------|
| 生成草稿后刷新页面 | 草稿丢失，需重新生成 | 打开卡片即显示草稿 |
| 生成摘要后关闭卡片再打开 | 需重新摘要 | 直接展示 Anna noticed |
| 修改草稿后刷新 | 修改丢失 | 最后一次 revise 结果保留 |
| 新扫描卡片 | 无草稿无摘要 | 无草稿无摘要（正常，新卡片） |

## 5. 估时

| 步骤 | 时间 |
|------|------|
| `storage_types.py` | 1 min |
| `storage_ops.py` | 1 min |
| `card_service.py` | 2 min |
| `main.py` ×3 handler | 10 min |
| `app.js` loadActiveCards | 10 min |
| 验证 | 10 min |
| **合计** | **~35 min** |
