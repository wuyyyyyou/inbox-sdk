# Anna Sampling JSON Repair Plan

## 背景

Anna LLM sampling 当前没有 `response_format` 或 JSON schema 参数，业务链路只能通过提示词约束模型输出 JSON。实际运行中，Anna sampling 偶发返回非标准 JSON，导致 `parse_json_response()` 失败，后续进入重试或 fallback。现有重试会重新执行原任务，但如果模型持续输出坏 JSON，重试仍容易失败。

本方案增加一层“LLM JSON repair”：当 Anna sampling 已经产出文本但不是合法 JSON 时，把上一次坏输出和修复提示词一起发给 Anna sampling，让它只负责把内容修成标准 JSON。

## 目标

- 降低 Anna sampling 因 JSON 格式错误导致的流程失败。
- 正常 JSON 路径不增加额外 LLM 调用。
- repair 只修格式和结构，不重新判断邮件内容。
- repair 失败时继续走现有 fallback 或异常路径。
- 不影响 DashScope 路径。

## 非目标

- 不实现真正的 JSON schema 约束。
- 不把 repair 当作业务重判或摘要重写。
- 不对空响应、严重截断、跑题输出做强行补全。
- 不改变后端 sampling 协议。

## 实施范围

优先修改：

- `new/executas/tool-zhaopy-inbox-executa-adumwz8w/src/mail_agent/llm.py`

重点函数：

- `call_llm_json()`
- `call_llm_json_safe()`
- `parse_json_response()`
- `_repair_json()`

## 实施步骤

1. 新增 `repair_json_with_sampling()` helper

   输入：

   - `sampling_create_message`
   - 原始坏输出 `bad_text`
   - parser error `parse_error`
   - 可选 `expected_shape`
   - 原调用 metadata

   要求：

   - helper 不吞异常。
   - repair 返回后仍必须经过 `parse_json_response()` 严格解析。
   - 不直接信任 repair 模型输出。

2. 调整 Anna sampling 分支

   在 `call_llm_json()` 中，Anna sampling 返回 `text` 后：

   - 先走现有 `parse_json_response(text)`。
   - 如果 parse 成功，直接返回。
   - 如果 parse 失败且 `text` 非空，调用一次 `repair_json_with_sampling()`。
   - repair 结果再次走 `parse_json_response()`。
   - 仍失败则进入现有 retry/fallback 逻辑。

3. 设计 repair prompt

   prompt 固定强调：

   - 你是 JSON repair function。
   - 只输出一个合法 JSON object。
   - 不输出 markdown、解释、分析。
   - 保留原字段含义和值。
   - 不新增事实。
   - 缺失且无法恢复的字段使用空字符串、`false`、`[]` 或 `null`。

   repair 输入只包含：

   - parser error
   - expected shape
   - invalid text

   不重新传完整邮件正文，避免 repair 变成业务重判。

4. 增加调用保护

   - 只在 `sampling_create_message is not None` 时启用。
   - 只在 `parse_json_response()` 失败时启用。
   - 空响应不 repair。
   - 每次原始 LLM 调用最多 repair 一次。
   - 对 `bad_text` 做长度上限，例如 12k 字符。
   - metadata 增加 `repair_for=<tool>`，便于日志定位。

5. 保持 DashScope 路径不变

   DashScope 已传 `response_format: {"type": "json_object"}`，不需要额外 repair sampling 调用。

## 测试计划

增加 focused unit tests：

- 缺逗号 JSON：本地 `_repair_json()` 已能修，不触发 LLM repair。
- markdown fence / 前后废话：本地 `parse_json_response()` 能提取，不触发 LLM repair。
- 严重格式错误：触发一次 LLM repair，并成功 parse。
- repair 仍失败：进入现有 fallback 或异常路径。
- 空响应：不触发 repair。
- DashScope 路径：不触发 sampling repair。

## 成功标准

- Anna sampling 输出轻微或中度 JSON 格式错误时，流程能继续。
- 正常 JSON 路径不增加额外 sampling 调用。
- repair 失败不会引入新的崩溃点。
- repair 不改变业务判断，只修复 JSON 可解析性。

## 风险与缓解

- 风险：repair 模型改写内容。
  - 缓解：prompt 明确禁止新增事实和改写字段语义；repair 后仍做 schema normalize。

- 风险：坏输出严重截断时 repair 幻觉补全。
  - 缓解：空响应不 repair；严重截断场景优先 fallback 或缩小输入重跑。

- 风险：额外 sampling 调用增加耗时和额度消耗。
  - 缓解：只在 parse 失败时触发，每次最多一次。

- 风险：同一模型 repair 仍输出坏 JSON。
  - 缓解：repair 后仍严格 parse，失败继续走现有 fallback。
