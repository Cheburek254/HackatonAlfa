"""Преобразование описаний каталога в ограниченный набор HTML-тегов."""

import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit

ALLOWED_TAGS = {"p", "br", "strong", "b", "em", "i", "ul", "ol", "li", "h1", "h2", "h3", "h4", "a"}
BLOCKED_TAGS = {"script", "style", "iframe", "object", "embed", "svg", "math"}
EMPTY_DESCRIPTION = "<p>Краткое описание пока недоступно.</p>"


class _DescriptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self.blocked: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in BLOCKED_TAGS:
            if tag != "embed":
                self.blocked.append(tag)
            return
        if self.blocked or tag not in ALLOWED_TAGS:
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            try:
                target = urlsplit(href)
            except ValueError:
                target = None
            if target and target.scheme in {"http", "https"} and target.hostname:
                self.output.append(f'<a href="{escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">')
                return
        self.output.append(f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if self.blocked:
            if self.blocked[-1] == tag:
                self.blocked.pop()
        elif tag in ALLOWED_TAGS and tag != "br":
            self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.blocked:
            self.output.append(escape(data))


def format_description_html(raw: str | None) -> str:
    if not raw or not raw.strip():
        return EMPTY_DESCRIPTION
    text = raw.strip()
    if re.search(r"</?[a-zA-Z][^>]*>", text):
        parser = _DescriptionParser()
        try:
            parser.feed(text)
            parser.close()
        except (AssertionError, ValueError):
            pass
        else:
            formatted = re.sub(r"<p>\s*(?:<br>\s*)?</p>", "", "".join(parser.output)).strip()
            return formatted or EMPTY_DESCRIPTION
    return "".join(
        f"<p>{escape(paragraph.strip()).replace(chr(10), '<br>')}</p>"
        for paragraph in re.split(r"\r?\n\s*\n", text.replace("\r\n", "\n")) if paragraph.strip()
    ) or EMPTY_DESCRIPTION
