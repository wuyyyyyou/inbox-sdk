# Brief 扫描性能问题与优化方案

> 更新时间：2026-06-25（北京时间）
> 状态：问题记录、实施记录与后续预案。后续优化按优先级逐项落地。  
> 适用范围：Brief Gmail 扫描、短 invoke 状态机、Phase 1/2、前端扫描体验。

## 0. 实施记录

2026-06-22 第一轮已落地：

- 短 invoke 主路径 `_brief_prepare_scan()` 接入已有 scan state，非首次扫描会把 `last_message_internal_date` 作为 `after_timestamp` 传给 `run_mail_scan()`。
- 扫描 progress 增加 `skipped`、`scan_elapsed_ms`、`incremental`、`after_timestamp` 等轻量观测字段。
- Phase 1 前增加 conservative low-value prefilter，明显 newsletter/digest/promotion/unsubscribe 邮件不进 LLM，但继续进入 `low_value_items`，用于 cleanup bundle 聚合。
- Phase 1 增加规则 fast path：安全、账单、星标、重要、人类回复、已知联系人、清晰请求/合作类邮件可直接生成 rule candidates，只有模糊邮件进入 LLM。
- Phase 1 Anna Sampling timeout 从 55s 降到 20s，超时或失败时用规则 fallback，不再长期卡在 header/snippet 分类阶段。
- Phase 1 progress 增加 `phase1_input`、`phase1_prefiltered`、`phase1_rule_candidates`、`phase1_llm_messages`。
- Phase 2 保留每批 4 个候选并发评估，但改为谁先完成就先更新进度并持久化卡片，避免第一批任一 LLM 慢导致前端长期停在 `Evaluating 0/N`。
- Phase 2 单项 Anna Sampling timeout 从 55s 降到 25s，`max_tokens` 继续保持 8000。
- 已新增聚焦测试：`tests/test_brief_incremental_scan.py`、`tests/test_phase1_prefilter.py`。

## 0.1 2026-06-23 第二轮已落地：HTML 正文清洗减轻 Phase 2 输入噪音

本轮目标是不压低 Anna Sampling 的 `max_tokens` 或 `timeout`，而是先减少进入 Phase 2 prompt 的无效正文，尤其是 HTML 邮件中的 `<style>`、`<table>`、内联 CSS、追踪像素和重复 multipart 内容。

### 结论摘要

当前 Phase 2 慢，不建议优先通过继续压低 Anna Sampling 的 `max_tokens` 或 `timeout` 解决。`8000/55s` 一类参数承担了平台 LLM 响应稳定性的兜底作用，盲目降低会增加空响应、JSON 不完整和 fallback 卡片比例。

更稳妥的方向是先减少进入 Phase 2 prompt 的无效正文，尤其是 HTML 邮件中的 `<style>`、`<table>`、内联 CSS、追踪像素和重复 multipart 内容。也就是说：保持 LLM 预算相对充足，但让模型读到更干净、更短、更接近正文语义的内容。

### 已确认的关键现状

- `mail_agent/mail_providers/gmail/adapter.py::_decode_body()` 当前会收集 `text/plain` 与 `text/html`，但实际优先使用 `text/html`。
- 下游 Phase 2 prompt 构建只做字符截断，不负责真正清洗 HTML。
- `MessageDetail.body_text` 会进入 Phase 2 的 `message_detail` 和 `thread_context`，因此 HTML 噪音会直接放大 Phase 2 token 与推理成本。
- 老缓存中已经持久化的 `body_text` 可能仍是原始 HTML；仅修改扫描阶段 decode 不能马上修复已有缓存。
- Phase 2 每个 candidate 一次 sampling，候选多时仍是线性成本；HTML 清洗不能改变调用次数，但能降低单次调用的输入噪音和失败概率。

### 已实施内容

按 `HTML邮件处理方案.md` 在 Gmail 适配层清洗正文：

- 已修改 `_decode_body()` 的合并策略：
  - 优先使用有效的 `text/plain`。
  - 仅当纯文本不存在或明显无效时，使用 `text/html`。
  - HTML 不再原样进入 `body_text`，先转为干净文本或 Markdown。
- 已增加 HTML 清洗规则：
  - 去掉 `style`、`script`、`noscript`、`head`、`meta`、`link`。
  - 去掉 `display:none` 等隐藏内容。
  - 保留链接文本和 URL，安全/账单邮件仍能判断跳转目标。
  - 折叠多余空白，避免 layout 文本占满截断窗口。
- 依赖策略：
  - 不新增硬依赖。
  - 若运行环境有 `html2text` / `beautifulsoup4`，优先使用它们。
  - 若没有，则使用标准库 `html.parser` 或 regex fallback，保证 Executa 仍可运行。

兼容旧缓存：

- 在 `_to_message_detail()` 附近增加轻量保护：
  - 如果缓存中的 `body_text` 看起来仍是 HTML，并且缓存保留了 Gmail `payload`，则读取时重新 decode。
  - 这样不需要用户全量重新扫描，也能让已有卡片/候选逐步受益。

Handle 原文展示与 LLM 正文分离：

