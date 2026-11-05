"""Accessibility backends. ``get_backend()`` picks one for the running OS; optional packages are never required."""
from .base import BackendError, CUBackend, Node, Rect, UnavailableBackend, find_in, get_backend, match

__all__ = ["BackendError", "CUBackend", "Node", "Rect", "UnavailableBackend", "find_in", "get_backend", "match"]
