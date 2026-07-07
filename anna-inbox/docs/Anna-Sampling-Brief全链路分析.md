# Anna Sampling 与 Brief

Brief 的 Phase 1、Phase 2、联系人记忆和部分 Ask/草稿任务可通过 Anna Host sampling 调用 LLM。调用集中在 `mail_agent/llm_runtime/service.py`。

## 调用规则

- provider 由工具上下文和参数选择，支持 Anna LLM 与 DashScope 路径。
- sampling 失败、超时或返回非法 JSON 时执行受限重试与 JSON repair。
- repair prompt 不包含原始邮件正文。
- 解析失败最终返回安全 fallback，不能让协议进程崩溃。
- 每次短 invoke 为平台调用上限保留余量。

## 数据最小化

- Phase 1 只发送压缩后的 header/snippet 信息。
- Phase 2 只读取候选所需的正文和 thread context。
- 日志只记录阶段、耗时、数量和错误类型，不记录 prompt、正文、subject 或 credential。

## 验证

- `tests/test_llm_json_repair.py`
- `tests/test_phase2_prompt_logging.py`
- `tests/test_judgment_priority_calibration.py`
- `tests/test_brief_phase2_slice.py`