- 新增专门面向用户展示的 display decode 路径，保留原始 HTML/text MIME 内容，不复用 Phase 2 清洗后的 `body_text`。
- `get_card_detail` 默认只返回卡片和 thread metadata，不返回原邮件正文。
- 前端 Handle 页面默认隐藏原邮件正文；用户点击 `Show full email` 后才用 `include_body=true` 手动加载原始邮件展示内容。
- 展示 HTML 仍走安全净化、CID 图片解析和远程图片处理；LLM prompt 继续使用清洗后的正文。

新增聚焦测试：

- `tests/test_gmail_body_decode.py`
  - 同时有 `text/plain` + `text/html` 时优先使用有效纯文本。
  - 只有 HTML 时输出不包含 `<style>`、`<table>`、`<script>` 等标签。
  - HTML 链接被保留为可读文本或 Markdown 链接。
  - 纯文本无效、HTML 有效时 fallback 到 HTML 清洗结果。
  - 老缓存 raw HTML 读取时可被重新清洗。
  - display decode 保留原始 HTML 标签和链接，用于手动查看原邮件。

本轮已运行：

- `uv run python tests/test_gmail_body_decode.py`
- `uv run python tests/test_brief_phase2_slice.py`
- `uv run python tests/test_llm_json_repair.py`
- `npm run build`

### 后续建议

补 Phase 2 可观测性。

- progress 或 debug 日志中增加非敏感指标：
  - `context_type`
  - `body_length_before_clean`
  - `body_length_after_clean`
  - `html_body_used`
  - `plain_body_used`
  - `phase2_prompt_chars`
  - `phase2_sampling_elapsed_ms`
- 指标不能记录正文、URL token、OAuth token、完整 credential context。

Phase 2 可恢复性优化。

- 保留按 `candidate_id` 可恢复的单候选 judgment，不改成多候选单次批量判断。
- 保留固定 batch 的单候选 judgment，不改成多候选单次批量判断。
- 不把已成功完成的 candidate 因为同批慢请求而重复评估。
- 在 run state 中按 `candidate_id` 识别已完成 judgment；下次 continue 只处理缺失 judgment 的 candidates。
- 该项能减少 invoke timeout 后的重复 LLM 消耗，但改动面比 HTML 清洗大，建议放在 HTML 清洗之后。

### 暂不推荐的方案

- 暂不把 Phase 2 `max_tokens` 从 8000 降到更低作为主方案。
- 暂不把 Phase 2 `timeout` 大幅降到 25s 作为主方案。
- 暂不把 Phase 2 改为多候选单次批量 judgment；这会减少调用次数，但会牺牲单候选可恢复性，且 JSON 漏项风险较高。
- 暂不在本轮做复杂引用/签名剥离；先保证正文 HTML 清洗正确，再处理 quoted reply。

### 预期收益

- HTML-only 邮件的有效正文会更早出现在 Phase 2 截断窗口内。
- 安全/账单/通知类 HTML 邮件不再把大量 CSS 和 table layout 送进 LLM。
- `8000/55s` 预算用于真实语义内容，而不是 HTML 噪音。
- 对 newsletter、营销邮件、商业系统通知尤其明显。
- 不改变前端契约，不改变卡片 schema，不改变 Executa tool 参数。

### 风险

- `text/plain` 有时可能只是“请查看 HTML 版本”的占位文本，需要设置有效性判断，而不是只判空。
- HTML 到文本的 fallback 若过于粗糙，可能丢失链接上下文；安全/账单邮件必须保留 URL。
- 旧缓存 read-side 重新 decode 要避免大对象写回和重复重算造成额外延迟。
- 不能在日志中记录清洗前后的正文内容，只记录长度和路径。

## 0.2 2026-06-25 补充：Phase 2 Evaluating 卡顿专项优化

用户反馈：Brief 扫描经常卡在 Phase 2 的 `Evaluating`，当前重点不再扩展新的 LLM 调用接口，而是先把 Phase 2 本身的调度、prompt、上下文读取、持久化和前端轮询打通。

### 当前 Phase 2 代码复核

截至 2026-06-25 前几轮优化后，当前 `_brief_run_phase2_slice()` 的真实形态：

- 每次 slice 从缺失 judgment 的 candidates 中固定取前 `phase2_llm_concurrency = 4` 个，形成一个 batch，符合 README 中 Phase 2 每批 3-5 个候选的 Invoke 约束。
- 当前 batch 的 `read_candidate_context()` 已并发读取，但下一批 candidate 不会提前进入队列。
- 进入 evaluate 后，每个 candidate 启一个 async task，并用 `asyncio.as_completed()` 谁先完成谁先持久化卡片和更新进度。
- 核心剩余瓶颈：虽然 batch 内谁先完成谁先展示，但必须等当前 4 个全部结束后，下一批 candidate 才会启动；如果 3 个很快完成、1 个接近 25s timeout，空出来的并发槽会闲置。
- `continue_mail_agent_run` 的 invoke budget 约 50s；当前 Phase 2 使用 4 并发、`phase2_sampling_timeout_s = 25`，为持久化和状态返回保留时间。
- progress 和 debug 日志已经开始记录真实 timeout、`max_tokens=8000`、context read、prompt chars、sampling elapsed、fallback 和 persist 耗时；日志不输出 prompt preview、邮件正文、subject 或用户请求原文。
- prompt 瘦身已回退；当前仍使用完整 Phase 2 prompt，不在本轮调度优化里改判断语义。
- `_brief_persist_cards()` 每完成一个 judgment 就读写 active cards；在 APS 较慢时，单项持久化也可能放大整批尾延迟。

