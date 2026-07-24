# AI 生产结构多方案选型调研

状态：**选型调研稿**（对照用；不替代实现方案）  
日期：2026-07-22  
性质：讨论稿，记录 A–E 架构候选与验证思路；**实现主路径见 [AI 生产结构优化方案](AI生产结构优化方案.md)（SQLite 读模型）**。

配套：

- [AI 生产结构优化方案](AI生产结构优化方案.md) — **当前采用方向：SQLite + FTS5**
- [AI 优化建议](AI优化建议.md) — 分层思想与开源参考（Onyx / Inbox Zero / Mailspring / Mail-0 / pgvector / Tika）
- [2.2.6 架构与发布基线](2.2.6架构与发布基线.md) — 当前代码事实
- [平台运行性能诊断与优化进度](平台运行性能诊断与优化进度.md)
- 测试结果：`tests/APP测试Tracking - INBOX测试-results.md`

---

## 0. 文档目的

本文保留多方案对照材料，供评审与回看取舍理由。正式落地以 SQLite 方案为准。本文做三件事：

1. 用 126 条 Tracking 与现码约束，固定「要解决什么问题」。
2. 结合 [AI 优化建议](AI优化建议.md) 中的开源项目，记录 **5 套可对照的架构候选**（可组合）。
3. 给出横向对比维度与验证探针；实现细节与阶段门槛见 [AI 生产结构优化方案](AI生产结构优化方案.md)。

硬目标（用于筛选方案，不是已承诺 SLA）：

| 目标 | 说明 |
| --- | --- |
| 完整最终答案延迟 | 用户侧 p95 目标 **5 秒内**给出完整答案（非仅 Thinking / run 已创建） |
| 成功率 | 可审计场景 **≥ 90%**（定义见 §5） |
| 产品定位 | 邮件助手：搜、读、总结、草稿、整理建议、多轮；写操作须确认 |
| 部署边界 | Executa 随 App 发布的二进制；Gmail 为邮件事实源；不默认引入常驻云服务 |

**约束（讨论时可放宽，但须显式声明代价）：**

- 默认倾向：不强制 PostgreSQL / Redis / Celery / S3 / 远程向量库 / Gmail Watch 常驻。
- 若某方案依赖上述能力，必须单列「平台/运维前提」与「与现部署模型冲突点」。
- 侧栏产品入口倾向保留 Host Agent Session；不恢复前端业务正则路由。
- 不静默 Gmail mutation；正文为不可信数据（prompt injection）。

---

## 1. 问题证据（所有方案共享）

### 1.1 当前路径（App 2.1.6 / Tool 2.2.6）

```text
用户自然语言
  → Host Agent Session（选型）
  → search_email（实时 Gmail；默认继承 7/30/60 天；≤20 条摘要）
  → read_email（按需 THREAD_REF）
  → 可能多轮工具循环
  → 模型生成
```

列表 All-mail / History 增量服务 **Inbox UI**，**不是** AI 检索主路径。`start_ai_turn` 的 5s 是建 run 等待上限，不是完整答案 SLA。

### 1.2 126 条 Tracking 摘要

| 指标 | 结果 |
| --- | ---: |
| 通过率 | 35.7%（45/126） |
| 平均 / 中位耗时 | 21.7s / ~20s |
| ≤5s 完成 | 4/126 |
| 「暂时不可用」类 | 27 |
| 审核器 JSON 失败 | 25（**不得**计入产品 KPI） |
| 可靠下界 / 乐观上界 | ~36% / ~56%（均远低于 90%） |

### 1.3 失败根因桶（方案要对准的靶）

| 桶 | 含义 | 典型测例 |
| --- | --- | --- |
| F1 | Top-K 截断、无完备性，「没有/全部/最早」不可证 | J03, J06, A08 |
| F2 | 默认时间窗 / 日期谓词错误导致漏检 | A06, A10, B14 |
| F3 | 多轮 Gmail + 模型尾延迟 / 超时 | B03, E03, D08 |
| F4 | 附件名等字段不在证据里 | A03, B07 |
| F5 | 多轮草稿/进度/thread 指针丢失 | D03-1, K03, K07 |
| F6 | 过度依赖「当前打开邮件」 | D01, K04 |
| F7 | 安全/风险规则不足 | C02, I05 |
| F8 | 同步/数据范围话术编造或不一致 | L05–L06 |
| F9 | 时间窗口结果非单调 | J01 |
| F10 | 审核器噪声 | 25 条 JSON 失败 |

所有候选方案至少要说明：如何处理 **F1–F3、F9**（正确性与延迟主因），以及 F5/F7/F8 的落点。

