"""Windows backend over UI Automation (``uiautomation`` package).

Invoke / Value / Toggle / SelectionItem patterns first; pixel clicks and synthetic
keystrokes (``pyautogui``) only when ``allow_pixels`` is set. There is no isolated
display on Windows, so the engine normally constructs this backend with
``allow_pixels=False`` and every pixel request becomes a structured failure.
"""
from __future__ import annotations

import io
from typing import Any

from ..schema import Target
from .base import BackendError, CUBackend, Node, Rect, find_in

try:
    import uiautomation as uia  # type: ignore

    _IMPORT_ERROR: str | None = None
except Exception as _e:  # noqa: BLE001
    uia = None  # type: ignore
    _IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

MAX_DEPTH = 40
MAX_NODES = 20_000


def _states(ctrl: Any) -> list[str]:
    s: set[str] = set()
    try:
        if not ctrl.IsOffscreen:
            s.update(("visible", "showing"))
        if ctrl.IsEnabled:
            s.add("enabled")
        if ctrl.HasKeyboardFocus:
            s.add("focused")
        if ctrl.IsKeyboardFocusable:
            s.add("focusable")
    except Exception:  # noqa: BLE001
        pass
    try:
        tp = ctrl.GetTogglePattern()
        if tp is not None and tp.ToggleState == 1:
            s.add("checked")
    except Exception:  # noqa: BLE001
        pass
    try:
        sp = ctrl.GetSelectionItemPattern()
        if sp is not None and sp.IsSelected:
            s.add("selected")
    except Exception:  # noqa: BLE001
        pass
    try:
        ep = ctrl.GetExpandCollapsePattern()
        if ep is not None and ep.ExpandCollapseState == 1:
            s.add("expanded")
    except Exception:  # noqa: BLE001
        pass
    return sorted(s)


