# Anna Sampling 与 Brief

Brief 的 Phase 1、Phase 2、联系人记忆和部分 Ask/草稿任务可通过 Anna Host sampling 调用 LLM。调用集中在 `mail_agent/llm_runtime/service.py`。

## 调用规则

- provider 由工具上下文和参数选择，支持 Anna LLM 与 DashScope 路径。
- 所有生产 Anna Sampling 调用通过同一预算守卫：单次超时 60 秒、单次最多 4096 tokens、同一 invoke 累计最多 6000 tokens。预算耗尽在本地失败并交给既有 fallback，避免 Host 返回累计 token 溢出错误。
- sampling 失败、超时或返回非法 JSON 时执行受限重试与 JSON repair。
- Ask 的规划 Sampling 若返回空内容，则在一次尝试后使用确定性 `AskPlan` fallback，避免空 Host 响应使整个扫描 run 失败。
- repair prompt 不包含原始邮件正文。
- 解析失败最终返回安全 fallback，不能让协议进程崩溃。
- 每次短 invoke 为平台调用上限保留余量。

## 观测

- 每次生产 Sampling 记录开始、成功或失败事件，日志只包含工具名、请求与授予 token、剩余 token、超时、UTF-8 `prompt_bytes`、耗时和错误类型。`prompt_bytes` 用于观察反向 JSON-RPC 请求是否接近协议帧上限。
- 日志不得包含 prompt、响应正文、邮件主题、地址、凭据或完整 metadata。

## 数据最小化

- Phase 1 只发送压缩后的 header/snippet 信息。
- Phase 2 只读取候选所需的正文和 thread context。
- 日志只记录阶段、耗时、数量和错误类型，不记录 prompt、正文、subject 或 credential。

## Ask 扫描载荷边界

- 检索阶段在有效 Scan Plan 范围内保存全部命中的紧凑来源信息并用于扫描统计。
- Ask 不再为筛选额外调用或重试 Sampling；全部命中由本地关键词、未读状态和时间排序，避免空响应和重复筛选消耗预算。
- 只有按请求关键词、未读状态和时间排序后的最多 8 封候选读取正文；每封正文最多 1200 字符，线程最多 3 条消息且每条最多 400 字符。
- Ask 在 Anna Sampling 下只执行一次紧凑 Answer 调用，最大输出 1536 tokens；JSON repair 固定最多 512 tokens，不会沿用 Answer 的输出额度。
- Ask 回答提示词要求基于证据用自己的话综合总结，除必要短引文或精确主题外不得逐字复制邮件正文。

## 验证

- `tests/test_llm_json_repair.py`
- `tests/test_phase2_prompt_logging.py`
- `tests/test_judgment_priority_calibration.py`
- `tests/test_brief_phase2_slice.py`
