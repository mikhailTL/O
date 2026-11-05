import json
import subprocess
from types import SimpleNamespace

import pytest

from o_engine.cu import ground as ground_mod
from o_engine.cu.ground import (
    ClaudeCodeGrounder,
    Grounder,
    GroundResult,
    LocatorCache,
    VisionGrounder,
    locator_key,
    screen_sha,
)
from o_engine.schema import SchemaViolation

PNG = b"\x89PNG\r\n\x1a\nfake-screen-1"
PNG2 = b"\x89PNG\r\n\x1a\nfake-screen-2"
BOX = {"x": 10, "y": 20, "w": 30, "h": 40}


def _chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])


def _chunks(text, size=5):
    out = [_chunk(text[i : i + size]) for i in range(0, len(text), size)]
    out.append(SimpleNamespace(choices=[]))
    return out


class FakeOpenAI:
    calls = []
    reply = ""
    error = None

    def __init__(self, base_url=None, api_key=None, timeout=None, max_retries=None, **kw):
        self.max_retries = max_retries
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        FakeOpenAI.calls.append(kw)
        if FakeOpenAI.error is not None:
            raise FakeOpenAI.error
        return iter(_chunks(FakeOpenAI.reply))


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    FakeOpenAI.calls = []
    FakeOpenAI.reply = ""
    FakeOpenAI.error = None
    monkeypatch.setattr(ground_mod, "OpenAI", FakeOpenAI)
    yield


@pytest.fixture
def fake_run(monkeypatch):
    state = {"calls": [], "stdout": "", "returncode": 0}

    def run(cmd, **kw):
        state["calls"].append((cmd, kw))
        return subprocess.CompletedProcess(cmd, state["returncode"], stdout=state["stdout"], stderr="")

    monkeypatch.setattr(ground_mod.subprocess, "run", run)
    return state


