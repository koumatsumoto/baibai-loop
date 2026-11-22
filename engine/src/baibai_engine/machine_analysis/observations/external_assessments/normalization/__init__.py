"""Small HTML helpers for verified public source pages."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Literal


class ParseError(ValueError):
    """The response does not demonstrate the expected observation contract."""


@dataclass
class Element:
    tag: str
    attrs: dict[str, str]
    children: list[Element | str] = field(default_factory=list)

    @property
    def text(self) -> str:
        parts: list[str] = []
        pending: list[Element | str] = list(reversed(self.children))
        while pending:
            child = pending.pop()
            if isinstance(child, str):
                parts.append(child)
            else:
                pending.extend(reversed(child.children))
        return "".join(parts)

    def find(self, tag: str | None = None, **attrs: str) -> list[Element]:
        found = []
        pending = [self]
        while pending:
            node = pending.pop()
            if (tag is None or node.tag == tag) and all(
                node.attrs.get(key) == value for key, value in attrs.items()
            ):
                found.append(node)
            pending.extend(child for child in reversed(node.children) if isinstance(child, Element))
        return found

    def has_class(self, value: str) -> bool:
        return value in self.attrs.get("class", "").split()


def numeric_text(element: Element) -> str:
    """Preserve explicit line breaks without adding gaps between inline elements."""
    parts: list[str] = []
    pending: list[Element | str] = [element]
    while pending:
        child = pending.pop()
        if isinstance(child, str):
            parts.append(child)
        elif child.tag == "br":
            parts.append("\n")
        else:
            pending.extend(reversed(child.children))
    return "".join(parts)


class _Document(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = Element(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(element)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append(element)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


class _DocumentWithoutTemplateContent(_Document):
    """Do not send inert template tags or text to the document's structural stack."""

    def __init__(self) -> None:
        super().__init__()
        self._template_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "template":
            self._template_depth += 1
        elif not self._template_depth:
            super().handle_starttag(tag, attrs)
        elif tag == "noscript":
            # A script-enabled document treats this fallback as text. Within
            # an inert template it cannot establish that the template ended.
            self.set_cdata_mode(tag, escapable=False)

    def handle_endtag(self, tag: str) -> None:
        if self._template_depth:
            if tag == "template":
                self._template_depth -= 1
            return
        super().handle_endtag(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "template":
            # HTML template is non-void: a malformed trailing slash does not close it.
            self.handle_starttag(tag, attrs)
        elif self._template_depth and (
            tag in self.CDATA_CONTENT_ELEMENTS
            or tag in self.RCDATA_CONTENT_ELEMENTS
            or tag in {"noscript", "plaintext"}
        ):
            # The same non-void rule applies to text elements inside a template.
            # Text spelling </template> must not escape the outer inert scope.
            self.set_cdata_mode(tag, escapable=tag in self.RCDATA_CONTENT_ELEMENTS)
        else:
            super().handle_startendtag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if not self._template_depth:
            super().handle_data(data)


def document(html: str, *, omit_template_content: bool = False) -> Element:
    parser = _DocumentWithoutTemplateContent() if omit_template_content else _Document()
    parser.feed(html)
    return parser.root


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def number(text: str, *, unit: Literal["yen", "rating"]) -> float | None:
    if compact(text) in {"--", "-", "－", "―", "—", "予想なし", "データなし", "該当なし"}:
        return None
    suffix = r"\s*円" if unit == "yen" else ""
    match = re.fullmatch(
        r"(-?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?)" + suffix,
        text.strip(),
    )
    if match is None:
        raise ParseError("unexpected_numeric_cell")
    return float(match[1].replace(",", ""))
