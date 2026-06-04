# Outlook 邮箱接入方案

## 目标

在现有 Gmail 邮件处理管线的基础上，支持接入 Outlook / Hotmail / Microsoft 365 邮箱，实现跨提供商的邮件扫描、卡片生成和操作。

---

## 现状分析：Gmail 耦合点

### 好消息

管线核心层（phase1 → judgment → card_service → storage_ops）完全操作提供商无关的抽象类型 `MessageLite` / `MessageDetail` / `ThreadContext`。接入 Outlook 的这些模块**不需要任何改动**。

### Gmail 耦合清单

| 文件 | 耦合点 |
|---|---|
| `mail_adapter.py` | Gmail API 调用、Google OAuth token、Gmail 格式解析 — **全部硬编码** |
| `planner.py` | LLM prompt 中教 Gmail 搜索语法；`_fallback_plan_for_request()` 写死 Gmail 语法 |
| `strategies.py` | `ScanPolicy.default_queries` 使用 Gmail 搜索语法 |
| `scan.py` | `run_mail_scan()` 调用 `mail_adapter.live_search_and_cache()` |
| `context.py` | 调用 `mail_adapter.get_message_detail()` / `get_thread_context()` |
| `handle_service.py` | 调用 `mail_adapter.send_reply()` / `get_thread_context()` |
| `pipeline.py` | 调用 `mail_adapter.gmail_request()` / `normalize_mailbox()` / `get_message_detail()` |
| `main.py` | 直接 import `mail_adapter` 函数（`get_access_token`、`send_reply`、`batch_mark_read`、`trash_email`、`get_authorized_email`） |
| `manifest.json` | 凭据只声明了 `GMAIL_ACCESS_TOKEN` / `GOOGLE_ACCESS_TOKEN` |

---

## 设计方案

### 核心思路：适配器模式

```
                    ┌──────────────────────────────┐
                    │   pipeline / scan / context   │  ← 不改
                    │   handle_service / phase1     │
                    │   judgment / card_service     │
                    └──────────────┬───────────────┘
                                   │ 依赖
                    ┌──────────────▼───────────────┐
                    │       mail_adapter.py         │  ← facade: get_adapter(mailbox)
                    │  (根据 provider 分发)         │
                    └──────┬────────────┬──────────┘
                           │            │
              ┌────────────▼──┐  ┌──────▼────────────┐
              │ GmailAdapter  │  │ OutlookAdapter     │
              │ (现逻辑迁移)  │  │ (全新实现)         │
              └───────────────┘  └───────────────────┘
```

`mail_adapter.py` 保留文件名和 public API（减少下游 import 改动），内部委托给适配器实例。

---

### 一、适配器接口定义

新文件 `mail_agent/base_adapter.py`：

```python
from abc import ABC, abstractmethod
from typing import Any

class BaseMailAdapter(ABC):
    """邮件提供商的抽象适配器。Gmail 和 Outlook 各自实现。"""

    # ── 元信息 ──
    @property
    @abstractmethod
    def provider(self) -> str:
        """返回 "gmail" 或 "outlook" """
        ...

    # ── 认证 / 授权 ──
    @abstractmethod
    def get_access_token(self, mailbox: str) -> str: ...

    @abstractmethod
    def check_auth(self, mailbox: str) -> dict[str, Any]:
        """返回 {"authorized": bool, "source": str}"""
        ...

    @abstractmethod
    def get_authorized_email(self) -> str: ...

    # ── 搜索 ──
    @abstractmethod
    def search(self, mailbox: str, query: str, max_results: int = 100) -> list[str]:
        """搜索邮件，返回 message_id 列表"""
        ...

    @abstractmethod
    def fetch_message(self, mailbox: str, message_id: str) -> dict[str, Any] | None:
        """获取单封邮件全文，返回归一化 dict"""
        ...

    @abstractmethod
    def fetch_message_parallel(self, mailbox: str, message_ids: list[str]) -> dict[str, dict[str, Any]]:
        """并发获取多封邮件，返回 {message_id: normalized_dict}"""
        ...

    # ── 归一化 ──
    @abstractmethod
    def normalize_message(self, mailbox: str, raw: dict[str, Any]) -> dict[str, Any]:
        """将提供商原始响应转为统一 dict 格式"""
        ...

    @abstractmethod
    def to_message_lite(self, msg: dict[str, Any]) -> "MessageLite": ...

    @abstractmethod
    def to_message_detail(self, msg: dict[str, Any]) -> "MessageDetail": ...

    # ── 写操作 ──
    @abstractmethod
    def send_reply(
        self, mailbox: str, thread_id: str, to_addr: str, body: str,
        *, reply_mode: str = "reply_to_sender", cc_addr: str = "",
    ) -> dict[str, Any]: ...

    @abstractmethod
    def mark_read(self, mailbox: str, message_ids: list[str]) -> dict[str, Any]: ...

    @abstractmethod
    def trash(self, mailbox: str, message_id: str) -> dict[str, Any]: ...
```

