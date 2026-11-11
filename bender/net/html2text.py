"""HTML → читаемый текст для модели (свой, на html.parser из стандартной библиотеки).

Заголовки — `#`, списки — `-`, ссылки — `[текст](адрес)` с абсолютными адресами, таблицы —
строки через `|`, код — как есть. Скрипты, стили, svg, формы выкидываются. Лишние пробелы
схлопываются, абзацы разделяются пустой строкой.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin

SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "object", "canvas", "select", "button", "head"}
BLOCK = {"p", "div", "section", "article", "main", "header", "footer", "aside", "nav", "form", "figure", "figcaption",
         "blockquote", "address", "details", "summary", "dl", "dt", "dd", "table", "thead", "tbody", "tfoot", "ul", "ol", "hr", "body", "html"}


class _Parser(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.out: list[str] = []
        self.skip = 0
        self.pre = 0
        self.title = ""
        self._in_title = False
        self.href: list[str | None] = []
        self.link_text: list[list[str]] = []
        self.list_stack: list[str] = []
        self.ol_counter: list[int] = []
        self.cell = False

    def emit(self, text: str) -> None:
        if self.link_text:
            self.link_text[-1].append(text)
        else:
            self.out.append(text)

    def nl(self, n: int = 1) -> None:
        self.emit("\n" * n)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
            return
        if tag in SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.nl(2)
            self.emit("#" * int(tag[1]) + " ")
        elif tag in BLOCK:
            self.nl(2 if tag in ("p", "blockquote", "table", "ul", "ol") else 1)
            if tag in ("ul", "ol"):
                self.list_stack.append(tag)
                self.ol_counter.append(0)
            if tag == "hr":
                self.emit("———")
                self.nl()
        elif tag == "li":
            self.nl()
            depth = max(0, len(self.list_stack) - 1)
            if self.list_stack and self.list_stack[-1] == "ol":
                self.ol_counter[-1] += 1
                self.emit("  " * depth + f"{self.ol_counter[-1]}. ")
            else:
                self.emit("  " * depth + "- ")
        elif tag == "br":
            self.nl()
        elif tag == "tr":
            self.nl()
            self.cell = False
        elif tag in ("td", "th"):
            if self.cell:
                self.emit(" | ")
            self.cell = True
        elif tag == "pre":
            self.pre += 1
            self.nl(2)
            self.emit("```\n")
        elif tag == "code" and not self.pre:
            self.emit("`")
        elif tag in ("strong", "b"):
            self.emit("**")
        elif tag == "a":
            href = a.get("href")
            if href and not href.startswith(("javascript:", "#")):
                self.href.append(urljoin(self.base, href))
                self.link_text.append([])
            else:
                self.href.append(None)
        elif tag == "img":
            alt = (a.get("alt") or "").strip()
            if alt:
                self.emit(f"[картинка: {alt}]")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
            return
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.nl(2)
        elif tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
                self.ol_counter.pop()
            self.nl()
        elif tag in BLOCK:
            self.nl()
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self.emit("\n```")
            self.nl(2)
        elif tag == "code" and not self.pre:
            self.emit("`")
        elif tag in ("strong", "b"):
            self.emit("**")
        elif tag == "a" and self.href:
            href = self.href.pop()
            if href is not None and self.link_text:
                text = re.sub(r"\s+", " ", "".join(self.link_text.pop())).strip()
                if text:
                    self.emit(f"[{text}]({href})")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self.skip:
            return
        if self.pre:
            self.emit(data)
        else:
            self.emit(re.sub(r"\s+", " ", data))


def html_to_text(html: str, base_url: str = "") -> tuple[str, str]:
    """(заголовок, текст)."""
    p = _Parser(base_url)
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 — кривой HTML не должен ронять тул
        pass
    text = "".join(p.out)
    text = unescape(text)
    lines = [ln.rstrip() for ln in text.split("\n")]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if not ln.strip():
            blank += 1
            if blank <= 1:
                out.append("")
            continue
        blank = 0
        out.append(ln if ln.startswith("```") or ln.startswith("  ") else ln.strip())
    return re.sub(r"\s+", " ", p.title).strip(), "\n".join(out).strip()
