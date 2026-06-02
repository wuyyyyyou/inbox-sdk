# Handle 按钮和抽屉实现分析

## 一、PRD-V2 要求回顾

### 1.1 Handle（Primary Action）按钮 — §1.1.3

```
[Snooze]                [Primary action]
```

| 要求 | 说明 |
|------|------|
| 位置 | 卡片右下角，Snooze 右侧 |
| 样式 | 主按钮（高亮） |
| 文案 | **动态文案（Primary action）**，不是静态 "Handle" |
| 一致性 | 所有卡片保持位置和交互结构一致 |

点击后打开右侧抽屉（Handle thread flow）。

### 1.2 抽屉信息 — §1.2.1 ~ §1.2.7

| 区域 | PRD 要求 | 细节 |
|------|---------|------|
| **顶部信息区** | From / To / CC / Thread / Latest message | CC 无则显示 None；不展示 Source/Status |
| **最新邮件正文** | Latest email 标题 + 全文 | 只读，默认展示与 attention item 最相关的邮件 |
| **Thread summary** | 默认文案 + Summarize thread 按钮 | 点击后总结：核心诉求、讨论进展、未解决问题、需回应点 |
| **Draft reply** | Reply mode + textarea + Revise | 草稿必须可编辑；Revise 不覆盖用户已编辑内容；Reply all 在无 CC 时弱化 |
| **底部处理动作** | Reply now / No action needed / Handled manually | 固定在抽屉底部；Reply now MVP 可 mock |

### 1.3 产品原则 — §1.2.7

抽屉是 **Handle thread flow**，不是邮件详情页，不是 debug 面板。

**不展示**：confidence、priority score、raw signals、source connection status、重复外部 card 已展示的 summary。

---

## 二、当前实现分析

### 2.1 Handle 按钮：文案为静态 "Handle"

**前端** `new/bundle/app.js`：

```javascript
// L166-172: 正确找到了 primary action（动态）
function primaryAction(card) {
  const actions = Array.isArray(card.actions) ? card.actions : [];
  return actions.find((action) => action.primary)
    || actions.find((action) => action.id !== "view")
    || { id: "handle", label: "Handle" };
}

// L252: 但按钮文案被硬编码为 "Handle"
<button class="primary-btn" data-handle-card="...">Handle</button>
```

- `primaryAction(card)` 逻辑正确，能动态解析出 primary action 及其 label
- 但 HTML 模板中按钮文案被硬编码为 `"Handle"`，**动态 label 从未被使用**
- `data-handle-card` 属性编码了 action ID，但点击时只取 cardId，action ID 被丢弃

### 2.2 Handle 按钮 label 的生成链路

```
LLM 输出 action_type (如 create_draft)
  → guards.py: 安全过滤
  → card_service.py._build_card_actions():
      1. 过滤 do_nothing / mark_read
      2. _action_label(act_type) 映射为可读标签
      3. 第一个非 view action 标为 primary=true
  → 前端 primaryAction(card) 找到 primary action
  → 但按钮文案用硬编码 "Handle"
```

**当前的 `_ACTION_LABELS` 映射（`card_service.py:308-314`）：**

| action_type | label |
|-------------|-------|
| `create_draft` | Prepare reply |
| `apply_label` | Add label |
| `create_reminder` | Remind me later |
| `save_note` | Save note |
| `archive` | Prepare cleanup |

### 2.3 抽屉：各区域实现情况

| 区域 | 状态 | 实现说明 |
|------|------|---------|
| 顶部信息区 | ✅ 已实现 | From / To / CC / Thread(名称+数量) / Latest message，不展示禁用项 |
| 最新邮件正文 | ✅ 已实现 | `drawer-section` 中展示 `original.body` |
| Thread summary | ✅ 已实现 | 默认文案 + "Summarize thread" 按钮，LLM 返回 `core_ask`/`current_progress`/`open_questions`/`user_action_needed`/`tone` |
| Draft reply — reply mode | ✅ 已实现 | Reply to sender / Reply all 切换，无 CC 时 Reply all disabled |
| Draft reply — textarea | ✅ 已实现 | 用户可编辑，`input` 事件实时同步到 `state.draftById` |
| Draft reply — revise | ✅ 已实现 | 用户输入反馈，LLM 基于当前草稿 + 修改要求重新生成 |
| Draft reply — generate | ✅ 已实现 | 首次生成 draft，LLM 返回 `subject`/`body`/`tone`/`note` |
| 底部动作 — Reply now | ⚠️ Stub | 仅 `showToast("Reply now requires final user confirmation in this MVP.")`，无后端工具 |
| 底部动作 — No action needed | ✅ 已实现 | 调用 `record_card_decision("no_action_needed")`，标记卡片为 resolved |
| 底部动作 — Handled manually | ✅ 已实现 | 调用 `record_card_decision("handled_manually")`，标记卡片为 resolved |