### 统一消息格式（normalize_message 输出）

无论来自 Gmail 还是 Outlook，`normalize_message()` 输出必须为统一 dict：

```python
{
    "id": str,              # 消息唯一 ID（provider 原生 id）
    "thread_id": str,       # 会话 ID
    "mailbox": str,         # 所属邮箱
    "provider": str,        # "gmail" | "outlook"
    # 时间
    "internal_date": str,   # 统一存 epoch 毫秒字符串
    "date": str,            # RFC 2822 date 头
    # 收发人
    "from": str, "to": str, "cc": str, "bcc": str,
    # 内容
    "subject": str,
    "message_id": str,      # RFC Message-ID header
    "in_reply_to": str,
    "references": str,
    "snippet": str,         # 正文预览 ~100 字符
    "body_text": str,       # 解码后的纯文本正文
    # 标记
    "label_ids": list[str], # Gmail: 原生 labels；Outlook: 映射为 ["INBOX"/"SENT"/"UNREAD"/"IMPORTANT"]
    "unread": bool,
    "starred": bool,        # Outlook: categories 中含 "Starred" → True
    "important": bool,      # Outlook: importance == "high" → True
    # 附件
    "has_attachment": bool,
    "attachments": list[dict],
    # 元数据
    "raw_headers": dict[str, str],
    "fetched_at": str,
    # 原始 payload（仅序列化用，不暴露给上层）
    "_raw": dict,
}
```

**关键约定**：上层管线（phase1 / judgment）只使用统一格式的字段，不访问 `_raw`。这样 Gmail 和 Outlook 对管线完全透明。

---

### 二、GmailAdapter（重构搬家）

**新文件 `mail_agent/gmail_adapter.py`**：

把 `mail_adapter.py` 中所有 Gmail 特有逻辑搬过来，几乎纯搬家，只做：
1. 去掉 `gmail_request()` 的独立函数，改为 `self._api_request()`
2. `get_access_token()` / `check_auth()` 行为不变
3. `search()` → 包装现有 `search_gmail()`
4. `fetch_message()` → 包装现有 `fetch_and_cache_message()`
5. `normalize_message()` → 就是现有 `_normalize_message()`，增加 `"provider": "gmail"`
6. `send_reply()` / `mark_read()` / `trash()` → 不变

```python
class GmailAdapter(BaseMailAdapter):
    provider = "gmail"

    def __init__(self):
        self._api_base = "https://gmail.googleapis.com/gmail/v1"
        self._token_uri = "https://oauth2.googleapis.com/token"

    # ... 搬迁所有现有 Gmail 逻辑 ...
```

### 三、OutlookAdapter（全新实现）

**新文件 `mail_agent/outlook_adapter.py`**：

#### 3.1 API 端点

```python
class OutlookAdapter(BaseMailAdapter):
    provider = "outlook"

    def __init__(self, tenant: str = "common"):
        self._api_base = "https://graph.microsoft.com/v1.0"
        self._token_uri = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
```

`tenant` 说明：
- `"common"` — 个人账户（outlook.com、hotmail.com）和任意 Microsoft 365 企业
- `"organizations"` — 仅企业账户
- `"consumers"` — 仅个人账户
- `<tenant-id>` — 指定企业租户

