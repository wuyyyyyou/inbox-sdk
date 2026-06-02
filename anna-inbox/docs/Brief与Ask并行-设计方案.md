# Brief 与 Ask 并行 — 设计方案

## 问题

Brief 扫描期间（`startScan()`），`state.view` 被强制设为 `"scanning"`，导致：
- Ask 视图的 textarea 无法输入文字
- 轮询每 2.5s 执行 `render()` → `content.innerHTML` 全量刷新 → textarea 被销毁重建
- 用户只能在扫描结束后使用 Ask

## 根因

两个前端设计缺陷：

1. `startScan()` 设置 `state.view = "scanning"`，`render()` 独占扫描进度页
2. 轮询期间 `render()` 全量刷新 DOM，textarea 失去焦点和内容

## 方案

核心思路：**Brief 扫描不抢占视图，用户自由切换。在 Brief 视图时显示全屏进度，切换到 Ask 时进度缩到底部 bar。**

### state 改动

```javascript
// 新增
briefScanProgress: null,  // { stage, progress, stepIndex } — 替代用 view 和 isScanning 驱动
```

### startScan() 改动

```javascript
// 之前
state.view = "scanning";  // 强制独占

// 之后
// 不改变 state.view，只更新 briefScanProgress
// 轮询时：
//   - 当前在 Brief → render() 更新全屏进度
//   - 当前在 Ask → renderBottomBar() 局部更新进度条
// 扫描完成时不强制跳转
```

### render() 改动

```javascript
// 之前
if (state.view === "scanning") renderScanning();  // 阻断所有其他视图

// 之后
if (state.view === "start" && state.isScanning) {
  renderScanning();  // 仅在 Brief 标签页显示全屏进度
}
// Ask 视图无论是否在扫描都正常渲染
if (state.view === "ask") renderAsk();
```

### 导航不改

```javascript
// go-ask / go-brief 不检查 isScanning
// 用户自由切换
```

### renderBottomBar() 改动

```javascript
// 之前检查 state.view === "scanning"
// 之后检查 state.isScanning

// Brief 视图时：底部栏显示简洁状态 "Scanning..."
// Ask 视图时：底部栏显示详细进度 "Processing messages (12/50)"
```

### renderScanning() 保留

保留全屏进度页面，仅在用户当前处于 Brief 标签页时显示。用户切换到 Ask 后自动隐藏。

## 效果

| 场景 | Brief 视图 | Ask 视图 | 底部栏 |
|------|-----------|---------|--------|
| 扫描中 | 全屏进度（orb + 步骤条） | 正常输入，可提交 Ask | 显示扫描进度 |
| 扫描完成 | 自动刷新卡片列表 | 保持当前 Ask 结果不变 | 恢复正常状态 |
| 切换 | 进度页面恢复 | 底部栏继续显示进度 | — |
