"""AT-SPI backend reads a text element's contents (real run: mousepad's text view always read as empty)."""
from types import SimpleNamespace

from o_engine.cu.backends import atspi as mod


class _Acc:
    """Mimics the PyGObject AtspiAccessible: get_text() is the deprecated no-argument interface getter."""

    def __init__(self, role, text="", children=()):
        self._role, self._text, self._children = role, text, list(children)

    def get_role_name(self):
        return self._role

    def get_name(self):
        return ""

    def get_text_iface(self):
        return self if self._text else None

    def get_text(self):  # shadows Text.get_text(start, end) on the real object
        return self

    def get_component_iface(self):
        return None

    def get_state_set(self):
        raise RuntimeError("no states in this fake")

    def get_child_count(self):
        return len(self._children)

    def get_child_at_index(self, i):
        return self._children[i]


def test_text_element_contents_are_read(monkeypatch):
    fake_text = SimpleNamespace(
        get_text=lambda acc, start, end: acc._text[start:end],
        get_character_count=lambda acc: len(acc._text),
    )
    monkeypatch.setattr(mod, "Atspi", SimpleNamespace(Text=fake_text), raising=False)
    monkeypatch.setattr(mod, "_IMPORT_ERROR", None)
    backend = mod.AtspiBackend(display=":99")
    view = _Acc("text", "Passport, wallet, phone charger\nSunscreen")
    node = backend._convert(_Acc("frame", children=[view]), "a0", 0, [0])
    assert node.children[0].role == "text"
    assert node.children[0].text == "Passport, wallet, phone charger\nSunscreen"
