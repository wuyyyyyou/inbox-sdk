# Brief 可续跑短 Invoke 状态机改造方案

更新时间：2026-06-11 15:37:44 +08:00（北京时间）

## 背景

Anna 平台对一次 tool invoke 有两个关键限制：

- 单次 invoke 约 65 秒超时。
- 同一个 `invoke_id` 下 `sampling_grant.maxCalls` 最多 8 次。

因此 Brief 不能再设计成一次 `continue_mail_agent_run` 阻塞跑完整扫描、筛选、Phase1、Phase2、卡片持久化和联系人记忆。真实邮箱里如果扫描邮件多、候选 item 多，完整链路很容易超过 65 秒或超过 8 次 sampling。

目标是把 Brief 改造成前端驱动的多次短 invoke 状态机：

- 每次 invoke 只推进一小段工作。
- 每次 invoke 都绑定当前 `invoke_id` 调用 Anna sampling。
- 每次 invoke 都控制 sampling 次数和耗时。
- Phase2 每批产出卡片后立即持久化，前端即可刷新展示。
- 联系人记忆在卡片展示后用独立 invoke 后台生成。

## 总体调用流程

```text
用户点击 Brief 扫描
  |
  v
start_mail_agent_run
  - 创建 run_id
  - 初始化持久化 run state
  - 不做重活
  - 不调用 sampling
  |
  v
前端循环调用 continue_mail_agent_run(run_id)
  |
  +-- scanning/filtering: 扫描、去重、过滤、线程合并
  |
  +-- phase1: header/snippet 批量 LLM 分类，分批并行
  |
  +-- phase2: candidate 深度判断，分批评估
  |
  +-- finalizing: 汇总运行记录、scan state、完成标记
  |
  v
continue 返回 status=done
  |
  v
前端最后刷新 get_active_cards
  |
  v
前端 fire-and-forget 调用 generate_contact_memories
```

## Tool 职责

### start_mail_agent_run

职责：

- 创建 `run_id`。
- 初始化 run state。
- 保存用户请求、mailbox、mode、provider、storage provider 等参数。
- 返回 `status=queued`。

不做：

- 不扫描 Gmail。
- 不调用 sampling。
- 不生成卡片。
- 不写联系人记忆。

示例返回：

```json
{
  "success": true,
  "run_id": "bg_xxx",
  "status": "queued",
  "stage": "queued",
  "needs_continue": true,
  "started_at": "2026-06-11T15:37:44+08:00"
}
```

### continue_mail_agent_run

职责：

- 读取 `run_id` 对应的持久化 run state。
- 根据当前 `stage` 推进一小段工作。
- 本次 invoke 内所有 sampling 调用都绑定当前 `invoke_id`。
- 在触达时间预算、sampling 预算或阶段边界时返回。

硬性结束条件：

- `elapsed_ms >= 45_000` 到 `55_000`。
- `sampling_calls_used >= 6`，最多不超过 7，保留平台余量。
- 当前阶段自然完成。
- 本批任务完成并已有卡片可展示。
- 发生可恢复错误，需要下次重试。
- 发生不可恢复错误，返回 failed。

示例返回：

```json
{
  "success": true,
  "run_id": "bg_xxx",
  "status": "running",
  "stage": "phase2",
  "needs_continue": true,
  "cards_added": 4,
  "cards_version": 3,
  "progress": {
    "evaluated": 8,
    "total": 31,
    "sampling_calls_used": 4
  }
}
```

### get_mail_agent_run

职责：

- 只读 run state。
- 给前端展示进度。
- 不推进状态机。
- 不调用 sampling。

### generate_contact_memories

职责：

- 在卡片已经展示后，由前端独立触发。
- 使用独立 invoke 的 `invoke_id` 调用 sampling。
- 只处理本次扫描后新增或更新的 active cards。
- 不阻塞 Brief 卡片展示。

## 状态机

```text
queued
  -> scanning
  -> filtering
  -> phase1
  -> phase2
  -> finalizing
  -> done

任意阶段可进入 failed。
```

### queued

初始状态，只保存参数。

### scanning

做 Gmail scan / cache read。

特点：