#### 3.2 认证

```python
def get_access_token(self, mailbox: str) -> str:
    # 平台注入的 token（优先级最高）
    token = (
        os.environ.get("OUTLOOK_ACCESS_TOKEN")
        or os.environ.get("MICROSOFT_ACCESS_TOKEN")
    )
    if token and token.strip():
        return token.strip()

    # 本地 token 文件
    return self._load_local_token(mailbox)

def _load_local_token(self, mailbox: str) -> str:
    # 从 scripts/outlook_token/.secrets/outlook_tokens/ 加载
    # 逻辑类比 Gmail 的 _load_token_record + _refresh_access_token
    ...
```

Token 刷新差异：
- Gmail：`POST oauth2.googleapis.com/token`，`grant_type=refresh_token`
- Outlook：`POST login.microsoftonline.com/{tenant}/oauth2/v2.0/token`，`grant_type=refresh_token`，额外参数 `scope=Mail.Read Mail.ReadWrite Mail.Send offline_access`

格式兼容，核心差异只是端点地址和 scope。

#### 3.3 搜索（search → list message IDs）

```python
def search(self, mailbox: str, query: str, max_results: int = 100) -> list[str]:
    """
    将现有的 Gmail 查询字符串翻译为 Graph API 调用。
    策略：尽量翻译已知的 Gmail 语法 → $filter / $search；
    不可翻译的纯文本关键词直接用 $search。
    """
    filter_clause = self._translate_gmail_query_to_graph(query)
    url = f"{self._api_base}/me/mailFolders/inbox/messages"
    params = {
        "$top": min(max_results, 500),
        "$select": "id,conversationId,receivedDateTime",
        "$orderby": "receivedDateTime desc",
    }
    if filter_clause:
        params["$filter"] = filter_clause

    response = self._api_get(url, params, mailbox)
    message_ids = []
    for item in response.get("value", []):
        if item.get("id"):
            message_ids.append(item["id"])
    return message_ids
```

**Gmail → Graph 查询翻译表**（`_translate_gmail_query_to_graph`）：

| Gmail 语法 | Graph API 等价 | 说明 |
|---|---|---|
| `is:unread` | `isRead eq false` | |
| `is:starred` | `categories/any(c:c eq 'Starred')` | 需用户配合标记 |
| `has:attachment` | `hasAttachments eq true` | |
| `category:primary` | `inferenceClassification eq 'focused'` | 近似映射 |
| `in:inbox` | （默认查询 inbox folder） | |
| `in:sent` | 改查 `/me/mailFolders/sentitems/messages` | |
| `newer_than:Nd` | `receivedDateTime ge {N天前的ISO时间}` | 动态计算 |
| `from:xxx` | `$search="from:xxx"` | KQL 搜索 |
| `to:xxx` | `$search="to:xxx"` | |
| `subject:xxx` | `$search="subject:xxx"` | |
| 通用关键词 | `$search="keywords"` | |
| `-in:sent` `-in:draft` | `$filter` 默认只查 inbox → 天然排除 | |

**翻译不到的**：不报错，降级为 `$search` 模糊匹配。日志中记录降级信息。

#### 3.4 获取单封邮件（fetch_message）

```python
def fetch_message(self, mailbox: str, message_id: str) -> dict | None:
    url = f"{self._api_base}/me/messages/{message_id}"
    params = {
        "$expand": "attachments",  # 附件元数据
    }
    raw = self._api_get(url, params, mailbox)
    return self.normalize_message(mailbox, raw)
```

#### 3.5 归一化（normalize_message）

核心字段映射：

