# Roadmap：Brief 扫描性能

状态：未来优化，不代表 2.0.1 已承诺功能。

后续评估方向：Gmail 分页并发、Phase 1 本地预过滤、Phase 2 slice 大小、sampling timeout、HTML/quoted content 压缩，以及 cards 增量可见性。

优化必须保持短 invoke 可恢复、sampling 调用上限、无正文日志和幂等持久化，并以 `test_brief_incremental_scan.py`、`test_brief_phase2_slice.py` 和实际耗时指标验证。
