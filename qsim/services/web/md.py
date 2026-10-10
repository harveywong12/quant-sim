"""轻量 Markdown → HTML（纯标准库，与前端 mdRender 同一子集）。

供 AI 复盘 / AI 对话服务端渲染。只做格式化，不执行任何用户代码。
（自 legacy quant-sim app/markdown.py 原样移植，保持页面渲染一致。）
"""
from __future__ import annotations

import html as _html
import re

_FENCE = re.compile(r"^```")
_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_RULE = re.compile(r"^\s*(---|\*\*\*|___)\s*$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:\-|]+\|?$")
_UL = re.compile(r"^\s*[-*+]\s+(.*)$")
_OL = re.compile(r"^\s*\d+\.\s+(.*)$")

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_ITALIC = re.compile(r"(^|[^*])\*([^*\n]+)\*")
_LINK = re.compile(r"\[([^\]]+)\]\((https?:[^\s)\]]+)\)")


def _escape(text: str) -> str:
    return _html.escape(str(text), quote=False)


def _inline(text: str) -> str:
    value = _escape(text)
    value = _INLINE_CODE.sub(r'<code class="md-code">\1</code>', value)
    value = _BOLD.sub(r"<strong>\1</strong>", value)
    value = _ITALIC.sub(r"\1<em>\2</em>", value)
    value = _LINK.sub(
        r'<a href="\2" target="_blank" rel="noopener" style="color:#FFB547">\1</a>',
        value)
    return value


def to_html(text: str) -> str:
    """把整段 markdown 转成 HTML 片段；非文本输入返回空字符串。"""
    if text is None:
        return ""
    lines = str(text).replace("\r\n", "\n").split("\n")
    out: list[str] = []
    list_tag: str | None = None
    code_buf: list[str] = []
    in_code = False

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    def close_code() -> None:
        if code_buf:
            out.append('<pre class="md-pre"><code>'
                       + _escape("\n".join(code_buf))
                       + "</code></pre>")
            code_buf.clear()

    i = 0
    while i < len(lines):
        raw = lines[i]
        if in_code:
            if _FENCE.match(raw):
                in_code = False
                close_code()
            else:
                code_buf.append(raw)
            i += 1
            continue
        if _FENCE.match(raw):
            close_list()
            in_code = True
            code_buf = []
            i += 1
            continue
        heading = _HEADING.match(raw)
        if heading:
            close_list()
            level = min(len(heading.group(1)) + 1, 5)
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
            continue
        if _RULE.match(raw):
            close_list()
            out.append('<hr style="border:none;border-top:1px solid rgba(42,50,67,.5);margin:6px 0;">')
            i += 1
            continue
        quote = _QUOTE.match(raw)
        if quote:
            close_list()
            out.append(f"<blockquote>{_inline(quote.group(1))}</blockquote>")
            i += 1
            continue
        if _TABLE_ROW.match(raw) and i + 1 < len(lines) \
                and _TABLE_SEP.match(lines[i + 1]) and "|" in lines[i + 1]:
            close_list()
            rows: list[list[str]] = []
            while i < len(lines) and _TABLE_ROW.match(lines[i]):
                cells = lines[i].strip().strip("|").split("|")
                rows.append([c.strip() for c in cells])
                i += 1
            html = '<table class="md-table"><thead><tr>'
            for cell in rows[0] if rows else []:
                html += f"<th>{_inline(cell)}</th>"
            html += "</tr></thead><tbody>"
            for row in rows[2:]:
                html += "<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in row) + "</tr>"
            out.append(html + "</tbody></table>")
            i += 1
            continue
        ul = _UL.match(raw)
        ol = _OL.match(raw)
        if ul or ol:
            tag = "ul" if ul else "ol"
            if list_tag != tag:
                close_list()
                list_tag = tag
                out.append(f'<{tag} class="md-list">')
            out.append(f"<li>{_inline((ul or ol).group(1))}</li>")
            i += 1
            continue
        close_list()
        if raw.strip() == "":
            out.append('<div class="md-blank"></div>')
        else:
            out.append(f'<div class="md-line">{_inline(raw)}</div>')
        i += 1

    close_list()
    close_code()
    return "".join(out)