```python
def normalize_message(self, mailbox: str, raw: dict) -> dict:
    body = raw.get("body", {})
    sender = raw.get("sender", {}).get("emailAddress", {})
    from_addr = f'{sender.get("name","")} <{sender.get("address","")}>'
    to_recipients = raw.get("toRecipients", [])
    to_addr = "; ".join(
        f'{r.get("emailAddress",{}).get("name","")} <{r.get("emailAddress",{}).get("address","")}>'
        for r in to_recipients
    )

    # 将 receivedDateTime (ISO 8601) 转为 epoch 毫秒 → 与 Gmail internalDate 统一
    received_iso = raw.get("receivedDateTime", "")
    internal_date = self._iso_to_epoch_ms(received_iso)

    # isRead=false → 未读 → 映射到 Gmail 的 UNREAD label
    is_unread = not raw.get("isRead", True)
    is_important = raw.get("importance", "normal") == "high"

    label_ids = []
    if is_unread:
        label_ids.append("UNREAD")
    if is_important:
        label_ids.append("IMPORTANT")

    return {
        "id": raw.get("id", ""),
        "thread_id": raw.get("conversationId", ""),
        "mailbox": mailbox,
        "provider": "outlook",
        "internal_date": internal_date,
        "date": raw.get("sentDateTime", ""),
        "from": from_addr,
        "to": to_addr,
        "cc": self._format_recipients(raw.get("ccRecipients", [])),
        "bcc": self._format_recipients(raw.get("bccRecipients", [])),
        "subject": raw.get("subject", ""),
        "message_id": raw.get("internetMessageId", ""),
        "snippet": raw.get("bodyPreview", ""),
        "body_text": body.get("content", ""),
        "label_ids": label_ids,
        "unread": is_unread,
        "starred": any(
            c == "Starred" for c in (raw.get("categories") or [])
        ),
        "important": is_important,
        "has_attachment": raw.get("hasAttachments", False),
        "attachments": [
            {
                "filename": a.get("name"),
                "mimeType": a.get("contentType"),
                "size": a.get("size"),
            }
            for a in (raw.get("attachments") or [])
        ],
        "raw_headers": self._extract_headers(raw),
        "fetched_at": beijing_now(),
    }
```

#### 3.6 写操作

**发送回复**：

```python
def send_reply(self, mailbox, thread_id, to_addr, body, **kwargs):
    # Graph API: POST /me/messages/{message_id}/reply
    # 先找到 thread 中最新一封的 id
    # 调用 reply endpoint
    url = f"{self._api_base}/me/messages/{latest_message_id}/reply"
    data = {"comment": body}
    return self._api_post(url, data, mailbox)
```

**标记已读**：

```python
def mark_read(self, mailbox, message_ids):
    # PATCH /me/messages/{id} 不支持批量
    # 逐个调用，或使用 $batch endpoint
    for mid in message_ids:
        self._api_patch(
            f"{self._api_base}/me/messages/{mid}",
            {"isRead": True},
            mailbox,
        )
    return {"ok": True, "count": len(message_ids)}
```

注意：Graph API 单条 PATCH 效率低。可改用 `$batch` 请求将多个 PATCH 合并为一次 HTTP 请求。

```python
def mark_read_batch(self, mailbox, message_ids):
    # POST /$batch with individual requests
    requests = [
        {
            "id": str(i),
            "method": "PATCH",
            "url": f"/me/messages/{mid}",
            "body": {"isRead": True},
            "headers": {"Content-Type": "application/json"},
        }
        for i, mid in enumerate(message_ids)
    ]
    ...
```

**移至回收站/已删除**：

```python
def trash(self, mailbox, message_id):
    # POST /me/messages/{id}/move
    url = f"{self._api_base}/me/messages/{message_id}/move"
    data = {"destinationId": "deleteditems"}
    return self._api_post(url, data, mailbox)
```

#### 3.7 附件下载（可选，后续实现）

Graph API 附件通过 `@microsoft.graph.downloadUrl` 提供临时下载链接，与 Gmail 的 `attachmentId` + base64 解码模式不同。附件处理统一到后续迭代。

---

### 四、mail_adapter.py 改造

`mail_adapter.py` 保留文件名不变（下游 import 路径兼容），内部改为 facade：

