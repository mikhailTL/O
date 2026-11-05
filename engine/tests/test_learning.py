"""LearningStore: observations record, derived profile, forgetting, privacy, prompt rendering."""
import datetime as dt
import json
import sqlite3
import time

import pytest

from o_engine.learning import LearnedNotes, LearningStore, Observation, scrub, task_choices


def _ts(days_ago: int, hour: int, minute: int = 0) -> float:
    d = dt.datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0) - dt.timedelta(days=days_ago)
    return d.timestamp()


def _past_weekdays(n: int, start: int = 1) -> list[int]:
    out, k = [], start
    while len(out) < n:
        if (dt.date.today() - dt.timedelta(days=k)).weekday() < 5:
            out.append(k)
        k += 1
    return out


def _past_mondays(n: int) -> list[float]:
    today = dt.datetime.now().replace(hour=9, minute=5, second=0, microsecond=0)
    back = (today.weekday() - 0) % 7 or 7
    first = today - dt.timedelta(days=back)
    return [(first - dt.timedelta(weeks=i)).timestamp() for i in range(n)]


def _lines(store):
    return [json.loads(l) for l in store.path.read_text().splitlines() if l.strip()]


# ------------------------------------------------------------------ record + preferences


def test_observe_appends_jsonl_and_profile_is_cache(tmp_path):
    s = LearningStore(tmp_path)
    o = s.observe("correction", "No, always name files YYYY-MM-DD")
    assert o.id.startswith("obs-")
    assert len(_lines(s)) == 1 and _lines(s)[0]["source"] == "correction"
    prof = json.loads(s.profile_path.read_text())
    assert prof["observations"] == 1
    s.profile_path.unlink()
    fresh = LearningStore(tmp_path)
    assert fresh.profile()["preferences"][0]["evidence"] == [o.id]


def test_observation_model_is_strict_frozen():
    with pytest.raises(Exception):
        Observation(id="x", ts=1.0, source="task", text="t", extra=1)
    with pytest.raises(Exception):
        Observation(id="x", ts=1.0, source="nope", text="t")
    with pytest.raises(Exception):
        Observation(id="x", ts=1.0, source="task", text="t" * 2001)
    o = Observation(id="x", ts=1.0, source="task", text="t")
    with pytest.raises(Exception):
        o.text = "y"


def test_two_corrections_three_similar_tasks(tmp_path):
    s = LearningStore(tmp_path)
    c1 = s.observe("correction", "No, always name files YYYY-MM-DD")
    c2 = s.observe("correction", "Use a formal tone in work emails")
    t1 = s.observe("task", "book a flight to NYC under $300", app="Chrome")
    t2 = s.observe("task", "find me a flight to Boston under $300")
    t3 = s.observe("task", "book a nonstop flight to Denver under $300")
    s.observe("task", "buy groceries under $300")  # same choice, different topic: not evidence
    prefs = s.profile()["preferences"]
    by_ev = {tuple(p["evidence"]): p for p in prefs}
    assert by_ev[(c1.id,)]["confidence"] == pytest.approx(0.9)
    assert by_ev[(c1.id,)]["text"] == "Always name files YYYY-MM-DD"
    assert by_ev[(c2.id,)]["confidence"] == pytest.approx(0.9)
    flight = by_ev[(t1.id, t2.id, t3.id)]
    assert flight["kind"] == "preference"
    assert "under $300" in flight["text"] and "flight" in flight["text"]
    assert flight["confidence"] == pytest.approx(0.65)
    assert all(set(p["evidence"]) <= {c1.id, c2.id, t1.id, t2.id, t3.id} for p in prefs)
    assert len(prefs) == 3


def test_near_duplicate_corrections_merge_deterministically(tmp_path):
    s = LearningStore(tmp_path)
    a = s.observe("correction", "name files YYYY-MM-DD")
    b = s.observe("correction", "actually, name the files YYYY-MM-DD")
    prefs = s.profile()["preferences"]
    assert len(prefs) == 1 and prefs[0]["evidence"] == [a.id, b.id]
    assert prefs[0]["confidence"] > 0.9