class StubGrounder:
    def __init__(self, name, result=None, error=None):
        self.name = name
        self.result = result
        self.error = error
        self.calls = 0

    def ground(self, png, text):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def test_vision_grounder_sends_image_and_parses_bbox_through_tokenfilter():
    FakeOpenAI.reply = "The element is here:\n```json\n" + json.dumps(BOX) + "\n```"
    g = VisionGrounder("http://127.0.0.1:8000/v1/", "ui-vlm")
    assert g.client.max_retries == 0
    r = g.ground(PNG, "Save button")
    assert r == GroundResult(x=10, y=20, w=30, h=40, confidence=1.0, source="vlm")
    msg = FakeOpenAI.calls[0]["messages"][1]["content"]
    assert msg[0]["type"] == "image_url"
    assert msg[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "Save button" in msg[1]["text"]
    assert "ONLY a JSON object" in FakeOpenAI.calls[0]["messages"][0]["content"]


def test_vision_grounder_empty_object_means_absent():
    FakeOpenAI.reply = "{}"
    assert VisionGrounder("http://x/v1", "m").ground(PNG, "nothing") is None


@pytest.mark.parametrize("reply", ["no idea", '{"x": 1}', '{"x":1,"y":2,"w":3,"h":4,"z":5}', '{"x":"1","y":2,"w":3,"h":4}', "[1,2,3,4]"])
def test_vision_grounder_rejects_garbage(reply):
    FakeOpenAI.reply = reply
    with pytest.raises(SchemaViolation):
        VisionGrounder("http://x/v1", "m").ground(PNG, "t")


def test_claude_grounder_writes_png_and_references_path(fake_run, tmp_path):
    fake_run["stdout"] = json.dumps({"result": "Sure: " + json.dumps(BOX)})
    r = ClaudeCodeGrounder("claude-bin").ground(PNG, "OK button")
    assert r.source == "claude" and (r.x, r.y, r.w, r.h) == (10, 20, 30, 40)
    cmd, kw = fake_run["calls"][0]
    assert cmd == ["claude-bin", "-p", "--output-format", "json"]
    assert ".png" in kw["input"] and "OK button" in kw["input"]


def test_claude_grounder_runs_in_the_screenshot_dir(fake_run):
    import os
    import re

    fake_run["stdout"] = json.dumps({"result": json.dumps(BOX)})
    ClaudeCodeGrounder("claude-bin").ground(PNG, "OK button")
    kw = fake_run["calls"][0][1]
    png_path = re.search(r"PNG file at: (\S+\.png)", kw["input"]).group(1)
    assert kw["cwd"] and os.path.dirname(png_path) == kw["cwd"]
    assert not os.path.exists(kw["cwd"])


def test_claude_grounder_nonzero_exit_is_violation(fake_run):
    fake_run["returncode"] = 1
    with pytest.raises(SchemaViolation):
        ClaudeCodeGrounder().ground(PNG, "x")


def test_locator_cache_get_put_append_only(tmp_path):
    cache = LocatorCache(tmp_path / "locators.sqlite")
    sha = screen_sha(PNG)
    assert cache.get("app", "Save", sha) is None
    assert cache.put("app", "Save", sha, BOX) is True
    got = cache.get("app", "Save", sha)
    assert {k: got[k] for k in BOX} == BOX
    assert cache.put("app", "Save", sha, {"x": 1, "y": 1, "w": 1, "h": 1}) is False
    assert {k: cache.get("app", "Save", sha)[k] for k in BOX} == BOX
    assert cache.get("app", "Save", screen_sha(PNG2)) is None
    assert cache.put("app", "Save", screen_sha(PNG2), BOX) is True
    assert cache.count() == 2
    assert LocatorCache.key("app", "Save", sha) == locator_key("app", "Save", sha)
    reopened = LocatorCache(tmp_path / "locators.sqlite")
    assert reopened.get("app", "Save", sha) is not None


def test_grounder_primary_first_returns_dict():
    primary = StubGrounder("vlm", GroundResult(**BOX, source="vlm"))
    fallback = StubGrounder("claude", GroundResult(x=1, y=1, w=1, h=1, source="claude"))
    g = Grounder(primary, fallback)
    out = g.ground(PNG, "Save")
    assert {k: out[k] for k in BOX} == BOX
    assert g.last_source == "vlm" and fallback.calls == 0


def test_grounder_primary_fail_then_fallback_once_then_none():
    primary = StubGrounder("vlm", error=SchemaViolation("bad"))
    fallback = StubGrounder("claude", GroundResult(**BOX, source="claude"))
    g = Grounder(primary, fallback)
    out = g.ground(PNG, "Save")
    assert {k: out[k] for k in BOX} == BOX
    assert (primary.calls, fallback.calls, g.last_source) == (1, 1, "claude")
    both = Grounder(StubGrounder("vlm", error=RuntimeError("down")), StubGrounder("claude", None))
    assert both.ground(PNG, "Save") is None
    assert both.primary.calls == 1 and both.fallback.calls == 1
    assert len(both.last_errors) == 2


def test_grounder_accepts_target_object():
    from o_engine.cu.schema import Target

    primary = StubGrounder("vlm", GroundResult(**BOX, source="vlm"))
    seen = {}

    def spy(png, text):
        seen["text"] = text
        return primary.result

    primary.ground = spy
    out = Grounder(primary).ground(PNG, Target(role="push button", name="Save"))
    assert {k: out[k] for k in BOX} == BOX
    assert "push button" in seen["text"] and "Save" in seen["text"]


def test_executor_uses_vision_then_locator_cache_with_zero_vlm_calls(tmp_path):
    try:
        from o_engine.actionlog import ActionLog
        from o_engine.cu.backends.fake import FakeBackend
        from o_engine.cu.executor import ScriptRunner
        from o_engine.cu.schema import parse_script
    except ImportError as e:  # F1's executor not landed yet
        pytest.skip(f"F1 module missing: {e}")
    from o_engine.cu.backends.base import Node

    script = parse_script(
        {"app": "demo", "goal": "press the hidden button", "steps": [
            {"op": "find", "as": "btn", "target": {"role": "push button", "name": "Hidden"}},
            {"op": "click", "ref": "btn"},
        ]}
    )
    primary = StubGrounder("vlm", GroundResult(**BOX, source="vlm"))
    grounder = Grounder(primary)
    cache = LocatorCache(tmp_path / "locators.sqlite")
    log = ActionLog(tmp_path / "data")
    for n in (1, 2):
        backend = FakeBackend(Node(id="root", role="frame", name="Demo"), screenshot=PNG, allow_pixels=True)
        runner = ScriptRunner(backend, log, grounder=grounder, locator_cache=cache)
        result = runner.run(script, f"job-{n}")
        assert result.ok, result
        find = result.steps[0]
        resolved = find.get("resolved_by") if isinstance(find, dict) else getattr(find, "resolved_by", None)
        assert resolved == ("vision" if n == 1 else "locator"), (n, find)
    assert primary.calls == 1



# ---------------------------------------------------------------- P3: zoom grounding + consistency gate

import sqlite3

from o_engine.cu.ground import MultiCropVote, ZoomGrounder, crop_png, png_size

W, H = 1000, 800
TRUE = GroundResult(x=600, y=400, w=40, h=20, source="stub")  # centre (620, 410)


class SeqGrounder:
    """Answers from a queue; records every (png, text) it was asked about."""

    name = "seq"

    def __init__(self, results):
        self.results = list(results)
        self.seen = []

    def ground(self, png, text):
        self.seen.append((png, text))
        if not self.results:
            raise AssertionError("SeqGrounder asked more times than scripted")
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def fake_crop(png, box):
    return json.dumps({"crop": list(box)}).encode()


def fake_size(png):
    return (W, H)


def _crop_box(png):
    return tuple(json.loads(png.decode())["crop"])


def _in_crop(result, box):
    l, t = box[0], box[1]
    return GroundResult(x=result.x - l, y=result.y - t, w=result.w, h=result.h, source=result.source)


def test_png_size_reads_ihdr_and_rejects_non_png():
    from PIL import Image
    import io

    buf = io.BytesIO()
    Image.new("RGB", (37, 19)).save(buf, format="PNG")
    assert png_size(buf.getvalue()) == (37, 19)
    with pytest.raises(SchemaViolation):
        png_size(PNG)


def test_zoom_step1_offset_step2_true_remaps_to_true_box():
    offset = GroundResult(x=500, y=300, w=40, h=20, source="stub")  # centre (520, 310), 100 px off
    crop_w, crop_h = 500, 400
    left, top = 520 - crop_w // 2, 310 - crop_h // 2  # 270, 110
    inner = SeqGrounder([offset, _in_crop(TRUE, (left, top))])
    z = ZoomGrounder(inner, scale=0.5, crop_fn=fake_crop, size_fn=fake_size)
    r = z.ground(PNG, "Save")
    assert (r.x, r.y, r.w, r.h) == (600, 400, 40, 20)
    assert r.source == "zoom"
    assert len(inner.seen) == 2 and inner.seen[0][0] is PNG and inner.seen[1][1] == "Save"
    assert _crop_box(inner.seen[1][0]) == (left, top, left + crop_w, top + crop_h)
    # step-2 centre sits (100, 100) px from the crop centre; normalised by the half-diagonal
    assert r.confidence == pytest.approx(1 - (2 ** 0.5 * 100) / (0.5 * (500 ** 2 + 400 ** 2) ** 0.5), abs=1e-6)
    assert z.last_consistency == pytest.approx(1 - r.confidence, abs=1e-6)


def test_zoom_crop_is_clamped_to_image_bounds():
    corner = GroundResult(x=0, y=0, w=10, h=10, source="stub")  # centre (5, 5)
    inner = SeqGrounder([corner, GroundResult(x=0, y=0, w=10, h=10, source="stub")])
    r = ZoomGrounder(inner, scale=0.5, crop_fn=fake_crop, size_fn=fake_size).ground(PNG, "x")
    assert _crop_box(inner.seen[1][0]) == (0, 0, 500, 400)
    assert (r.x, r.y) == (0, 0)
    far = GroundResult(x=990, y=790, w=10, h=10, source="stub")
    inner = SeqGrounder([far, GroundResult(x=495, y=395, w=5, h=5, source="stub")])
    r = ZoomGrounder(inner, scale=0.5, crop_fn=fake_crop, size_fn=fake_size).ground(PNG, "x")
    assert _crop_box(inner.seen[1][0]) == (500, 400, 1000, 800)
    assert (r.x, r.y, r.w, r.h) == (995, 795, 5, 5)


def test_zoom_step2_far_from_centre_gives_low_confidence():
    inner = SeqGrounder([TRUE, GroundResult(x=0, y=0, w=4, h=4, source="stub")])  # crop corner
    r = ZoomGrounder(inner, scale=0.5, crop_fn=fake_crop, size_fn=fake_size).ground(PNG, "x")
    assert r.confidence < 0.3
    assert 0.0 <= r.confidence <= 1.0


def test_zoom_step1_absent_is_absent_and_step2_absent_is_zero_confidence():
    inner = SeqGrounder([None])
    assert ZoomGrounder(inner, crop_fn=fake_crop, size_fn=fake_size).ground(PNG, "x") is None
    assert len(inner.seen) == 1
    inner = SeqGrounder([TRUE, None])
    r = ZoomGrounder(inner, crop_fn=fake_crop, size_fn=fake_size).ground(PNG, "x")
    assert (r.x, r.y, r.w, r.h) == (600, 400, 40, 20) and r.confidence == 0.0


def test_zoom_step1_result_already_carries_confidence_floor():
    # inner reports its own confidence; zoom never raises above it
    weak = GroundResult(x=600, y=400, w=40, h=20, confidence=0.2, source="stub")
    inner = SeqGrounder([weak, _in_crop(TRUE, (370, 210))])
    r = ZoomGrounder(inner, crop_fn=fake_crop, size_fn=fake_size).ground(PNG, "x")
    assert r.confidence <= 0.2


def test_zoom_without_size_fn_needs_a_real_png():
    inner = SeqGrounder([TRUE])
    with pytest.raises(SchemaViolation):
        ZoomGrounder(inner).ground(PNG, "x")


def test_zoom_with_real_pillow_crop():
    import io

    from PIL import Image

    img = Image.new("RGB", (400, 300), (255, 255, 255))
    for px in range(300, 330):
        for py in range(200, 215):
            img.putpixel((px, py), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    screen = buf.getvalue()

    class RedFinder:
        name = "red"

        def __init__(self):
            self.calls = 0

        def ground(self, png, text):
            self.calls += 1
            im = Image.open(io.BytesIO(png)).convert("RGB")
            xs, ys = [], []
            for x in range(im.width):
                for y in range(im.height):
                    if im.getpixel((x, y)) == (255, 0, 0):
                        xs.append(x)
                        ys.append(y)
            if not xs:
                return None
            box = GroundResult(x=min(xs), y=min(ys), w=max(xs) - min(xs) + 1, h=max(ys) - min(ys) + 1, source="red")
            if self.calls == 1:  # deliberately offset on the full screen
                return GroundResult(x=box.x - 20, y=box.y - 10, w=box.w, h=box.h, source="red")
            return box

    r = ZoomGrounder(RedFinder(), scale=0.5).ground(screen, "red block")
    assert (r.x, r.y, r.w, r.h) == (300, 200, 30, 15)
    assert r.confidence > 0.5
    assert Image.open(io.BytesIO(crop_png(screen, (10, 20, 110, 70)))).size == (100, 50)


def test_grounder_gate_rejects_low_confidence_and_runs_fallback():
    primary = StubGrounder("vlm", GroundResult(**BOX, confidence=0.1, source="vlm"))
    fallback = StubGrounder("claude", GroundResult(x=1, y=2, w=3, h=4, source="claude"))
    g = Grounder(primary, fallback, min_confidence=0.3)
    out = g.ground(PNG, "Save")
    assert {k: out[k] for k in BOX} == {"x": 1, "y": 2, "w": 3, "h": 4}
    assert out["confidence"] == 1.0 and out["source"] == "claude"
    assert g.last_source == "claude" and g.last_confidence == 1.0
    assert (primary.calls, fallback.calls) == (1, 1)
    assert any("confidence" in e and "vlm" in e for e in g.last_errors)


def test_grounder_gate_without_fallback_returns_none():
    primary = StubGrounder("vlm", GroundResult(**BOX, confidence=0.1, source="vlm"))
    g = Grounder(primary, min_confidence=0.3)
    assert g.ground(PNG, "Save") is None
    assert g.last_result is None and g.last_source == "none"
    assert Grounder(primary, min_confidence=0.0).ground(PNG, "Save")["confidence"] == 0.1


def test_grounder_result_dict_carries_confidence():
    primary = StubGrounder("vlm", GroundResult(**BOX, confidence=0.75, source="vlm"))
    out = Grounder(primary).ground(PNG, "Save")
    assert out == {**BOX, "confidence": 0.75, "source": "vlm"}


def test_gated_result_never_reaches_locator_cache_via_executor(tmp_path):
    try:
        from o_engine.actionlog import ActionLog
        from o_engine.cu.backends.fake import FakeBackend
        from o_engine.cu.executor import ScriptRunner
        from o_engine.cu.schema import parse_script
    except ImportError as e:
        pytest.skip(f"F1 module missing: {e}")
    from o_engine.cu.backends.base import Node

    script = parse_script(
        {"app": "demo", "goal": "press", "steps": [
            {"op": "find", "as": "btn", "target": {"role": "push button", "name": "Hidden"}},
        ]}
    )
    # zoom step 2 lands in the crop corner -> confidence below the gate -> fallback (absent) -> no row
    inner = SeqGrounder([TRUE, GroundResult(x=0, y=0, w=4, h=4, source="stub")])
    zoom = ZoomGrounder(inner, crop_fn=fake_crop, size_fn=fake_size)
    fallback = StubGrounder("claude", None)
    cache = LocatorCache(tmp_path / "locators.sqlite")
    backend = FakeBackend(Node(id="root", role="frame", name="Demo"), screenshot=PNG, allow_pixels=True)
    runner = ScriptRunner(backend, ActionLog(tmp_path / "data"), grounder=Grounder(zoom, fallback, min_confidence=0.3), locator_cache=cache)
    result = runner.run(script, "job-1")
    assert not result.ok
    assert fallback.calls == 1
    assert cache.count() == 0

    # a confident zoom result is written with its confidence
    inner = SeqGrounder([TRUE, _in_crop(TRUE, (370, 210))])
    backend = FakeBackend(Node(id="root", role="frame", name="Demo"), screenshot=PNG, allow_pixels=True)
    runner = ScriptRunner(backend, ActionLog(tmp_path / "data"), grounder=Grounder(ZoomGrounder(inner, crop_fn=fake_crop, size_fn=fake_size)), locator_cache=cache)
    assert runner.run(script, "job-2").ok
    row = cache.get("demo", script.steps[0].target, screen_sha(PNG))
    assert (row["x"], row["y"], row["w"], row["h"]) == (600, 400, 40, 20)
    assert row["source"] == "zoom" and row["confidence"] == pytest.approx(1.0)


def test_multicrop_vote_picks_mode():
    agree = GroundResult(x=600, y=400, w=40, h=20, source="stub")
    inner = SeqGrounder([TRUE])  # step 1
    v = MultiCropVote(inner, k=3, shift=0.1, scale=0.5, crop_fn=fake_crop, size_fn=fake_size)
    # answer each crop relative to its own box: two agree on TRUE, one is far off
    crops = []

    def ground(png, text):
        inner.seen.append((png, text))
        if len(inner.seen) == 1:
            return TRUE
        box = _crop_box(png)
        crops.append(box)
        if len(crops) == 2:
            return GroundResult(x=5, y=5, w=4, h=4, source="stub")
        return _in_crop(agree, box)

    v.inner = type("G", (), {"name": "stub", "ground": staticmethod(ground)})()
    r = v.ground(PNG, "x")
    assert (r.x, r.y, r.w, r.h) == (600, 400, 40, 20)
    assert r.source == "vote"
    assert len(crops) == 3 and len({c[:2] for c in crops}) == 3  # three distinct shifted crops
    assert v.last_votes == 2
    assert 0.0 < r.confidence <= 2 / 3


def test_multicrop_vote_absent_and_all_disagree():
    inner = SeqGrounder([None])
    assert MultiCropVote(inner, crop_fn=fake_crop, size_fn=fake_size).ground(PNG, "x") is None
    calls = {"n": 0}

    def ground(png, text):
        calls["n"] += 1
        if calls["n"] == 1:
            return TRUE
        box = _crop_box(png)
        return GroundResult(x=10 * calls["n"], y=10 * calls["n"], w=4, h=4, source="s")  # scattered

    g = type("G", (), {"name": "s", "ground": staticmethod(ground)})()
    r = MultiCropVote(g, k=3, crop_fn=fake_crop, size_fn=fake_size).ground(PNG, "x")
    assert r is not None and r.confidence <= 1 / 3


def test_locator_cache_round_trips_confidence(tmp_path):
    cache = LocatorCache(tmp_path / "locators.sqlite")
    sha = screen_sha(PNG)
    assert cache.put("app", "Save", sha, {**BOX, "confidence": 0.42, "source": "zoom"}) is True
    row = cache.get("app", "Save", sha)
    assert row["confidence"] == pytest.approx(0.42) and row["source"] == "zoom"
    assert cache.put("app", "Go", sha, GroundResult(**BOX, confidence=0.9, source="vote")) is True
    assert cache.get("app", "Go", sha)["confidence"] == pytest.approx(0.9)
    assert cache.put("app", "Plain", sha, BOX) is True
    assert cache.get("app", "Plain", sha)["confidence"] == 1.0


def test_locator_cache_opens_old_db_without_confidence_column(tmp_path):
    path = tmp_path / "locators.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE locators (key TEXT PRIMARY KEY, app TEXT NOT NULL, target TEXT NOT NULL,"
        " screen_sha TEXT NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL, w INTEGER NOT NULL,"
        " h INTEGER NOT NULL, created REAL NOT NULL)"
    )
    sha = screen_sha(PNG)
    conn.execute(
        "INSERT INTO locators (key, app, target, screen_sha, x, y, w, h, created) VALUES (?,?,?,?,?,?,?,?,?)",
        (locator_key("app", "Old", sha), "app", "Old", sha, 1, 2, 3, 4, 0.0),
    )
    conn.commit()
    conn.close()
    cache = LocatorCache(path)
    old = cache.get("app", "Old", sha)
    assert (old["x"], old["y"], old["w"], old["h"]) == (1, 2, 3, 4)
    assert old["confidence"] == 1.0 and old["source"] == ""
    assert cache.put("app", "Old", sha, {**BOX, "confidence": 0.5}) is False  # append-only kept
    assert cache.put("app", "New", sha, {**BOX, "confidence": 0.5, "source": "zoom"}) is True
    assert cache.get("app", "New", sha)["confidence"] == pytest.approx(0.5)
    cols = {r[1] for r in sqlite3.connect(str(path)).execute("PRAGMA table_info(locators)")}
    assert {"confidence", "source"} <= cols
    LocatorCache(path)  # idempotent migration


# ---------------------------------------------------------------- B1a: Pillow is a declared hard dependency

def test_crop_png_crops_generated_rgba_png_via_pillow():
    import io

    from PIL import Image

    img = Image.new("RGBA", (120, 80), (0, 0, 0, 0))
    for px in range(40, 70):
        for py in range(20, 50):
            img.putpixel((px, py), (0, 255, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    screen = buf.getvalue()
    assert png_size(screen) == (120, 80)
    out = crop_png(screen, (40, 20, 70, 50))
    assert png_size(out) == (30, 30)
    with Image.open(io.BytesIO(out)) as cropped:
        assert cropped.mode == "RGBA"
        assert cropped.getpixel((0, 0)) == (0, 255, 0, 255)
        assert cropped.getpixel((29, 29)) == (0, 255, 0, 255)


def test_zoom_grounder_defaults_to_pillow_crop_but_keeps_crop_and_size_pluggable():
    z = ZoomGrounder(SeqGrounder([]))
    assert z.crop_fn is crop_png and z.size_fn is png_size
    z = ZoomGrounder(SeqGrounder([]), crop_fn=fake_crop, size_fn=fake_size)
    assert z.crop_fn is fake_crop and z.size_fn is fake_size
    v = MultiCropVote(SeqGrounder([]), crop_fn=fake_crop, size_fn=fake_size)
    assert v.crop_fn is fake_crop and v.size_fn is fake_size


def test_pyproject_declares_pillow_as_hard_dependency():
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    deps = text.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert re.search(r'"pillow>=\d', deps, re.IGNORECASE), deps
    optional = text.split("[project.optional-dependencies]", 1)[1]
    assert "pillow" not in optional.lower()