```python
# mail_adapter.py 改造后

from .base_adapter import BaseMailAdapter
from .gmail_adapter import GmailAdapter
from .outlook_adapter import OutlookAdapter

_adapters: dict[str, BaseMailAdapter] = {}

def _get_adapter(mailbox: str, provider: str = "") -> BaseMailAdapter:
    """根据邮箱获取适配器。provider 明确时直接使用，否则从 registry 查询。"""
    sanitized = sanitize_mailbox_id(mailbox)
    if sanitized in _adapters:
        return _adapters[sanitized]

    if not provider:
        provider = _get_provider_from_registry(mailbox)

    if provider == "outlook":
        adapter = OutlookAdapter()
    else:
        adapter = GmailAdapter()

    _adapters[sanitized] = adapter
    return adapter

def _get_provider_from_registry(mailbox: str) -> str:
    """从 mailbox registry 读取 provider 信息。"""
    # 从 storage 查询 mailbox registry
    ...

# ── 现有 public API 保持签名兼容，内部委托 ──

def get_access_token(mailbox: str) -> str:
    return _get_adapter(mailbox).get_access_token(mailbox)

def search_gmail(mailbox: str, query: str, max_results: int = 100) -> list[str]:
    return _get_adapter(mailbox).search(mailbox, query, max_results)

def fetch_and_cache_message(mailbox: str, message_id: str) -> dict | None:
    adapter = _get_adapter(mailbox)
    normalized = adapter.fetch_message(mailbox, message_id)
    if normalized:
        write_message(mailbox, normalized)
    return normalized

def live_search_and_cache(mailbox, query, max_results=100, **kwargs):
    adapter = _get_adapter(mailbox)
    # 复用现有缓存逻辑 + adapter.search()
    ...

# ... 其余 public 函数同理 ...
```

**关键原则**：
- 缓存层（`write_index`、`write_message`、`read_cache`、`read_message`、`list_messages`）保持**共用**（与 provider 无关，操作本地 JSON）
- token 管理（`get_access_token`、`check_auth`、`get_authorized_email`）**委托给 adapter**
- 邮件获取/搜索（`search_gmail` → `search`、`fetch_and_cache_message`、`live_search_and_cache`）**委托给 adapter**
- 写操作（`send_reply`、`batch_mark_read`、`trash_email`）**委托给 adapter**
- `_normalize_message` → 由 adapter 各自实现，保留旧函数做兼容包装
- `_to_message_lite` / `_to_message_detail` → 基于统一 dict 格式，**可共用**

---

### 五、Planner 搜索语法适配

### 重要修正：不要翻译 Gmail query 字符串

Outlook 接入时，不建议让 `OutlookAdapter` 去“翻译 Gmail query 字符串”为 Microsoft Graph query。原因是 Gmail 搜索语法和 Graph `$filter` / `$search` / folder API 不是一一对应关系，复杂查询很容易翻译错误，例如：

- Gmail 的 `newer_than:2d category:primary -in:sent` 和 Outlook 的 focused inbox / folder / receivedDateTime 语义并不完全等价。
- Graph 对 `$filter` + `$orderby` 有额外限制，简单拼接容易触发 `InefficientFilter`。
- `$search`、`$filter`、folder path、`$select`、`$top` 混在一个字符串里后，adapter 很难安全解析和校验。

更稳的做法是把内部查询结构从 `gmail_queries` 升级为 provider-neutral 的 `mail_queries`，由 Planner 直接输出结构化查询对象，再由各 provider adapter 负责转换为具体 API 请求。

建议结构：

```typescript
type MailQuery = {
  provider: "gmail" | "outlook";
  folder?: "inbox" | "sent" | "drafts" | "all";
  query?: string;        // Gmail 搜索字符串，仅 Gmail 使用
  filter?: string;       // Microsoft Graph $filter，仅 Outlook 使用
  search?: string;       // Microsoft Graph $search，仅 Outlook 使用
  orderby?: string;      // Microsoft Graph $orderby，仅 Outlook 使用
  max_results: number;
  purpose?: string;
  priority?: "high" | "medium" | "low";
};
```

Gmail 示例：

```json
{
  "provider": "gmail",
  "folder": "inbox",
  "query": "category:primary -in:sent newer_than:2d",
  "max_results": 50,
  "purpose": "Recent primary inbox messages"
}
```

Outlook 示例：

