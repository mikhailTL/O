"""Regression tests for the second 2026-11-05 real run ("write a 3-line packing list for Lisbon and save it as
~/lisbon.txt"), in which all four computer_run jobs failed. No model and no display are used here; the real-display
regression is ``test_cu_real_atspi.py::test_save_as_dialog_name_entry_found_by_tree``.

1. Save As: the GTK location entry is ``text ''`` labelled by ``label 'Name:'``; modal dialogs lead the focused tree;
   a planner's ``"name": ""`` is an absent selector, not a SchemaViolation.
2. vision-grounded (synthetic) nodes: set_value / type / invoke fall back to pixels on the isolated display.
3. ``key ~/lisbon.txt`` is typed as text, with the coercion logged; the planner prompt says what key is for.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from o_engine.actionlog import ActionLog
from o_engine.cu.backends import atspi as atspi_mod
from o_engine.cu.backends.base import Node, focused_roots
from o_engine.cu.backends.fake import FakeBackend
from o_engine.cu.executor import ScriptRunner, is_key_chord
from o_engine.cu.planner import STATIC_PREFIX
from o_engine.cu.schema import Target, parse_script
from o_engine.schema import SchemaViolation


def _editor_with_save_as(dialog_states=("active", "modal", "visible"), frame_states=("visible",)):
    return Node(id="a0", role="application", name="mousepad", children=[
        Node(id="a0.0", role="frame", name="Untitled 1 - Mousepad", states=list(frame_states), children=[
            Node(id="a0.0.0", role="text", text="Passport", states=["editable", "visible"]),
        ]),
        Node(id="a0.1", role="file chooser", name="Save As", states=list(dialog_states), children=[
            Node(id="a0.1.0", role="text", name="Name:", states=["editable", "focused", "visible"]),
            Node(id="a0.1.1", role="label", name="Name:", states=["visible"]),
            Node(id="a0.1.2", role="push button", name="Save", states=["visible"]),
        ]),
    ])


def _records(tmp_path):
    out = []
    for p in tmp_path.glob("*.jsonl"):
        out += [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    return out


class _Vision:
    def __init__(self, bbox):
        self.bbox = bbox
        self.calls = 0

    def ground(self, screenshot, target):
        self.calls += 1
        return dict(self.bbox)


def _run(backend, steps, tmp_path, grounder=None):
    log = ActionLog(str(tmp_path))
    rn = ScriptRunner(backend, log, grounder=grounder, sleep=lambda s: None)
    return rn.run(parse_script({"app": "mousepad", "goal": "save it", "steps": steps}), "j1"), log


# ================================================================== 1. Save As dialog

def test_blank_selectors_are_absent_not_a_schema_violation():
    # real run: "steps.0.find.target.name: String should have at least 1 character" aborted plan and heal
    s = parse_script({"app": "mousepad", "goal": "g", "steps": [
        {"op": "find", "as": "name", "target": {"role": "text", "name": ""}},
        {"op": "assert", "target": {"role": "text", "name": "", "text": "", "state": "focused"}},
    ]})
    assert s.steps[0].target == Target(role="text")
    assert s.steps[1].target == Target(role="text") and s.steps[1].state == "focused"
    with pytest.raises(SchemaViolation, match="at least one of role, name, text"):
        parse_script({"app": "m", "goal": "g", "steps": [{"op": "find", "as": "x", "target": {"name": ""}}]})


def test_open_modal_dialog_leads_the_focused_tree_even_before_the_wm_activates_it():
    # just mapped: the frame is still the active window, the modal Save As is showing
    roots = focused_roots([_editor_with_save_as(dialog_states=("modal", "visible"), frame_states=("active", "visible"))])
    assert [w.id for w in roots[0].children] == ["a0.1", "a0.0"]
    # activated: same order; a hidden (not showing) modal is not promoted
    roots = focused_roots([_editor_with_save_as()])
    assert [w.id for w in roots[0].children] == ["a0.1", "a0.0"]
    roots = focused_roots([_editor_with_save_as(dialog_states=("modal",), frame_states=("active", "visible"))])
    assert [w.id for w in roots[0].children] == ["a0.0", "a0.1"]


def test_modal_dialog_of_another_process_is_a_focused_root_of_its_own():
    editor = Node(id="a0", role="application", name="mousepad", children=[
        Node(id="a0.0", role="frame", name="Untitled 1 - Mousepad", states=["active", "visible"]),
    ])
    portal = Node(id="a1", role="application", name="xdg-desktop-portal-gtk", children=[
        Node(id="a1.0", role="file chooser", name="Save As", states=["modal", "visible"]),
        Node(id="a1.1", role="frame", name="unrelated", states=["visible"]),
    ])
    other = Node(id="a2", role="application", name="calc", children=[Node(id="a2.0", role="frame", states=["visible"])])
    roots = focused_roots([editor, portal, other])
    assert [r.id for r in roots] == ["a1", "a0"]
    assert [w.id for w in roots[0].children] == ["a1.0"]


class _Rel:
    def __init__(self, kind, targets):
        self.kind, self.targets = kind, targets

    def get_relation_type(self):
        return self.kind

    def get_n_targets(self):
        return len(self.targets)

    def get_target(self, i):
        return self.targets[i]


class _Acc:
    def __init__(self, name="", rels=()):
        self.name, self.rels = name, list(rels)

    def get_name(self):
        return self.name

    def get_relation_set(self):
        return self.rels


def test_unnamed_entry_takes_the_name_of_its_label(monkeypatch):
    kinds = SimpleNamespace(LABELLED_BY="labelled-by", LABEL_FOR="label-for")
    monkeypatch.setattr(atspi_mod, "Atspi", SimpleNamespace(RelationType=kinds))
    entry = _Acc(rels=[_Rel("label-for", [_Acc("wrong")]), _Rel("labelled-by", [_Acc("  Name:\n")])])
    assert atspi_mod._label_name(entry) == "Name:"
    assert atspi_mod._label_name(_Acc()) == ""
    assert "text" in atspi_mod._LABELLED_ROLES and "label" not in atspi_mod._LABELLED_ROLES


def test_planner_name_glob_finds_the_labelled_entry(tmp_path):
    b = FakeBackend([_editor_with_save_as()], allow_pixels=True)
    res, _ = _run(b, [
        {"op": "find", "as": "name", "target": {"role": "text", "name": "Name:*"}},
        {"op": "assert", "target": {"role": "text", "name": "Name*", "state": "focused"}},
        {"op": "set_value", "ref": "name", "value": "~/lisbon.txt"},
    ], tmp_path)
    assert res.ok, res.error
    assert res.steps[0].resolved_by == "a11y"
    assert b.actions == [("set_value", "a0.1.0", "~/lisbon.txt")]


# ================================================================== 2. vision-grounded nodes

BBOX = {"x": 11, "y": 39, "w": 41, "h": 15}  # the real run's vision:11,39,41,15


def _no_entry():
    return Node(id="a0", role="application", name="mousepad", children=[
        Node(id="a0.0", role="frame", name="Save As", states=["active", "visible"]),
    ])


def test_set_value_on_a_vision_node_clicks_selects_all_and_types(tmp_path):
    b = FakeBackend([_no_entry()], allow_pixels=True)
    res, _ = _run(b, [
        {"op": "find", "as": "name", "target": {"role": "text", "name": "Name*"}},
        {"op": "set_value", "ref": "name", "value": "~/lisbon.txt"},
    ], tmp_path, grounder=_Vision(BBOX))
    assert res.ok, res.error
    assert res.steps[0].resolved_by == "vision"
    assert b.actions == [("pixel_click", 31, 46), ("key", "ctrl+a"), ("type_text", None, "~/lisbon.txt")]
    assert "clicked (31,46), selected all, typed 12 chars" in res.steps[1].note
    assert any(r.get("detail", {}).get("coerced") == "set_value->pixels" for r in _records(tmp_path))


def test_type_on_a_vision_node_clicks_then_types_and_invoke_clicks(tmp_path):
    b = FakeBackend([_no_entry()], allow_pixels=True)
    res, _ = _run(b, [
        {"op": "find", "as": "field", "target": {"role": "text"}},
        {"op": "type", "ref": "field", "text": "abc"},
        {"op": "find", "as": "save", "target": {"role": "push button", "name": "Save"}},
        {"op": "invoke", "ref": "save", "action": "click"},
    ], tmp_path, grounder=_Vision(BBOX))
    assert res.ok, res.error
    assert b.actions == [("pixel_click", 31, 46), ("type_text", None, "abc"), ("pixel_click", 31, 46)]
    assert "clicked its center" in res.steps[3].note


def test_vision_node_without_pixels_fails_with_a_reason_not_a_tree_error(tmp_path):
    b = FakeBackend([_no_entry()], allow_pixels=False)
    res, _ = _run(b, [
        {"op": "find", "as": "name", "target": {"role": "text", "name": "Name*"}},
        {"op": "set_value", "ref": "name", "value": "x"},
    ], tmp_path, grounder=_Vision(BBOX))
    assert not res.ok and res.failed_index == 1
    assert "not in the accessibility tree" not in res.error
    assert "found only on screen" in res.error and "pixel actions are disabled" in res.error
    assert b.actions == []


# ================================================================== 3. key vs type

@pytest.mark.parametrize("keys", ["ctrl+s", "Return", "ctrl+a", "ctrl+shift+t", "alt+F4", "Escape", "ctrl+a Delete",
                                  "a", "ctrl+/", "F12", "ctrl++"])
def test_key_chords(keys):
    assert is_key_chord(keys)


@pytest.mark.parametrize("keys", ["~/lisbon.txt", "lisbon.txt", "hello world", "Passport, wallet", "Hello", "a b c",
                                  "ctrl+lisbon", "foo+s", "/home/me/x.txt"])
def test_not_key_chords(keys):
    assert not is_key_chord(keys)


def test_key_with_text_is_typed_and_the_coercion_logged(tmp_path):
    b = FakeBackend([_editor_with_save_as()], allow_pixels=True)
    res, _ = _run(b, [
        {"op": "key", "keys": "ctrl+a"},
        {"op": "key", "keys": "~/lisbon.txt"},
        {"op": "key", "keys": "Return"},
    ], tmp_path)
    assert res.ok, res.error
    assert b.actions == [("key", "ctrl+a"), ("type_text", None, "~/lisbon.txt"), ("key", "Return")]
    assert res.steps[1].note == "key '~/lisbon.txt' is not a key chord; typed it as text instead"
    notes = [r for r in _records(tmp_path) if r.get("detail", {}).get("coerced") == "key->type"]
    assert len(notes) == 1 and notes[0]["detail"]["keys"] == "~/lisbon.txt"


def test_planner_prompt_says_key_is_for_chords():
    assert "key is only for chords like ctrl+s, Return, Escape; use type for any text, paths and filenames." in STATIC_PREFIX
    assert 'Omit a field rather than giving it ""' in STATIC_PREFIX


class _LateDialog(FakeBackend):
    """The Save As dialog maps a few tree reads after ctrl+s (real run: the find ran before it existed)."""

    def __init__(self, reads_until_mapped=3):
        super().__init__([_no_entry()], allow_pixels=True)
        self.reads = 0
        self.until = reads_until_mapped

    def key(self, keys):
        super().key(keys)
        self.reads = 0

    def tree(self, window=None):
        self.reads += 1
        if self.reads == self.until:
            self.replace_tree([_editor_with_save_as()])
        return super().tree(window)


def test_find_after_an_action_waits_for_the_new_dialog_before_using_vision(tmp_path):
    b = _LateDialog()
    vision = _Vision(BBOX)
    res, _ = _run(b, [
        {"op": "key", "keys": "ctrl+s"},
        {"op": "find", "as": "name", "target": {"role": "text", "name": "Name*"}},
        {"op": "set_value", "ref": "name", "value": "~/lisbon.txt"},
    ], tmp_path, grounder=vision)
    assert res.ok, res.error
    assert res.steps[1].resolved_by == "a11y" and vision.calls == 0
    assert b.actions[-1] == ("set_value", "a0.1.0", "~/lisbon.txt")


def test_find_on_a_settled_tree_goes_to_vision_after_one_extra_read(tmp_path):
    b = FakeBackend([_no_entry()], allow_pixels=True)
    sleeps = []
    rn = ScriptRunner(b, ActionLog(str(tmp_path)), grounder=_Vision(BBOX), sleep=sleeps.append)
    res = rn.run(parse_script({"app": "m", "goal": "g", "steps": [
        {"op": "key", "keys": "ctrl+s"},
        {"op": "find", "as": "name", "target": {"role": "text", "name": "Name*"}},
    ]}), "j")
    assert res.ok and res.steps[1].resolved_by == "vision"
    assert len(sleeps) == 1  # the tree did not change: no further waiting
