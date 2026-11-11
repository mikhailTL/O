"""macOS backend over the Accessibility API (``atomacos``, else pyobjc ApplicationServices directly).

``atomacos`` is used when it imports; otherwise ``ax_pyobjc`` (the same surface over pyobjc's HIServices
bindings). ``O_AX_IMPL=pyobjc`` forces the pyobjc path. The process that calls the AX API must be allowed in
System Settings > Privacy & Security > Accessibility (for O's engine that is O itself, or Terminal when O was
started from Terminal): ``available`` is False with ``ACCESSIBILITY_NEEDED`` as the reason until it is.

``AXPress`` / ``AXSetValue`` first; pixel clicks and synthetic keystrokes via
``pyautogui`` only when ``allow_pixels`` is set. Screenshots use ``screencapture``.
macOS has no isolated display, so the engine normally builds this backend with
``allow_pixels=False``.

``tree()`` is scoped to one application: the one named by ``app`` (a name, bundle id or pid) or, when none is
given, the frontmost one; it never walks every running process. A ``window`` glob filters the app's windows
before anything under them is converted. The last tree per (pid, window filter) is kept for
``TREE_CACHE_TTL_S`` so the executor's before / find / after / settle reads within one step reuse it, and every
public call holds the backend's lock: the slot-0 backend is shared by job threads and the HTTP routes, and a
concurrent ``/computer/tree`` must not clear the element handles a running step is about to click.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

from ..schema import Target
from .base import BackendError, CUBackend, Node, Rect, find_in

ACCESSIBILITY_NEEDED = "accessibility permission needed"
ACCESSIBILITY_HINT = ("Accessibility access is off for O. Turn it on in System Settings > Privacy & Security > "
                      "Accessibility (O, or Terminal if O was started from Terminal).")


def _load_impl() -> tuple[Any, str, str | None]:
    """(module, implementation name, import error). atomacos first unless O_AX_IMPL=pyobjc; then pyobjc."""
    errors: list[str] = []
    if os.environ.get("O_AX_IMPL", "").strip().lower() != "pyobjc":
        try:
            import atomacos as mod  # type: ignore

            return mod, "atomacos", None
        except Exception as e:  # noqa: BLE001
            errors.append(f"atomacos: {type(e).__name__}: {e}")
    try:
        from . import ax_pyobjc as mod  # type: ignore

        return mod, "pyobjc", None
    except Exception as e:  # noqa: BLE001
        errors.append(f"pyobjc ApplicationServices: {type(e).__name__}: {e}")
    return None, "", "; ".join(errors)


atomacos, AX_IMPL, _IMPORT_ERROR = _load_impl()


def accessibility_trusted() -> bool | None:
    """``AXIsProcessTrusted()`` for this process: True/False on macOS, None when it cannot be asked
    (another OS, or pyobjc ApplicationServices missing). Live: a grant made while O runs shows up at once."""
    if sys.platform != "darwin":
        return None
    try:
        from ApplicationServices import AXIsProcessTrusted  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    try:
        return bool(AXIsProcessTrusted())
    except Exception:  # noqa: BLE001
        return None

MAX_DEPTH = 40
MAX_NODES = 20_000
TREE_CACHE_TTL_S = 1.5
WINDOW_ROLE = "AXWindow"


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
        self.impl = AX_IMPL
        self._lock = threading.RLock()
        # (pid, window glob or None) -> (monotonic time, nodes, digest)
        self._cache: dict[tuple[int, str | None], tuple[float, list[Node], str]] = {}
        self.cache_ttl_s = TREE_CACHE_TTL_S
        self.walks = 0  # fresh AX walks performed (tests and diagnostics)
        self._clock = time.monotonic

    @property
    def trusted(self) -> bool | None:
        return accessibility_trusted()

    @property
    def available(self) -> bool:
        return _IMPORT_ERROR is None and self.trusted is not False

    @property
    def reason(self) -> str:
        if _IMPORT_ERROR:
            return f"accessibility backend not importable (re-run install/install.sh): {_IMPORT_ERROR}"
        if self.trusted is False:
            return ACCESSIBILITY_NEEDED
        return ""

    @reason.setter
    def reason(self, _value: str) -> None:  # CUBackend.__init__ assigns ""; the reason is always computed
        pass

    def env(self) -> dict[str, str]:
        return {}

    def _require(self) -> None:
        if not self.available:
            if self.reason == ACCESSIBILITY_NEEDED:
                raise BackendError(f"ax backend unavailable: {ACCESSIBILITY_NEEDED}. {ACCESSIBILITY_HINT}")
            raise BackendError(f"ax backend unavailable: {self.reason}")

    # ------------------------------------------------------------------ applications

    @staticmethod
    def _running_apps() -> list[Any]:
        """``NSRunningApplication`` rows of the regular (Dock) apps; [] when AppKit is not importable."""
        try:
            from AppKit import NSWorkspace  # type: ignore

            apps = NSWorkspace.sharedWorkspace().runningApplications()
            return [a for a in apps if a.activationPolicy() == 0]
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _frontmost_pid() -> int | None:
        try:
            from AppKit import NSWorkspace  # type: ignore

            front = NSWorkspace.sharedWorkspace().frontmostApplication()
            return int(front.processIdentifier()) if front is not None else None
        except Exception:  # noqa: BLE001
            return None

    def resolve_pid(self, app: str | int | None = None, pid: int | None = None) -> int | None:
        """The pid of the target application: ``pid``; else ``app`` as a pid, a (case-insensitive) localized name,
        a bundle identifier or an executable name; else the frontmost application. None when nothing matches."""
        if pid is not None:
            return int(pid)
        if isinstance(app, int):
            return app
        want = (str(app or "")).strip()
        if want.isdigit():
            return int(want)
        if want:
            low = want.lower()
            rows = self._running_apps()
            for attr in ("localizedName", "bundleIdentifier"):
                for a in rows:
                    val = _call(a, attr)
                    if isinstance(val, str) and val.lower() == low:
                        return int(a.processIdentifier())
            for a in rows:
                url = _call(a, "bundleURL")
                exe = _call(a, "executableURL")
                names = {str(_call(u, "lastPathComponent") or "").lower() for u in (url, exe) if u is not None}
                if low in names or f"{low}.app" in names:
                    return int(a.processIdentifier())
            for a in rows:
                val = _call(a, "localizedName")
                if isinstance(val, str) and low in val.lower():
                    return int(a.processIdentifier())
            return None
        return self._frontmost_pid()

    # ------------------------------------------------------------------ tree

    def tree(self, window: str | None = None, app: str | int | None = None, pid: int | None = None) -> list[Node]:
        """The accessibility tree of one application (see the module docstring); ``[]`` when none is found."""
        self._require()
        with self._lock:
            target = self.resolve_pid(app, pid)
            if target is None:
                return []
            node = self._app_tree(target, window or None)
            return [node] if node is not None else []

    def tree_all(self) -> list[Node]:
        """Every regular application (the slow path: dialog cleanup and ``focus_existing`` across apps)."""
        self._require()
        with self._lock:
            roots: list[Node] = []
            for a in self._running_apps():
                node = self._app_tree(int(a.processIdentifier()), None)
                if node is not None:
                    roots.append(node)
            return roots

    def _app_tree(self, pid: int, window: str | None) -> Node | None:
        key = (pid, window)
        hit = self._cache.get(key)
        now = self._clock()
        if hit is not None and now - hit[0] <= self.cache_ttl_s:
            return hit[1][0] if hit[1] else None
        try:
            app_el = atomacos.getAppRefByPid(pid)
        except Exception:  # noqa: BLE001
            return None
        self.walks += 1
        handles: dict[str, Any] = {}
        node = self._convert_app(app_el, pid, window, handles)
        if node is not None and window and not node.children:
            node = None  # the app has no window matching the glob
        # swap only this app's handles: another app's (or another window filter's) stay valid
        prefix = f"p{pid}."
        self._handles = {k: v for k, v in self._handles.items() if k != f"p{pid}" and not k.startswith(prefix)}
        self._handles.update(handles)
        nodes = [node] if node is not None else []
        self._cache[key] = (now, nodes, self.tree_digest(nodes))
        self._prune_cache(now)
        return node

    def _prune_cache(self, now: float) -> None:
        stale = [k for k, (t, _, _) in self._cache.items() if now - t > self.cache_ttl_s]
        for k in stale:
            del self._cache[k]

    def invalidate(self, pid: int | None = None) -> None:
        """Forget cached trees (all, or one app's) so the next read walks again; handles stay."""
        with self._lock:
            if pid is None:
                self._cache.clear()
            else:
                for k in [k for k in self._cache if k[0] == pid]:
                    del self._cache[k]

    def _convert_app(self, app_el: Any, pid: int, window: str | None, handles: dict[str, Any]) -> Node | None:
        count = [0]
        path = f"p{pid}"
        role = _role(app_el)
        name = _attr(app_el, "AXTitle", "") or _attr(app_el, "AXDescription", "") or ""
        kids = list(_attr(app_el, "AXChildren", None) or [])[:2000]
        children: list[Node] = []
        for i, kid in enumerate(kids):
            if window and _attr(kid, "AXRole", "") == WINDOW_ROLE:
                title = _attr(kid, "AXTitle", "") or _attr(kid, "AXDescription", "") or ""
                if not _glob(str(title), window):
                    continue  # filtered before anything under it is read
            elif window:
                continue  # with a window filter only the matching windows are walked (no menu bar)
            cn = self._convert(kid, f"{path}.{i}", 1, count, handles)
            if cn is not None:
                children.append(cn)
        node = Node(id=path, role=role or "application", name=str(name), text="", rect=None,
                    states=_states(app_el), children=children)
        handles[path] = app_el
        return node

    def _convert(self, el: Any, path: str, depth: int, count: list[int], handles: dict[str, Any] | None = None) -> Node | None:
        if handles is None:
            handles = self._handles
        if depth > MAX_DEPTH or count[0] >= MAX_NODES:
            return None
        count[0] += 1
        role = _role(el)
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
            cn = self._convert(kid, f"{path}.{i}", depth + 1, count, handles)
            if cn is not None:
                children.append(cn)
        node = Node(id=path, role=role, name=str(name), text=str(text), rect=rect, states=_states(el), children=children)
        handles[path] = el
        return node

    def find(self, target: Target, within: Node | None = None, app: str | int | None = None) -> Node | None:
        with self._lock:
            return find_in(self.tree(app=app), target, within=within)

    def focus_existing(self, app: str) -> str | None:
        """When ``app`` is running with a window, focus (raise) that window and return its name; else None."""
        from .base import WINDOW_ROLES

        with self._lock:
            roots = self.tree(app=app) if app else []
            for root in roots:
                wins = [c for c in root.children if c.role in WINDOW_ROLES]
                if not wins:
                    continue
                win = next((w for w in wins if "active" in w.states), wins[0])
                try:
                    self.focus_window(win)
                except BackendError:
                    pass
                return win.name or root.name
            return None

    def _handle(self, node: Node) -> Any | None:
        if node.synthetic:
            return None
        el = self._handles.get(node.id)
        if el is None:
            pid = _pid_of(node.id)
            if pid is not None:
                self.invalidate(pid)
                self._app_tree(pid, None)
            el = self._handles.get(node.id)
        return el

    def click(self, node: Node) -> None:
        with self._lock:
            self._click(node)

    def _click(self, node: Node) -> None:
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
        with self._lock:
            self._invoke(node, action)

    def _invoke(self, node: Node, action: str) -> None:
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
        with self._lock:
            self._set_value(node, value)

    def _set_value(self, node: Node, value: str) -> None:
        self._require()
        el = self._handle(node)
        if el is None:
            raise BackendError(f"set_value: node {node.id!r} is not in the accessibility tree")
        try:
            el.AXValue = value
        except Exception as e:  # noqa: BLE001
            raise BackendError(f"set_value: AXSetValue failed: {e}") from None

    def type_text(self, node: Node | None, text: str) -> None:
        with self._lock:
            self._type_text(node, text)

    def _type_text(self, node: Node | None, text: str) -> None:
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
        with self._lock:
            self._key(keys)

    def _key(self, keys: str) -> None:
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


def _role(el: Any) -> str:
    role = (_attr(el, "AXRole", "") or "")
    return role[2:].lower() if role.startswith("AX") else role.lower()


def _call(obj: Any, name: str) -> Any:
    """``obj.name()`` (pyobjc selectors are methods); None when missing or raising."""
    try:
        fn = getattr(obj, name)
        return fn() if callable(fn) else fn
    except Exception:  # noqa: BLE001
        return None


def _pid_of(node_id: str) -> int | None:
    head = node_id.split(".", 1)[0]
    if head.startswith("p") and head[1:].isdigit():
        return int(head[1:])
    return None


def _glob(name: str, pattern: str) -> bool:
    from fnmatch import fnmatchcase

    return fnmatchcase((name or "").lower(), pattern.lower())
