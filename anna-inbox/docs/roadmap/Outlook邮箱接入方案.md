# Roadmap：Outlook 接入

状态：未实现。2.0.1 仅支持 Gmail。

接入 Outlook 前需要先定义 provider-neutral adapter，覆盖账户发现、列表、线程读取、正文、附件、标签/文件夹映射、发送和删除等能力；再实现 Microsoft Graph OAuth 与 API adapter。

必须重新审查 Gmail 查询语义、thread 模型、draft、联系人头像、附件访问和 mutation guard，不能只替换搜索 API。Mailbox registry 已有 provider 字段，但这不代表 Outlook 已可用。
