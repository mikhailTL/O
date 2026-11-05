"""Linux backend over AT-SPI2 (``gi.repository.Atspi``).

Actions go through the accessibility API: ``Atspi.Action`` (click/press/activate),
``Atspi.EditableText``/``Atspi.Value`` for values, ``Atspi.Component.grab_focus`` for
focus. Only when the backend was built with ``allow_pixels=True`` does it fall back to
``xdotool`` for coordinate clicks and synthetic key presses. The DISPLAY is the one
handed to the constructor; the host display is never read.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Any

from ..schema import Target
from .base import BackendError, CUBackend, Node, Rect, find_in, focused_roots

try:  # optional: python3-gi + at-spi2
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi  # type: ignore

    _IMPORT_ERROR: str | None = None
except Exception as _e:  # noqa: BLE001 - any failure means "not available"
    Atspi = None  # type: ignore
    _IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

MAX_DEPTH = 60
MAX_NODES = 20_000
_API_ACTIONS = ("click", "press", "activate", "jump", "toggle", "invoke")
_INTERESTING_STATES = {
    "focused", "checked", "visible", "showing", "enabled", "sensitive", "selected", "expanded", "active",
    "editable", "pressed", "focusable", "modal", "single line", "multi line",
}
# Unnamed fields of these roles take the name of the label that labels them (GTK file chooser: the location
# entry is `text ''` with a sibling `label 'Name:'` linked by LABELLED_BY; the planner asks for name 'Name*').
_LABELLED_ROLES = frozenset({
    "text", "entry", "password text", "combo box", "spin button", "slider", "editbar", "terminal", "list box",
})


def _label_name(acc: Any) -> str:
    """The text of the first LABELLED_BY target of ``acc`` (whitespace collapsed), else ""."""
    try:
        for rel in acc.get_relation_set() or []:
            if rel.get_relation_type() != Atspi.RelationType.LABELLED_BY:
                continue
            for i in range(rel.get_n_targets()):
                target = rel.get_target(i)
                label = " ".join((target.get_name() or "").split()) if target is not None else ""
                if label:
                    return label
    except Exception:  # noqa: BLE001 - a missing relation leaves the node unnamed
        pass
    return ""


def _state_names(acc: Any) -> list[str]:
    names: list[str] = []
    try:
        state_set = acc.get_state_set()
        for st in state_set.get_states():
            n = st.value_nick.replace("-", " ").lower()
            if n in _INTERESTING_STATES:
                names.append(n)
    except Exception:  # noqa: BLE001
        pass
    if "showing" in names and "visible" not in names:
        names.append("visible")
    return sorted(set(names))


class AtspiBackend(CUBackend):
    name = "atspi"

    def __init__(self, allow_pixels: bool = False, display: str | None = None, focused_only: bool = True) -> None:
        super().__init__(allow_pixels=allow_pixels, display=display)
        # Actions and judging read the application that owns the active window (see ``focused_roots``):
        # with two instances of an app on the display, the one in front is the one acted on and judged.
        self.focused_only = bool(focused_only)
        self._handles: dict[str, Any] = {}
        if _IMPORT_ERROR:
            self.reason = f"gi/Atspi not importable: {_IMPORT_ERROR}"
        elif display:
            # AT-SPI finds its bus through the X display of the process; point it at ours.
            os.environ.setdefault("AT_SPI_DISPLAY", display)
            self.reason = ""
        else:
            self.reason = "no isolated DISPLAY given"

    @property
    def available(self) -> bool:
        return _IMPORT_ERROR is None and bool(self.display)

    def _require(self) -> None:
        if not self.available:
            raise BackendError(f"atspi backend unavailable: {self.reason}")

    # ---------------------------------------------------------------- tree

    def tree(self, window: str | None = None) -> list[Node]:
        roots = self._roots(window)
        if window or not self.focused_only:
            return roots
        return focused_roots(roots)

    def tree_all(self) -> list[Node]:
        return self._roots(None)

    def _roots(self, window: str | None) -> list[Node]:
        self._require()
        self._handles.clear()
        desktop = Atspi.get_desktop(0)
        roots: list[Node] = []
        count = [0]
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if app is None:
                continue
            node = self._convert(app, f"a{i}", 0, count)
            if node is None:
                continue
            if window and not any(_glob(w.name, window) for w in node.children):
                continue
            roots.append(node)
        return roots

    def _convert(self, acc: Any, path: str, depth: int, count: list[int]) -> Node | None:
        if depth > MAX_DEPTH or count[0] >= MAX_NODES:
            return None
        count[0] += 1
        try:
            role = acc.get_role_name() or ""
            name = acc.get_name() or ""
        except Exception:  # noqa: BLE001
            return None
        if not name and role in _LABELLED_ROLES:
            name = _label_name(acc)
        text = ""
        try:
            t = acc.get_text_iface() if hasattr(acc, "get_text_iface") else Atspi.Accessible.get_text(acc)
            if t is not None:
                # Through Atspi.Text explicitly: on the PyGObject object, Accessible.get_text() (no arguments, the
                # deprecated interface getter) shadows Text.get_text(start, end), so t.get_text(0, n) always raised
                # and every text field / editor read as empty.
                text = Atspi.Text.get_text(t, 0, min(Atspi.Text.get_character_count(t), 4000)) or ""
        except Exception:  # noqa: BLE001
            pass
        rect = None
        try:
            comp = acc.get_component_iface() if hasattr(acc, "get_component_iface") else Atspi.Accessible.get_component(acc)
            if comp is not None:
                ext = comp.get_extents(Atspi.CoordType.SCREEN)
                rect = Rect(x=int(ext.x), y=int(ext.y), w=max(0, int(ext.width)), h=max(0, int(ext.height)))
        except Exception:  # noqa: BLE001
            pass
        children: list[Node] = []
        try:
            n = acc.get_child_count()
        except Exception:  # noqa: BLE001
            n = 0
        for i in range(min(n, 2000)):
            try:
                child = acc.get_child_at_index(i)
            except Exception:  # noqa: BLE001
                continue
            if child is None:
                continue
            cn = self._convert(child, f"{path}.{i}", depth + 1, count)
            if cn is not None:
                children.append(cn)
        node = Node(id=path, role=role, name=name, text=text, rect=rect, states=_state_names(acc), children=children)
        self._handles[path] = acc
        return node

    def find(self, target: Target, within: Node | None = None) -> Node | None:
        return find_in(self.tree(), target, within=within)

    def _handle(self, node: Node) -> Any | None:
        if node.synthetic:
            return None
        acc = self._handles.get(node.id)
        if acc is None:
            self.tree()
            acc = self._handles.get(node.id)
        return acc

    # ------------------------------------------------------------- actions

    def click(self, node: Node) -> None:
        self._require()
        acc = self._handle(node)
        if acc is not None:
            try:
                action = Atspi.Accessible.get_action_iface(acc)
            except Exception:  # noqa: BLE001
                action = None
            if action is not None:
                names = {}
                for i in range(action.get_n_actions()):
                    names[(action.get_action_name(i) or "").lower()] = i
                for want in _API_ACTIONS:
                    if want in names:
                        if action.do_action(names[want]):
                            return
                if names:
                    action.do_action(0)
                    return
        self.require_pixels("click")
        if node.rect is None:
            raise BackendError("click: node has no rect")
        self.pixel_click(*node.rect.center)

    def pixel_click(self, x: int, y: int) -> None:
        self.require_pixels("pixel click")
        self._xdotool("mousemove", str(x), str(y), "click", "1")

    def invoke(self, node: Node, action: str) -> None:
        self._require()
        acc = self._handle(node)
        if acc is None:
            raise BackendError(f"invoke {action!r}: node {node.id!r} is not in the accessibility tree")
        if action == "focus":
            comp = Atspi.Accessible.get_component_iface(acc)
            if comp is None or not comp.grab_focus():
                raise BackendError("invoke focus: grab_focus refused")
            return
        act = Atspi.Accessible.get_action_iface(acc)
        if act is None:
            raise BackendError(f"invoke {action!r}: element has no Action interface")
        for i in range(act.get_n_actions()):
            if (act.get_action_name(i) or "").lower() == action.lower():
                if not act.do_action(i):
                    raise BackendError(f"invoke {action!r}: action refused")
                return
        raise BackendError(f"invoke {action!r}: element has no such action")

    def set_value(self, node: Node, value: str) -> None:
        self._require()
        acc = self._handle(node)
        if acc is None:
            raise BackendError(f"set_value: node {node.id!r} is not in the accessibility tree")
        et = Atspi.Accessible.get_editable_text_iface(acc)
        if et is not None:
            if et.set_text_contents(value):
                return
            raise BackendError("set_value: EditableText refused the value")
        val = Atspi.Accessible.get_value_iface(acc)
        if val is not None:
            try:
                val.set_current_value(float(value))
                return
            except ValueError:
                raise BackendError(f"set_value: {value!r} is not numeric for a Value element") from None
        raise BackendError("set_value: element supports neither EditableText nor Value")

    def type_text(self, node: Node | None, text: str) -> None:
        self._require()
        if node is not None:
            acc = self._handle(node)
            if acc is not None:
                et = Atspi.Accessible.get_editable_text_iface(acc)
                if et is not None:
                    try:
                        t = Atspi.Accessible.get_text_iface(acc)
                        pos = t.get_character_count() if t is not None else 0
                    except Exception:  # noqa: BLE001
                        pos = 0
                    if et.insert_text(pos, text, len(text)):
                        return
                comp = Atspi.Accessible.get_component_iface(acc)
                if comp is not None:
                    comp.grab_focus()
        self.require_pixels("type")
        self._xdotool("type", "--delay", "20", "--", text)

    def key(self, keys: str) -> None:
        self._require()
        self.require_pixels("key press")
        self._xdotool("key", "--", keys)

    # ------------------------------------------------------------ windows

    def focus_window(self, node: Node) -> None:
        """Activate the X window behind a top-level a11y window (window manager raise + focus)."""
        acc = self._handle(node)
        pid = None
        if acc is not None:
            try:
                pid = int(acc.get_process_id())
            except Exception:  # noqa: BLE001
                pid = None
        candidates: list[str] = []
        if pid:
            candidates = self._xdotool_ids("search", "--onlyvisible", "--pid", str(pid))
        if not candidates and node.name:
            candidates = self._xdotool_ids("search", "--onlyvisible", "--name", f"^{_re_escape(node.name)}$")
        for wid in candidates:
            try:
                self._xdotool("windowactivate", "--sync", wid)
                return
            except BackendError:
                continue
        comp = Atspi.Accessible.get_component_iface(acc) if acc is not None else None
        if comp is not None and comp.grab_focus():
            return
        raise BackendError(f"focus: no X window found for {node.name!r}")

    def focus_existing(self, app: str) -> str | None:
        found = super().focus_existing(app)
        if found:
            return found
        # an app without an accessibility bridge still has an X window with its WM_CLASS
        for wid in self._xdotool_ids("search", "--onlyvisible", "--class", f"^{_re_escape(app)}$"):
            try:
                self._xdotool("windowactivate", "--sync", wid)
            except BackendError:
                continue
            return f"X window {wid} (class {app})"
        return None

    def _xdotool_ids(self, *args: str) -> list[str]:
        try:
            out = self._xdotool_out(*args)
        except BackendError:
            return []
        return [line.strip() for line in out.splitlines() if line.strip().isdigit()]

    # ---------------------------------------------------------------- pixels

    def _xdotool(self, *args: str) -> None:
        self._xdotool_out(*args)

    def _xdotool_out(self, *args: str) -> str:
        exe = shutil.which("xdotool")
        if exe is None:
            raise BackendError("xdotool is not installed")
        env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
        env.update(self.env())
        proc = subprocess.run([exe, *args], env=env, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise BackendError(f"xdotool {args[0]} failed: {proc.stderr.strip()[:200]}")
        return proc.stdout or ""

    def screenshot(self) -> bytes:
        self._require()
        env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
        env.update(self.env())
        failures: list[str] = []
        magick = shutil.which("import")
        if magick:
            png, why = _capture([magick, "-window", "root", "png:-"], env)
            if png:
                return png
            failures.append(why)
        xwd = shutil.which("xwd")
        convert = shutil.which("convert") or shutil.which("magick")
        if xwd and convert:
            with tempfile.NamedTemporaryFile(suffix=".xwd", delete=False) as tmp:
                path = tmp.name
            try:
                raw, why = _capture([xwd, "-root", "-silent", "-out", path], env, want_stdout=False)
                if why:
                    failures.append(why)
                else:
                    png, why = _capture([convert, f"xwd:{path}", "png:-"], env)
                    if png:
                        return png
                    failures.append(why)
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        if not failures:
            raise BackendError("no screenshot tool: install ImageMagick (`import`) or xwd+convert")
        raise BackendError(f"screenshot of {self.display} failed: " + "; ".join(failures))


def _capture(argv: list[str], env: dict[str, str], want_stdout: bool = True) -> tuple[bytes, str]:
    """(stdout, "") on success, else (b"", "<command> <what went wrong>: <stderr>")."""
    shown = " ".join(os.path.basename(argv[0:1][0]) if i == 0 else a for i, a in enumerate(argv))
    try:
        proc = subprocess.run(argv, env=env, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        return b"", f"`{shown}` timed out after 30 s"
    except OSError as e:
        return b"", f"`{shown}` could not run: {e}"
    stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        return b"", f"`{shown}` exited {proc.returncode}: {stderr[:300] or '(no stderr)'}"
    if want_stdout and not proc.stdout:
        return b"", f"`{shown}` wrote no image: {stderr[:300] or '(no stderr)'}"
    return proc.stdout or b"ok", ""


def _re_escape(text: str) -> str:
    import re

    return re.escape(text)


def _glob(name: str, pattern: str) -> bool:
    from fnmatch import fnmatchcase

    return fnmatchcase((name or "").lower(), pattern.lower())
