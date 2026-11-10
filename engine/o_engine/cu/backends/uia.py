"""Windows backend over UI Automation (``uiautomation`` package) with a real pixel path.

Windows has no isolated display and no TCC-style prompts: the host desktop IS the working display, so the
engine constructs this backend with ``allow_pixels=True`` (``server.py: cu_backend``). The order of
preference never changes:

1. UIA patterns first: Invoke / Toggle / SelectionItem / ExpandCollapse for clicks, Value / RangeValue for
   text, ``SetFocus`` for focus.
2. Pixels when a pattern is missing (and only when ``allow_pixels`` is set): ``pyautogui`` moves the pointer,
   clicks, double/right clicks, drags, scrolls, types and sends key chords; ``pyautogui.FAILSAFE`` is off
   because the pointer legitimately visits the screen corners on a multi-monitor desktop.
3. Screenshots through ``mss`` (whole virtual screen, every monitor), falling back to ``PIL.ImageGrab``.

Coordinates. The process is made per-monitor DPI aware (``shcore.SetProcessDpiAwareness(2)``, else
``user32.SetProcessDPIAware``) before the first UIA or pixel call, so ``BoundingRectangle`` and the pointer
share one physical-pixel space. Every rect the backend hands out is translated into *screenshot space*:
the top-left of the virtual screen (which is negative when a monitor sits left of or above the primary one)
becomes (0, 0), exactly like the PNG ``screenshot()`` returns. Pixel actions translate back. A vision bbox,
a UIA rect and a click therefore all agree.

Optional packages are never required at import time: the backend reports ``available=False`` with the
reason when ``uiautomation`` is missing, and every action turns a missing ``pyautogui`` / ``mss`` into a
``BackendError`` (the executor turns it into a failed step).
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from fnmatch import fnmatchcase
from typing import Any

from ..schema import Target
from .base import BackendError, CUBackend, Node, Rect, find_in

try:
    import uiautomation as uia  # type: ignore

    _IMPORT_ERROR: str | None = None
except Exception as _e:  # noqa: BLE001
    uia = None  # type: ignore
    _IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

import threading as _threading

_COM_READY = _threading.local()


def _com_init_this_thread() -> None:
    """UI Automation is COM: every thread that touches it needs COM initialised once. The server answers
    requests on a thread pool, so the import-time initialisation of the main thread does not cover them
    (symptom: 'CoInitialize has not been called' -> HTTP 500 on /computer/tree). Idempotent per thread."""
    if getattr(_COM_READY, "done", False) or uia is None:
        return
    try:
        init = getattr(uia, "InitializeUIAutomationInCurrentThread", None)
        if callable(init):
            init()
        else:  # older uiautomation: initialise COM directly
            import comtypes  # type: ignore

            try:
                comtypes.CoInitializeEx(getattr(comtypes, "COINIT_APARTMENTTHREADED", 2))
            except OSError:  # already initialised with another model on this thread: fine
                pass
    except Exception:  # noqa: BLE001 - a failure here surfaces on the first real call with its own error
        pass
    _COM_READY.done = True


MAX_DEPTH = 40
MAX_NODES = 20_000
MAX_CHILDREN = 2000
TYPE_INTERVAL_S = 0.02
DRAG_DURATION_S = 0.4

# UIA ControlTypeName (minus "Control", lowercased) -> the AT-SPI style role names the planner, the judge and
# ``base.py`` (``WINDOW_ROLES``, dialog button lookup) already speak. Unlisted types keep the UIA name.
ROLE_MAP: dict[str, str] = {
    "window": "frame",
    "pane": "panel",
    "button": "push button",
    "splitbutton": "push button",
    "checkbox": "check box",
    "radiobutton": "radio button",
    "combobox": "combo box",
    "edit": "text",
    "text": "label",
    "listitem": "list item",
    "menuitem": "menu item",
    "menubar": "menu bar",
    "treeitem": "tree item",
    "tabitem": "page tab",
    "tab": "page tab list",
    "hyperlink": "link",
    "scrollbar": "scroll bar",
    "toolbar": "tool bar",
    "statusbar": "status bar",
    "titlebar": "title bar",
    "dataitem": "table cell",
    "datagrid": "table",
    "progressbar": "progress bar",
    "spinner": "spin button",
    "tooltip": "tool tip",
    "appbar": "tool bar",
    "semanticzoom": "panel",
}

# Key names the planner emits (and X11/macOS spellings) -> pyautogui key names.
KEY_ALIASES: dict[str, str] = {
    "control": "ctrl", "cmd": "win", "command": "win", "super": "win", "meta": "win", "windows": "win",
    "option": "alt", "return": "enter", "esc": "escape", "del": "delete", "ins": "insert",
    "pgup": "pageup", "pgdn": "pagedown", "page_up": "pageup", "page_down": "pagedown",
    "spacebar": "space", "backspace": "backspace", "back_space": "backspace",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
    "plus": "+", "minus": "-", "comma": ",", "period": ".", "slash": "/",
}

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

_dpi_state: dict[str, str] = {}


def _windll() -> Any | None:
    """``ctypes.windll`` on Windows, None elsewhere (every Win32 call below is skipped off-platform)."""
    if sys.platform != "win32":
        return None
    import ctypes

    return getattr(ctypes, "windll", None)


def ensure_dpi_aware() -> str:
    """Make this process per-monitor DPI aware once, so UIA rects and pointer coordinates are both physical
    pixels. Returns a short description of what took effect (``"n/a"`` off Windows)."""
    if "mode" in _dpi_state:
        return _dpi_state["mode"]
    dll = _windll()
    mode = "n/a"
    if dll is not None:
        try:
            hr = dll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
            mode = "per-monitor" if hr == 0 else "already set"
        except Exception:  # noqa: BLE001 - shcore is Windows 8.1+; older or restricted hosts fall through
            try:
                dll.user32.SetProcessDPIAware()
                mode = "system"
            except Exception:  # noqa: BLE001
                mode = "unaware"
    _dpi_state["mode"] = mode
    return mode


def virtual_screen() -> tuple[int, int, int, int]:
    """``(left, top, width, height)`` of the virtual screen spanning every monitor; (0, 0, 0, 0) off Windows."""
    dll = _windll()
    if dll is None:
        return 0, 0, 0, 0
    try:
        m = dll.user32.GetSystemMetrics
        return int(m(SM_XVIRTUALSCREEN)), int(m(SM_YVIRTUALSCREEN)), int(m(SM_CXVIRTUALSCREEN)), int(m(SM_CYVIRTUALSCREEN))
    except Exception:  # noqa: BLE001
        return 0, 0, 0, 0


def process_name(pid: int) -> str:
    """Executable base name (lowercase, without ``.exe``) of ``pid``; "" when it cannot be read."""
    dll = _windll()
    if dll is None or not pid:
        return ""
    import ctypes

    try:
        handle = dll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return ""
        try:
            size = ctypes.c_ulong(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if not dll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return _exe_base(buf.value)
        finally:
            dll.kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return ""


def _exe_base(path: str) -> str:
    base = os.path.basename(str(path or "")).lower()
    return base[:-4] if base.endswith(".exe") else base


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
    for getter, check, state in (
        ("GetTogglePattern", lambda p: p.ToggleState == 1, "checked"),
        ("GetSelectionItemPattern", lambda p: bool(p.IsSelected), "selected"),
        ("GetExpandCollapsePattern", lambda p: p.ExpandCollapseState == 1, "expanded"),
        ("GetWindowPattern", lambda p: bool(p.IsModal), "modal"),
    ):
        try:
            pat = getattr(ctrl, getter)()
            if pat is not None and check(pat):
                s.add(state)
        except Exception:  # noqa: BLE001
            pass
    return s and sorted(s) or []


def _pattern(ctrl: Any, getter: str) -> Any | None:
    try:
        return getattr(ctrl, getter)()
    except Exception:  # noqa: BLE001
        return None


class UiaBackend(CUBackend):
    name = "uia"

    def __init__(self, allow_pixels: bool = False, display: str | None = None) -> None:
        super().__init__(allow_pixels=allow_pixels, display=display)
        self._handles: dict[str, Any] = {}
        self._origin: tuple[int, int] = (0, 0)
        self.reason = f"uiautomation not importable: {_IMPORT_ERROR}" if _IMPORT_ERROR else ""
        self.dpi_mode = ensure_dpi_aware() if self.available else "n/a"

    @property
    def available(self) -> bool:
        return _IMPORT_ERROR is None

    def env(self) -> dict[str, str]:
        return {}

    def _require(self) -> None:
        if not self.available:
            raise BackendError(f"uia backend unavailable: {self.reason}")
        _com_init_this_thread()

    # ------------------------------------------------------------------ coordinates

    def _refresh_origin(self) -> None:
        left, top, _, _ = virtual_screen()
        self._origin = (left, top)

    def to_shot(self, x: int, y: int) -> tuple[int, int]:
        """Screen (virtual desktop) coordinates -> screenshot coordinates."""
        return int(x) - self._origin[0], int(y) - self._origin[1]

    def to_screen(self, x: int, y: int) -> tuple[int, int]:
        """Screenshot coordinates -> screen (virtual desktop) coordinates, what the pointer takes."""
        return int(x) + self._origin[0], int(y) + self._origin[1]

    # ------------------------------------------------------------------ tree

    def tree(self, window: str | None = None) -> list[Node]:
        """One ``application`` root per process (named after its executable), each holding that process'
        top-level windows; ``window`` narrows to windows whose title matches the glob."""
        self._require()
        self._handles.clear()
        self._refresh_origin()
        root = uia.GetRootControl()
        count = [0]
        foreground = self._foreground_handle()
        apps: dict[int, tuple[str, list[Node]]] = {}
        order: list[int] = []
        try:
            tops = list(root.GetChildren())
        except Exception as e:  # noqa: BLE001
            raise BackendError(f"uia: cannot read the desktop: {e}") from None
        for i, top in enumerate(tops):
            node = self._convert(top, f"w{i}", 0, count, foreground=foreground)
            if node is None:
                continue
            if window and not _glob(node.name, window):
                continue
            pid = 0
            try:
                pid = int(top.ProcessId or 0)
            except Exception:  # noqa: BLE001
                pass
            if pid not in apps:
                apps[pid] = (process_name(pid) or node.name or f"pid {pid}", [])
                order.append(pid)
            apps[pid][1].append(node)
        roots: list[Node] = []
        for pid in order:
            app_name, wins = apps[pid]
            roots.append(Node(id=f"app{pid}", role="application", name=app_name, states=["enabled"], children=wins))
        return roots

    def _foreground_handle(self) -> int:
        try:
            return int(uia.GetForegroundWindow() or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _convert(self, ctrl: Any, path: str, depth: int, count: list[int], foreground: int = 0) -> Node | None:
        if depth > MAX_DEPTH or count[0] >= MAX_NODES:
            return None
        count[0] += 1
        try:
            raw = (ctrl.ControlTypeName or "").replace("Control", "").lower()
            name = ctrl.Name or ""
        except Exception:  # noqa: BLE001
            return None
        role = ROLE_MAP.get(raw, raw)
        text = ""
        vp = _pattern(ctrl, "GetValuePattern")
        if vp is not None:
            try:
                text = vp.Value or ""
            except Exception:  # noqa: BLE001
                pass
        rect = None
        try:
            r = ctrl.BoundingRectangle
            x, y = self.to_shot(int(r.left), int(r.top))
            rect = Rect(x=x, y=y, w=max(0, int(r.width())), h=max(0, int(r.height())))
        except Exception:  # noqa: BLE001
            pass
        states = _states(ctrl)
        if depth == 0:
            if "modal" in states and role in ("frame", "window"):
                role = "dialog"
            try:
                if foreground and int(ctrl.NativeWindowHandle or 0) == foreground:
                    states = sorted({*states, "active"})
            except Exception:  # noqa: BLE001
                pass
        children: list[Node] = []
        try:
            kids = ctrl.GetChildren()
        except Exception:  # noqa: BLE001
            kids = []
        for i, kid in enumerate(kids[:MAX_CHILDREN]):
            cn = self._convert(kid, f"{path}.{i}", depth + 1, count)
            if cn is not None:
                children.append(cn)
        node = Node(id=path, role=role, name=name, text=text, rect=rect, states=states, children=children)
        self._handles[path] = ctrl
        return node

    def find(self, target: Target, within: Node | None = None) -> Node | None:
        return find_in(self.tree(), target, within=within)

    def _handle(self, node: Node) -> Any | None:
        if node.synthetic or node.role == "application":
            return None
        ctrl = self._handles.get(node.id)
        if ctrl is None:
            self.tree()
            ctrl = self._handles.get(node.id)
        return ctrl

    # ------------------------------------------------------------------ actions (patterns first, pixels second)

    def _pixel_fallback(self, node: Node, what: str, action: str = "click") -> None:
        """A pattern was missing: act on the node's center with the pointer, or explain why that is refused."""
        self.require_pixels(what)
        if node.rect is None:
            raise BackendError(f"{what}: node {node.id!r} has no rect to fall back to")
        x, y = node.rect.center
        if action == "double":
            self.pixel_double_click(x, y)
        elif action == "right":
            self.pixel_right_click(x, y)
        else:
            self.pixel_click(x, y)

    def click(self, node: Node) -> None:
        self._require()
        ctrl = self._handle(node)
        if ctrl is not None:
            pat = _pattern(ctrl, "GetInvokePattern")
            if pat is not None:
                pat.Invoke()
                return
            pat = _pattern(ctrl, "GetTogglePattern")
            if pat is not None:
                pat.Toggle()
                return
            pat = _pattern(ctrl, "GetSelectionItemPattern")
            if pat is not None:
                pat.Select()
                return
            pat = _pattern(ctrl, "GetExpandCollapsePattern")
            if pat is not None:
                pat.Expand() if pat.ExpandCollapseState != 1 else pat.Collapse()
                return
        self._pixel_fallback(node, "click")

    def double_click(self, node: Node) -> None:
        self._require()
        self._pixel_fallback(node, "double click", "double")

    def right_click(self, node: Node) -> None:
        self._require()
        self._pixel_fallback(node, "right click", "right")

    def invoke(self, node: Node, action: str) -> None:
        self._require()
        ctrl = self._handle(node)
        a = action.lower()
        if ctrl is None:
            if a in ("click", "press", "activate", "invoke", "toggle", "select", "focus"):
                self._pixel_fallback(node, f"invoke {action!r}")
                return
            raise BackendError(f"invoke {action!r}: node {node.id!r} is not in the accessibility tree")
        if a == "focus":
            try:
                ctrl.SetFocus()
                return
            except Exception:  # noqa: BLE001
                self._pixel_fallback(node, "invoke 'focus'")
                return
        if a in ("click", "press", "activate", "invoke"):
            pat = _pattern(ctrl, "GetInvokePattern")
            if pat is not None:
                pat.Invoke()
                return
            self._pixel_fallback(node, f"invoke {action!r} (no Invoke pattern)")
            return
        if a == "toggle":
            pat = _pattern(ctrl, "GetTogglePattern")
            if pat is not None:
                pat.Toggle()
                return
            self._pixel_fallback(node, "invoke toggle (no Toggle pattern)")
            return
        if a == "select":
            pat = _pattern(ctrl, "GetSelectionItemPattern")
            if pat is not None:
                pat.Select()
                return
            self._pixel_fallback(node, "invoke select (no SelectionItem pattern)")
            return
        if a in ("expand", "collapse"):
            pat = _pattern(ctrl, "GetExpandCollapsePattern")
            if pat is not None:
                pat.Expand() if a == "expand" else pat.Collapse()
                return
            self._pixel_fallback(node, f"invoke {action!r} (no ExpandCollapse pattern)")
            return
        if a in ("double_click", "doubleclick", "open"):
            self._pixel_fallback(node, f"invoke {action!r}", "double")
            return
        if a in ("right_click", "context", "context_menu"):
            self._pixel_fallback(node, f"invoke {action!r}", "right")
            return
        if a in ("close", "minimize", "maximize", "restore"):
            self._window_action(ctrl, node, a)
            return
        raise BackendError(f"invoke {action!r}: unknown action")

    def _window_action(self, ctrl: Any, node: Node, action: str) -> None:
        pat = _pattern(ctrl, "GetWindowPattern")
        if pat is not None:
            try:
                if action == "close":
                    pat.Close()
                else:
                    pat.SetWindowVisualState({"minimize": 2, "maximize": 1, "restore": 0}[action])
                return
            except Exception:  # noqa: BLE001
                pass
        if action == "close":
            self.focus_window(node)
            self.key("alt+F4")
            return
        raise BackendError(f"invoke {action!r}: window has no Window pattern")

    def set_value(self, node: Node, value: str) -> None:
        self._require()
        ctrl = self._handle(node)
        if ctrl is not None:
            vp = _pattern(ctrl, "GetValuePattern")
            if vp is not None:
                try:
                    vp.SetValue(value)
                    return
                except Exception:  # noqa: BLE001 - read-only Value pattern: fall through to typing
                    pass
            rp = _pattern(ctrl, "GetRangeValuePattern")
            if rp is not None:
                try:
                    rp.SetValue(float(value))
                except ValueError:
                    raise BackendError(f"set_value: {value!r} is not numeric for a RangeValue element") from None
                return
        # No settable pattern: click into the field, select everything, type the replacement.
        self.require_pixels("set_value (element has neither Value nor RangeValue pattern)")
        if node.rect is None:
            raise BackendError(f"set_value: node {node.id!r} has no rect to fall back to")
        self.pixel_click(*node.rect.center)
        self.key("ctrl+a")
        self._send_text(value)

    def type_text(self, node: Node | None, text: str) -> None:
        self._require()
        if node is not None:
            ctrl = self._handle(node)
            if ctrl is not None:
                vp = _pattern(ctrl, "GetValuePattern")
                if vp is not None:
                    try:
                        vp.SetValue((vp.Value or "") + text)
                        return
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    ctrl.SetFocus()
                except Exception:  # noqa: BLE001
                    if node.rect is not None and self.allow_pixels:
                        self.pixel_click(*node.rect.center)
            elif node.rect is not None:
                self.require_pixels("type")
                self.pixel_click(*node.rect.center)
        self.require_pixels("type")
        self._send_text(text)

    def _send_text(self, text: str) -> None:
        if not text:
            return
        if text.isascii():
            _pyautogui().write(text, interval=TYPE_INTERVAL_S)
            return
        _send_unicode(text)

    def key(self, keys: str) -> None:
        self._require()
        self.require_pixels("key press")
        parts = parse_chord(keys)
        if not parts:
            raise BackendError(f"key: empty chord {keys!r}")
        pg = _pyautogui()
        pg.hotkey(*parts) if len(parts) > 1 else pg.press(parts[0])

    # ------------------------------------------------------------------ pixels

    def pixel_click(self, x: int, y: int) -> None:
        self.require_pixels("pixel click")
        sx, sy = self.to_screen(x, y)
        _pyautogui().click(sx, sy)

    def pixel_double_click(self, x: int, y: int) -> None:
        self.require_pixels("pixel double click")
        sx, sy = self.to_screen(x, y)
        _pyautogui().doubleClick(sx, sy)

    def pixel_right_click(self, x: int, y: int) -> None:
        self.require_pixels("pixel right click")
        sx, sy = self.to_screen(x, y)
        _pyautogui().rightClick(sx, sy)

    def pixel_move(self, x: int, y: int) -> None:
        self.require_pixels("pointer move")
        sx, sy = self.to_screen(x, y)
        _pyautogui().moveTo(sx, sy)

    def pixel_drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = DRAG_DURATION_S) -> None:
        self.require_pixels("pixel drag")
        pg = _pyautogui()
        pg.moveTo(*self.to_screen(x1, y1))
        pg.dragTo(*self.to_screen(x2, y2), duration=max(0.0, float(duration)), button="left")

    def pixel_scroll(self, x: int, y: int, clicks: int, horizontal: bool = False) -> None:
        """Scroll ``clicks`` notches at (x, y): positive is up (or right when ``horizontal``)."""
        self.require_pixels("scroll")
        pg = _pyautogui()
        sx, sy = self.to_screen(x, y)
        if horizontal:
            pg.hscroll(int(clicks), x=sx, y=sy)
        else:
            pg.scroll(int(clicks), x=sx, y=sy)

    def scroll(self, node: Node | None, clicks: int, horizontal: bool = False) -> None:
        """Scroll pattern first (one notch = one small increment), else wheel events over the node's center
        (or the screen center when ``node`` is None)."""
        self._require()
        ctrl = self._handle(node) if node is not None else None
        if ctrl is not None:
            pat = _pattern(ctrl, "GetScrollPattern")
            if pat is not None:
                try:
                    # ScrollAmount: SmallDecrement=1, NoAmount=2, SmallIncrement=4. Positive clicks scroll up
                    # (vertical position decrements) or right (horizontal position increments), like the wheel.
                    up = clicks > 0
                    h_amount = (4 if up else 1) if horizontal else 2
                    v_amount = 2 if horizontal else (1 if up else 4)
                    for _ in range(abs(int(clicks))):
                        pat.Scroll(h_amount, v_amount)
                    return
                except Exception:  # noqa: BLE001
                    pass
        if node is not None and node.rect is not None:
            x, y = node.rect.center
        else:
            _, _, w, h = virtual_screen()
            x, y = w // 2, h // 2
        self.pixel_scroll(x, y, clicks, horizontal=horizontal)

    def screenshot(self) -> bytes:
        """PNG of the whole virtual screen (every monitor); readable regardless of ``allow_pixels``."""
        self._require()
        self._refresh_origin()
        try:
            import mss  # type: ignore
            import mss.tools  # type: ignore

            with mss.mss() as sct:
                mon = sct.monitors[0]  # the bounding box of every monitor
                self._origin = (int(mon.get("left", self._origin[0])), int(mon.get("top", self._origin[1])))
                shot = sct.grab(mon)
                return mss.tools.to_png(shot.rgb, shot.size)
        except BackendError:
            raise
        except Exception as mss_error:  # noqa: BLE001 - fall back to PIL
            try:
                from PIL import ImageGrab  # type: ignore

                img = ImageGrab.grab(all_screens=True)
            except Exception as pil_error:  # noqa: BLE001
                raise BackendError(f"screenshot: mss failed ({mss_error}); PIL failed ({pil_error})") from None
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

    # ------------------------------------------------------------------ windows and apps

    def focus_window(self, node: Node) -> None:
        ctrl = self._handle(node)
        if ctrl is None:
            if node.role == "application" and node.children:
                self.focus_window(node.children[0])
                return
            raise BackendError(f"focus_window: {node.id!r} is not in the accessibility tree")
        for method, args in (("SetActive", ()), ("SwitchToThisWindow", ()), ("SetFocus", ())):
            fn = getattr(ctrl, method, None)
            if not callable(fn):
                continue
            try:
                fn(*args)
                return
            except Exception:  # noqa: BLE001
                continue
        raise BackendError(f"focus_window: could not activate {node.name!r}")

    def focus_existing(self, app: str) -> str | None:
        """Focus a running app by executable name (``notepad``, ``notepad.exe``, ``C:\\...\\code.exe``) or by
        window-title glob; returns the focused window's title, else None."""
        want = _exe_base(app)
        roots = self.tree_all()
        for root in roots:
            if root.role != "application" or _exe_base(root.name) != want or not root.children:
                continue
            win = next((w for w in root.children if "active" in w.states), root.children[0])
            try:
                self.focus_window(win)
            except BackendError:
                pass
            return win.name or root.name
        for root in roots:
            for win in root.children:
                if win.name and (_glob(win.name, f"*{app}*") or _glob(win.name, app)):
                    try:
                        self.focus_window(win)
                    except BackendError:
                        pass
                    return win.name
        return None

    def launch_app(self, cmd: str | list[str]) -> str:
        """Start a program, document or URI on the host desktop. One token (``notepad``, ``ms-settings:display``,
        ``C:\\x.docx``) goes through ``os.startfile`` (ShellExecute: App Paths, file associations, URIs); a
        command line with arguments is started detached. Returns a one-line note."""
        import shlex

        args = shlex.split(cmd, posix=False) if isinstance(cmd, str) else [str(a) for a in cmd]
        args = [a.strip('"') for a in args if a]
        if not args:
            raise BackendError("launch_app: empty command")
        if len(args) == 1:
            starter = getattr(os, "startfile", None)
            if callable(starter):
                try:
                    starter(args[0])
                    return f"started {args[0]} via ShellExecute"
                except OSError as e:
                    raise BackendError(f"launch_app: {e}") from None
        try:
            proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
        except OSError as e:
            raise BackendError(f"launch_app: {e}") from None
        return f"launched pid {proc.pid}: {' '.join(args)}"


