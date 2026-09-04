"""Safe Markdown rendering for Basecamp's supported rich-text subset."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

from markdown_it import MarkdownIt

_MARKDOWN = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
_ALLOWED_TAGS = {"div", "h1", "br", "strong", "em", "strike", "a", "pre", "ol", "ul", "li", "blockquote"}
_VOID_TAGS = {"br"}
_PERSON_SGID = re.compile(r"^\d+$")


class _BasecampSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.output: list[str] = []
        self.stack: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = "div" if tag == "p" else "h1" if tag in {"h2", "h3", "h4", "h5", "h6"} else tag
        if normalized not in _ALLOWED_TAGS:
            self.stack.append(None)
            return
        rendered_attrs = ""
        if normalized == "a":
            href = next((value or "" for name, value in attrs if name.lower() == "href"), "")
            parsed = urlparse(href)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                self.stack.append(None)
                return
            rendered_attrs = f' href="{html.escape(href, quote=True)}"'
        self.output.append(f"<{normalized}{rendered_attrs}>")
        self.stack.append(None if normalized in _VOID_TAGS else normalized)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.stack:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            return
        normalized = self.stack.pop()
        if normalized:
            self.output.append(f"</{normalized}>")

    def handle_data(self, data: str) -> None:
        self.output.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.output.append(f"&#{name};")


def format_message(markdown: str) -> str:
    """Render untrusted Markdown into Basecamp-safe HTML."""
    sanitizer = _BasecampSanitizer()
    sanitizer.feed(_MARKDOWN.render(markdown))
    sanitizer.close()
    return "".join(sanitizer.output).strip()


def format_chunks(markdown: str, *, max_length: int) -> list[str]:
    """Split Markdown without emitting a Basecamp payload over max_length."""
    if max_length < 256:
        raise ValueError("Basecamp chunk length must be at least 256 characters")
    remaining = markdown.strip()
    if not remaining:
        return ["<div></div>"]
    chunks: list[str] = []
    while remaining:
        rendered = format_message(remaining)
        if len(rendered) <= max_length:
            chunks.append(rendered)
            break
        low, high = 1, len(remaining)
        while low < high:
            midpoint = (low + high + 1) // 2
            if len(format_message(remaining[:midpoint])) <= max_length:
                low = midpoint
            else:
                high = midpoint - 1
        split_at = max(1, low)
        newline = remaining.rfind("\n", 0, split_at + 1)
        if newline > split_at // 2:
            split_at = newline
        chunks.append(format_message(remaining[:split_at].rstrip()))
        remaining = remaining[split_at:].lstrip()
    return chunks


def render_person_mention(person_id: str, name: str) -> str:
    """Render a trusted Basecamp person reference as a structured mention."""
    if not _PERSON_SGID.fullmatch(person_id):
        raise ValueError("Basecamp person ID must be numeric")
    escaped_name = html.escape(name, quote=False)
    return (
        '<bc-attachment content-type="application/vnd.basecamp.mention" '
        f'sgid="sgid://bc3/Person/{person_id}">{escaped_name}</bc-attachment>'
    )