```json
{
  "provider": "outlook",
  "folder": "inbox",
  "filter": "isRead eq false and receivedDateTime ge 2026-06-01T00:00:00Z",
  "orderby": "receivedDateTime desc",
  "max_results": 50,
  "purpose": "Unread recent inbox messages"
}
```

落地时可以先兼容旧字段：

- `gmail_queries` 保留为旧 Gmail pipeline 的输入。
- 新实现优先读取 `mail_queries`。
- 如果只有 `gmail_queries`，在 Gmail provider 下转换为 `mail_queries[{provider:"gmail", query: ...}]`。
- Outlook provider 不消费 `gmail_queries`，必须由 Planner 生成 `mail_queries`，或使用 Outlook 默认查询模板。

#### 5.1 方案选择

两种方案：

| 方案 | 描述 | 优点 | 缺点 |
|---|---|---|---|
| A: 双 prompt | Planner 按 provider 加载不同的 system prompt | 直接，LLM 生成正确语法的查询 | 两套 prompt 需分别维护调优 |
| B: 语义意图 | Planner 输出抽象意图 → adapter 翻译为具体查询 | Planner 不变 | 翻译层易遗漏边界 case，复杂查询翻译准确率低 |

**选择方案 A**：Planner 是 LLM，它理解自然语言的最佳方式就是直接教它目标语法。维护两套 prompt 的成本低于维护翻译层。

#### 5.2 实现

```python
# planner.py 改造

_OUTLOOK_SYSTEM_PROMPT = """You are Anna's scan planner...
## Output format
{
  "title": "...",
  "description": "...",
  "search_queries": [
    {
      "query": "Microsoft Graph $filter or $search expression",
      "purpose": "Why this query",
      "max_results": 50,
      "priority": "high"
    }
  ],
  ...
}

## Microsoft Graph search reference
- $search="keyword" — full-text search
- $filter=isRead eq false — unread only
- $filter=hasAttachments eq true — has attachments
- $filter=receivedDateTime ge 2026-06-01T00:00:00Z — date range
- $filter=importance eq 'high' — high importance
- $search="from:someone" OR $search="subject:keyword" — field-specific
- $filter=inferenceClassification eq 'focused' — Focused inbox
- $top=50 —limit results
- $select=id,conversationId,subject,from,receivedDateTime,isRead,bodyPreview — select fields
- $orderby=receivedDateTime desc — sort direction
...
"""

def generate_custom_plan(user_request, mailbox="", provider="gmail", sampling=None):
    if provider == "outlook":
        system_prompt = _OUTLOOK_SYSTEM_PROMPT
        field_name = "search_queries"
    else:
        system_prompt = _PLANNER_SYSTEM_PROMPT  # 现有 Gmail prompt
        field_name = "gmail_queries"

    # 其余逻辑不变，只是 prompt + key 不同
```

#### 5.3 strategies.py 适配

```python
# strategies.py
# ScanPolicy.default_queries 按 provider 分版本

GMAIL_DEFAULT_QUERIES = [
    {"query": "category:primary -in:sent newer_than:2d", ...},
    {"query": "is:unread in:inbox", ...},
]

OUTLOOK_DEFAULT_QUERIES = [
    {"query": "$filter=isRead eq false and receivedDateTime ge {2d_ago}", ...},
    {"query": "$search='urgent'&$filter=isRead eq false", ...},
]

def get_queries_for_provider(provider: str) -> list[dict]:
    if provider == "outlook":
        return OUTLOOK_DEFAULT_QUERIES
    return GMAIL_DEFAULT_QUERIES
```

---

### 六、Mailbox Registry 扩展

在之前多邮箱方案的基础上，`mailbox-registry` 增加 `provider` 字段：

```json
{
  "mailboxes": [
    {"email": "alice@gmail.com", "provider": "gmail", "added_at": "..."},
    {"email": "bob@outlook.com", "provider": "outlook", "added_at": "..."},
    {"email": "c@company.com", "provider": "outlook", "tenant": "xxx", "added_at": "..."}
  ],
  "updated_at": "2026-06-03T10:00:00+08:00"
}
```

更新 `storage_ops.py` 的 registry 读写函数适配新结构。