# ---------------------------------------------------------------------- helpers


def parse_chord(keys: str) -> list[str]:
    """``"Ctrl+Shift+S"`` -> ``["ctrl", "shift", "s"]`` with the planner's aliases normalised to pyautogui's names.
    A lone ``"+"`` is the plus key."""
    raw = (keys or "").strip()
    if raw == "+":
        return ["+"]
    parts: list[str] = []
    for p in raw.split("+"):
        p = p.strip()
        if not p:
            continue
        low = p.lower()
        parts.append(KEY_ALIASES.get(low, low if len(low) > 1 else p.lower()))
    return parts


def _pyautogui() -> Any:
    try:
        import pyautogui  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise BackendError(f"pyautogui not importable: {e}") from None
    # The pointer legitimately visits the corners of a multi-monitor desktop; the fail-safe would abort the step.
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.02
    return pyautogui


def _send_unicode(text: str) -> None:
    """Type arbitrary Unicode (pyautogui skips non-ASCII) through ``SendInput`` with ``KEYEVENTF_UNICODE``."""
    dll = _windll()
    if dll is None:
        raise BackendError("type: unicode text needs SendInput, which exists only on Windows")
    import ctypes
    from ctypes import wintypes

    KEYEVENTF_UNICODE, KEYEVENTF_KEYUP, INPUT_KEYBOARD = 0x0004, 0x0002, 1

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    events: list[INPUT] = []
    for ch in text:
        for unit in _utf16_units(ch):
            events.append(INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0, unit, KEYEVENTF_UNICODE, 0, None)))
            events.append(INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None)))
    arr = (INPUT * len(events))(*events)
    sent = dll.user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
    if sent != len(events):
        raise BackendError(f"type: SendInput sent {sent} of {len(events)} events")


def _utf16_units(ch: str) -> list[int]:
    data = ch.encode("utf-16-le")
    return [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]


def _glob(name: str, pattern: str) -> bool:
    return fnmatchcase((name or "").lower(), (pattern or "").lower())