### 直接优化方向

P0：把观测修准。

- 修正 progress 中的 `timeout_s`、`max_tokens`、`batch_size`、`phase2_llm_concurrency`，全部以真实代码参数为准。
- 增加非敏感耗时：
  - `phase2_context_read_ms`
  - `phase2_prompt_chars`
  - `phase2_sampling_first_done_ms`
  - `phase2_sampling_batch_ms`
  - `phase2_persist_ms`
  - `phase2_cards_added`
  - `phase2_pending_count`
- 每个 candidate 记录 `candidate_id`、`context_type`、`prompt_chars`、`sampling_elapsed_ms`、`fallback_used`，但不要记录 prompt preview、subject、正文、URL、用户请求原文或凭据。

P0：修正 timeout 与 invoke budget 不一致。

- `continue_mail_agent_run` 当前等待约 50s，Phase 2 单项 sampling timeout 不应大于这个预算。
- 建议先设：
  - `phase2_slice_budget_s = 45`
  - `phase2_sampling_timeout_s = 25`
  - 持久化与状态返回预留 5-8s
- progress 里同步展示真实 timeout。
- 如果 timeout 降低导致 fallback 明显增加，再基于 p95 调整，不要只凭体感加大到 60s。

P1：上下文读取并发化。

- 将当前串行：

```python
contexts = [await read_candidate_context(mailbox, candidate) for candidate in batch]
```

改为有界并发：

```python
contexts = await asyncio.gather(*[
    read_candidate_context(mailbox, candidate)
    for candidate in batch
])
```

- 初始并发度与 Phase 2 LLM 并发一致，默认 4。
- 需要记录 context read 单项耗时，确认本地 cache/APS/文件读取是否也是慢点。

P1：保留固定 batch，并完善可恢复性与观测。

- 当前 Phase 2 继续采用固定 batch：每次 `continue` 处理前 4 个未完成 candidate。
- batch 内保持 `asyncio.as_completed()`，谁先完成谁先持久化和更新 progress。
- batch 间仍保留边界，不在本轮引入滑动补位队列，避免和短 invoke 预算约束互相打架。
- `phase2_cursor` 继续表示“按原始顺序连续完成到哪里”。
- 每次 continue 根据 `candidate_id` 判断哪些 judgment 已完成；下次 continue 只处理剩余 candidate，避免 invoke timeout 后重复评估。
- 如果需要取消 pending task，要先确认 `SamplingClient` 的 pending future 会被清理；否则优先通过固定 batch + 单候选 timeout 控制风险。

P1：前端轮询与早展示确认。

- 后端已经在 `as_completed()` 中逐个更新 run state 和 `cards_version`，但如果前端等待 `continue_mail_agent_run` 返回后才轮询，用户仍看不到中途进度。
- 前端扫描页应在 `continue` 请求挂起期间也周期性调用 `get_mail_agent_run`，看到 `cards_version` 增加就刷新 `get_active_cards`。
- UI 上 `Evaluating x/N` 应显示已完成数量、当前 batch、卡片新增数，而不是只等本次 continue 返回。

P1/P2：Phase 2 prompt 观测，瘦身暂缓。

- prompt 瘦身曾尝试过中等力度方案，但已按产品判断回退。
- 当前调度优化不改变 prompt 内容，避免把性能变化和判断语义变化混在一起。
- 继续保留 `phase2_prompt_chars`、timeout、fallback、`prompt_profile` 等观测。
- 如果后续重新审批 prompt 瘦身，应单独评估安全、账单、等待回复、禁止发送/删除等关键 guardrail，不要和 Phase 2 调度改造混在一起。

P1：规则 fast-exit，减少不必要 Phase 2 LLM。

- Phase 1 已经有规则 fast path，但仍可能有明显自动通知、纯 receipt、明显 cleanup 被带入 Phase 2。
- 在 Phase 2 前加 conservative fast-exit：
  - no-reply / notification sender，且无安全/账单风险。
  - receipt / subscription confirmation，且无付款失败、异常金额、deadline。
  - newsletter / digest / promotion，且无明确个人请求。
- fast-exit 直接生成低优先级 review judgment，走同一 card/filter/guardrail 流程。
- 只允许对“明确低风险”命中；星标、important、已知联系人、附件、问句、安全/账单关键词必须继续 LLM。

P1：持久化节流。

- 当前每个 judgment 完成立即 `_brief_persist_cards()`，优点是早展示，风险是 APS 慢时每项都读写 active cards。
- 建议策略：
  - 第一个可展示 card 立即持久化，保证首卡快出现。
  - 同一 batch 中后续完成项按 2 个或 500-1000ms 合并持久化。
  - progress 仍逐项更新，但 active cards 写入做轻量 debounce。