---

### 七、Auth & 本地 OAuth

#### 7.1 Manifest 凭据扩展

```json
// inbox-tool/manifest.json
"credentials": [
  {
    "name": "GMAIL_ACCESS_TOKEN",
    "display_name": "Gmail Access Token",
    ...
  },
  {
    "name": "GOOGLE_ACCESS_TOKEN",
    ...
  },
  // 新增
  {
    "name": "OUTLOOK_ACCESS_TOKEN",
    "display_name": "Outlook Access Token",
    "description": "Microsoft Graph OAuth access token for Outlook / Microsoft 365.",
    "required": true,
    "sensitive": true
  },
  {
    "name": "MICROSOFT_ACCESS_TOKEN",
    "display_name": "Microsoft Access Token",
    "description": "Alternative Microsoft OAuth access token name.",
    "required": true,
    "sensitive": true
  }
]
```

#### 7.2 本地 OAuth 脚本

新增 `scripts/outlook_token/`：

```
scripts/outlook_token/
  outlook_local_oauth.py   # 类比 gmail_local_oauth.py
  README.md
  .secrets/outlook_tokens/  # token 文件存储目录
    bob@outlook.com.json
    c@company.com.json
```

`outlook_local_oauth.py` 核心流程：
1. 用户在 Azure Portal 注册应用，获取 `client_id` 和 `client_secret`
2. 脚本构造授权 URL → 用户在浏览器中授权 → 回调获取 `authorization_code`
3. 用 `authorization_code` 换取 `access_token` + `refresh_token`
4. 持久化到 `.secrets/outlook_tokens/<sanitized_email>.json`

与 Gmail OAuth 脚本的主要差异：
- 授权端点不同：`login.microsoftonline.com/common/oauth2/v2.0/authorize` vs `accounts.google.com/o/oauth2/v2/auth`
- Token 端点不同：`login.microsoftonline.com/common/oauth2/v2.0/token` vs `oauth2.googleapis.com/token`
- Scope 不同：`Mail.Read Mail.ReadWrite Mail.Send offline_access` vs `https://www.googleapis.com/auth/gmail.modify`

#### 7.3 平台环境变量检测

```python
# main.py _is_platform() 扩展
def _is_platform() -> bool:
    if getattr(sys, "_MEIPASS", ""):
        return True
    if any(
        os.environ.get(k)
        for k in (
            "GMAIL_ACCESS_TOKEN", "GOOGLE_ACCESS_TOKEN",
            "OUTLOOK_ACCESS_TOKEN", "MICROSOFT_ACCESS_TOKEN",
        )
    ):
        return True
    return False
```

---

### 八、前端改动

| 文件 | 改动 |
|---|---|
| `src/types/mail.ts` | Card 增加 `provider?: "gmail" \| "outlook"`；新增 `MailboxProvider` 类型 |
| `src/app/state.ts` | `mailboxRegistry` 中每项带 `provider` |
| `src/api/mailAgentClient.ts` | `addMailbox` 参数增加 `provider` |
| 添加邮箱 UI | 输入邮箱地址 + 下拉选择 provider（自动检测域名 + 手动修正） |
| BriefView 卡片 | 混合视图下显示 provider 图标（Gmail / Outlook） |
| 授权状态面板 | 按 provider 显示不同授权引导 |

---

### 九、改动文件清单

#### 后端新增

| 文件 | 内容 |
|---|---|
| `mail_agent/base_adapter.py` | 抽象基类 `BaseMailAdapter` |
| `mail_agent/gmail_adapter.py` | `GmailAdapter` — 从 `mail_adapter.py` 搬迁 |
| `mail_agent/outlook_adapter.py` | `OutlookAdapter` — 全新实现 |
| `scripts/outlook_token/outlook_local_oauth.py` | 本地 Outlook OAuth |
| `scripts/outlook_token/README.md` | 使用说明 |
| `tests/test_outlook_adapter.py` | Outlook adapter 单元测试 |

#### 后端改造