class UiaBackend(CUBackend):
    name = "uia"

    def __init__(self, allow_pixels: bool = False, display: str | None = None) -> None:
        super().__init__(allow_pixels=allow_pixels, display=display)
        self._handles: dict[str, Any] = {}
        self.reason = f"uiautomation not importable: {_IMPORT_ERROR}" if _IMPORT_ERROR else ""

    @property
    def available(self) -> bool:
        return _IMPORT_ERROR is None

    def env(self) -> dict[str, str]:
        return {}

    def _require(self) -> None:
        if not self.available:
            raise BackendError(f"uia backend unavailable: {self.reason}")

    def tree(self, window: str | None = None) -> list[Node]:
        self._require()
        self._handles.clear()
        root = uia.GetRootControl()
        count = [0]
        roots: list[Node] = []
        for i, top in enumerate(root.GetChildren()):
            node = self._convert(top, f"w{i}", 0, count)
            if node is None:
                continue
            if window and not _glob(node.name, window):
                continue
            roots.append(node)
        return roots

    def _convert(self, ctrl: Any, path: str, depth: int, count: list[int]) -> Node | None:
        if depth > MAX_DEPTH or count[0] >= MAX_NODES:
            return None
        count[0] += 1
        try:
            role = (ctrl.ControlTypeName or "").replace("Control", "").lower()
            name = ctrl.Name or ""
        except Exception:  # noqa: BLE001
            return None
        text = ""
        try:
            vp = ctrl.GetValuePattern()
            if vp is not None:
                text = vp.Value or ""
        except Exception:  # noqa: BLE001
            pass
        rect = None
        try:
            r = ctrl.BoundingRectangle
            rect = Rect(x=int(r.left), y=int(r.top), w=max(0, int(r.width())), h=max(0, int(r.height())))
        except Exception:  # noqa: BLE001
            pass
        children: list[Node] = []
        try:
            kids = ctrl.GetChildren()
        except Exception:  # noqa: BLE001
            kids = []
        for i, kid in enumerate(kids[:2000]):
            cn = self._convert(kid, f"{path}.{i}", depth + 1, count)
            if cn is not None:
                children.append(cn)
        node = Node(id=path, role=role, name=name, text=text, rect=rect, states=_states(ctrl), children=children)
        self._handles[path] = ctrl
        return node

    def find(self, target: Target, within: Node | None = None) -> Node | None:
        return find_in(self.tree(), target, within=within)

    def _handle(self, node: Node) -> Any | None:
        if node.synthetic:
            return None
        ctrl = self._handles.get(node.id)
        if ctrl is None:
            self.tree()
            ctrl = self._handles.get(node.id)
        return ctrl

    def click(self, node: Node) -> None:
        self._require()
        ctrl = self._handle(node)
        if ctrl is not None:
            for getter in ("GetInvokePattern", "GetTogglePattern", "GetSelectionItemPattern", "GetExpandCollapsePattern"):
                try:
                    pat = getattr(ctrl, getter)()
                except Exception:  # noqa: BLE001
                    pat = None
                if pat is None:
                    continue
                if getter == "GetInvokePattern":
                    pat.Invoke()
                elif getter == "GetTogglePattern":
                    pat.Toggle()
                elif getter == "GetSelectionItemPattern":
                    pat.Select()
                else:
                    pat.Expand() if pat.ExpandCollapseState != 1 else pat.Collapse()
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
        ctrl = self._handle(node)
        if ctrl is None:
            raise BackendError(f"invoke {action!r}: node {node.id!r} is not in the accessibility tree")
        a = action.lower()
        if a in ("focus",):
            ctrl.SetFocus()
            return
        if a in ("click", "press", "activate", "invoke"):
            pat = ctrl.GetInvokePattern()
            if pat is None:
                raise BackendError(f"invoke {action!r}: no Invoke pattern")
            pat.Invoke()
            return
        if a == "toggle":
            pat = ctrl.GetTogglePattern()
            if pat is None:
                raise BackendError("invoke toggle: no Toggle pattern")
            pat.Toggle()
            return
        if a == "select":
            pat = ctrl.GetSelectionItemPattern()
            if pat is None:
                raise BackendError("invoke select: no SelectionItem pattern")
            pat.Select()
            return
        if a in ("expand", "collapse"):
            pat = ctrl.GetExpandCollapsePattern()
            if pat is None:
                raise BackendError(f"invoke {action!r}: no ExpandCollapse pattern")
            pat.Expand() if a == "expand" else pat.Collapse()
            return
        raise BackendError(f"invoke {action!r}: unknown action")

    def set_value(self, node: Node, value: str) -> None:
        self._require()
        ctrl = self._handle(node)
        if ctrl is None:
            raise BackendError(f"set_value: node {node.id!r} is not in the accessibility tree")
        vp = ctrl.GetValuePattern()
        if vp is None:
            rp = ctrl.GetRangeValuePattern()
            if rp is None:
                raise BackendError("set_value: element has neither Value nor RangeValue pattern")
            try:
                rp.SetValue(float(value))
            except ValueError:
                raise BackendError(f"set_value: {value!r} is not numeric for a RangeValue element") from None
            return
        vp.SetValue(value)

    def type_text(self, node: Node | None, text: str) -> None:
        self._require()
        if node is not None:
            ctrl = self._handle(node)
            if ctrl is not None:
                vp = ctrl.GetValuePattern()
                if vp is not None:
                    vp.SetValue((vp.Value or "") + text)
                    return
                ctrl.SetFocus()
        self.require_pixels("type")
        _pyautogui().write(text, interval=0.02)

    def key(self, keys: str) -> None:
        self._require()
        self.require_pixels("key press")
        parts = [p.strip().lower() for p in keys.split("+") if p.strip()]
        _pyautogui().hotkey(*parts) if len(parts) > 1 else _pyautogui().press(parts[0])

    def screenshot(self) -> bytes:
        self._require()
        img = _pyautogui(need_pixels=False).screenshot()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


def _pyautogui(need_pixels: bool = True) -> Any:
    try:
        import pyautogui  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise BackendError(f"pyautogui not importable: {e}") from None
    if need_pixels:
        pyautogui.FAILSAFE = True
    return pyautogui


def _glob(name: str, pattern: str) -> bool:
    from fnmatch import fnmatchcase

    return fnmatchcase((name or "").lower(), pattern.lower())
