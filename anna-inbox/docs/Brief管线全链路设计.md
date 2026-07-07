# Brief 管线

Brief 是后端中的可预测注意力工作流。2.0 主界面是 Inbox Workspace，但 Brief 工具、卡片存储和兼容组件仍然保留。

## 流程

```text
Gmail scan
  -> 已处理过滤和 thread 去重
  -> Phase 1: reply / review / ignore
  -> Phase 2: 深度判断、优先级、reply gaps
  -> 规则 guard
  -> Attention Cards / Cleanup Bundle
  -> storage + run history
```

Phase 1 位于 `mail_agent/core/phase1.py`，只使用轻量邮件信息。Phase 2 位于 `mail_agent/judgment_engine/service.py`，按需读取正文和 thread context。`cards/service.py` 负责持久卡片、合并和前端 DTO。

## 一致性与安全

- thread 去重优先保留最新有效 INBOX/DRAFT，纯 SENT 不产生“回复自己”卡片。
- 规则 guard 的结论优先于 LLM 自由文本。
- 卡片操作使用卡片自身 mailbox。
- cards、processed IDs、scan state 和 history 通过 `mail_agent/storage/ops.py` 写入。
- 邮件正文不写入运行日志。

## 当前定位

Brief 不是 Inbox 文件夹分类器。Inbox Workspace 直接展示 Gmail 和本地状态；Attention Cards 是独立的后端任务模型。
