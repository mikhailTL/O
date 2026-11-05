"""macOS backend over the Accessibility API (``atomacos``).

``AXPress`` / ``AXSetValue`` first; pixel clicks and synthetic keystrokes via
``pyautogui`` only when ``allow_pixels`` is set. Screenshots use ``screencapture``.
macOS has no isolated display, so the engine normally builds this backend with
``allow_pixels=False``.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Any

from ..schema import Target
from .base import BackendError, CUBackend, Node, Rect, find_in

try:
    import atomacos  # type: ignore

    _IMPORT_ERROR: str | None = None
except Exception as _e:  # noqa: BLE001
    atomacos = None  # type: ignore
    _IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

MAX_DEPTH = 40
MAX_NODES = 20_000


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name)
    except Exception:  # noqa: BLE001
        return default


def _states(el: Any) -> list[str]:
    s: set[str] = set()
    if _attr(el, "AXFocused", False):
        s.add("focused")
    if _attr(el, "AXEnabled", True):
        s.add("enabled")
    if _attr(el, "AXSelected", False):
        s.add("selected")
    if _attr(el, "AXExpanded", False):
        s.add("expanded")
    if _attr(el, "AXMain", False) or _attr(el, "AXFocused", False):
        s.add("active")
    role = _attr(el, "AXRole", "") or ""
    val = _attr(el, "AXValue", None)
    if role in ("AXCheckBox", "AXRadioButton", "AXMenuItem") and val in (1, True):
        s.add("checked")
    if _attr(el, "AXHidden", False) is False:
        s.update(("visible", "showing"))
    return sorted(s)


class AxBackend(CUBackend):
    name = "ax"

    def __init__(self, allow_pixels: bool = False, display: str | None = None) -> None:
        super().__init__(allow_pixels=allow_pixels, display=display)
        self._handles: dict[str, Any] = {}
        self.reason = f"atomacos not importable: {_IMPORT_ERROR}" if _IMPORT_ERROR else ""

    @property
    def available(self) -> bool:
        return _IMPORT_ERROR is None

    def env(self) -> dict[str, str]:
        return {}

    def _require(self) -> None:
        if not self.available:
            raise BackendError(f"ax backend unavailable: {self.reason}")

    def tree(self, window: str | None = None) -> list[Node]:
        self._require()
        self._handles.clear()
        count = [0]
        roots: list[Node] = []
        try:
            from AppKit import NSWorkspace  # type: ignore

            apps = NSWorkspace.sharedWorkspace().runningApplications()
            pids = [a.processIdentifier() for a in apps if a.activationPolicy() == 0]
        except Exception:  # noqa: BLE001
            pids = []
        for i, pid in enumerate(pids):
            try:
                app = atomacos.getAppRefByPid(pid)
            except Exception:  # noqa: BLE001
                continue
            node = self._convert(app, f"p{pid}", 0, count)
            if node is None:
                continue
            if window and not any(_glob(w.name, window) for w in node.children):
                continue
            roots.append(node)
        return roots

    def _convert(self, el: Any, path: str, depth: int, count: list[int]) -> Node | None:
        if depth > MAX_DEPTH or count[0] >= MAX_NODES:
            return None
        count[0] += 1
        role = (_attr(el, "AXRole", "") or "")
        role = role[2:].lower() if role.startswith("AX") else role.lower()
        name = _attr(el, "AXTitle", "") or _attr(el, "AXDescription", "") or ""
        val = _attr(el, "AXValue", None)
        text = val if isinstance(val, str) else ""
        rect = None
        try:
            pos, size = _attr(el, "AXPosition"), _attr(el, "AXSize")
            if pos is not None and size is not None:
                rect = Rect(x=int(pos[0]), y=int(pos[1]), w=max(0, int(size[0])), h=max(0, int(size[1])))
        except Exception:  # noqa: BLE001
            pass
        children: list[Node] = []
        kids = _attr(el, "AXChildren", None) or []
        for i, kid in enumerate(list(kids)[:2000]):
            cn = self._convert(kid, f"{path}.{i}", depth + 1, count)
            if cn is not None:
                children.append(cn)
        node = Node(id=path, role=role, name=str(name), text=str(text), rect=rect, states=_states(el), children=children)
        self._handles[path] = el
        return node

    def find(self, target: Target, within: Node | None = None) -> Node | None:
        return find_in(self.tree(), target, within=within)

    def _handle(self, node: Node) -> Any | None:
        if node.synthetic:
            return None
        el = self._handles.get(node.id)
        if el is None:
            self.tree()
            el = self._handles.get(node.id)
        return el

    def click(self, node: Node) -> None:
        self._require()
        el = self._handle(node)
        if el is not None:
            try:
                actions = list(el.getActions() or [])
            except Exception:  # noqa: BLE001
                actions = []
            for want in ("Press", "Confirm", "Open", "Pick"):
                if want in actions:
                    getattr(el, want)()
                    return
        self.require_pixels("click")
        if node.rect is None:
            raise BackendError("click: node has no rect")
        self.pixel_click(*node.rect.center)

    def pixel_click(self, x: int, y: int) -> None:
        self.require_pixels("pixel click")
        _pyautogui().click(x, y)

    def invoke(self, node: Node, action: str) -> None:
        self._require()
        el = self._handle(node)
        if el is None:
            raise BackendError(f"invoke {action!r}: node {node.id!r} is not in the accessibility tree")
        a = action.strip()
        if a.lower() == "focus":
            try:
                el.AXFocused = True
                return
            except Exception as e:  # noqa: BLE001
                raise BackendError(f"invoke focus: {e}") from None
        canonical = {"click": "Press", "press": "Press", "activate": "Press", "invoke": "Press"}.get(a.lower(), a)
        if canonical.startswith("AX"):
            canonical = canonical[2:]
        try:
            actions = list(el.getActions() or [])
        except Exception:  # noqa: BLE001
            actions = []
        if canonical not in actions:
            raise BackendError(f"invoke {action!r}: element offers {actions or 'no actions'}")
        getattr(el, canonical)()

    def set_value(self, node: Node, value: str) -> None:
        self._require()
        el = self._handle(node)
        if el is None:
            raise BackendError(f"set_value: node {node.id!r} is not in the accessibility tree")
        try:
            el.AXValue = value
        except Exception as e:  # noqa: BLE001
            raise BackendError(f"set_value: AXSetValue failed: {e}") from None

    def type_text(self, node: Node | None, text: str) -> None:
        self._require()
        if node is not None:
            el = self._handle(node)
            if el is not None:
                current = _attr(el, "AXValue", None)
                if isinstance(current, str):
                    try:
                        el.AXValue = current + text
                        return
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    el.AXFocused = True
                except Exception:  # noqa: BLE001
                    pass
        self.require_pixels("type")
        _pyautogui().write(text, interval=0.02)

    def key(self, keys: str) -> None:
        self._require()
        self.require_pixels("key press")
        parts = [p.strip().lower().replace("cmd", "command") for p in keys.split("+") if p.strip()]
        _pyautogui().hotkey(*parts) if len(parts) > 1 else _pyautogui().press(parts[0])

    def screenshot(self) -> bytes:
        self._require()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            path = tmp.name
        try:
            proc = subprocess.run(["screencapture", "-x", "-t", "png", path], capture_output=True, timeout=30)
            if proc.returncode != 0:
                raise BackendError(f"screencapture failed: {proc.stderr.decode(errors='replace')[:200]}")
            with open(path, "rb") as f:
                return f.read()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


def _pyautogui() -> Any:
    try:
        import pyautogui  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise BackendError(f"pyautogui not importable: {e}") from None
    pyautogui.FAILSAFE = True
    return pyautogui


def _glob(name: str, pattern: str) -> bool:
    from fnmatch import fnmatchcase

    return fnmatchcase((name or "").lower(), pattern.lower())