- 非 LLM。
- 不消耗 sampling calls。
- 尽量一次 invoke 内完成。
- 如果扫描非常慢，也可以在时间预算接近上限时保存 scan cursor，下次继续。

输出：

- `message_refs`
- `scan_total`
- `scan_cursor`

### filtering

做存储过滤、thread dedup、already replied filter。

特点：

- 非 LLM。
- 不消耗 sampling calls。
- 产出进入 Phase1 的 message pool。

输出：

- `phase1_messages`
- `phase1_total`
- `phase1_cursor = 0`

### phase1

对 header/snippet 做轻量 LLM 分类。

推荐策略：

- 每 20 封邮件组成一个 Phase1 batch。
- 每个 batch 发一次 sampling。
- 每次 invoke 并行 4 个 batch。
- 也就是一次 Phase1 invoke 最多处理约 80 封邮件。
- 4 次 sampling 调用低于 maxCalls=8，给 retry / JSON repair / 异常留余量。

200 封邮件示例：

```text
continue #1: scanning + filtering，得到 200 封待 Phase1 邮件
continue #2: Phase1 batch 0-3，并行 4 次 sampling，处理 80 封
continue #3: Phase1 batch 4-7，并行 4 次 sampling，处理 80 封
continue #4: Phase1 batch 8-9，并行 2 次 sampling，处理 40 封，生成 candidates
continue #5+: Phase2 分批评估 candidates
```

注意：

- 不建议按“每 20 封完整 Phase1 + Phase2”推进。
- 应先建立全量候选池，再对候选池分批 Phase2。
- 这样可以避免先展示低优先级卡片，也避免重复 parse/策略构建和 processed 状态错位。

Phase1 state 示例：

```json
{
  "stage": "phase1",
  "phase1": {
    "batch_size": 20,
    "cursor": 160,
    "total": 200,
    "results": [
      {
        "batch_index": 0,
        "status": "done",
        "items": []
      }
    ],
    "failed_batches": []
  }
}
```

### phase2

对 Phase1 产出的 candidates 做深度判断。

推荐策略：

- 每次 invoke 评估 3 到 5 个 candidates。
- 如果每个 candidate 可能有 2 次 attempt，则本批数量要更保守。
- 每批完成后立刻持久化卡片。
- 前端看到 `cards_added > 0` 或 `cards_version` 增加后刷新 `get_active_cards`。

Phase2 invoke 示例：

```text
continue #5:
  - 读取 candidates[0..3]
  - 并行评估
  - 生成 2 张卡片
  - 持久化 active cards
  - phase2_cursor = 4
  - 返回 cards_added=2, needs_continue=true

continue #6:
  - 读取 candidates[4..7]
  - 并行评估
  - 生成 3 张卡片
  - 持久化 active cards
  - phase2_cursor = 8
  - 返回 cards_added=3, needs_continue=true
```

### finalizing

所有 candidates 评估完成后：

- 保存 run record。
- 更新 scan state。
- 更新 run history。
- 标记 `status=done`。

## Run State 持久化结构

建议最小结构：

```json
{
  "run_id": "bg_xxx",
  "status": "running",
  "stage": "phase2",
  "started_at": "2026-06-11T15:37:44+08:00",
  "updated_at": "2026-06-11T15:38:12+08:00",
  "args": {
    "mailbox": "user@example.com",
    "mode": "auto",
    "ai_provider": "anna-llm",
    "storage_provider": "aps"
  },
  "scan": {
    "message_refs": [],
    "total": 200,
    "cursor": 200
  },
  "phase1": {
    "batch_size": 20,
    "cursor": 200,
    "total": 200,
    "results": [],
    "failed_batches": []
  },
  "phase2": {
    "cursor": 8,
    "total": 31,
    "judgments": [],
    "failed_candidate_ids": []
  },
  "cards_version": 3,
  "progress": {
    "scanned": 200,
    "phase1_done": 200,
    "evaluated": 8,
    "total_candidates": 31
  },
  "error": ""
}
```

## 前端循环

