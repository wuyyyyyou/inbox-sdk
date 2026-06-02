# Snooze 按钮实现验证报告

## PRD-V2 要求（1.1.3 节）

| 选项 | PRD 要求 |
|------|---------|
| **Tomorrow** | 从当前 briefing 临时移除，第二天重新进入 attention scanning |
| **Next week** | 一周后重新允许进入 briefing |
| **Don't prioritize threads like this** | 记录用户偏好，**降低类似 thread/sender/category 未来进入 briefing 的概率**，不是 block/delete |

---

## 发现的问题

### 1. 【关键缺陷】`dont_prioritize` 偏好记录了但从未被使用

`main.py:1004-1010` 中，`dont_prioritize` 的处理逻辑是：
- 调用 `add_snooze_sender()` 和 `add_snooze_thread()` 将发件人和线程写入 `prefs/snooze.json`
- 将当前卡片标记为 `resolved`

但在整个流水线中——`candidate.py`、`phase1.py`、`judgment.py`、`plan.py`、`strategies.py` ——**没有任何地方调用 `get_user_prefs()` 来读取 `SnoozePrefs`**。也就是说，这些偏好数据被存下来了，但完全不会影响后续扫描的结果。相似 sender/thread 的邮件下次扫描时照样会进入 briefing。

PRD 说的"降低概率"完全没有实现，`dont_prioritize` 的效果等同于一次性的 `resolved`。

### 2. 【小问题】`next_week` 语义偏差

`main.py:1019-1020` 中 `next_week` 的计算是"下周一 9:00 AM"：
```python
days_until_monday = (7 - now.weekday()) % 7 or 7
until = (now + timedelta(days=days_until_monday)).replace(hour=9, ...)
```

PRD 中文写"一周后"（7天后），而"下周一"在不同星期几操作差异较大：周五操作只需等 3 天，周二操作需等 6 天。不过英文 "Next week" 本身可理解为"下周"，这更偏向产品决策而非 bug。

### 3. 【小问题】`SnoozePrefs.categories` 字段未使用

`storage_types.py:156` 定义了 `categories: list[str]`，但没有 `add_snooze_category()` 函数，`dont_prioritize` 处理中也未记录 category。不过当前流水线本身也不产出 category，属于预留字段。

---

## 正确的部分

- **Tomorrow / Next week 的 snooze 生命周期**：`merge_cards()` 在 `card_service.py:341-348` 中正确检查 `snooze_until` 是否过期，过期自动恢复为 `pending`。
- **前端 UI**：三个选项的菜单、按钮布局、点击处理逻辑与 PRD 一致。
- **卡片过滤**：前端 `app.js:150` 和 `cards_to_frontend` 正确排除了 `snoozed` 状态的卡片。

---

## 结论

核心缺陷是第 1 点：**`dont_prioritize` 只写不读**，PRD 要求的"降低未来出现概率"没有实际生效。需要决定在流水线的哪个环节（Phase1 过滤/降分？candidate 信号降权？plan 聚合过滤？）消费 `SnoozePrefs`，然后补充实现。

---

## 修复方案："降低未来出现概率"的设计

### 核心设计理念

PRD 说的"降低概率"不是硬过滤（hard filter），而是"提升门槛"（raise the bar）。被降权的 sender/thread 仍然可以产生 card，但需要更强的信号。

### 实现位置选择

| 位置 | 方式 | 评估 |
|------|------|------|
| `scan.py` | 搜索时过滤 | 等同于 block sender，违背 PRD |
| `candidate.py` | 降低信号分 | 信号分是规则计算的，降权幅度不好量化；且只影响 rule-based 路径 |
| `phase1.py` | 在 prompt 中告知 LLM | Phase1 只看邮件头做粗筛，判断精度不够 |
| **`judgment.py` (Phase 2)** | 在 prompt 中告知 LLM | 有完整正文+线程上下文，能做精准判断；是最终决定卡片生不生成的位置 |

**选择 Phase 2 (judgment.py)**，这是最 AI-native 的做法，匹配 PRD "attention prioritization preference" 的定位。

### Prompt 注入内容

```
## User Attention Preferences
The user has indicated they want to deprioritize attention from these sources.
These are NOT blocks — emails from these sources CAN still surface if they
genuinely require user action. However, apply a stricter standard:
only surface items where the user MUST personally act (reply, approve,
review a security issue). Routine updates, newsletters, and informational
emails from these sources should be classified as low/ignore.

Deprioritized senders: newsletter@spam.com, bot@service.com
Deprioritized threads: Weekly Digest Thread
```

核心逻辑：让 LLM 在评估时知道这些 sender/thread 需要更严格的判断标准。日常通知/newsletter/自动邮件 → ignore；真正需要用户回复/审批/处理的事情 → 依然可以进入 briefing。

### 为什么这样设计是正确的

1. **AI-native**：用 LLM 的判断力区分"来自降权发件人的重要邮件"和"来自降权发件人的日常通知"，而不是一刀切。
2. **可逆**：不修改 `SnoozePrefs` 的数据结构，偏好只是 prompt 中的一个 soft signal。
3. **可扩展**：未来如果需要对降权做更细粒度的控制（比如自动学习），只需扩展 `SnoozePrefs` 字段，prompt 渲染逻辑同步更新即可。

---

## 修复实施：改动总结

### 修改的文件

**1. `judgment.py`** — 3 处改动

| 改动 | 位置 | 说明 |
|------|------|------|
| 新增 import | L19 | `from .storage_types import SnoozePrefs` |
| 新增函数 | L64-86 | `_render_snooze_prefs_context(prefs)` — 将 `SnoozePrefs` 格式化为 prompt 片段，告知 LLM 对降权 sender/thread 应用更严格标准但不完全屏蔽 |
| 新增参数 | 4 个函数签名 | `build_judgment_prompt`、`build_batch_judgment_prompt`、`evaluate_item`、`evaluate_items_batch` 均新增 `snooze_prefs: SnoozePrefs \| None = None`，并透传至 prompt 构建 |

**2. `pipeline.py`** — 2 处改动

| 改动 | 位置 | 说明 |
|------|------|------|
| 加载偏好 | L153-161 | 在 read_context 完成后、evaluation 开始前，统一加载 `SnoozePrefs`，异常安全 |
| 传入参数 | L172-179, L217-223 | 两个 evaluation 路径（Anna batch + DashScope single）均传入 `snooze_prefs=snooze_prefs` |

### 生效链路

```
用户点击 dont_prioritize
  → main.py 写 SnoozePrefs 到 prefs/snooze.json
  → 标记当前卡片为 resolved

下次扫描
  → pipeline.py 加载 SnoozePrefs
  → judgment.py 将偏好注入 Phase 2 prompt
  → LLM 对降权 sender/thread 应用更严格标准：
      - 日常通知/newsletter/自动邮件 → low/ignore
      - 真正需要用户操作的事项 → 依然可以进入 briefing
```
