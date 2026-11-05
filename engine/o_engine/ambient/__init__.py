"""Ambient sensing and autonomous infrastructure layer (plan section J).

Everything here is opt-in (``O_AMBIENT=0`` by default, every sensor off by default), runs on its
own threads, processes on-device, and only derived room-level presence features cross the bus.
``layer.AmbientLayer`` wires the parts together for ``server.create_app``.
"""
from __future__ import annotations

PRESENT = "USER_PRESENT"
ABSENT = "USER_ABSENT"
UNKNOWN = "UNKNOWN"
STATES = (UNKNOWN, PRESENT, ABSENT)

__all__ = ["PRESENT", "ABSENT", "UNKNOWN", "STATES"]
