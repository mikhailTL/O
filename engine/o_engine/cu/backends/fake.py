"""In-memory backend for tests. Records every action in ``.actions`` and never touches a display."""
from __future__ import annotations

from ..schema import Target
from .base import BackendError, CUBackend, Node, find_in

_TOGGLE_ROLES = {"check box", "checkbox", "toggle button", "radio button"}


class FakeBackend(CUBackend):
    name = "fake"

    def __init__(
        self,
        tree: Node | list[Node],
        screenshot: bytes = b"\x89PNG\r\n\x1a\nfake",
        allow_pixels: bool = False,
        display: str | None = None,
    ) -> None:
        super().__init__(allow_pixels=allow_pixels, display=display)
        self._roots: list[Node] = list(tree) if isinstance(tree, list) else [tree]
        self._screenshot = screenshot
        self.actions: list[tuple] = []

    # ------------------------------------------------------------- mutation

    def replace_tree(self, tree: Node | list[Node]) -> None:
        self._roots = list(tree) if isinstance(tree, list) else [tree]

    def set_screenshot(self, data: bytes) -> None:
        self._screenshot = data

    def _rewrite(self, node_id: str, **update) -> None:
        found = False

        def rebuild(node: Node) -> Node:
            nonlocal found
            children = [rebuild(c) for c in node.children]
            if node.id == node_id:
                found = True
                return node.model_copy(update={"children": children, **update})
            if children != node.children:
                return node.model_copy(update={"children": children})
            return node

        self._roots = [rebuild(r) for r in self._roots]
        if not found:
            raise BackendError(f"fake: no node {node_id!r}")

    def set_state(self, node_id: str, states: list[str]) -> None:
        self._rewrite(node_id, states=list(states))

    def set_text(self, node_id: str, text: str) -> None:
        self._rewrite(node_id, text=text)

    def add_node(self, parent_id: str, node: Node) -> None:
        parent = self._by_id(parent_id)
        if parent is None:
            raise BackendError(f"fake: no node {parent_id!r}")
        self._rewrite(parent_id, children=[*parent.children, node])

    def _by_id(self, node_id: str) -> Node | None:
        for root in self._roots:
            for n in root.walk():
                if n.id == node_id:
                    return n
        return None

    # ------------------------------------------------------------- contract

    def tree(self, window: str | None = None) -> list[Node]:
        return list(self._roots)

    def find(self, target: Target, within: Node | None = None) -> Node | None:
        scope = self._by_id(within.id) if within is not None else None
        if within is not None and scope is None:
            return None
        return find_in(self._roots, target, within=scope)

    def _live(self, node: Node) -> Node | None:
        """The current version of a tree node, or None for synthetic / vanished nodes."""
        if node.synthetic:
            return None
        return self._by_id(node.id)

    def click(self, node: Node) -> None:
        live = self._live(node)
        if live is not None:
            self.actions.append(("click", live.id))
            self._toggle(live)
            return
        self.require_pixels("click")
        if node.rect is None:
            raise BackendError("click: node has no rect")
        self.pixel_click(*node.rect.center)

    def pixel_click(self, x: int, y: int) -> None:
        self.require_pixels("pixel click")
        self.actions.append(("pixel_click", x, y))

    def _toggle(self, live: Node) -> None:
        if live.role.lower() in _TOGGLE_ROLES:
            states = [s for s in live.states if s != "checked"] if "checked" in live.states else [*live.states, "checked"]
            self._rewrite(live.id, states=states)

    def invoke(self, node: Node, action: str) -> None:
        live = self._live(node)
        if live is None:
            raise BackendError(f"invoke {action!r}: node {node.id!r} is not in the accessibility tree")
        self.actions.append(("invoke", live.id, action))
        if action in ("click", "press", "activate", "toggle"):
            self._toggle(live)

    def set_value(self, node: Node, value: str) -> None:
        live = self._live(node)
        if live is None:
            raise BackendError(f"set_value: node {node.id!r} is not in the accessibility tree")
        self.actions.append(("set_value", live.id, value))
        self._rewrite(live.id, text=value)

    def type_text(self, node: Node | None, text: str) -> None:
        if node is None:
            self.require_pixels("type into focused element")
            self.actions.append(("type_text", None, text))
            return
        live = self._live(node)
        if live is None:
            self.require_pixels("type into a screen region")
            self.actions.append(("type_text", node.id, text))
            return
        self.actions.append(("type_text", live.id, text))
        self._rewrite(live.id, text=live.text + text)

    def key(self, keys: str) -> None:
        self.actions.append(("key", keys))

    def screenshot(self) -> bytes:
        return self._screenshot
