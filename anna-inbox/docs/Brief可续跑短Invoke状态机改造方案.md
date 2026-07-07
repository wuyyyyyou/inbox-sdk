# Brief 短 Invoke 状态机

Brief 通过 `start_mail_agent_run` 创建 run，再由 `continue_mail_agent_run` 分段推进。入口实现在 `anna_inbox_executa/brief_flow.py`。

## 原则

- start 只初始化参数和 run state，不执行完整扫描。
- continue 每次推进有限扫描页、Phase 1 batch 或 Phase 2 slice。
- 每次更新都持久化 cursor、候选、完成项、错误和进度。
- 重试同一个 run 时跳过已完成候选，避免重复 sampling 和重复卡片。
- mailbox 之间的失败相互隔离。
- 完成后写 cards、scan state 和 run history。

前端或调用方根据返回的 `status`、`stage`、`progress` 和 `run_id` 决定继续轮询。轮询超时不等同于任务失败。

## 验证

- `tests/test_brief_incremental_scan.py`
- `tests/test_brief_phase2_slice.py`
- `tests/test_scan_window.py`