---

## 2. 开源参考 → 可借鉴点（非照搬技术栈）

来自 [AI 优化建议](AI优化建议.md) 第十一节，映射到 Anna 二进制部署时的**可借 / 难借**：

| 项目 | 可借鉴 | 难直接照搬 | 主要启发方案 |
| --- | --- | --- | --- |
| **[Onyx](https://github.com/onyx-dot-app/onyx)** | Connector、Document Pipeline、Index、Hybrid Search、RAG 分层 | 常驻 Worker、Postgres、队列、多源企业检索体量 | C / D |
| **[Inbox Zero](https://github.com/elie222/inbox-zero)** | AI Reply、Rules、Draft、Batch、Waiting on reply 等产品状态机 | 其云端/账号模型与 Anna Host 不同 | A / E |
| **[Mailspring](https://github.com/Foundry376/Mailspring)** | IMAP/本地 DB、Thread 模型、离线缓存、客户端读模型 | Electron 本地盘假设 vs 平台 `./.data` 未知持久性 | B / C |
| **Mail-0 / Zero** | 现代 Gmail 集成、多账户、schema 思路 | Postgres 主存储 | C / D（仅 schema 思路） |
| **pgvector** | Hybrid：结构化过滤 + 向量召回 | 需 Postgres 或另建向量运行时；二进制体积与索引成本 | D（可选二期） |
| **Unstructured / Apache Tika** | 附件 PDF/Office/OCR 解析流水线 | 体积、安全沙箱、CPU；不宜问答时同步跑 | C/D 的 jobs 侧车 |

共性原则（建议文核心，各方案都应遵守）：

> **搜索/取证交给确定性系统；理解/文案交给模型。**  
> 避免「LLM 自己反复 search → get → search」。

---

## 3. 五套架构候选（待讨论）

下列方案按「改动面从小到大 / 基础设施从轻到重」排列。  
**可组合**：例如「A 的 Rules + B 的缓存优先 + E 的 Query Plan」。最终可能是混合体，而非五选一。

---

### 方案 A — 增强实时 Agent（现状演进）

**一句话：** 不建新索引引擎；继续实时 Gmail，但把「乱搜」改成「有预算的计划执行 + 产品规则」。

**参考：** Inbox Zero（Rules / Draft / Batch 产品逻辑）；建议文「Query Plan」中的计划层（计划在代码或一次轻量模型中生成，执行不交给自由多轮）。

```text
用户问题
  → Host Agent（或一次轻量 Planner）
  → 结构化 Query Plan（时间窗、发件人、完备性要求、max_calls）
  → 受预算约束的 Gmail 执行器（并行/分页有上限，返回 coverage）
  → Evidence Bundle + 边界事实
  → 一次（或零次）模型生成
  → 草稿 / 规则命中提示 / 确认卡片
```

| 模块 | 设计要点 |
| --- | --- |
| 执行器 | `search_email` 保留，但包装为：强制返回 `truncated` / `next_page_token` / `applied_time_range`；禁止静默「没有」 |
| 预算 | 单 turn 最大 Gmail 调用次数、总 HTTP 时间、最大 read 正文数 |
| Rules | Inbox Zero 式：风险域名、密码索要、改发票、能力边界 → 确定性文案 |
| 会话 | conversation / 草稿写入现有 APS/KV（ask_history 扩展），不依赖本地 DB |
| 附件 | 仍按需；可先把「附件文件名」打进 metadata 摘要字段 |

**优点：** 实现路径最短；不依赖磁盘持久性；可快速修 F7/F8 话术与部分 F3。  
**风险：** F1/F9（完备统计）与 5s 硬 SLA 仍可能做不到；Gmail 尾延迟不可控。  
**适合验证的问题：**「不建索引，只改编排，上限能摸到多少成功率 / 延迟？」

---

### 方案 B — All-mail / 现有缓存优先（Mailspring 轻量本地读）

**一句话：** AI 检索主路径改为「先查已有 All-mail + 正文缓存」，缺口再受控回源 Gmail。

**参考：** Mailspring Offline Cache / Thread Model；当前仓库已有列表缓存与 History 增量。

```text
Gmail ──History/全量刷新──► 现有 All-mail / body 缓存（APS 或 local JSON）
                              │
用户问题 → query 层（local_query 增强）
                              ├─ 命中且 coverage 足够 → Evidence
                              └─ 缺口 → 一次 Gmail 回源 → 回写缓存 → Evidence
                              → 模型（可选）
```

| 模块 | 设计要点 |
| --- | --- |
| 覆盖证明 | 缓存记录 `coverage_start/end`、是否穷尽、history 是否健康 |
| 查询 | 扩展 `local_query`：日期单调 COUNT、发件人、关键词；突破「仅 UI 列表」语义 |
| 与 UI 对齐 | 列表展示范围 ≠ AI 可证范围时，必须向用户说清（修 F8） |
| 存储 | **不新建 SQLite**；在现有存储上加索引字段或二级倒排（视 APS 能力） |
| 正文 | 详情已缓存则禁止再打 Gmail（已有线程缓存策略可复用） |

**优点：** 复用已投成本；延迟在缓存命中时接近本地；比 A 更易修 F1/F2。  
**风险：** APS/JSON 不适合重 FTS；大邮箱关键词召回弱；缓存与 Gmail 一致性边界复杂。  
**适合验证的问题：**「现有 400 封级 All-mail 缓存覆盖了多少 Tracking 场景？命中率与正确率？」

---

### 方案 C — 本地 SQLite 读模型 + FTS5（Mailspring DB × Onyx Index 简化）

**一句话：** 二进制内维护每邮箱隔离的 SQLite 读模型；FTS5 全文；jobs 表做断点任务；问答默认不走 Gmail。

**参考：** Mailspring Local DB；Onyx 的 Index + Pipeline（去掉常驻集群）；建议文「数据库负责找」。

```text
Gmail
  │ 首次/增量/缺口回源
  v
SQLite（WAL）：messages / threads / attachments / message_fts / sync_state / jobs / conversation_state
  │ 预处理 jobs：HTML→text、签名候选、附件清单、thread_facts
  v
query_mail_evidence（确定性 SQL+FTS+完备性）
  v
Evidence + boundary → 模板或一次模型 → 答案
```

| 模块 | 设计要点 |
| --- | --- |
| 持久化前提 | **平台 `./.data`（或等价路径）跨重启可读** — 未验证则本方案不可上线 |
| FTS5 | 主题 + 清洗正文 + 参与人 + 附件文件名；首期不做向量 |
| 完备性 | COUNT + 覆盖窗；「没有」必须带 coverage |
| jobs | 同步/清洗/摘要/附件解析可断点；进程退出可续 |
| Host | 白名单收敛为复合取证工具，禁止自由 search 环 |

**优点：** 最贴近 5s + 完备查询的结构性解；F1/F4/F5/F9 有明确落点。  
**风险：** 平台磁盘不持久则全盘失败；迁移与存储膨胀；首期开发量大。  
**适合验证的问题：**「平台 SQLite 是否持久？导入 90 天索引体积/耗时？确定性查询正确率？」

---

### 方案 D — Onyx 式 Pipeline + Hybrid（FTS + 可选向量）+ 附件解析

**一句话：** 在 C（或独立存储）之上，完整 Document Pipeline：清洗 → 分块 →（可选）Embedding → Hybrid 召回；附件走 Tika/Unstructured 风格异步解析。

**参考：** Onyx 全链路；pgvector Hybrid；Unstructured/Tika。

```text
Connector(Gmail)
  → Fetch → Clean → Chunk → Index(FTS [+ Vector])
  → 附件 Parse jobs（PDF/Office/OCR 白名单）
  → Hybrid Retrieve → Rerank（可选）→ LLM
```

| 模块 | 设计要点 |
| --- | --- |
| 存储 | 理想形态是 Postgres+pgvector；在 Anna 约束下可退化为 **SQLite FTS + 本地轻量向量文件** 或 **远期平台托管检索** |
| 向量 | 明确标为**二期可选项**；126 失败主因不是语义召回不足 |
| 附件 | 同步只写文件名；解析异步，失败有错误码，禁止问答线程内重型 OCR |
| Worker | 无常驻进程时仍用 jobs + 短预算领取（同 C） |

**优点：** 长期检索质量与附件场景（发票 PDF）上限最高；与建议文「理想架构」最接近。  
**风险：** 工程与资源成本最高；向量对当前 KPI 可能 ROI 低；部署模型冲突最大。  
**适合验证的问题：**「附件/语义类测例占比与收益是否撑得起向量与重 Pipeline？」先用 C 的 FTS-only 对照。

---

### 方案 E — 确定性 Query Plan + 单次 Evidence Bundle（编排层，可叠 A/B/C）

**一句话：** 不先争论存在哪，先固定「找」与「说」的接口：Plan → 取证一次 → 生成一次。

**参考：** 建议文第六/七节 Query Router / LLM 职责拆分；Onyx 的检索-生成边界；可与 A/B/C 任意底层组合。

```text
Question
  → Planner（规则 + 可选一次小模型）
       intent / time_range / entities / completeness_required / allow_gmail_live
  → Retriever 适配器（实现可换）
       backend = live_gmail | allmail_cache | sqlite_fts | hybrid
  → Evidence Bundle（统一 schema）
       items[], coverage, truncated, risk_flags, attachments_meta
  → Answerer
       事实模板 | 一次 LLM；禁止再调检索
```

| 模块 | 设计要点 |
| --- | --- |
| Evidence Schema | 全方案统一，便于 A/B/C/D 换底层做 A/B 测 |
| 完备性字段 | `coverage_*` / `result_complete` 强制；修 F1/F8 |
| 多轮 | `conversation_state` 引用上一轮 evidence_id / draft_id |
| Rules | 在 Bundle 后、Answer 前跑 F7 规则 |

**优点：** 降低「绑死某一存储」的决策风险；评测可对同一 G30 换 backend。  
**风险：** 单独不解决存储与覆盖；若只做 E 而 Retriever 仍是烂的，收益有限。  
**定位：** **推荐作为公共骨架**，与 A/B/C/D 正交；调研期优先落地 Schema + 适配器接口。

---

## 4. 横向对比（讨论用）

| 维度 | A 增强实时 | B 缓存优先 | C SQLite+FTS | D Pipeline+Hybrid | E Plan/Evidence 骨架 |
| --- | --- | --- | --- | --- | --- |
| 对 F1 完备性 | 弱～中 | 中 | 强 | 强 | 取决于 backend |
| 对 F3 延迟 | 弱 | 中～强（命中时） | 强（热索引） | 中（索引构建重） | 减工具环有帮助 |
| 对 F4 附件名 | 中（改摘要） | 中 | 强 | 强（含解析） | 透传 |
| 对 F5 多轮 | 中（APS） | 中 | 强 | 强 | 强（状态契约） |
| 平台持久化依赖 | 低 | 中（APS） | **高（本地盘）** | 高 | 低 |
| 与现部署契合 | 最高 | 高 | 中（待验证） | 低 | 最高 |
| 实现周期（估） | 短 | 中短 | 中长 | 长 | 短（接口）+ 随 backend |
| 5s/90% 理论上限 | 可能不够 | 部分场景够 | 较有希望 | 长期最高 | 不单独保证 |
| 主要参考 | Inbox Zero | Mailspring cache | Mailspring DB + Onyx Index | Onyx + Tika + pgvector | 建议文 Query Plan |

### 4.1 建议的讨论立场（非结论）

1. **E 作为公共契约** — 无论选 A/B/C，先统一 Evidence / coverage / 边界事实，避免各做各的。
2. **A 与 B 适合做「两周内可证伪」的上限实验** — 回答「不建 SQLite 能不能摸到 60%+ / 更低延迟」。
3. **C 适合在平台磁盘 go 之后做主候选** — 若持久性 no-go，则 C/D 降级或改 APS 承载索引。
4. **D 不进入首轮主路径投票** — 除非 G30 证明 FTS 召回不足或附件正文刚需占比高。
5. **产品 Rules（Inbox Zero）应与存储方案解耦** — F7 不该等索引做完才做。

---

## 5. 成功率与延迟：统一度量（所有方案同一把尺）

### 5.1 成功

同时满足：

1. 返回完整最终文本或草稿 artifact（非仅进度）。
2. 含真实 thread/message 引用，或**明确**覆盖不足 / 不可用原因。
3. 不编造金额、日期、附件、联系人、同步机制、进度。
4. 无未授权 Gmail mutation。

审核器失败单独记账，不计入成功/失败。

### 5.2 延迟

- 主指标：完整答案端到端 p50/p95。
- 拆分：Planner / Retriever（含 Gmail 或本地）/ Answerer / 前端。
- 5s 为**产品目标**；验证期同时记录「若放宽到 10s，成功率多少」，避免单一 SLA 掩盖结构问题。

### 5.3 黄金集

- **G30**：P0 优先，覆盖 A/B/D/J/K/I 与失败桶 F1–F9。
- **全量 126**：方向候选通过 G30 后再跑。

---

## 6. 验证计划（先选型，再实现）

目标：用实验数据在候选中收敛，而不是先写死后端。

### 6.1 阶段 0 — 公共基建（所有方案前置，约 1–2 天）

| 事项 | 产出 |
| --- | --- |
| 修审核器 JSON 契约 | 噪声 &lt; 5% 或可剔除 |
| 原始回答 + 工具次数 + 安全耗时落盘 | 可审计 |
| 失败桶标签 + G30 | 统一评测入口 |
| 定义 **Evidence Bundle v0 schema**（方案 E） | JSON schema + 示例 |

### 6.2 阶段 0b — 并行探针（约 2–3 天，可并行）

| 探针 | 对应方案 | 关键问题 | 通过示意 |
| --- | --- | --- | --- |
| P-Live | A | 现路径 + 预算限制 + coverage 字段后，G30 如何变化？ | 延迟/成功率相对基线有无显著提升 |
| P-Cache | B | 仅用 All-mail 缓存能答对 G30 中几条？缺口类型？ | 命中率、错误「没有」比例 |
| P-Disk | C/D | 平台 SQLite/WAL 跨重启、重载、会话重建是否可读？ | go / no-go |
| P-SQL | C | 若本地有 30–90 天导入数据，确定性 8 查询是否正确且 &lt;1s？ | ≥6/8 正确 |
| P-Model | 全部 | Host 单次生成 p95 是否 ≤3s？ | 决定模板化比例与 SLA 是否可达成 |
| P-Attach | D 相关 | Tracking 中「必须附件正文」占比；仅文件名能否过 A03/B07？ | 是否需要 Tika 级解析 |

### 6.3 阶段 0c — 选型会（0.5 天）

输入：P-* 报告 + §4 对比表。  
输出（三选一或组合声明，**写回本文状态**）：

- 主路径 = ?  
- 副路径 / 回退 = ?  
- 明确不做 = ?  
- 下一迭代仅实现的垂直切片 = ?

实现切换以 [AI 生产结构优化方案](AI生产结构优化方案.md) 阶段门槛为准；未过阶段 0 不得切换生产默认检索主路径。

### 6.4 组合示例（供讨论，非预设）

| 组合名 | 内容 | 适用条件 |
| --- | --- | --- |
| **A+E** | 实时 Gmail + 统一 Evidence | 磁盘 no-go 或要最快上线体验修复 |
| **B+E+Rules** | 缓存优先 + 规则层 | 缓存命中率高且 APS 够用 |
| **C+E+Rules** | SQLite FTS 主路径 | 磁盘 go 且完备/延迟要求硬 |
| **C→D** | FTS 达标后再加附件解析/向量 | G30 证明召回/附件不足 |
| **A 过渡 → C** | 先修编排与话术，并行验证 C | 风险分散 |

---

## 7. 现状汇报

| 项 | 状态 |
| --- | --- |
| 生产主路径 | 仍为 Host Agent + 实时 Gmail 多轮取证 |
| 基线成绩 | ~36% 通过 / ~20s 中位 / 5s 内 4 条 |
| 架构方向 | **主路径已定为方案 C（SQLite+FTS5）**；A/B 作磁盘 no-go 备选；D 为二期；E 为公共契约 |
| 已做 | Tracking 基线、问题桶、开源映射、A–E 对照 |
| 落地文档 | [AI 生产结构优化方案](AI生产结构优化方案.md) |

---

## 8. 明确不做（调研期内）

- 未经验证宣布「已采用 SQLite / 已采用向量 / 已放弃实时 Gmail」。
- 为理想架构引入未获平台支持的 Postgres/Redis/Celery/S3 作为**默认**前提（可作为远期分支单独评估）。
- 恢复前端业务意图正则路由。
- 静默 Gmail 写操作。
- 用审核器噪声粉饰成功率。

---

## 9. 待讨论问题清单（会议用）

1. 5 秒完整答案是**硬门槛**还是可先做到「正确优先、延迟分档」？
2. 平台是否承诺 Executa 本地目录跨会话持久？若不承诺，C/D 是否改为 APS 承载索引？
3. AI 是否允许与 Inbox 列表使用**不同覆盖范围**（例如 AI 90 天、列表 60 天）？
4. 完备性类需求（计数/最早/全部）是否 P0？若是，A 单独是否直接否决？
5. Rules / 安全提示是否同意与检索架构解耦、先行上线？
6. 附件：首期是否只保证**文件名与存在性**，正文解析放到 D？
7. Host Agent 是否接受收敛为「一次复合取证 + 一次生成」，削弱自由多工具环？
8. 黄金集 G30 由谁维护、是否与产品验收共用？

---

## 10. 修订记录

| 日期 | 说明 |
| --- | --- |
| 2026-07-22 | 初稿曾偏向单一 SQLite 路线 |
| 2026-07-22 | 多方案调研稿：A–E 候选与对照 |
| 2026-07-22 | 与实现方案拆分：本文保留选型对照；主路径 SQLite 见 `AI生产结构优化方案.md` |