def test_task_choices_extraction():
    choices, topic = task_choices("Export the report as PDF and book a window seat under $450")
    assert "PDF output" in choices and "window seats" in choices and "under $450" in choices
    assert "report" in topic


# ------------------------------------------------------------------ habits from screenpipe


def _screen_db(path, frames):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE frames (id INTEGER PRIMARY KEY, timestamp TEXT, app_name TEXT, window_name TEXT);
        CREATE TABLE ocr_text (frame_id INTEGER, text TEXT);
        """
    )
    for i, (ts, app, win) in enumerate(frames, start=1):
        iso = dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace("+00:00", "Z")
        conn.execute("INSERT INTO frames VALUES (?,?,?,?)", (i, iso, app, win))
        conn.execute("INSERT INTO ocr_text VALUES (?,?)", (i, "Dear bob@example.com, your PIN is 4421 SECRET BODY TEXT"))
    conn.commit()
    conn.close()


def test_screenpipe_weekday_morning_habit(tmp_path):
    frames = []
    for d in _past_weekdays(4):
        start = _ts(d, 8, 2)
        frames += [(start + 60 * k, "Mail", "Inbox (3)") for k in range(10)]
        frames += [(start + 600 + 60 * k, "Slack", "standup") for k in range(5)]
        frames += [(start + 900 + 60 * k, "Figma", "design") for k in range(3)]
    db = tmp_path / "sp.sqlite"
    _screen_db(db, frames)
    s = LearningStore(tmp_path / "learn", screenpipe_db=str(db))
    prof = s.profile()  # first read derives and ingests
    habits = prof["habits"]
    mail = [h for h in habits if h["app"] == "Mail" and "~8am on weekdays" in h["text"]]
    assert mail, habits
    assert mail[0]["confidence"] >= 0.7 and len(mail[0]["evidence"]) == 4
    raw = s.path.read_text()
    assert "SECRET BODY TEXT" not in raw and "bob@example.com" not in raw and "4421" not in raw
    assert all(r["source"] == "screen" and r["duration_s"] > 0 for r in _lines(s))
    n = len(_lines(s))
    s.derive()
    assert len(_lines(s)) == n  # ingestion is deduped


def test_screen_habit_threshold_needs_three_days(tmp_path):
    frames = []
    for d in _past_weekdays(2):
        frames += [(_ts(d, 8) + 60 * k, "Mail", "Inbox") for k in range(10)]
    db = tmp_path / "sp.sqlite"
    _screen_db(db, frames)
    s = LearningStore(tmp_path / "learn", screenpipe_db=str(db))
    assert s.profile()["habits"] == []


# ------------------------------------------------------------------ suggestions + app skills


def test_four_monday_tasks_yield_schedule_suggestion(tmp_path):
    s = LearningStore(tmp_path)
    ids = [s.observe("task", "Send the weekly status report to the team", ts=t, outcome="ok").id for t in _past_mondays(4)]
    sugg = s.profile()["suggestions"]
    assert len(sugg) == 1
    sch = sugg[0]["suggest_schedule"]
    assert sch["prompt"] == "Send the weekly status report to the team"
    assert sch["when"] == "every Monday ~9am" and sch["cron"] == "0 9 * * 1"
    assert set(sugg[0]["evidence"]) == set(ids)
    assert s.profile()["habits"] == []


def test_app_skill_from_outcomes_interfaces_and_procedures(tmp_path):
    from o_engine.cu.interfaces import InterfaceStore

    class Proc:
        def __init__(self, goal, healed=None):
            self.app, self.goal, self.healed_step = "LibreOffice Calc", goal, healed

    class Procs:
        def list(self):
            return [Proc("a"), Proc("a", healed=2), Proc("b")]

    ifs = InterfaceStore(tmp_path / "interfaces")
    ifs.write("LibreOffice Calc", [{"name": "set_cell", "signature": "(backend, cell, v)", "body": "return None"}])
    ifs.write_fitness("LibreOffice Calc", 4, 3)
    s = LearningStore(tmp_path / "learn", interfaces=ifs, procedures=lambda: Procs())
    s.observe("task", "put 42 in A1", app="LibreOffice Calc", outcome="ok", task_id="j1")
    s.observe("own", "put 43 in A2", app="LibreOffice Calc", outcome="failed", task_id="j2")
    s.observe("task", "sum column B", app="LibreOffice Calc", outcome="ok")
    skills = s.profile()["app_skills"]
    assert len(skills) == 1
    sk = skills[0]
    assert sk["stats"]["tasks"] == 3 and sk["stats"]["ok"] == 2
    assert sk["stats"]["fitness_passed"] == 3 and sk["stats"]["procedures"] == 2 and sk["stats"]["heals"] == 1
    assert "2/3 tasks ok" in sk["text"] and "fitness 3/4" in sk["text"]
    assert 0 < sk["confidence"] < 1


# ------------------------------------------------------------------ forgetting / confirming


def test_forget_removes_item_and_survives_rederivation(tmp_path):
    s = LearningStore(tmp_path)
    c = s.observe("correction", "Use a formal tone in work emails")
    tasks = [s.observe("task", f"book a flight to {city} under $300") for city in ("NYC", "Boston")]
    prefs = s.profile()["preferences"]
    flight = next(p for p in prefs if "flight" in p["text"])
    s.forget(flight["id"])
    assert all(p["id"] != flight["id"] for p in s.profile()["preferences"])
    assert any(p["evidence"] == [c.id] for p in s.profile()["preferences"])
    n = len(_lines(s))
    assert _lines(s)[-1]["text"] == f"forget {flight['id']}" and _lines(s)[-1]["source"] == "own"
    s.profile_path.unlink()
    fresh = LearningStore(tmp_path)
    fresh.derive()
    assert all(p["id"] != flight["id"] for p in fresh.profile()["preferences"])
    assert len(_lines(fresh)) == n
    assert {t.id for t in tasks} <= {r["id"] for r in _lines(fresh)}  # nothing deleted from the record


def test_forget_drops_items_evidenced_only_by_it(tmp_path):
    s = LearningStore(tmp_path)
    ids = [s.observe("task", "Send the weekly status report", ts=t, outcome="ok", app="Mail").id for t in _past_mondays(4)]
    prof = s.profile()
    sugg = prof["suggestions"][0]
    assert prof["app_skills"] and set(prof["app_skills"][0]["evidence"]) == set(ids)
    s.forget(sugg["id"])
    prof = s.profile()
    assert prof["suggestions"] == [] and prof["app_skills"] == []


def test_confirm_raises_confidence(tmp_path):
    s = LearningStore(tmp_path)
    for city in ("NYC", "Boston"):
        s.observe("task", f"book a flight to {city} under $300")
    p = s.profile()["preferences"][0]
    s.confirm(p["id"])
    assert s.item(p["id"])["confidence"] == pytest.approx(p["confidence"] + 0.1)


# ------------------------------------------------------------------ privacy


PII = [
    ("email me at jane.doe+work@example.co.uk", "jane.doe"),
    ("card 4111 1111 1111 1111 exp 12/29", "4111"),
    ("card 4111-1111-1111-1111", "1111-1111"),
    ("account 000123456789 routing", "000123456789"),
    ("IBAN DE89 3704 0044 0532 0130 00", "3704"),
    ("my password is hunter2!", "hunter2"),
    ("password: Tr0ub4dor&3", "Tr0ub4dor"),
    ("use token=ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abc"),
    ("key sk-live-abcdefghijklmnop1234", "abcdefghijklmnop"),
    ("Authorization: Bearer abc.def.ghi-123", "abc.def"),
    ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig_abc-123", "eyJhbGci"),
    ("AKIAIOSFODNN7EXAMPLE leaked", "AKIAIOSFODNN7"),
    ("ssn 123-45-6789", "123-45-6789"),
    ("api_key: 9f8e7d6c5b4a", "9f8e7d6c5b4a"),
]


@pytest.mark.parametrize("text,secret", PII)
def test_scrub_regexes(text, secret):
    assert secret not in scrub(text)
    assert scrub(scrub(text)) == scrub(text)


def test_pii_never_reaches_observations_jsonl(tmp_path):
    s = LearningStore(tmp_path)
    for text, _ in PII:
        s.observe("task", f"please {text} then book a flight under $300", app="Mail jane.doe+work@example.co.uk")
    s.observe("correction", "send to bob@corp.com, pin 9911")
    raw = s.path.read_text() + s.profile_path.read_text()
    for _, secret in PII:
        assert secret not in raw
    assert "bob@corp.com" not in raw and "9911" not in raw
    assert "under $300" in raw  # ordinary choices survive


def test_scrub_keeps_ordinary_text():
    assert scrub("book a flight to NYC under $300 on 2026-11-05 at 9am") == "book a flight to NYC under $300 on 2026-11-05 at 9am"


# ------------------------------------------------------------------ model merge + prompt


class FakeLocal:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append(user)
        return self.reply


class FakeClient:
    def __init__(self, local):
        self._local = local
        self.cloud_calls = 0

    def local_only(self):
        return self._local

    def complete_json(self, system, user):
        self.cloud_calls += 1
        raise AssertionError("cloud fallback must never be used")


def test_model_merge_is_local_only_and_persists(tmp_path):
    s = LearningStore(tmp_path)
    a = s.observe("correction", "Prefer short replies")
    b = s.observe("correction", "Keep email answers brief")
    ids = sorted(p["id"] for p in s.profile()["preferences"])
    reply = json.dumps({"merge": [{"ids": ids, "text": "Prefers brief replies"}]})
    client = FakeClient(FakeLocal(reply))
    s.model = client
    assert s.merge_with_model() == 1
    prefs = s.profile()["preferences"]
    assert len(prefs) == 1 and prefs[0]["text"] == "Prefers brief replies"
    assert set(prefs[0]["evidence"]) == {a.id, b.id}
    assert client.cloud_calls == 0
    s.observe("task", "something else")  # a later derive keeps the merge
    assert len(LearningStore(tmp_path).profile()["preferences"]) == 1


def test_no_local_model_means_no_call(tmp_path):
    s = LearningStore(tmp_path, model=FakeClient(None))
    s.observe("correction", "Prefer short replies")
    s.observe("correction", "Keep email answers brief")
    assert s.merge_with_model() == 0
    assert s.merge_async() is False


def test_render_for_prompt_and_learned_notes(tmp_path):
    s = LearningStore(tmp_path)
    for i in range(20):
        s.observe("correction", f"rule number {i} about thing{i} widget{i}", derive=False)
    s.observe("task", "put 1 in A1", app="Calc", outcome="ok")
    text = s.render_for_prompt(app="Calc")
    lines = text.splitlines()
    assert len(lines) <= 15 and lines[-1].startswith("O's record here: Calc")
    only7 = s.render_for_prompt(topic="number 7")
    assert only7.count("\n- ") == 1 and "Rule number 7 about" in only7

    class Inner:
        def for_prompt(self, app, max_chars=6000):
            return "## notes"

        def append(self, app, text):
            return "appended"

    notes = LearnedNotes(Inner(), s)
    out = notes.for_prompt("Calc")
    assert out.startswith("## notes\n\nLearned from") and "O's record here" in out
    assert notes.append("Calc", "x") == "appended"
