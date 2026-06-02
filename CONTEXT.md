# Anna Inbox

Anna Inbox 是 Anna App 中用于处理 Gmail 收件箱注意力管理的产品语境。它把邮箱内容整理成可行动的简报、可处理的卡片，以及按自然语言发起的自定义查询。

## Language

**Mailbox**:
一个被 Anna Inbox 读取和处理的 Gmail 邮箱账户。一个 Brief、Ask 或 Scan Plan 都针对一个 Mailbox。
_Avoid_: account, source account

**Brief**:
一个稳定、可预测的邮箱扫描工作流，目的是产出用户当天需要关注的 Attention Cards。Brief 不是开放式问答。
_Avoid_: daily scan, inbox summary, workflow page

**Ask**:
一个由用户自然语言请求驱动的自定义邮箱查询，用于回答开放问题或执行一次性扫描。Ask 不等同于 Brief，也不产出 Brief 的卡片队列作为主要目标。
_Avoid_: custom brief, search page

**Custom Scan**:
一次 Ask 请求的执行实例。它围绕用户的自然语言请求读取相关邮件，并返回结构化结果。
_Avoid_: ask run, custom workflow

**Custom Scan Plan**:
Ask 为某个自然语言请求形成的可复用查询计划。一个 Custom Scan Plan 可以被再次执行，但它不是 Brief 的 Scan Plan。
_Avoid_: saved search, ask template

**Scan Plan**:
Mailbox 的 Brief 扫描偏好，包括扫描窗口、频率和包含范围。Scan Plan 描述后续 Brief 如何扫描，而不是 Ask 如何回答问题。
_Avoid_: schedule settings, source settings

**Attention Card**:
Brief 产出的一个待处理邮件事项。Attention Card 表示用户需要回复、查看、暂缓或标记已处理的一个邮件线程或事项。
_Avoid_: email card, task card

**Cleanup Bundle**:
Brief 将低价值邮件聚合成的一张可折叠卡片。Cleanup Bundle 用于从行动队列中隔离噪音，而不是逐封生成独立 Attention Card。
_Avoid_: low priority list, ignored emails

**Handle**:
用户处理单张 Attention Card 的详情体验，包括查看线程上下文、生成或修改草稿、发送回复、标记无需操作或手动完成。
_Avoid_: detail page, reply drawer

**Run**:
一次后台执行记录，可以属于 Brief、Ask、草稿生成或线程总结等长耗时操作。Run 是执行实例，不是用户可复用的计划。
_Avoid_: job, task

## Example Dialogue

开发者：这个 Mailbox 的 Brief 今天没有 Attention Card，是不是说明 Ask 也没结果？

领域专家：不是。Brief 只产出稳定的注意力队列；Ask 是用户临时提问，可以针对同一个 Mailbox 做不同的 Custom Scan。

开发者：那用户保存的查询应该叫 Scan Plan 吗？

领域专家：如果它来自 Ask，就叫 Custom Scan Plan。Scan Plan 特指 Brief 的扫描偏好。

开发者：Cleanup Bundle 里的邮件也算 Attention Card 吗？

领域专家：它是 Brief 产出的卡片，但语义上是噪音聚合，不应该当成需要逐项处理的普通 Attention Card。
