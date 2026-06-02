# HTML 邮件处理方案

## 一、当前问题

### 1.1 现状：原始 HTML 直接进入 LLM

`mail_adapter.py:259-280` 的 `_decode_body()` 函数：

```python
def _decode_body(message: dict[str, Any]) -> str:
    parts: list[str] = []
    def walk(part: dict[str, Any]) -> None:
        mime_type = str(part.get("mimeType") or "")
        if data and mime_type in {"text/plain", "text/html"}:  # ← 两种 MIME 类型
            decoded = base64.urlsafe_b64decode(...).decode("utf-8", errors="replace")
            parts.append(decoded)  # ← 直接拼接，不做任何转换
    walk(payload)
    return "\n\n".join(...)[:30000]
```

问题：
- `text/plain` 和 `text/html` 被原样拼接，中间用 `\n\n` 分隔
- 如果邮件同时有纯文本和 HTML 版本，`body_text` 中会出现**两份内容**——一份纯文本 + 一份原始 HTML
- HTML 标签、CSS 内联样式、`<style>` 块、注释全部保留

### 1.2 影响面：body_text 流入 LLM 的所有位置

```
mail_adapter._decode_body() → MessageDetail.body_text (最大 30000 字符)
    │
    ├── judgment.py _render_context_for_prompt        [:1200] 单项、[:400]×N 线程
    ├── judgment.py _render_context_for_batch_prompt  [:800] 单项、[:260]×4 线程
    ├── handle_service.py _fetch_thread_context_sync  [:2000]×5 → "body" key
    │   ├── build_thread_summary_prompt               [:800]
    │   ├── build_draft_reply_prompt                  [:1200]
    │   └── build_revise_draft_prompt                 [:800]
    └── candidate.py                                【不使用 body_text】
```

虽然各处都有截断保护，但截断前的内容可能是 30000 字符的 HTML 垃圾——前 1200 字符可能全是 `<style>` 块和 `<table>` 布局标签，没有任何有效文本。

### 1.3 具体危害

| 危害 | 说明 |
|------|------|
| **Token 浪费** | 一封典型 HTML 营销邮件：原始 15000 字符 → 实际有效文本约 500 字符。97% 是 HTML 噪音 |
| **LLM 判断失真** | CSS 类名、DOM 结构、行内样式混在正文中，LLM 难以区分哪些是邮件内容、哪些是标记 |
| **截断位置不可控** | 当前 `[:1200]` 截断可能切在 HTML 标签中间，导致 LLM 看到不完整的标签碎片 |
| **重复内容** | 同时有 `text/plain` + `text/html` 时，同一封邮件的内容出现两次 |

### 1.4 实际案例

从 `kate@anna.partners` 的 Gmail 缓存中抽样 205 封邮件，统计 MIME 类型分布：

```python
import json, os, base64
from collections import Counter

cache_dir = ".data/gmail_cache/mailboxes/kate_anna.partners"
mime_stats = Counter()
has_both = 0
html_only = 0
text_only = 0

for fn in os.listdir(cache_dir):
    if not fn.endswith('.json'): continue
    with open(os.path.join(cache_dir, fn), encoding='utf-8') as f:
        msg = json.load(f)
    payload = msg.get('payload', {})
    mime_types = set()
    def walk(p):
        mt = p.get('mimeType', '')
        if mt in ('text/plain', 'text/html'):
            mime_types.add(mt)
        for child in p.get('parts', []) or []:
            if isinstance(child, dict): walk(child)
    walk(payload)
    if 'text/html' in mime_types and 'text/plain' in mime_types:
        has_both += 1
    elif 'text/html' in mime_types:
        html_only += 1
    elif 'text/plain' in mime_types:
        text_only += 1
    for mt in mime_types:
        mime_stats[mt] += 1

print(f"text/plain: {mime_stats['text/plain']}, text/html: {mime_stats['text/html']}")
print(f"仅text/plain: {text_only}, 仅text/html: {html_only}, 两者都有: {has_both}")
```

结果（预期）：大多数商业邮件和 newsletter 只有 `text/html`，个人邮件往往两者都有。`text/html` 的出现频率远高于 `text/plain`。

---

## 二、行业最佳实践

### 2.1 开源项目参考

