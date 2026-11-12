"""最小 HTML/DOM 工具（标准库实现）。

不依赖第三方包，提供规则选择器所需的子集能力，并为每个命中节点记录
稳定的 DOM 位置（同标签兄弟序号路径 + CSS 提示路径），作为抽取证据
随字段原文一起保存。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable

# HTML 中无需闭合的空元素，解析时不产生子树。
_VOID_TAGS = frozenset({"br", "img", "hr", "input", "meta", "link"})


@dataclass
class Element:
    tag: str
    attrs: dict[str, str]
    parent: "Element | None" = None
    children: list["Element"] = field(default_factory=list)
    # 直接文本（不含子元素文本），按文档顺序累积。
    text_parts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(self.text_parts).strip()

    def text_content(self) -> str:
        parts: list[str] = []
        if self.text:
            parts.append(self.text)
        for child in self.children:
            content = child.text_content()
            if content:
                parts.append(content)
        return " ".join(parts).strip()

    def classes(self) -> list[str]:
        return self.attrs.get("class", "").split()

    def has_class(self, name: str) -> bool:
        return name in self.classes()

    def iter_descendants(self) -> Iterable["Element"]:
        for child in self.children:
            yield child
            yield from child.iter_descendants()

    def _sibling_index(self) -> int:
        if self.parent is None:
            return 0
        return [sib for sib in self.parent.children if sib.tag == self.tag].index(self)

    def dom_path(self) -> str:
        """同标签兄弟序号路径，例如 article[0] > section[0] > time[1]。"""
        chain: list[str] = []
        node: Element | None = self
        while node is not None and node.tag != "#root":
            chain.append(f"{node.tag}[{node._sibling_index()}]")
            node = node.parent
        return " > ".join(reversed(chain))

    def css_path(self) -> str:
        """带关键属性的 CSS 提示路径，便于人工核对选择器。"""
        chain: list[str] = []
        node: Element | None = self
        while node is not None and node.tag != "#root":
            label = node.tag
            if node.attrs.get("data-role"):
                label += f'[data-role="{node.attrs["data-role"]}"]'
            elif node.attrs.get("data-field"):
                label += f'[data-field="{node.attrs["data-field"]}"]'
            elif node.attrs.get("class"):
                label += "." + ".".join(node.classes())
            chain.append(label)
            node = node.parent
        return " > ".join(reversed(chain))

    def position(self) -> dict[str, str]:
        return {"dom_path": self.dom_path(), "css_path": self.css_path()}


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element(tag="#root", attrs={})
        self._stack: list[Element] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = Element(
            tag=tag.lower(),
            attrs={key.lower(): value or "" for key, value in attrs},
            parent=self._stack[-1],
        )
        self._stack[-1].children.append(element)
        if tag.lower() not in _VOID_TAGS:
            self._stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = Element(
            tag=tag.lower(),
            attrs={key.lower(): value or "" for key, value in attrs},
            parent=self._stack[-1],
        )
        self._stack[-1].children.append(element)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        # 容错：只闭合栈内最近的同名标签，忽略孤立闭合。
        for depth in range(len(self._stack) - 1, 0, -1):
            if self._stack[depth].tag == tag:
                del self._stack[depth:]
                return

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._stack[-1].text_parts.append(data.strip())


def parse_html(content: str | bytes) -> Element:
    """解析 HTML 文本/字节，返回虚拟根节点 #root。"""
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    builder = _TreeBuilder()
    builder.feed(content)
    builder.close()
    return builder.root


# ---------------------------------------------------------------------------
# 极简 CSS 选择器：支持 tag / .class / [attr] / [attr="v"]、逗号分组、
# :nth-of-type(n) 与后代空格组合（例如 article time.start、
# span.place, p[data-field="place"]）。
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _SimpleSelector:
    tag: str | None
    classes: tuple[str, ...]
    attr: str | None
    attr_value: str | None
    nth_of_type: int | None  # 1 基同标签序号，None 表示不限制

    def matches(self, node: Element) -> bool:
        if self.tag is not None and node.tag != self.tag:
            return False
        if any(not node.has_class(name) for name in self.classes):
            return False
        if self.attr is not None:
            if self.attr not in node.attrs:
                return False
            if self.attr_value is not None and node.attrs[self.attr] != self.attr_value:
                return False
        if self.nth_of_type is not None and node._sibling_index() + 1 != self.nth_of_type:
            return False
        return True


def _parse_simple(token: str) -> _SimpleSelector:
    tag: str | None = None
    classes: list[str] = []
    attr: str | None = None
    attr_value: str | None = None
    nth_of_type: int | None = None

    index = 0

    def read_identifier(end_markers: str) -> str:
        nonlocal index
        start = index
        while index < len(token) and token[index] not in end_markers:
            index += 1
        return token[start:index]

    if token and token[0] not in ".[":
        name = read_identifier(".:[")
        tag = name or None
    while index < len(token):
        marker = token[index]
        if marker == ".":
            index += 1
            classes.append(read_identifier(".:["))
        elif marker == "[":
            end = token.index("]", index)
            expression = token[index + 1 : end]
            index = end + 1
            if "=" in expression:
                attr, raw_value = expression.split("=", 1)
                attr_value = raw_value.strip().strip('"').strip("'")
            else:
                attr = expression
        elif marker == ":":
            pseudo = read_identifier(".[")
            if pseudo.startswith(":nth-of-type(") and pseudo.endswith(")"):
                nth_of_type = int(pseudo[len(":nth-of-type(") : -1])
            else:
                raise ValueError(f"不支持的伪类: {pseudo}")
        else:
            raise ValueError(f"无法解析的选择器片段: {token!r}")
    return _SimpleSelector(
        tag=tag,
        classes=tuple(classes),
        attr=attr,
        attr_value=attr_value,
        nth_of_type=nth_of_type,
    )


def _split_groups(selector: str) -> list[str]:
    """按逗号拆分选择器分组（方括号内的逗号受保护）。"""
    groups: list[str] = []
    buffer: list[str] = []
    depth = 0
    for char in selector:
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        if char == "," and depth == 0:
            groups.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(char)
    tail = "".join(buffer).strip()
    if tail:
        groups.append(tail)
    return groups


def _parse_selector(selector: str) -> list[list[_SimpleSelector]]:
    groups = _split_groups(selector)
    if not groups or any(not group for group in groups):
        raise ValueError("空选择器")
    parsed = []
    for group in groups:
        tokens = [token for token in group.replace(">", " > ").split() if token != ">"]
        parsed.append([_parse_simple(token) for token in tokens])
    return parsed


def select_all(root: Element, selector: str) -> list[Element]:
    """选择器查询，支持逗号分组、后代组合与 :nth-of-type(n)。"""
    all_matches: list[Element] = []
    seen: set[int] = set()
    for parts in _parse_selector(selector):
        candidates: list[Element] = [root]
        for part in parts:
            next_round: list[Element] = []
            for node in candidates:
                for descendant in node.iter_descendants():
                    if part.matches(descendant):
                        next_round.append(descendant)
            candidates = next_round
        for node in candidates:
            if id(node) not in seen:
                seen.add(id(node))
                all_matches.append(node)
    return all_matches


def select_one(root: Element, selector: str) -> Element | None:
    matches = select_all(root, selector)
    return matches[0] if matches else None