```ts
const started = await client.startBriefRun(args);
let cardsVersion = 0;
let done = false;

while (!done) {
  const step = await client.continueBriefRun({ run_id: started.run_id });

  updateProgress(step);

  if (step.cards_added > 0 || Number(step.cards_version || 0) > cardsVersion) {
    cardsVersion = Number(step.cards_version || cardsVersion);
    await loadActiveCards();
  }

  if (step.status === "done") {
    done = true;
  } else if (step.status === "failed") {
    showError(step.error || "Brief scan failed");
    break;
  } else if (!step.needs_continue) {
    break;
  }
}

await loadActiveCards();

void client.generateContactMemories({
  mailbox,
  since: started.started_at,
  ai_provider,
  storage_provider,
}).catch(() => {});
```

## Sampling Budget

单次 invoke 最大 8 calls，但不应把 8 用满。

建议：

- 默认预算：6 calls。
- 绝对上限：7 calls。
- 保留 1 到 2 calls 给 retry / repair / 平台抖动。

Phase1：

- 每 batch 20 封。
- 每 invoke 4 个 batch 并行。
- 最多 4 calls。

Phase2：

- 每 candidate 通常 1 call。
- 如果允许 2 attempts，则每 invoke 评估 3 个 candidates 更稳。
- 如果禁用重试，则每 invoke 可评估 4 到 5 个 candidates。

## 进度展示

前端展示应来自 run state，而不是依赖 `continue` 最终返回。

阶段文案建议：

```text
scanning: Reading Gmail source
filtering: Filtering processed threads
phase1: Classifying headers 80/200
phase2: Evaluating candidates 8/31
finalizing: Saving brief
done: Scan complete
```

当 `cards_added > 0`：

- 立即刷新卡片列表。
- 允许用户先处理已出现的卡片。
- 进度卡继续显示剩余评估进度。

## 失败与恢复

### Phase1 batch 失败

处理方式：

- 记录 `failed_batches`。
- 本次 invoke 返回 `needs_continue=true`。
- 下次优先重试 failed batch。
- 超过重试次数后，用 rule-based fallback 或标记该批跳过。

### Phase2 candidate 失败

处理方式：

- 为 candidate 生成 fallback judgment，或记录到 `failed_candidate_ids`。
- 不阻塞其他 candidates。
- 继续持久化已成功生成的卡片。

### 前端中断

处理方式：

- run state 已持久化。
- 用户再次进入时可以读取未完成 run。
- 前端可以继续调用 `continue_mail_agent_run(run_id)`。

### invoke 超时前主动返回

后端必须主动根据 elapsed time 提前返回，而不是等平台杀掉进程：

```text
elapsed_ms >= 45_000: 停止领取新任务
elapsed_ms >= 55_000: 保存状态并返回
```

## 联系人记忆

联系人记忆不属于主 Brief 阻塞路径。

推荐流程：

```text
Phase2 批次生成卡片
  -> 持久化 active cards
  -> 前端刷新卡片
  -> Brief done 后或卡片出现后调用 generate_contact_memories
```

`generate_contact_memories`：

- 使用独立 invoke。
- 使用独立 `invoke_id` 调用 sampling。
- 通过 `since` 只处理本次扫描后的新卡片。
- 不影响 Brief 主进度。

## 不推荐方案

### 单次 continue 跑完整 Brief

问题：

- 容易超过 65 秒。
- 容易超过 8 次 sampling。
- 卡片必须等所有候选评估完成才显示。

### 每 20 封完整跑 Phase1 + Phase2

问题：

- 先展示的卡片不一定重要。
- Phase1 视野碎片化。
- processed / thread dedup / candidate ordering 更容易错位。
- parse intent 和策略构建可能重复消耗 sampling。

## 推荐落地顺序

1. 把 run state 持久化结构补齐，支持 stage/cursor/cards_version。
2. 改造 `continue_mail_agent_run` 为短 invoke 状态机。
3. 先实现 scanning/filtering 一次推进。
4. 实现 Phase1 batch cursor，每 20 封一个 batch，每 invoke 并行 4 batch。
5. 实现 Phase2 candidate cursor，每批评估 3 到 5 个 candidates。
6. 前端改成 `while needs_continue` 循环，并在 `cards_version` 变化时刷新卡片。
7. 保留 `generate_contact_memories` 为后置独立 invoke。
8. 增加恢复逻辑：未完成 run 可继续、失败 batch/candidate 可重试。