- 需要记录 `phase2_persist_ms`，确认是否值得改。

P2：优先级队列。

- Phase 1 后先排：
  - security/billing/starred/important。
  - known contact + possible request。
  - unread + high confidence reply。
  - 普通 review。
- 每个 slice 先评估最可能出主卡的 candidate。
- 即使总耗时不变，用户会更早看到有价值卡片。

P2：低风险候选 micro-batch 实验。

- 高风险、reply、security/billing 继续单候选 LLM，保证可恢复与准确性。
- 仅对低风险 review 候选尝试 2-3 个一组的 compact batch judgment。
- JSON 漏项或解析失败时，只对缺失项回退单候选评估。
- 该方案可减少 Sampling 调用次数，但风险高于 prompt 瘦身和调度优化，应放后面。

### 建议落地顺序

1. 修正 Phase 2 progress 与真实代码参数不一致的问题。
2. 加 per-candidate / per-batch 非敏感耗时，先确认慢在 context、sampling 还是 persist。
3. 并发读取 `read_candidate_context()`。
4. 按 `candidate_id` 完善 Phase 2 可恢复性，避免 timeout 后重复评估已完成候选。
5. 保留 README 约束下的固定 batch Phase 2 调度，并继续观察是否仍有明显的慢尾问题。
6. 调整前端轮询，让 `continue` 挂起期间也能看到 cards/progress 更新。
7. prompt 瘦身暂缓，保留 prompt chars 和 timeout/fallback 观测，后续单独审批。
8. 加 Phase 2 前 conservative fast-exit。
9. 再做优先级队列和低风险 micro-batch 实验。

### 预期收益

- 减少固定 batch 的慢尾阻塞。
- 在 README 约束的 3-5 候选 batch 内，提高 Phase 2 slice 的可观测性和可恢复性。
- 候选数量较多、单项耗时差异明显时，预计比固定 6 个一批更快完成同等数量 candidate。
- 前端 `Evaluating x/N` 和卡片刷新会更连续，不再表现为一批结束后才进入下一批。

## 1. 背景

用户反馈：Brief 邮件扫描速度极慢，等待时间明显影响体验。

当前前端主路径是：

```text
start_mail_agent_run
  -> continue_mail_agent_run 多次推进
  -> get_active_cards 刷新卡片
```

当前后端主路径是：

```text
anna_inbox_executa/brief_flow.py
  _brief_prepare_scan
  _brief_run_phase1_slice
  _brief_check_replied_after_phase1
  _brief_run_phase2_slice
  _brief_finalize_run
```

Gmail 扫描核心是 `mail_agent/core/scan.py::run_mail_scan()`，按 Gmail thread 分页读取，每页最多 50 个 thread，再并发拉取 full thread。

## 2. 文档中已有的解决思路

### 2.1 短 invoke 状态机

来源：`Brief可续跑短Invoke状态机改造方案.md`、`README.md`

已有设计目标：

- 单次 invoke 控制在 45-55 秒内返回，避免平台约 65 秒超时。
- 每次 invoke 只推进一个有限阶段。
- Phase 1 分批处理。
- Phase 2 每批评估 3-5 个 candidates。
- 每批 Phase 2 生成卡片后立即持久化，前端看到 `cards_added/cards_version` 变化就刷新展示。
- 联系人记忆不阻塞主 Brief，放到卡片展示后独立运行。

当前落地情况：

- 已有 `start_mail_agent_run` + `continue_mail_agent_run` 状态机。
- Phase 1 已按 cursor 分批推进。
- Phase 2 已按 cursor 分批推进并在每批后持久化卡片。
- 但扫描阶段 `_brief_prepare_scan()` 仍是一次性完成 Gmail thread 列表和 full thread 拉取，没有 scan cursor，也没有时间预算提前返回。

### 2.2 增量扫描

来源：`项目优化基线与路线图.md`、`README.md`

已有设计目标：

- 每邮箱维护 `processed_message_ids` 和 `scan_state`。
- 老 pipeline `mail_agent/core/pipeline.py::run_mail_task()` 已经读取 `ScanState.total_scans/last_message_internal_date`。
- 非首次扫描可传 `after_timestamp=last_message_internal_date`，让 Gmail query 追加 `after:<seconds>`。

当前落地情况：

- 当前 UI 主路径 `_brief_prepare_scan()` 只传 `newer_than_days=scan_window_days`。
- 也就是说非首次扫描仍会反复拉取最近 N 天 thread，再靠 processed index 过滤。
- 这会减少重复进入 LLM 的邮件，但不能减少 Gmail API 拉取 thread/full thread 的成本。

### 2.3 Scan Plan 控制扫描范围

来源：`Next-Scan-实施状态.md`、`README.md`、`项目优化基线与路线图.md`

已有设计目标：

- 通过 Scan Plan 控制扫描窗口和最大扫描量。
- 可进一步消费 `priorities/include_archived/batch_behavior` 等字段。
- 触顶时提示用户是否继续扫描。

当前落地情况：