| 文件 | 改动 |
|---|---|
| `mail_agent/mail_adapter.py` | 改为 facade，委托给 adapter；保留 public API 签名 |
| `mail_agent/planner.py` | 按 provider 切换 system prompt；`_fallback_plan_for_request` 适配 |
| `mail_agent/strategies.py` | `default_queries` 按 provider 分版本 |
| `mail_agent/scan.py` | 最小改动：通过 adapter 调用 |
| `mail_agent/context.py` | 最小改动：通过 adapter 调用 |
| `mail_agent/handle_service.py` | 最小改动：通过 adapter 调用 |
| `mail_agent/pipeline.py` | 替换直接 `gmail_request` 调用为 adapter 调用 |
| `mail_agent/storage_ops.py` | `mailbox-registry` 结构增加 `provider` 字段 |
| `mail_agent/storage_types.py` | 新增 `MailboxRegistryEntry` dataclass |
| `anna_inbox_executa/main.py` | 新增 Outlook 凭据检测；部分工具 handler 适配 |
| `inbox-tool/manifest.json` | 新增凭据声明（`OUTLOOK_ACCESS_TOKEN`、`MICROSOFT_ACCESS_TOKEN`） |
| `inbox-tool/src/pyproject.toml` | 可能需要加 `requests` 依赖（Graph API 更方便用 requests 而非 urllib） |

#### 前端

| 文件 | 改动 |
|---|---|
| `src/types/mail.ts` | 增加 `provider` 字段 |
| `src/app/state.ts` | mailbox registry 带 provider |
| `src/api/mailAgentClient.ts` | `addMailbox` 带 provider 参数 |
| `src/app/useAppController.ts` | 添加邮箱流程增加 provider 选择 |
| `src/features/brief/BriefView.tsx` | 卡片显示 provider 图标 |
| `src/features/drawers/Drawers.tsx` | 邮箱管理面板增加 provider 显示 |

---

### 十、实施顺序

| 阶段 | 内容 | 风险 |
|---|---|---|
| **Phase 1** | 定义 `base_adapter.py` 接口；新建 `gmail_adapter.py`；`mail_adapter.py` 改为 facade | 纯重构，功能不变，回归测试验证 |
| **Phase 2** | 实现 `OutlookAdapter`（搜索 + 获取 + 归一化） | 需要 Outlook 测试账号 |
| **Phase 3** | 本地 OAuth 脚本 + manifest 凭据扩展 | 需要 Azure 应用注册 |
| **Phase 4** | `mail_adapter.py` facade 路由（根据 provider 选择 adapter） | 切换逻辑，需要测试 |
| **Phase 5** | Planner + strategies 搜索语法适配 | LLM prompt 变更，需验证生成质量 |
| **Phase 6** | Outlook 写操作（send_reply / mark_read / trash） | 需要 Outlook 测试账号 |
| **Phase 7** | 前端 UI 适配（provider 选择、图标、邮箱管理） | 纯前端 |
| **Phase 8** | 联调测试 + 边界情况（Graph API 限流、大附件、分页等） | 集成测试 |

---

### 十一、风险与注意事项

1. **Graph API 速率限制**：Outlook 个人账户限制比 Gmail 更严格（每 10 秒最多 4 个并发请求）。需要在 `OutlookAdapter` 中加入请求节流。
2. **Graph API 权限**：`Mail.Read` 是委托权限（delegated），不能读取其他用户的邮箱。每个邮箱需要独立的 OAuth 授权。
3. **Delta 同步**：Graph API 提供 `$delta` 查询用于增量同步。后续可以用它替代当前的"全量搜索 + 缓存去重"模式。
4. **HTML 正文**：Outlook 邮件的 `body.content` 通常是 HTML，当前管线假设 `body_text` 是纯文本。需要增加 HTML→text 转换。`GmailAdapter` 也需要这个（Gmail 正文也可能是 HTML），可以共用。
5. **企业邮箱的复杂性**：Microsoft 365 企业邮箱可能受 IT 管理员策略限制（如禁止第三方应用访问）。需要文档说明。
6. **附件处理**：Gmail 附件是 base64 内联在 message parts 中；Outlook 附件是独立资源通过 URL 下载。附件处理需要抽象。
