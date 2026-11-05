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
from collections import Counter
from dataclasses import dataclass
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


WINDOW_ROLES = frozenset({"frame", "dialog", "alert", "window", "file chooser"})
DIALOG_ROLES = frozenset({"dialog", "alert"})


@dataclass(frozen=True)
class KnownDialog:
    """A first-run / session-restore dialog that is dismissed through the accessibility tree when it appears.

    ``app`` and ``text`` are case-insensitive globs over the application name and over the dialog's
    whole text (its name plus every descendant's name and text); ``buttons`` are tried in order
    (accessible names, mnemonic underscores already stripped by the toolkit)."""

    label: str
    app: str
    text: str
    buttons: tuple[str, ...]


_DECLINE = ("don't restore", "do not restore", "start new session", "no", "not now", "skip", "no thanks",
            "dismiss", "cancel", "close")

KNOWN_DIALOGS: tuple[KnownDialog, ...] = (
    # Mousepad: shown when a second instance finds the first one's session backup (real run: it covered the editor).
    KnownDialog("mousepad session restore", "mousepad*", "*previous session did not end normally*", _DECLINE),
    KnownDialog("session restore", "*", "*restore*session*", _DECLINE),
    KnownDialog("session restore", "*", "*session*restore*", _DECLINE),
    KnownDialog("crash recovery", "*", "*recover*unsaved*", _DECLINE),
    KnownDialog("first run", "*", "*welcome to *", ("close", "skip", "not now", "no thanks", "dismiss", "ok")),
    KnownDialog("first run", "*", "*tip of the day*", ("close", "ok")),
)


MODAL_ROLES = frozenset({"dialog", "alert", "file chooser"})


def is_open_modal(window: Node) -> bool:
    """A dialog-like top-level window that is on screen and modal: it takes the input of the app it belongs to."""
    return (window.role in MODAL_ROLES and "modal" in window.states
            and ("showing" in window.states or "visible" in window.states))


def _window_rank(window: Node) -> int:
    if is_open_modal(window):
        return 0  # a just-mapped Save As / alert owns the input even before the window manager activates it
    return 1 if "active" in window.states else 2


def focused_roots(roots: list[Node]) -> list[Node]:
    """Only the application that owns the active window, with its open modal dialogs first, then the active
    window, then its other windows. An open modal dialog of another application (a portal file chooser, a
    second process' alert) is a root of its own, placed first. Unchanged when no window is active (no window
    manager): then every application is returned in the platform's order."""
    for i, app in enumerate(roots):
        if any("active" in w.states for w in app.children):
            kids = sorted(app.children, key=_window_rank)
            others = []
            for j, other in enumerate(roots):
                modal = [w for w in other.children if is_open_modal(w)] if j != i else []
                if modal:
                    others.append(other.model_copy(update={"children": modal}))
            return [*others, app.model_copy(update={"children": kids})]
    return list(roots)


# Buttons that leave the document as it was: never "No"/"Don't save" (they would discard edits).
_CLOSE_BUTTONS = ("cancel", "close", "ok", "dismiss")


def _is_open_dialog(window: Node) -> bool:
    return window.role in MODAL_ROLES and ("showing" in window.states or "visible" in window.states)


def dialog_text(node: Node) -> str:
    return " ".join(f"{n.name} {n.text}" for n in node.walk()).lower()


