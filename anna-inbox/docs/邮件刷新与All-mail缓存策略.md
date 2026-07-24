# 邮件刷新与 All-mail 缓存策略

> 同步工作集（180 天 priority + backfill、`sync_boundary`、History/Watch）见 [邮箱同步 P0：缓存优先](邮箱同步P0缓存优先方案.md)。本文只描述**列表 UI 投影**与展示时间窗。

## 目标

- 切换分类时不再按分类打 Gmail；统一 All mail 缓存 + 标签投影。
- 强制刷新：清空 → 重建优先工作集 → 前端按 `display_range_days` 投影。
- **扩大时间窗**只追加更早邮件（7→30→60→All），**不改变** AI 检索边界。
- **首屏 100 条 + 触底自动加载更多**；**分页流式加载 + 单行入场动画**；不展示 Show more 按钮。

## 设置与分页

| 字段 / 常量 | 选项 | 含义 |
| --- | --- | --- |
| `display_range_days` | 7 / 30 / 60 | 初始时间窗（仅列表投影，不限 AI） |
| `INBOX_FEED_PAGE_SIZE` | 固定 `100` | 前端首屏与触底续页步进；**不**进后端同步 |

- 列表分页**只在前端**（`localLimit`）；所有分类（Inbox / Important / Other / 自定义 Split / Todos / Snoozed / Done / Starred / Sent / Trash / Spam / All mail）共用。
- `.mail-feed` 滚到接近底部（`isMailFeedNearBottom`）时自动抬高 `localLimit`，不够再续缓存/Gmail。
- Inbox 标签角标：计数 `> 99` 显示 `99+`（`formatInboxTabCount`），不截断真实列表。
- 缓存可多拉（`ALL_MAIL_CACHE_FETCH_LIMIT=400` 仅 UI 快照 RPC；AI 扫全量本地缓存）。

## 时间窗阶梯

`7 → 30 → 60 → All time`  
底部「Show emails older than…」→ `expandInboxFeedWindow`（只 append）。

## 流式加载与动画

```
list_inbox_emails / list_cached 分页
  → 首屏 apply 后立刻结束 loading
  → 列表触底自动续页（抬 localLimit / append 缓存）
  → 新出现的行挂 is-entering，按单行 stagger 入场
切分类：skipEnterAnim，瞬间切换；localLimit 重置为 100
```

## 底部按钮

1. **Show emails older than N days**（扩时间窗 7→30→60→ALL）— 手动  
2. Drafts：`Refresh drafts`— 手动  
当前时间窗内更多：**不按钮**，滚动触底自动加载。

## Inbox vs All mail

| 视图 | 过滤 |
| --- | --- |
| Inbox | 仅 INBOX；排除 todos/done/snoozed |
| All mail | 排除 TRASH/SPAM/CHAT（含 Sent、归档） |

All mail ≫ Inbox 通常正常。

## 本地分类

| 分类 | 更多加载 |
| --- | --- |
| Todos / Snoozed / Done | 触底抬 `localLimit` |
| Drafts | `Refresh drafts` 按钮 |

## 验证

```sh
uv --directory inbox-tool/src run python tests/test_inbox_settings_storage.py
uv --directory inbox-tool/src run python tests/test_inbox_feed.py
cd anna-inbox && npm test -- src/features/settings/inboxSettings.test.ts src/features/home/HomeView.test.ts
```
