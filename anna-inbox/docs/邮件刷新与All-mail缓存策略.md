# 邮件刷新与 All-mail 缓存策略

> 同步工作集（180 天 priority + backfill、`sync_boundary`、History/Watch）见 [邮箱同步 P0：缓存优先](邮箱同步P0缓存优先方案.md)。本文只描述**列表 UI 投影**与展示时间窗。

## 目标

- 切换分类时不再按分类打 Gmail；统一 All mail 缓存 + 标签投影。
- 强制刷新：清空 → 重建优先工作集 → 前端按 `display_range_days` 投影。
- **扩大时间窗**只追加更早邮件（7→30→60→All），**不改变** AI 检索边界。
- **首屏阈值 + Show more**；**分页流式加载 + 单行入场动画**。

## 设置项

| 字段 | 选项 | 含义 |
| --- | --- | --- |
| `display_range_days` | 7 / 30 / 60 | 初始时间窗 |
| `initial_list_size` | 100 / 200 / 400 | 首屏最多展示 thread 行数 |

阈值**只控展示**；缓存可多拉（`ALL_MAIL_CACHE_FETCH_LIMIT=400`）。

## 时间窗阶梯

`7 → 30 → 60 → All time`  
底部「Show emails older than…」→ `expandInboxFeedWindow`（只 append）。

## 流式加载与动画

```
list_inbox_emails / list_cached 分页
  → 首屏 apply 后立刻结束 loading
  → 后续页 append 进快照（边加载边渲染）
  → 新出现的行挂 is-entering，按单行 stagger 入场
切分类：skipEnterAnim，瞬间切换
```

## 底部按钮顺序

1. **Show more from the last N days**（当前时间段内更多）— 抬高 `localLimit`，不够再续缓存/Gmail  
2. **Show emails older than N days**（扩时间窗 7→30→60→ALL）  
3. Drafts：`Refresh drafts`

## Inbox vs All mail

| 视图 | 过滤 |
| --- | --- |
| Inbox | 仅 INBOX；排除 todos/done/snoozed |
| All mail | 排除 TRASH/SPAM/CHAT（含 Sent、归档） |

All mail ≫ Inbox 通常正常。

## 本地分类

| 分类 | 底部 |
| --- | --- |
| Todos / Snoozed / Done | Show more emails（抬 localLimit） |
| Drafts | Refresh drafts |

## 验证

```sh
uv --directory inbox-tool/src run python tests/test_inbox_settings_storage.py
uv --directory inbox-tool/src run python tests/test_inbox_feed.py
cd anna-inbox && npm test -- src/features/settings/inboxSettings.test.ts src/features/home/HomeView.test.ts
```
