"""Minimal stand-in for ``atomacos`` built directly on pyobjc's ApplicationServices (HIServices) bindings.

Used by ``ax.py`` when ``atomacos`` cannot be imported (or ``O_AX_IMPL=pyobjc`` is set). It offers exactly the
surface ``ax.py`` uses: ``getAppRefByPid(pid)`` and elements whose ``AX*`` attributes read and write as Python
attributes, ``getActions()`` (names without the ``AX`` prefix) and one callable per action (``el.Press()``).
Importing this module on a machine without pyobjc raises ImportError, which ``ax.py`` reports.
"""
from __future__ import annotations

from typing import Any

from ApplicationServices import (  # type: ignore
    AXUIElementCopyActionNames,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementGetTypeID,
    AXUIElementPerformAction,
    AXUIElementSetAttributeValue,
    AXValueGetType,
    AXValueGetTypeID,
    AXValueGetValue,
    kAXValueCGPointType,
    kAXValueCGSizeType,
)
from CoreFoundation import CFGetTypeID  # type: ignore

AX_SUCCESS = 0


class AXError(Exception):
    def __init__(self, what: str, code: int) -> None:
        super().__init__(f"{what}: AXError {code}")
        self.code = code


def _is(value: Any, type_id: int) -> bool:
    try:
        return CFGetTypeID(value) == type_id
    except Exception:  # noqa: BLE001 - plain Python values have no CF type
        return False


def _convert(value: Any) -> Any:
    if value is None:
        return None
    if _is(value, AXUIElementGetTypeID()):
        return AXElement(value)
    if _is(value, AXValueGetTypeID()):
        kind = AXValueGetType(value)
        if kind == kAXValueCGPointType:
            ok, p = AXValueGetValue(value, kAXValueCGPointType, None)
            return (float(p.x), float(p.y)) if ok else None
        if kind == kAXValueCGSizeType:
            ok, s = AXValueGetValue(value, kAXValueCGSizeType, None)
            return (float(s.width), float(s.height)) if ok else None
        return value
    if isinstance(value, (list, tuple)) or (hasattr(value, "count") and hasattr(value, "objectAtIndex_")):
        return [_convert(v) for v in list(value)]
    if isinstance(value, str):
        return str(value)
    return value


class AXElement:
    """One AXUIElementRef. ``AX*`` attribute access maps to Copy/SetAttributeValue; other names to actions."""

    __slots__ = ("_ref",)

    def __init__(self, ref: Any) -> None:
        object.__setattr__(self, "_ref", ref)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("AX"):
            err, value = AXUIElementCopyAttributeValue(self._ref, name, None)
            if err != AX_SUCCESS:
                raise AttributeError(f"{name}: AXError {err}")
            return _convert(value)
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self.getActions():
            return lambda: self._perform("AX" + name)
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if not name.startswith("AX"):
            raise AttributeError(f"cannot set {name}")
        err = AXUIElementSetAttributeValue(self._ref, name, value)
        if err != AX_SUCCESS:
            raise AXError(f"set {name}", err)

    def getActions(self) -> list[str]:  # noqa: N802 - atomacos' name
        err, names = AXUIElementCopyActionNames(self._ref, None)
        if err != AX_SUCCESS or not names:
            return []
        return [str(n)[2:] if str(n).startswith("AX") else str(n) for n in list(names)]

    def _perform(self, action: str) -> None:
        err = AXUIElementPerformAction(self._ref, action)
        if err != AX_SUCCESS:
            raise AXError(action, err)


def getAppRefByPid(pid: int) -> AXElement:  # noqa: N802 - atomacos' name
    return AXElement(AXUIElementCreateApplication(int(pid)))
