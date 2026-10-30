"""Small HTML tree for explicit native regions, links and metadata; no script execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser

_VOID = {
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
}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    children: list[Node | str] = field(default_factory=list)

    def walk(self) -> list[Node]:
        return [
            self,
            *(node for child in self.children if isinstance(child, Node) for node in child.walk()),
        ]

    def text(self) -> str:
        if self.tag in {"script", "style", "nav", "aside"}:
            return ""
        parts = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            else:
                parts.append(child.text())
                if child.tag in {"p", "div", "h1", "h2", "h3", "li", "br", "tr"}:
                    parts.append("\n")
        return "".join(parts)

    def has_class(self, name: str) -> bool:
        return name in self.attrs.get("class", "").split()


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def parse_html(text: str) -> Node:
    parser = _Tree()
    parser.feed(text)
    return parser.root


def plain_text(text: str) -> str:
    return parse_html(text).text().strip()
