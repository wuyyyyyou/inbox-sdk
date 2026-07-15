# Anna Inbox 文档索引

本目录只保留当前实现文档和明确标注的未来路线图。代码和 manifest 是最终事实来源。

当前基线：**App `2.0.23` / Tool `2.1.4`**（版本解耦）。

## 当前实现

- [2.1.4 架构与发布基线](2.1.4架构与发布基线.md)
- [多邮箱接入](多邮箱接入方案.md)
- [邮件详情抽屉](邮件详情抽屉改造方案.md)
- [附件下载与外部链接](附件下载与外部链接打开方案.md)
- [AI 侧边栏意图路由](AI侧边栏意图路由优化方案.md)（兼容旁路；默认已迁 `start_ai_turn`）
- [AI 对话本地 Router 与白名单工具](AI对话本地Router与白名单工具设计.md)（阶段 A+B 已落地）
- [Ask 链路](Ask链路重构方案.md)
- [Brief 管线](Brief管线全链路设计.md)
- [Brief 短 Invoke 状态机](Brief可续跑短Invoke状态机改造方案.md)
- [Anna Sampling 与 Brief](Anna-Sampling-Brief全链路分析.md)
- [联系人记忆](联系人记忆架构设计.md)
- [PyInstaller 打包](PyInstaller二进制打包指南.md)

## 路线图

`roadmap/` 中的内容没有发布承诺，也不能当作现有功能：

- [Brief 性能](roadmap/Brief扫描性能问题与优化方案.md)
- [Draft 质量](roadmap/Draft质量优化方案.md)
- [Outlook 接入](roadmap/Outlook邮箱接入方案.md)

历史实施计划、旧 PRD 校验报告和旧单文件前端文档已在 2.0.1 清理；仍有效的约束已合并到上述文档。