- 当前代码实际稳定字段是 `scan_window_days/max_messages/scan_categories`。
- `_brief_prepare_scan()` 已读取 `scan_window_days/max_messages`。
- `scan_categories` 暂未进入 Gmail query。
- `Next-Scan-实施状态.md` 中提到的 `time_range/priorities/include_archived/batch_behavior` 与当前 dataclass 已不完全一致，需要先做字段校准。

### 2.4 多邮箱扫描体验

来源：`多邮箱接入方案.md`、`README.md`

已有设计目标：

- 多邮箱串行扫描。
- 第一个邮箱完成后立即展示卡片。
- 后续邮箱在底部栏继续显示进度。
- 某个邮箱失败不影响其他邮箱。

当前落地情况：

- 前端已有多邮箱选择和串行扫描思路。
- 性能问题在多邮箱场景会被放大：每个邮箱都会重复执行 Gmail scan + Phase 1 + Phase 2。

### 2.5 降低 LLM 阶段成本

来源：`Anna-Sampling-Brief全链路分析.md`、`Brief管线全链路设计.md`

已有设计目标：

- Phase 1 只读 header/snippet，低价值邮件进入 cleanup bundle，零 Phase 2 成本。
- Phase 2 只处理候选邮件。
- 联系人记忆后置，避免阻塞 Brief 主路径。

当前落地情况：

- Phase 1 / Phase 2 分层已经存在。
- 但 Phase 1 目前依赖 Anna Sampling，每 8 封一批；如果扫描量很大，会出现较多 sampling 调用。
- Phase 2 每个 candidate 一次 sampling，是体验上最容易拖慢的阶段之一。

## 3. 当前性能瓶颈判断

### 3.1 Gmail 扫描重复拉取成本高

`run_mail_scan()` 当前按 thread 搜索：

```text
threads.list q="-in:chats newer_than:7d"
  -> 每页最多 50 thread
  -> 对每个 thread 调 threads.get(format=full)
  -> normalize + write_message cache
```

问题：

- 即使大部分邮件已经 processed，仍要重新 `threads.list` 和 `threads.get`。
- 非首次扫描没有使用 `after_timestamp`，重复读取最近窗口内 thread。
- full thread 拉取会取 thread 内所有 message payload，网络和 JSON 解析成本高。

### 3.2 扫描阶段没有 cursor，无法分段返回

短 invoke 设计要求 scanning 也能在慢时保存 cursor 下次继续，但当前 `_brief_prepare_scan()` 一次性等待 `run_mail_scan()` 返回。

问题：

- Gmail API 慢、thread 多、网络抖动时，用户长时间卡在 scanning。
- 前端无法在扫描过程中展示部分结果。
- 如果一次 scan 接近平台超时，后续 Phase 1/2 还没机会开始。

### 3.3 已处理过滤发生在 full fetch 之后

当前流程：

```text
Gmail full thread fetch
  -> 得到 MessageLite
  -> filter_unprocessed(message_ids)
```

问题：

- processed index 能省 LLM，但省不了 Gmail full fetch。
- 对已经处理过的大量 thread，仍重复付出 Gmail 网络成本。

### 3.4 Phase 1/2 LLM 成本随扫描量线性增长

当前短 invoke 路径：

- Phase 1：每 20 封一个前端状态批次，但内部 `_run_phase1_single_batch()` 对 Anna Sampling 仍以 8 封为一批。
- Phase 2：每次处理 4 个 candidates，每个 candidate 一次 sampling。

问题：

- 扫描量上来后，Phase 1 调用次数变多。
- 候选多时，Phase 2 是线性耗时。
- 当前没有先用规则对明显低价值邮件做本地预过滤，所有新邮件都进入 Phase 1。

### 3.5 多邮箱串行会放大等待

如果用户勾选多个邮箱，扫描总时间近似为每个邮箱耗时累加。

已有体验设计是第一个邮箱完成后先展示，但后端每个邮箱仍会完整执行扫描链路。

## 4. 优化目标

优先目标不是“扫更多”，而是“更快给用户可用结果”：

- 首屏卡片更早出现。
- 非首次扫描显著减少 Gmail API 调用。
- Gmail scanning 阶段可恢复、可继续、可解释。
- LLM 调用集中在真正可能有价值的邮件上。
- 多邮箱场景避免一个慢邮箱拖住所有体验。

## 5. 预想解决方案

### P0：先补性能观测

目标：知道慢在哪里。

建议：

- 在 Brief run progress 中记录阶段耗时：
  - `scan_ms`
  - `phase1_ms`
  - `check_replied_ms`
  - `phase2_ms`
  - `finalize_ms`
- 在 scan progress 中记录：
  - `threads_listed`
  - `threads_fetched`
  - `messages_scanned`
  - `cached_messages_used`
  - `gmail_api_errors`
- 在 run history 或 debug progress 中展示：
  - scanned / new / skipped_processed / candidates / cards_added

验收：

- 用户反馈“慢”时能从 run 状态判断是 Gmail、storage、Phase 1 还是 Phase 2 慢。
- 不记录 token、邮件正文或完整 credential context。

### P1：把增量扫描迁入短 invoke 主路径

目标：非首次扫描不要重复 full fetch 最近 N 天旧 thread。

