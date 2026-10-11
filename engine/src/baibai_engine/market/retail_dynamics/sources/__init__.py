"""Small HTML helpers for the two verified public source pages."""

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
        return "".join(child if isinstance(child, str) else child.text for child in self.children)

    def find(self, tag: str | None = None, **attrs: str) -> list[Element]:
        found = []
        if (tag is None or self.tag == tag) and all(
            self.attrs.get(key) == value for key, value in attrs.items()
        ):
            found.append(self)
        for child in self.children:
            if isinstance(child, Element):
                found.extend(child.find(tag, **attrs))
        return found

    def has_class(self, value: str) -> bool:
        return value in self.attrs.get("class", "").split()


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


def document(html: str) -> Element:
    parser = _Document()
    parser.feed(html)
    return parser.root


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def number(text: str, *, unit: Literal["yen", "rating"]) -> float | None:
    text = compact(text)
    if text in {"--", "-", "－", "―", "—", "予想なし", "データなし", "該当なし"}:
        return None
    suffix = "円" if unit == "yen" else ""
    match = re.fullmatch(r"(-?[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)" + suffix, text)
    if match is None:
        raise ParseError("unexpected_numeric_cell")
    return float(match[1].replace(",", ""))