### 2.4 后端工具清单

| 工具 | 说明 |
|------|------|
| `get_card_detail` | 获取卡片详情 + 线程上下文 |
| `summarize_thread` | LLM 总结线程 |
| `generate_draft_reply` | LLM 生成回复草稿 |
| `revise_draft` | LLM 根据反馈修改草稿 |
| `record_card_decision` | 记录用户处理决定 |
| `reply_now` | **不存在** |

---

## 三、差距分析

### 3.1 核心缺陷：Handle 按钮文案为静态

**PRD 要求**：Primary action（动态文案）
**当前实现**：按钮始终显示 "Handle"

`primaryAction(card)` 函数已经正确识别了每个卡片的不同 primary action，但它的 `label` 从未被渲染到按钮上。这是本次需要修复的核心问题。

### 3.2 Handle 按钮文案的设计考究

动态文案不是简单地把 `_ACTION_LABELS` 的值贴到按钮上。按钮文案需要满足：

1. **动词优先**：按钮是 Call-to-Action，必须以动词开头或隐含动作
2. **简短有力**：按钮空间有限，1-2 个词为宜
3. **语义精准**：用户扫一眼就知道 Anna 建议做什么，无需展开卡片
4. **差异化**：不同 action_type 的按钮文案要有区分度，否则"动态"就没有意义
5. **与抽屉定位一致**：按钮打开的是 Handle thread flow，文案应暗示"进入处理流程"

**当前 `_ACTION_LABELS` 用于按钮的不足：**

| 当前 label | 问题 |
|-----------|------|
| "Prepare reply" | 偏长；"Prepare" 弱化了动作感 |
| "Add label" | 信息性强，动作感弱 |
| "Remind me later" | 过于口语化，像 snooze 变体 |
| "Save note" | 可接受 |
| "Prepare cleanup" | "Prepare" 多余 |

**需要区分"卡片按钮文案"和"内部 action 标签"两个概念。**
前者是 CTA（用户看到的按钮），后者是系统内部的动作描述。两者可以不同。

### 3.3 Drawer 已实现部分与 PRD 的差距

| 差距 | PRD 要求 | 当前状态 | 优先级 |
|------|---------|---------|--------|
| Reply now 无后端 | MVP 可 mock | 仅前端 toast | 低（PRD 允许 mock） |
| No action needed 未记录偏好 | "可记录为用户偏好信号" | 仅标记 resolved，不写 learning | 中 |
| Reply all 弱化方式 | "弱化展示" | `disabled` 属性完全禁用 | 低 |
| 草稿编辑保护 | "Anna 修改不能覆盖用户已编辑内容，除非用户明确确认" | 用户编辑随 `input` 事件实时同步到 state，revision 基于当前内容（含用户编辑），未被覆盖 | ✅ 合理 |
| 抽屉标题 | "Handle thread" | ✅ 与 PRD 一致 |

### 3.4 技术方案上的优化点

**当前 `_build_card_actions()` 的问题：**

1. **两轮 primary 标记冗余**：第一轮 `primary=len(actions) == 1`，第二轮再做一次遍历确保只有一个 primary。逻辑重复。

2. **"Not important" 始终追加**：即使卡片已经有具体 action，"Not important" 也会作为备选出现在 actions 列表中。这个设计合理（给用户降级选项），但当前在 `record_card_decision` 中 "not_important" 不是一个合法的 decision 值——前端根本没有处理 "Not important" 按钮的点击。

3. **`_ACTION_LABELS` 同时服务于卡片按钮和内部展示**：需要拆分职责。

4. **前端 `primaryAction()` 有三层 fallback**：`primary=true` → `id !== "view"` → 硬编码 `{ id: "handle", label: "Handle" }`。第三层 fallback 不应出现（每个卡片都应该有 primary action），如果出现说明后端有问题。

---

## 四、优化方案

### 4.1 Handle 按钮动态文案

**方案**：新增 `_PRIMARY_BUTTON_LABELS` 映射，与 `_ACTION_LABELS` 分离。