建议：

- 在 `_brief_prepare_scan()` 读取 `get_scan_state(mailbox)`。
- 若 `total_scans > 0` 且 `last_message_internal_date` 有值，调用：

```python
run_mail_scan(
    mailbox,
    configured_max,
    newer_than_days=scan_window_days,
    after_timestamp=last_message_internal_date,
)
```

- 保留 `filter_unprocessed()` 作为第二层防重。
- 扩展 `test_scan_window.py` 覆盖 `after:` query。

风险：

- Gmail thread query `after:` 是按搜索结果匹配，不等于完全准确的 history delta。
- 如果上次 scan_state 写错，可能漏扫。需要保留 Scan Plan 的回看窗口和手动全量重扫入口。

预期收益：

- 二次扫描 Gmail API 调用数量明显下降。
- processed index 从“主要防重手段”退回“兜底防重手段”。

### P1：扫描阶段拆成可续跑 cursor

目标：Gmail scan 慢时也能短 invoke 返回，不阻塞到平台超时。

建议：

- 将 `run_mail_scan()` 拆成 page/cursor 形式，或新增 `run_mail_scan_page()`。
- run state 中保存：

```json
{
  "scan": {
    "query": "-in:chats newer_than:7d after:...",
    "page_token": "...",
    "threads_seen": [],
    "messages": [],
    "threads_fetched": 50,
    "done": false
  }
}
```

- 每次 continue 只拉 1 页或有限页 thread。
- 接近 45-55 秒时保存 scan state 并返回 `needs_continue=true`。

风险：

- run state 不能无限塞大数组；只保存 MessageLite 摘要，完整邮件仍走 Gmail cache。
- `threads_seen` 需要限长或用 set 序列化，小心 APS KV 大小。

预期收益：

- 扫描阶段可恢复。
- 前端进度更真实，不会长时间卡死。

### P1：先用 Gmail metadata/list 做轻量预筛，再 full fetch

目标：减少不必要的 `threads.get(format=full)`。

建议：

- 对 thread 列表先获取轻量字段：
  - thread id
  - snippet
  - historyId
  - latest message headers / labelIds / internalDate（如果 Gmail fields 支持）
- 对明显已 processed 或不在窗口内的 thread 不拉 full。
- 或改为先 `messages.list` + `messages.get(format=metadata)`，只对候选再拉 full/thread context。

风险：

- Gmail threads API 的 fields 能力有限，需要实测。
- 当前 Brief 需要 thread 内多封 cleanup 聚合，不能简单只取最新一封。

预期收益：

- full payload 拉取显著减少。
- 网络慢邮箱收益最大。

### P1b：Phase 1 前本地规则预过滤明显低价值邮件

目标：减少 Phase 1 Sampling 调用。

背景：

- 当前 Phase 1 会把所有新邮件都送入 LLM 批量分类。
- Anna Sampling 路径下 `_ANNA_PHASE1_BATCH_SIZE = 8`，扫描量上来后调用次数会明显增加。
- 现有 `candidate.py` 已有 `detect_signals()`、`classify_candidate_kind_by_rule()` 等规则能力，但主要用于 LLM 失败 fallback 或 LLM 后 evidence 融合。

建议：

- 在 `phase1.py` 中新增 conservative prefilter，例如 `prefilter_phase1_messages(messages, strategy, profile)`。
- `run_phase1_batch_classify()` 开头先拆分：
  - `llm_messages`：继续进入 Phase 1 LLM。
  - `rule_low_value_items`：规则判定为确定低价值的邮件。
  - `metrics`：记录输入数量、过滤数量、过滤原因分布。
- 后续只对 `llm_messages` 调用 LLM。
- 最终返回时合并 `rule_low_value_items + llm_low_value_items`。
- 被 prefilter 命中的邮件必须进入 `low_value_items`，继续参与 cleanup bundle 聚合展示，不能直接丢弃。
- 命中规则必须保守，宁可少过滤，不漏重要邮件。

建议先允许直接过滤的情况：

- 只有 `list_unsubscribe`。
- 只有 `low_value_bulk_possible`。
- 信号集合只包含 `low_value_bulk_possible`、`list_unsubscribe`、`unread`。
- subject/snippet 明显是 newsletter、digest、promotion、sale、webinar、unsubscribe 等低价值批量内容。

必须强制进入 LLM 的情况：

- `starred` 或 `important`。
- 命中 `security_keyword`、`billing_keyword`。
- 命中 `possible_request`、`human_reply`、`known_contact`。
- 有附件且不是明确低价值批量邮件。
- 任意规则无法确定的模糊邮件。

首轮落地边界：

- 只在 Phase 1 前过滤“确定低价值”的邮件。
- 不用规则直接生成高价值 candidates，避免把 Phase 1 语义改大。
- 不改前端契约，不改存储 schema，不改 Gmail 扫描。
- progress 中可以补充 `phase1_prefiltered`、`phase1_llm_messages` 等轻量指标。

风险：

- 误过滤会让重要邮件进 cleanup。
- 需要测试覆盖安全、账单、星标、重要发件人不能被过滤。
- 需要特别测试 cleanup bundle 聚合数量，避免回退到“聚合邮件有多封但只显示一封”的旧问题。