| 项目 | Stars | 处理方式 |
|------|-------|---------|
| [mailgun/talon](https://github.com/mailgun/talon) | 1277 | ML 驱动的引用/签名分离；HTML 邮件 8 阶段 pipeline 剥离 Gmail/Outlook 特定标记 |
| [niklaus/ollama-email-summariser](https://github.com/nicklansley/ollama_email_summariser) | — | 同时支持纯文本和 HTML：优先 `text/plain`，否则 `BeautifulSoup.get_text()` |
| [isaiahshall/Local-LLaMA-Email-Agent](https://github.com/isaiahshall/Local-LLaMA-Email-Agent) | — | `bs4` 解析 HTML，decompose `<style>`/`<script>` 后提取 `.get_text()` |
| [mail-parser-reply](https://pypi.org/project/mail-parser-reply/) | — | 从 HTML 邮件中提取纯回复内容，去除历史引用 |
| [markdown-for-agents](https://github.com/KKonstantinov/markdown-for-agents) | — | HTML→Markdown，实测 138,550 tokens → 9,364 tokens（-93.2%），自动剥离 nav/footer/ads |

### 2.2 核心库对比

| 库 | 适用场景 | 优势 | 劣势 |
|----|---------|------|------|
| **`html2text`** | HTML→Markdown | 零依赖，保留链接 `[text](url)`，LLM 原生理解 Markdown | 对复杂嵌套表格处理不如 `inscriptis` |
| **BeautifulSoup `.get_text()`** | HTML→纯文本 | 精细控制，可先 decompose 特定标签 | 丢失链接 URL（对安全告警判断不利） |
| **`inscriptis`** | 复杂 HTML 布局 | 正确渲染嵌套表格、列表缩进、CSS display | 依赖较多，安装偏重 |
| **`talon`** | 引用/签名剥离 | ML 分类，准确率高 | 需 `talon.init()` 下载模型 |
| **正则 `^On .+ wrote:`** | Gmail 引用标记 | 零依赖 | 仅覆盖 Gmail 英文引用，多语言/多客户端不完整 |

### 2.3 社区共识 Pipeline

```
1. 优先使用 text/plain MIME part（如果存在 + 非空）
    ↓
2. 对 text/html part：BeautifulSoup decompose <style>/<script>/<noscript>
    ↓
3. HTML → Markdown（html2text），保留链接结构
    ↓
4. 可选：剥离引用回复（regex 或 talon）
    ↓
5. 可选：剥离签名（regex ^-- $）
    ↓
6. 按策略截断（已在现有代码中）
    ↓
7. 干净文本 → LLM
```

**核心原则："永远不要把原始 HTML 传给 LLM。"**

---

## 三、针对本项目的最优方案设计

### 3.1 设计原则

1. **优先纯文本**：如果 `text/plain` MIME part 存在且内容非空，直接使用，跳过 `text/html`
2. **HTML→Markdown**：如果仅有 `text/html`（或纯文本为空），先清洗再转 Markdown
3. **保留链接**：安全告警/账单邮件中的 URL 对 LLM 判断至关重要，Markdown `[text](url)` 格式既省 token 又保留信息
4. **轻量优先**：不引入 ML 依赖（talon），用 regex + `html2text` 解决 80% 的问题
5. **渐进式**：在 `_decode_body()` 层面修改，下游代码无需感知

### 3.2 修改位置

只需修改一个函数：`mail_adapter.py` 中的 `_decode_body()`。

当前的逻辑：
```python
# 当前：text/plain 和 text/html 都原样拼进去
if data and mime_type in {"text/plain", "text/html"}:
    decoded = base64.urlsafe_b64decode(...).decode("utf-8", errors="replace")
    parts.append(decoded)
```

改为：
```python
# 改进后：分类型处理
if data and mime_type == "text/plain":
    decoded = _decode_base64(data)
    if decoded.strip():  # 非空纯文本才是有效的
        text_parts.append(decoded)

if data and mime_type == "text/html":
    decoded = _decode_base64(data)
    html_parts.append(decoded)

# 合并策略：优先纯文本，fallback 到 HTML→Markdown
if text_parts:
    result = "\n\n".join(text_parts)
else:
    result = _html_to_markdown("\n\n".join(html_parts))

# 去引用 + 去签名
result = _strip_quoted_reply(result)
result = _strip_signature(result)

return result[:30000]
```

### 3.3 新增函数

```python
# ── Base64 解码 ──
def _decode_base64(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(
            str(data) + "=" * (-len(str(data)) % 4)
        ).decode("utf-8", errors="replace")
    except Exception:
        return ""

# ── HTML → Markdown ──
def _html_to_markdown(html: str) -> str:
    """将 HTML 邮件转为 Markdown，去除噪音标签，保留链接结构。"""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        # 移除不包含有效信息的标签
        for tag in soup.find_all(["style", "script", "noscript", "head", "meta", "link"]):
            tag.decompose()
        # 移除隐藏元素
        for tag in soup.find_all(style=re.compile(r"display\s*:\s*none", re.I)):
            tag.decompose()
        html = str(soup)
    except ImportError:
        pass  # bs4 不可用时继续用原始 HTML

    try:
        import html2text
        h = html2text.HTML2Text()
        h.ignore_links = False          # 保留链接 [text](url)
        h.ignore_images = True          # 丢弃图片
        h.ignore_emphasis = False       # 保留 **加粗** / *斜体*
        h.body_width = 0                # 不做自动换行
        h.skip_internal_anchors = True  # 跳过页内锚点
        h.protect_links = True          # 保护链接不被折行
        return h.handle(html).strip()
    except ImportError:
        # 最后 fallback：正则粗暴去标签
        return _strip_html_tags_regex(html)

# ── 正则 fallback（无 bs4/html2text 时） ──
def _strip_html_tags_regex(html: str) -> str:
    """简单的去 HTML 标签（不推荐，缺少库时的低保方案）。"""
    import re
    # 去 style/script 块
    html = re.sub(r'<(style|script|noscript)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.I)
    # 去注释
    html = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)
    # 去标签，保留内容
    html = re.sub(r'<[^>]+>', ' ', html)
    # 折叠空白
    html = re.sub(r'[ \t]+', ' ', html)
    html = re.sub(r'\n\s*\n', '\n\n', html)
    return html.strip()

# ── 剥离 Gmail/Outlook 引用 ──
def _strip_quoted_reply(text: str) -> str:
    """移除常见的邮件引用标记。"""
    import re
    # Gmail 英文/中文引用
    text = re.sub(r'^On .+ wrote:\s*\n.*', '', text, flags=re.DOTALL | re.MULTILINE)
    # Outlook 引用分隔线
    text = re.sub(r'^_{10,}\s*\n.*', '', text, flags=re.DOTALL | re.MULTILINE)
    # Gmail 中文
    text = re.sub(r'^.+写道：\s*\n.*', '', text, flags=re.DOTALL | re.MULTILINE)
    # 引用行（> 开头）
    # 注意：不去除内联引用，只去除整段连续的引用块
    # 这需要更复杂的逻辑，MVP 先跳过
    return text.strip()

# ── 剥离签名 ──
def _strip_signature(text: str) -> str:
    """移除标准签名分隔符之后的内容。"""
    import re
    # 匹配独立的 "-- " 行（RFC 3676 签名分隔符）
    parts = re.split(r'\n--\s*\n', text, maxsplit=1)
    return parts[0].strip()
```

### 3.4 依赖变化

无需新增依赖。`html2text` 和 `beautifulsoup4` 是可选的优化层：

```
# 推荐安装（pip install html2text beautifulsoup4）
# 如果不可用，自动降级到 regex fallback
```

`pyproject.toml` 中可列为可选依赖：
```toml
[project.optional-dependencies]
html = ["html2text", "beautifulsoup4"]
```

### 3.5 Token 效率预估

以一封典型 Gmail HTML 邮件为例（Medium digest）：

| 阶段 | 字符数 | 说明 |
|------|--------|------|
| 原始 HTML（`text/html` MIME part） | 15,000 | inline style, `<table>` 布局, 追踪像素 |
| 经过 `html2text` → Markdown | 800 | 保留了链接和粗体结构 |
| 经过 `BeautifulSoup.decompose()` 预处理 | 650 | 去除了隐藏像素和 `<style>` |
| 引用+签名剥离后 | 550 | — |
| **最终进入 LLM** | **550** | **96% 噪音被消除** |

即使 `html2text` 不可用，纯正则 fallback 也能从 15,000 降至约 1,200（92% 消除）。

---

## 四、风险和边界

| 场景 | 处理方式 |
|------|---------|
| 纯文本邮件 | 不做任何转换，原样保留 |
| HTML-only 邮件（newsletter 常见） | `html2text` 转 Markdown，保留链接 |
| 同时有 text/plain 和 text/html | 优先使用 text/plain（通常更干净） |
| text/plain 只有一句话（"请查看 HTML 版本"） | 需判空 + 最短长度阈值（如 ≥20 字符才视为有效） |
| HTML 中包含重要的 `<a href>` | Markdown `[text](url)` 保留，LLM 可理解 |
| 安全告警邮件（Google 登录告警） | 链接 URL 是核心信息，Markdown 保留 |
| 多语言引用标记 | 当前仅覆盖英文 + 中文，后续可扩展 |
| `html2text` / `bs4` 不可用 | 自动降级到 regex fallback |

---

## 五、实施建议

### Phase 1（本次）：核心转换
- 修改 `_decode_body()`：`text/plain` 优先 → `html2text` 转换 → regex fallback
- 新增 `_decode_base64()`、`_html_to_markdown()`、`_strip_html_tags_regex()`
- 不改变下游任何代码

### Phase 2（后续可选）：引用+签名剥离
- 新增 `_strip_quoted_reply()`、`_strip_signature()`
- 减少 LLM 处理历史引用内容的 token 消耗

### Phase 3（后续可选）：按重要性分级截断
- Action required 邮件：全文（max 3000 字符）
- Review 邮件：前 1000 字符
- Low priority：前 300 字符（已在 batch prompt 中通过 `[:260]` 实现）