```python
# card_service.py

_ACTION_LABELS: dict[str, str] = {
    "create_draft": "Prepare reply",
    "apply_label": "Add label",
    "create_reminder": "Remind me later",
    "save_note": "Save note",
    "archive": "Prepare cleanup",
}

# 卡片主按钮文案：更短、动作感更强
_PRIMARY_BUTTON_LABELS: dict[str, str] = {
    "create_draft": "Reply",
    "apply_label": "Label",
    "create_reminder": "Remind",
    "save_note": "Save note",
    "archive": "Clean up",
}
```

前端修改：使用 `primaryAction(card).label` 替代硬编码 `"Handle"`。

```javascript
// app.js renderAttentionCard 中
const action = primaryAction(card);
// ...
<button class="primary-btn" data-handle-card="...">${escapeHtml(action.label)}</button>
```

但 `action.label` 目前来自 `_ACTION_LABELS`。如果后端不加新映射，按钮会显示 "Prepare reply" 而非 "Reply"。

**推荐做法**：在 `CardAction` 中新增 `button_label` 字段，与 `label` 分离：

```python
@dataclass
class CardAction:
    id: str = ""
    label: str = ""           # 内部/详情中使用
    button_label: str = ""    # 卡片主按钮文案
    primary: bool = False
    status_title: str = ""
    status: str = ""
```

- `label`：在抽屉内 action 列表中使用（如 "Prepare reply"）
- `button_label`：在卡片主按钮上使用（如 "Reply"）
- 前端 `primaryAction(card)` 优先取 `button_label`，fallback 到 `label`，最终 fallback 到 `"Handle"`

**这样做的好处**：
- 按钮文案和详情文案可以分别迭代优化
- 后端控制文案，前端只负责渲染
- 向后兼容：`button_label` 为空时自动 fallback 到 `label`

### 4.2 `_build_card_actions` 简化

去掉两轮 primary 标记，改为单次遍历：

```python
def _build_card_actions(fd: Any) -> list[CardAction]:
    actions: list[CardAction] = [
        CardAction(id="view", label="View original", button_label="", primary=False),
    ]

    for act in (fd.recommended_actions or []):
        act_type = act.get("action_type", "do_nothing")
        if act_type in ("do_nothing", "mark_read"):
            continue
        actions.append(CardAction(
            id=act_type,
            label=_action_label(act_type),
            button_label=_primary_button_label(act_type),
            primary=False,
        ))

    actions.append(CardAction(
        id="not_important",
        label="Not important",
        button_label="",
        primary=False,
        status_title="Got it.",
        status="I'll treat this as not important for this briefing.",
    ))

    # Mark first non-view action as primary
    for a in actions:
        if a.id != "view":
            a.primary = True
            break

    return actions
```

### 4.3 "Not important" 按钮的交互完善

当前前端没有处理 "Not important" 按钮点击。需要在抽屉底部动作区的旁边（或作为更轻量的选项）让用户可以选择"不重要的卡片"降级处理。

考虑在卡片按钮区让 "Not important" 作为第三个按钮（或不作为按钮，仅在抽屉内部提供）。

### 4.4 "No action needed" 写入学习偏好

PRD 说"可记录为用户偏好信号"。当前 `record_card_decision("no_action_needed")` 只标记卡片 resolved。建议在 `_handle_v2_tool("record_card_decision")` 中，当 decision 为 `no_action_needed` 时，顺便调用 `add_snooze_sender()` 和 `add_snooze_thread()`，记录用户对该 sender/thread 的负向偏好。这样下次扫描时该 sender 会被自动降权。

### 4.5 Reply now 的 MVP 实现

PRD 允许 mock，但当前连后端工具都没有。建议：
1. 新增 `reply_now` JSON-RPC 工具
2. MVP 阶段：验证 draft 非空，调用 Gmail API 发送（如果需要真实发送）或返回 "Mock: reply would be sent" 的确认
3. 发送后标记卡片为 resolved

### 4.6 涉及的文件改动总结

| 文件 | 改动 |
|------|------|
| `storage_types.py` | `CardAction` 新增 `button_label` 字段 |
| `card_service.py` | 新增 `_PRIMARY_BUTTON_LABELS`；`_build_card_actions` 简化并填充 `button_label` |
| `app.js` | `renderAttentionCard` 中按钮文案用 `primaryAction(card).label`；fallback 改为读取 `button_label \|\| label \|\| "Handle"` |
| `main.py` | `record_card_decision("no_action_needed")` 写入 snooze 偏好；可选新增 `reply_now` 工具 |
| `handle_service.py` | 可选新增 `reply_now` 的 LLM/发送逻辑 |