预期收益：

- newsletter 较多的邮箱，Phase 1 调用减少。
- 用户更快看到主卡片。
- 例如 80 封新邮件中 40 封为明确低价值批量邮件时，Phase 1 Sampling 调用可从约 10 次降到约 5 次。

### P1c：Phase 2 Evaluating 专项优化

目标：让 Phase 2 首个结果更早出现，避免单个慢请求拖住整批 `Evaluating`。

背景：

- 当前 Phase 2 每个 slice 固定取前 4 个未完成 candidate。
- 当前 batch 的上下文读取已经并发化，但下一批 candidate 不会提前进入队列。
- LLM evaluate 已用 `asyncio.create_task()` + `asyncio.as_completed()`，batch 内谁先完成谁先持久化和更新 progress。
- 剩余瓶颈是 batch 屏障：必须等当前 4 个全部结束，后面的 candidate 才能启动。
- 当前代码已把 Phase 2 单项 Sampling timeout 控制到约 25s，并预留短 invoke 的持久化和返回时间。
- Prompt 瘦身已回退，当前仍使用完整 prompt；本轮不把 prompt 语义改动和调度优化混在一起。
- 每个 judgment 完成后都会触发 active cards 读写，APS 慢时可能放大延迟。

建议：

- 修正 progress 中 `timeout_s` / `max_tokens` / batch size / concurrency，确保与真实代码一致。
- 增加 Phase 2 耗时指标：context read、prompt chars、sampling first done、sampling batch、persist、fallback。
- `read_candidate_context()` 改为有界并发读取。
- Phase 2 保留显式 `phase2_llm_concurrency`，当前默认 4。
- Phase 2 保留固定 batch：每次 continue 处理前 4 个未完成 candidate。
- 单次 Sampling timeout 必须小于 invoke slice budget，并预留持久化和返回时间。
- 按 `candidate_id` 保存完成状态；下次 continue 只处理缺失 judgment 的 candidate。
- `phase2_cursor` 只表示连续完成进度；继续按 `candidate_id` 恢复，避免重复评估。
- deadline 不足时停止补新任务，但不能让 `_brief_run_phase2_slice()` 返回后留下未追踪后台 sampling task。
- 前端在 `continue_mail_agent_run` 挂起期间也轮询 `get_mail_agent_run`，看到 `cards_version` 增加即刷新 cards。
- prompt 瘦身暂缓，继续观察 `phase2_prompt_chars`、timeout 和 fallback。
- Phase 2 前增加 conservative fast-exit，明显低风险自动通知/receipt/cleanup 直接生成低优先级 review judgment。
- 首个可展示 card 立即持久化，后续完成项可按 2 个或 500-1000ms debounce 合并写 active cards。

风险：

- 主动取消 pending Sampling 时要确认 `SamplingClient` 会清理 `_pending`，否则可能留下 orphan pending。
- 不能因为 timeout 过短而显著增加 fallback judgment；需要基于真实 p95 决定 timeout。
- 并发度过高可能触发 host `maxCalls`、provider rate limit 或更高失败率。
- 如果持久化很慢，按“持久化后才补位”会让并发槽短暂空闲；是否改成 debounce 需要另行审批。
- fast-exit 必须保守，星标、important、已知联系人、附件、问句、安全/账单关键词都不能直接跳过 LLM。

预期收益：

- 首个 judgment/card 更早出现。
- Phase 2 不再被单个慢尾请求拖住后续 candidate 启动。
- 重复评估、invoke timeout 后重跑、进度长时间不变的问题减少。

### P2：Phase 2 优先级队列与早展示

目标：先评估最可能重要的 candidates。

建议：

- Phase 1 后按 `priority_hint/confidence/unread/important/starred` 排序 candidates。
- Phase 2 优先评估高优先级。
- 每批持久化后前端立即刷新，低优先级可继续后台评估。

风险：

- 排序不能改变最终卡片集合。
- 需要确保 run history 最终汇总完整。

预期收益：

- 用户更早看到真正重要的卡片。

### P2：多邮箱扫描并发或分层调度

目标：多邮箱下减少总体等待。

保守方案：

- 继续串行，但第一个邮箱完成后立即展示。
- 后续邮箱保持底部进度。
- 慢邮箱失败或超时不阻塞其他邮箱。

激进方案：

- Gmail scan 阶段可并发多个邮箱。
- LLM Phase 1/2 仍按 invoke/sampling grant 分邮箱推进，避免超过 maxCalls。

风险：

- 多邮箱并发会放大 Gmail API quota、storage 写入和前端状态复杂度。
- Anna Sampling grant 绑定 invoke，不适合多个邮箱抢同一个 invoke 内的 calls。

预期收益：

- 多邮箱用户体感更好。

### P3：Gmail History API 增量

目标：从“搜索最近窗口”升级到“只处理真实变更”。

建议：

- 在 `ScanState` 中使用已有 `last_history_id` 字段。
- 每次扫描先调用 Gmail history.list 获取新增/变更 message ids。
- 只对变更 message/thread 拉详情。
- history 过期或不可用时 fallback 到 `newer_than + after_timestamp`。