def _button_name(node: Node) -> str:
    return node.name.replace("_", "").strip().lower()


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

    def tree_all(self) -> list[Node]:
        """Every application on the display (``tree()`` may narrow to the focused one)."""
        return self.tree()

    def focus_window(self, node: Node) -> None:
        """Bring one top-level window to the front and give it focus."""
        self.invoke(node, "focus")

    def focus_existing(self, app: str) -> str | None:
        """When ``app`` already has a window on this display, focus it and return its name; else None."""
        want = app.lower()
        for root in self.tree_all():
            if root.role != "application" or root.name.lower() != want:
                continue
            wins = [c for c in root.children if c.role in WINDOW_ROLES]
            if not wins:
                continue
            win = next((w for w in wins if "active" in w.states), wins[0])
            try:
                self.focus_window(win)
            except BackendError:
                pass  # already the only window; a refused focus is not a reason to start a second instance
            return win.name or root.name
        return None

    def dismiss_known_dialogs(self, nodes: list[Node] | None = None) -> list[str]:
        """Click the decline button of every ``KNOWN_DIALOGS`` match in ``nodes`` (default: the whole display).
        Returns one line per dismissed dialog; a dialog without a matching button is left alone."""
        done: list[str] = []
        for root in (nodes if nodes is not None else self.tree_all()):
            app = root.name.lower() if root.role == "application" else ""
            for node in root.walk():
                if node.role not in DIALOG_ROLES:
                    continue
                text = dialog_text(node)
                for known in KNOWN_DIALOGS:
                    if not (fnmatchcase(app, known.app) and fnmatchcase(text, known.text)):
                        continue
                    buttons = {_button_name(b): b for b in node.walk() if b.role in ("push button", "button")}
                    pick = next((buttons[b] for b in known.buttons if b in buttons), None)
                    if pick is None:
                        continue
                    try:
                        self.click(pick)
                    except BackendError as e:
                        done.append(f"{known.label}: could not click {pick.name!r} ({e})")
                        break
                    done.append(f"{known.label} ({root.name or '?'}): clicked {pick.name!r}")
                    break
        return done

    def open_dialogs(self) -> list[tuple[str, str, str]]:
        """``(app, role, name)`` of every dialog / alert / file chooser on screen, across all applications."""
        out: list[tuple[str, str, str]] = []
        for root in self.tree_all():
            app = root.name if root.role == "application" else ""
            for win in (root.children if root.role == "application" else [root]):
                if _is_open_dialog(win):
                    out.append((app, win.role, win.name))
        return out

    def close_dialogs(self, keep: Iterable[tuple[str, str, str]] = (), max_rounds: int = 6,
                      settle: float = 0.4) -> list[str]:
        """Close every open dialog that is not in ``keep`` (a multiset of ``open_dialogs()`` rows), topmost
        first: its Cancel / Close / OK button through the a11y tree, else Escape on it (pixels only).
        One dialog per round, then the tree is read again (an error alert sits over the Save As it came from).
        Returns one line per dialog acted on."""
        import time as _time

        done: list[str] = []
        stuck: set[tuple[str, str, str]] = set()
        for _ in range(max(1, max_rounds)):
            budget = Counter(keep)
            pick: tuple[str, Node] | None = None
            for root in self.tree_all():
                app = root.name if root.role == "application" else ""
                wins = [w for w in (root.children if root.role == "application" else [root]) if _is_open_dialog(w)]
                # alerts before the dialogs they sit on
                for win in sorted(wins, key=lambda w: 0 if w.role == "alert" else 1):
                    key = (app, win.role, win.name)
                    if budget[key] > 0:
                        budget[key] -= 1
                        continue
                    if key in stuck:
                        continue
                    pick = (app, win)
                    break
                if pick:
                    break
            if pick is None:
                break
            app, win = pick
            key = (app, win.role, win.name)
            buttons = {_button_name(b): b for b in win.walk() if b.role in ("push button", "button")}
            button = next((buttons[b] for b in _CLOSE_BUTTONS if b in buttons), None)
            try:
                if button is not None:
                    self.click(button)
                    done.append(f"{win.role} {win.name!r} ({app or '?'}): clicked {button.name!r}")
                else:
                    try:
                        self.focus_window(win)
                    except BackendError:
                        pass
                    self.key("Escape")
                    done.append(f"{win.role} {win.name!r} ({app or '?'}): pressed Escape")
            except BackendError as e:
                done.append(f"{win.role} {win.name!r} ({app or '?'}): could not close ({e})")
                stuck.add(key)
            if settle:
                _time.sleep(settle)
        return done

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
