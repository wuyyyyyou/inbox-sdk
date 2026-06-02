# Ask 流程审查：Plan-and-Execute 范式

## 架构概览

```
用户自然语言请求
    │
    ▼
┌─ Plan 阶段 ──────────────────────────────────────────┐
│  Planner LLM (generate_custom_plan)                   │
│  输出: gmail_queries[], read_depth, task_prompt       │
│  容错: JSON repair → 重试 → DashScope → 规则 fallback │
└──────────────────────────────────────────────────────┘
    │
    ▼
┌─ Execute 阶段 ────────────────────────────────────────┐
│  1. Gmail 搜索 (run_mail_scan)                        │
│  2. 按 read_depth 读取邮件正文/线程                    │
│  3. 一次 LLM 调用分析全部邮件 → JSON 答案              │
│  容错: 降载重试 (compact → short → headers)           │
└──────────────────────────────────────────────────────┘
    │
    ▼
  结构化答案 (title + summary + sections[])
```

## 正确实现的部分

1. **Plan-Execute 分离清晰**。Planner 和 Executor 职责明确，不耦合。Planner 出 `gmail_queries` + `read_depth` + `task_prompt`，Executor 严格按计划执行。

2. **JSON 鲁棒性做得不错**。`call_llm_json` 有三层保障：正则修复常见错误（漏逗号、尾部逗号）→ 失败后发 repair sampling 请求 → 再失败回退 DashScope。`call_llm_json_safe` 外面还有一层 try-catch + fallback。

3. **Plan 可复用**（`re_run_custom_scan`）。用户可以对同一个 plan 反复执行，省掉规划 LLM 调用的成本。

4. **降载重试机制**。大响应超出 token 限制时，自动降级：`compact`(截断正文) → `short`(更短) → `headers`(仅主题发件人)。这在 `anna-llm` provider 路径下会触发，DashScope 路径走全量。

5. **与 Brief 管线完全解耦**。不走 phase1/judgment/guards/cards，不产生持久化卡片，符合 Ask 语义。

## 偷工减料 / 设计缺陷

### 1. "一次 LLM 调用"是最大的瓶颈

50 封邮件全部塞进一个 prompt，LLM 只能泛读。没有 map-reduce、没有分块、没有迭代。用户问"帮我找所有需要回复的邮件"，Executor LLM 要同时完成搜索结果的筛选、分类、优先级判断和草稿撰写。这不是 plan-and-execute，是 plan-and-**one-shot**。

### 2. Planner 是"盲人"

Planner 只看用户请求文本，完全不知道邮箱里有什么。生成 `gmail_queries` 全靠猜（"from:christopher OR to:christopher"）。真正的 plan-and-execute 应该先做一次轻量级 scout（只拉 subject/sender/date），再出精准计划。

### 3. `task_prompt` 是纯自然语言，无结构化约束

Planner 输出一段 free-form 英文指令给 Executor LLM。没有 schema 校验 `task_prompt` 是否完整、是否自相矛盾、是否覆盖了用户请求的所有维度。如果 Planner 输出了一个含糊的 `task_prompt`（比如"Review emails and summarize"），Executor 做出来的结果质量就没保证。

### 4. 无闭环反馈

Executor 执行完就结束了。没有质量检查步骤——Executor 是否真正回答了用户的问题？答案是否有遗漏？如果用户问"有几封未读"，Executor 输出的是"找到了3封"但实际有5封，系统完全不知道。

### 5. 规则 fallback 质量很低

`_fallback_plan_for_request()` 用简单的 stopword 过滤提取关键词，组装成 `"Brief OR mailbox OR surface OR only OR emails"` 这种无意义的 Gmail 查询。之前日志里出现的第三个查询就是这个 fallback 产生的——把用户请求里的每个单词用 OR 拼接，这在 Gmail 搜索中毫无意义。

### 6. 降载策略有信息损失

降载到 `headers` 级别时，LLM 只能看到 subject/from/date/snippet。对于"需要回复吗"这类判断，没有邮件正文是不可能准确回答的。本质上等于放弃了这次 Ask。

### 7. 没有成本/延迟的可预测性

用户不知道一次 Ask 要花多少钱（几次 LLM 调用）、要等多久（取决于邮件量）。scan_budget 在 planner 里设了但没在前端展示，用户无法控制。

### 8. Sampled vs DashScope 路径不对称

`anna-llm` 路径走降载重试（最多3次 LLM 调用），`dashscope` 路径一次全量（无降载）。同一个请求在不同 provider 下的行为根本不同。

## 效果评估

| 维度 | 评价 |
|------|------|
| 简单统计类（"多少未读""谁发的最多"） | 尚可，header_only 就够了 |
| 中等判断类（"哪些需要回复""有没有安全问题"） | 勉强，依赖 Executor LLM 的理解能力，但单次调用信息过载时容易漏 |
| 复杂分析类（"总结最近合作进展""帮我准备周报"） | 很差，单次 LLM 没有足够上下文窗口做深度分析 |
| 可靠性 | 低，Planner 盲猜 + Executor 一次过 + 无验证 = 答案不可信 |

## 优化思路（按投入产出比排序）

### 第一优先：Scout-then-Plan

在 Plan 阶段前加一步轻量级 scout：用 `newer_than:7d` 拉最近邮件的 subject/from/date/count，作为 Planner 的附加上下文。这样 Planner 能感知到邮箱里有什么，生成的 `gmail_queries` 更精准，`task_prompt` 更有针对性。成本很低（只多一次轻量 Gmail 搜索，不调 LLM）。

### 第二优先：修复 fallback

当前 fallback 太粗糙了。至少做一下意图分类（统计类 vs 查找类 vs 分析类），不同意图用不同的默认查询策略，而不是关键词 OR 拼接。

### 第三优先：添加验证步骤

Executor 输出答案后，追加一次轻量级 self-check："基于提供的邮件，这个答案是否完整？是否有遗漏？"。用最小的 token 预算做质量把关。

### 第四优先：Map-Reduce 执行

当邮件量超过阈值（比如 20 封），自动分块：每块 10 封，先独立分析提取要点，再汇总到一次 synthesis 调用。这比降载截断靠谱得多。

### 第五优先：Planner 输出结构化校验

给 `task_prompt` 加一个最小检查——是否包含"what to look for""how to organize""what to output"三个要素？缺失时自动补全，而不是靠 LLM 自觉。

---

总结：当前的 Ask 是一个**诚实的 MVP**，Plan-Execute 的骨架搭对了，但 Execute 阶段的 "one-shot LLM" 限制了能力天花板。最划算的改进是 **Scout-then-Plan**，让 Planner 不再盲猜。
