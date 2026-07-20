"""外发富文本 HTML 白名单的脚本式测试。"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> None:
    from mail_agent.mail_providers.gmail.outgoing_html import sanitize_outgoing_html

    html = (
        '<div><b>Bold</b> <i>italic</i> '
        '<font size="4" color="#2563eb">sized</font> '
        '<a href="javascript:alert(1)" onclick="steal()">bad</a> '
        '<a href="mailto:test@example.com">mail</a>'
        '<script>alert(1)</script></div>'
    )
    result = sanitize_outgoing_html(html)
    assert result == (
        '<p><strong>Bold</strong> <em>italic</em> '
        '<span style="font-size: 18px; color: #2563eb">sized</span> '
        '<a>bad</a> <a href="mailto:test@example.com">mail</a></p>'
    )
    assert sanitize_outgoing_html('<span style="position:fixed;color:#dc2626;font-size:99px">x</span>') == '<span style="color: #dc2626">x</span>'
    print("PASS outgoing html sanitizer")


if __name__ == "__main__":
    main()
