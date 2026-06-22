# Brief 扫描性能问题与优化方案

> 更新时间：2026-06-22（北京时间）  
> 状态：问题记录与预案。本文先不代表已实施，后续优化按优先级逐项落地。  
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
- Phase 2 单项 Anna Sampling timeout 从 55s 降到 25s，`max_tokens` 从 8000 降到 5000。
- 已新增聚焦测试：`tests/test_brief_incremental_scan.py`、`tests/test_phase1_prefilter.py`。

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
4. 扩展 `test_scan_window.py` 和 Phase 1 prefilter 聚焦测试。
5. 前端扫描进度展示 scanned/new/skipped/candidates。

第二阶段：减少阻塞和 LLM 调用

1. 扫描阶段拆 cursor。
2. Gmail metadata-first 扫描。
3. Phase 2 candidates 排序，优先展示高价值卡片。

第三阶段：更大结构优化

1. 多邮箱调度优化。
2. Gmail History API。

## 7. 需要验证的指标

每次优化至少记录：

- 首次扫描总耗时。
- 非首次扫描总耗时。
- Gmail API 调用数：
  - threads.list 次数
  - threads.get 次数
- Phase 1 sampling 调用数。
- Phase 2 sampling 调用数。
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
```

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

当前最可能的性能缺口是：**UI 主路径的扫描阶段还没有真正增量化，也没有可续跑 scan cursor；它仍会在每次扫描时重复拉取最近窗口内的 Gmail full threads。**

因此建议下一步先做 P0/P1：

1. 补性能观测。
2. 把 `after_timestamp` 接入 `_brief_prepare_scan()`。
3. 为扫描阶段拆 cursor 做设计和小步实现。
