"""P10: offline grounding/judge regression on two bundled synthetic episodes (stub backends)."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "offline"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from eval.osworld import offline, summarize  # noqa: E402

# Stub behaviour (documented in offline.py): the grounder always answers the top-left
# 32x32 box, the judge always answers ok=True. Expected counts follow from the fixtures.
GEDIT = json.loads((FIXTURES / "ep-gedit" / "meta.json").read_text())
CALC = json.loads((FIXTURES / "ep-calc" / "meta.json").read_text())


def _expected_hits(meta: dict) -> int:
    return sum(1 for s in meta["steps"] if s["click"][0] < 32 and s["click"][1] < 32)


def test_fixture_shape():
    assert GEDIT["app"] != CALC["app"]
    assert GEDIT["result"] is True and CALC["result"] is False
    assert _expected_hits(GEDIT) == 1 and _expected_hits(CALC) == 1  # one miss in gedit


def test_load_episodes_layout_and_limit():
    eps = offline.load_episodes(FIXTURES)
    assert [e.name for e in eps] == ["ep-calc", "ep-gedit"]
    assert eps[0].app == CALC["app"] and eps[0].instruction == CALC["instruction"]
    assert len(eps[0].steps) == 1 and eps[0].steps[0].screenshot.is_file()
    assert offline.load_episodes(FIXTURES, limit=1)[0].name == "ep-calc"


def test_load_rejects_bad_meta(tmp_path):
    (tmp_path / "ep").mkdir()
    (tmp_path / "ep" / "meta.json").write_text('{"instruction": "x", "app": "a", "result": true, "steps": [{"click": [1, 2]}]}')
    with pytest.raises(ValueError):
        offline.load_episodes(tmp_path)


def test_dataset_hash_is_deterministic_without_git(tmp_path):
    import shutil
    src = tmp_path / "eps"
    shutil.copytree(FIXTURES, src)
    h1 = offline.dataset_hash(src)
    h2 = offline.dataset_hash(src)
    assert h1 == h2 and h1.startswith("sha256:")
    names = sorted(p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file())
    lines = "\n".join(f"{n}:{(src / n).stat().st_size}" for n in names)
    assert h1 == "sha256:" + hashlib.sha256(lines.encode("utf-8")).hexdigest()
    (tmp_path / "x.bin").write_bytes(b"ab")
    assert offline.dataset_hash(tmp_path) != h1


def test_run_with_stubs_counts_match_stub_behaviour():
    report = offline.run(offline.load_episodes(FIXTURES), offline.StubGrounder(), offline.StubJudge())
    apps = {r.app: r for r in report.rows}
    assert set(apps) == {GEDIT["app"], CALC["app"]}
    g, c = apps[GEDIT["app"]], apps[CALC["app"]]
    assert (g.steps, g.hits) == (len(GEDIT["steps"]), _expected_hits(GEDIT))
    assert (c.steps, c.hits) == (len(CALC["steps"]), _expected_hits(CALC))
    # judge always says ok=True: agrees with gedit (result true), disagrees with calc (result false)
    assert (g.judged, g.agree) == (1, 1)
    assert (c.judged, c.agree) == (1, 0)
    assert report.total.hits == _expected_hits(GEDIT) + _expected_hits(CALC)
    assert report.total.steps == len(GEDIT["steps"]) + len(CALC["steps"])


def test_grounder_exception_or_none_is_a_miss_not_a_crash():
    class Broken:
        def ground(self, png, target):
            raise RuntimeError("boom")

    class Absent:
        def ground(self, png, target):
            return None

    for g in (Broken(), Absent()):
        report = offline.run(offline.load_episodes(FIXTURES), g, offline.StubJudge())
        assert report.total.hits == 0 and report.total.steps == 3
        assert report.total.failures == 3


def test_cli_writes_table_with_rows_hash_and_counts(tmp_path):
    out = tmp_path / "table.md"
    proc = subprocess.run(
        [sys.executable, "-m", "eval.osworld.offline", "--episodes", str(FIXTURES), "--grounder", "stub",
         "--judge", "stub", "--out", str(out)],
        cwd=ENGINE, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    text = out.read_text(encoding="utf-8")
    assert text.strip() in proc.stdout  # the CLI prints what it writes
    assert f"dataset hash: {offline.dataset_hash(FIXTURES)}" in text
    parsed = offline.parse_table(text)
    assert set(parsed) == {GEDIT["app"], CALC["app"], "all"}
    assert parsed[GEDIT["app"]]["hits"] == _expected_hits(GEDIT)
    assert parsed[GEDIT["app"]]["steps"] == len(GEDIT["steps"])
    assert parsed[CALC["app"]]["hits"] == _expected_hits(CALC)
    assert parsed[CALC["app"]]["agree"] == 0 and parsed[GEDIT["app"]]["agree"] == 1
    assert parsed["all"]["hits"] == _expected_hits(GEDIT) + _expected_hits(CALC)
    assert parsed["all"]["judged"] == 2


def test_cli_limit(tmp_path):
    out = tmp_path / "t.md"
    rc = offline.main(["--episodes", str(FIXTURES), "--grounder", "stub", "--judge", "stub", "--limit", "1",
                       "--out", str(out)])
    assert rc == 0
    parsed = offline.parse_table(out.read_text())
    assert set(parsed) == {CALC["app"], "all"}


def test_cli_empty_dir_fails(tmp_path):
    assert offline.main(["--episodes", str(tmp_path), "--grounder", "stub", "--judge", "stub"]) != 0


def test_summarize_includes_offline_table(tmp_path, capsys):
    import shutil
    src = tmp_path / "eps"
    shutil.copytree(FIXTURES, src)
    out = tmp_path / "table.md"
    assert offline.main(["--episodes", str(src), "--grounder", "stub", "--judge", "stub", "--out", str(out)]) == 0
    capsys.readouterr()
    rc = summarize.main(["--result_dir", str(tmp_path / "no-results"), "--offline", str(out)])
    printed = capsys.readouterr().out
    assert rc == 0
    parsed = offline.parse_table(printed)
    assert parsed["all"]["steps"] == len(GEDIT["steps"]) + len(CALC["steps"])
    assert "dataset hash: sha256:" in printed


def test_summarize_without_anything_still_fails(tmp_path, capsys):
    assert summarize.main(["--result_dir", str(tmp_path / "none")]) == 1


def test_readme_documents_layout_and_datasets():
    readme = (ENGINE / "eval" / "osworld" / "README.md").read_text(encoding="utf-8")
    for needle in ("meta.json", "offline.py", "--offline", "xlangai/ubuntu_osworld_verified_trajs",
                   "likaixin/ScreenSpot-Pro", "sai-osworld-v2-benchmark-runs", "dataset hash"):
        assert needle in readme, needle


# ---------------------------------------------------------------- B1b: --zoom {off,on,both}

def test_zoom_flag_default_off_and_choices():
    args = offline.build_parser().parse_args(["--episodes", "x"])
    assert args.zoom == "off"
    for choice in ("off", "on", "both"):
        assert offline.build_parser().parse_args(["--episodes", "x", "--zoom", choice]).zoom == choice
    with pytest.raises(SystemExit):
        offline.build_parser().parse_args(["--episodes", "x", "--zoom", "maybe"])


def test_zoomed_wraps_backend_in_zoom_grounder_at_half_scale():
    from o_engine.cu.ground import Grounder, ZoomGrounder

    stub = offline.StubGrounder()
    z = offline.zoomed(stub)
    assert isinstance(z, ZoomGrounder) and z.inner is stub and z.scale == 0.5
    g = offline.zoomed(Grounder(offline.StubGrounder(), offline.StubGrounder(), min_confidence=0.0))
    assert isinstance(g, Grounder)
    assert isinstance(g.primary, ZoomGrounder) and isinstance(g.fallback, ZoomGrounder)
    assert g.min_confidence == 0.0


def test_run_with_stub_under_zoom_still_counts_hits():
    from o_engine.cu.ground import ZoomGrounder

    inner = offline.StubGrounder()
    calls = []
    orig = inner.ground

    def spy(png, target):
        calls.append(target)
        return orig(png, target)

    inner.ground = spy
    report = offline.run(offline.load_episodes(FIXTURES), ZoomGrounder(inner, scale=0.5), offline.StubJudge())
    assert report.total.steps == 3 and report.total.failures == 0
    assert report.total.hits == _expected_hits(GEDIT) + _expected_hits(CALC)
    assert len(calls) == 2 * report.total.steps  # zoom grounds twice per step
    boxes = [d["box"] for d in report.details if "box" in d]
    assert all(set(b) >= {"x", "y", "w", "h"} for b in boxes)


def test_cli_zoom_on_with_stub_produces_hit_counts(tmp_path):
    out = tmp_path / "t.md"
    assert offline.main(["--episodes", str(FIXTURES), "--grounder", "stub", "--judge", "stub", "--zoom", "on",
                         "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "- zoom: on" in text
    parsed = offline.parse_table(text)
    assert set(parsed) == {GEDIT["app"], CALC["app"], "all"}
    assert parsed["all"]["hits"] == _expected_hits(GEDIT) + _expected_hits(CALC)
    assert parsed["all"]["steps"] == 3


def test_cli_zoom_both_prints_two_sections_with_zoom_column(tmp_path):
    out = tmp_path / "both.md"
    proc = subprocess.run(
        [sys.executable, "-m", "eval.osworld.offline", "--episodes", str(FIXTURES), "--grounder", "stub",
         "--judge", "stub", "--zoom", "both", "--out", str(out)],
        cwd=ENGINE, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    text = out.read_text(encoding="utf-8")
    assert text.strip() in proc.stdout
    assert text.count(offline.ZOOM_TABLE_HEADER) == 2
    assert "## zoom: off" in text and "## zoom: on" in text
    assert text.index("## zoom: off") < text.index("## zoom: on")
    assert offline.ZOOM_TABLE_HEADER.startswith("| zoom | app |")
    parsed = offline.parse_table(text)
    assert set(parsed) == {f"{z}:{a}" for z in ("off", "on") for a in (GEDIT["app"], CALC["app"], "all")}
    for z in ("off", "on"):
        assert parsed[f"{z}:all"]["steps"] == 3
        assert parsed[f"{z}:all"]["hits"] == _expected_hits(GEDIT) + _expected_hits(CALC)
        assert parsed[f"{z}:all"]["judged"] == 2
    assert "| off | gedit |" in text and "| on | gedit |" in text


def test_summarize_offline_parses_zoom_both_table(tmp_path, capsys):
    out = tmp_path / "both.md"
    assert offline.main(["--episodes", str(FIXTURES), "--grounder", "stub", "--judge", "stub", "--zoom", "both",
                         "--out", str(out)]) == 0
    capsys.readouterr()
    assert summarize.main(["--result_dir", str(tmp_path / "none"), "--offline", str(out)]) == 0
    printed = capsys.readouterr().out
    parsed = offline.parse_table(printed)
    assert parsed["off:all"]["steps"] == 3 and parsed["on:all"]["steps"] == 3


def test_readme_documents_zoom_flag_and_claims_no_number():
    readme = (ENGINE / "eval" / "osworld" / "README.md").read_text(encoding="utf-8")
    assert "--zoom" in readme and "both" in readme and "ZoomGrounder" in readme
    zoom_section = readme[readme.index("--zoom"):]
    assert not re.search(r"\d+(\.\d+)?\s*%", zoom_section), "README must not state a hit rate"