风险：

- Gmail historyId 有有效期和过期语义。
- 初次接入复杂度较高。

预期收益：

- 长期性能最佳。
- 也能支撑 Gmail 外部状态变更感知。

## 6. 建议落地顺序

第一阶段：低风险、立刻可验证

1. 补性能观测字段。
2. 短 invoke 主路径接入 `after_timestamp`。
3. Phase 1 前增加 conservative low-value prefilter。
4. 修正 Phase 2 progress 中 `timeout_s`、batch size、并发度等字段，确保和代码真实配置一致。
5. 增加 Phase 2 per-candidate / per-batch 耗时指标，确认慢在 context、sampling 还是 persist。
6. 增加 `test_sampling_parallel`，用 synthetic prompt 诊断 Sampling 并发与排队情况。
7. 扩展 `test_scan_window.py` 和 Phase 1 prefilter 聚焦测试。
8. 前端扫描进度展示 scanned/new/skipped/candidates，并在 `continue` 挂起期间继续轮询 run state。

第二阶段：减少阻塞和 LLM 调用

1. 扫描阶段拆 cursor。
2. Gmail metadata-first 扫描。
3. Phase 2 上下文读取并发化。
4. Phase 2 按 `candidate_id` 可恢复，保持固定 batch，不留下未追踪后台 sampling task。
5. 保留固定 batch 的 Phase 2 调度，并继续观察慢尾与 fallback 分布。
6. Phase 2 前加 conservative fast-exit，减少明显低风险候选的 LLM 调用。
7. Phase 2 candidates 排序，优先展示高价值卡片。
8. prompt 瘦身暂缓，后续如果继续做，需要单独审批判断语义影响。

第三阶段：更大结构优化

1. 多邮箱调度优化。
2. Gmail History API。
3. 低风险 review 候选 micro-batch 实验。

## 7. 需要验证的指标

每次优化至少记录：

- 首次扫描总耗时。
- 非首次扫描总耗时。
- Gmail API 调用数：
  - threads.list 次数
  - threads.get 次数
- Phase 1 sampling 调用数。
- Phase 2 sampling 调用数。
- Phase 2 并发度：
  - `phase2_parallel_requested`
  - `first_done_ms`
  - `wall_ms`
  - `sum_call_ms`
  - `max_call_ms`
  - `per_call_elapsed_ms`
  - `timeout_count`
  - `rate_limit_count`
- 首张卡片出现时间。
- 扫描完成时间。
- scanned/new/skipped_processed/candidates/cards_added。

## 8. 推荐测试

```sh
cd inbox-tool/src
uv run python tests/test_scan_window.py
uv run python tests/test_gmail_query_normalize.py
uv run python tests/test_storage_integration.py
uv run python tests/test_cleanup_bundle_thread_messages.py
```

如果改 Phase 1 / Phase 2：

```sh
uv run python tests/test_llm_json_repair.py
uv run python tests/test_brief_phase2_slice.py
```

如果新增 Sampling 并发诊断工具：

```sh
uv run python tests/test_sampling_parallel.py
```

真实并发 smoke test 需要 Anna Sampling grant、网络和平台 host 支持；本地单元测试只能覆盖调度、计时字段和不泄露邮件内容。

如果改前端扫描进度或卡片刷新：

```sh
cd anna-inbox
npm run test
npm run build
```

真实性能验收需要有效 Gmail token、Anna Sampling grant 和网络，不能只靠本地 fake tests。

## 9. 当前结论

项目文档里已经有解决性能问题的方向，最关键的是：

- 短 invoke 状态机。
- 增量扫描 `after_timestamp`。
- Scan Plan 控制范围和上限。
- Phase 1/2 分批和早展示。
- 联系人记忆后置。

针对当前用户体感最明显的 `Evaluating` 卡顿，最可能的 Phase 2 缺口是：

- Phase 2 已按 README 约束保持 4 并发固定 batch；一个慢请求仍可能拖住整批收尾。
- Phase 2 上下文读取、真实 timeout/progress、candidate 级日志已经做过第一轮修正，日志不输出 prompt preview 或邮件内容。
- `continue_mail_agent_run` 内部仍要等当前 slice 收尾，但前端应在请求挂起期间持续轮询 run state。
- Prompt 偏重仍可能影响单项耗时，但瘦身已回退，后续需要单独审批。
- 单项持久化 active cards 可能在 APS 较慢时放大尾延迟。

因此建议下一步优先做 Phase 2 P0/P1：

1. 继续保留固定 batch 的 4 并发调度，重点观察慢尾 candidate 分布。
2. 继续利用 `candidate_id` 可恢复性，减少 invoke timeout 后的重复评估。
3. 结合 `phase2_prompt_chars`、`phase2_sampling_batch_ms`、`phase2_persist_ms` 判断瓶颈是在 sampling 还是 persist。
4. 确认前端在 `continue` 挂起期间持续轮询 run state。
5. 继续观察 persist 耗时，再决定是否单独做 active cards debounce。
6. prompt 瘦身和 fast-exit 作为后续单独审批项。
