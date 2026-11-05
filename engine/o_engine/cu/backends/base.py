"""Backend contract for computer use.

A backend exposes the accessibility tree of a display as ``Node`` objects and acts on
them through the platform's accessibility API first (invoke, set value, focus).
Pixel actions (moving the pointer, clicking coordinates, typing into whatever is
focused) are permitted only when the backend was constructed with
``allow_pixels=True``, which the engine does solely for an isolated display.
"""
from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from fnmatch import fnmatchcase
from typing import Iterable, Iterator

from pydantic import BaseModel, ConfigDict, Field

from ..schema import Target

SYNTHETIC_STATE = "synthetic"  # a node built from a bbox (locator cache / vision), not from the a11y tree


class BackendError(RuntimeError):
    """An action could not be performed. Executor turns it into a failed step; it never escapes run()."""


class Rect(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    x: int
    y: int
    w: int = Field(ge=0)
    h: int = Field(ge=0)

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


class Node(BaseModel):
    """One accessible element. ``states`` are lowercase platform state names (focused, checked, ...)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    id: str = Field(min_length=1)
    role: str = ""
    name: str = ""
    text: str = ""
    rect: Rect | None = None
    states: list[str] = Field(default_factory=list)
    children: list["Node"] = Field(default_factory=list)

    @property
    def synthetic(self) -> bool:
        return SYNTHETIC_STATE in self.states

    def walk(self) -> Iterator["Node"]:
        yield self
        for child in self.children:
            yield from child.walk()

    def summary(self) -> dict:
        return {"id": self.id, "role": self.role, "name": self.name, "states": list(self.states)}


Node.model_rebuild()


def synthetic_node(target: Target, bbox: dict, source: str) -> Node:
    """A node standing for a screen region found outside the accessibility tree."""
    rect = Rect(x=int(bbox["x"]), y=int(bbox["y"]), w=int(bbox["w"]), h=int(bbox["h"]))
    return Node(
        id=f"{source}:{rect.x},{rect.y},{rect.w},{rect.h}",
        role=target.role or "",
        name=target.name or "",
        text=target.text or "",
        rect=rect,
        states=[SYNTHETIC_STATE, "visible"],
    )


def match(node: Node, target: Target) -> bool:
    """Role equality, case-insensitive glob on name, case-insensitive substring on text. Ignores index/within."""
    if target.role is not None and node.role.lower() != target.role.lower():
        return False
    if target.name is not None and not fnmatchcase(node.name.lower(), target.name.lower()):
        return False
    if target.text is not None and target.text.lower() not in node.text.lower():
        return False
    return True


def iter_matches(nodes: Iterable[Node], target: Target) -> Iterator[Node]:
    for root in nodes:
        for node in root.walk():
            if match(node, target):
                yield node


def find_in(nodes: Iterable[Node], target: Target, within: Node | None = None) -> Node | None:
    """The ``target.index``-th match in document order, searched inside ``within`` when given."""
    scope: Iterable[Node] = [within] if within is not None else nodes
    for i, node in enumerate(iter_matches(scope, target)):
        if i == target.index:
            return node
    return None


class CUBackend(ABC):
    """Accessibility-first driver for one display."""

    name: str = "abstract"

    def __init__(self, allow_pixels: bool = False, display: str | None = None) -> None:
        self.allow_pixels = bool(allow_pixels)
        self.display = display
        self.reason = ""

    @property
    def available(self) -> bool:
        return True

    def env(self) -> dict[str, str]:
        """Environment for processes launched on this backend's display."""
        return {"DISPLAY": self.display} if self.display else {}

    @abstractmethod
    def tree(self, window: str | None = None) -> list[Node]: ...

    def find(self, target: Target, within: Node | None = None) -> Node | None:
        return find_in(self.tree(), target, within=within)

    @abstractmethod
    def click(self, node: Node) -> None: ...

    @abstractmethod
    def invoke(self, node: Node, action: str) -> None: ...

    @abstractmethod
    def set_value(self, node: Node, value: str) -> None: ...

    @abstractmethod
    def type_text(self, node: Node | None, text: str) -> None: ...

    @abstractmethod
    def key(self, keys: str) -> None: ...

    @abstractmethod
    def screenshot(self) -> bytes: ...

    def focused_window(self) -> Node | None:
        for root in self.tree():
            for node in root.walk():
                if "active" in node.states or "focused" in node.states:
                    return node
        return None

    def pixel_click(self, x: int, y: int) -> None:
        raise BackendError(f"{self.name}: pixel click not implemented")

    def require_pixels(self, what: str) -> None:
        if not self.allow_pixels:
            raise BackendError(f"{what} needs a pixel action, which is disabled on this display (allow_pixels=False)")

    @staticmethod
    def tree_digest(tree: Iterable[Node]) -> str:
        """SHA-256 over the structural content of a tree (ids, roles, names, text, rects, states)."""
        payload = [n.model_dump() for n in tree]
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class UnavailableBackend(CUBackend):
    """Stands in when no platform backend can be imported. Every action fails with the reason."""

    name = "unavailable"

    def __init__(self, reason: str, allow_pixels: bool = False, display: str | None = None) -> None:
        super().__init__(allow_pixels=allow_pixels, display=display)
        self.reason = reason

    @property
    def available(self) -> bool:
        return False

    def tree(self, window: str | None = None) -> list[Node]:
        return []

    def _fail(self) -> None:
        raise BackendError(f"no computer-use backend: {self.reason}")

    def click(self, node: Node) -> None:
        self._fail()

    def invoke(self, node: Node, action: str) -> None:
        self._fail()

    def set_value(self, node: Node, value: str) -> None:
        self._fail()

    def type_text(self, node: Node | None, text: str) -> None:
        self._fail()

    def key(self, keys: str) -> None:
        self._fail()

    def screenshot(self) -> bytes:
        self._fail()
        return b""


def get_backend(display: str | None = None, allow_pixels: bool = False) -> CUBackend:
    """The platform backend for this OS. Never raises: import failures yield ``available=False``."""
    import sys

    try:
        if sys.platform.startswith("linux"):
            from .atspi import AtspiBackend

            return AtspiBackend(allow_pixels=allow_pixels, display=display)
        if sys.platform == "win32":
            from .uia import UiaBackend

            return UiaBackend(allow_pixels=allow_pixels, display=display)
        if sys.platform == "darwin":
            from .ax import AxBackend

            return AxBackend(allow_pixels=allow_pixels, display=display)
    except Exception as e:  # a backend module itself failed to import
        return UnavailableBackend(f"backend import failed: {e}", allow_pixels=allow_pixels, display=display)
    return UnavailableBackend(f"unsupported platform {sys.platform}", allow_pixels=allow_pixels, display=display)
