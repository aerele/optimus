"""Recorded actions are analysed, saved and rendered in the order they started."""

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from optimus import analyze
from optimus.analyzers import per_action
from optimus.tests.test_analyze_delivery_guards import delivery  # noqa: F401

T0 = datetime(2026, 11, 6, 10, 0, 0)


def _rec(uuid, time):
	return {"uuid": uuid, "time": time, "path": f"/{uuid}", "calls": []}


def _uuids(recs):
	return [r["uuid"] for r in recs]


def test_run_hands_every_stage_the_chronological_list(delivery, monkeypatch):  # noqa: F811
	state = delivery
	state.requested = ["a1", "b2", "c3"]
	state.recordings = [
		_rec("a1", T0 + timedelta(seconds=2)),
		_rec("b2", T0),
		_rec("c3", T0 + timedelta(seconds=1)),
	]
	seen = {}

	def spy(name):
		def record(recs, *args, **kwargs):
			seen.setdefault(name, _uuids(recs))

		return record

	monkeypatch.setattr(analyze, "_get_analyzers", lambda: [spy("analyzer")])
	monkeypatch.setattr(analyze, "_persist", lambda docname, context, recs, *a, **kw: spy("persist")(recs))
	monkeypatch.setattr(
		analyze, "_render_and_attach_reports", lambda docname, recs, *a, **kw: spy("render")(recs)
	)
	analyze.run("s")
	expected = ["b2", "c3", "a1"]
	assert seen == {"analyzer": expected, "persist": expected, "render": expected}


def test_orders_by_start_time_across_types():
	recs = [
		_rec("a", T0 + timedelta(seconds=3)),
		_rec("b", str(T0 + timedelta(seconds=1))),
		_rec("c", str(T0 + timedelta(seconds=2, microseconds=5))),
		_rec("d", T0),
		_rec("e", str(T0 + timedelta(seconds=1, microseconds=1))),
	]
	assert _uuids(analyze._chronological(recs)) == ["d", "b", "e", "c", "a"]


def test_equal_times_fall_back_to_uuid_even_when_input_is_reversed():
	recs = [_rec("c", T0), _rec("b", T0), _rec("a", T0)]
	assert _uuids(analyze._chronological(recs)) == ["a", "b", "c"]


@pytest.mark.parametrize("bad", [None, "", "garbage", 5, {"x": 1}, b"x", "__missing__"])
def test_untimed_recordings_go_last_ordered_by_uuid(bad):
	untimed = []
	for uuid in ("z", "y", "x"):
		rec = _rec(uuid, bad)
		if bad == "__missing__":
			del rec["time"]
		untimed.append(rec)
	recs = untimed + [_rec("t2", T0 + timedelta(seconds=1)), _rec("t1", T0)]
	assert _uuids(analyze._chronological(recs)) == ["t1", "t2", "x", "y", "z"]


def test_aware_time_never_raises_and_keeps_every_recording():
	recs = [
		_rec("a", T0),
		_rec("b", datetime(2026, 11, 6, 4, 30, 1, tzinfo=timezone.utc)),
		_rec("c", "2026-11-06 10:00:00+05:30"),
	]
	assert sorted(_uuids(analyze._chronological(recs))) == ["a", "b", "c"]


def test_missing_uuid_is_tolerated():
	recs = [{"time": T0}, {"uuid": None, "time": None}, _rec("a", T0)]
	assert len(analyze._chronological(recs)) == 3


def test_does_not_mutate_input():
	recs = [_rec("b", T0 + timedelta(seconds=1)), _rec("a", T0)]
	analyze._chronological(recs)
	assert _uuids(recs) == ["b", "a"]


_when = st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1))
_time = st.one_of(st.none(), st.text(max_size=8), st.integers(), _when, _when.map(str))


@given(st.lists(st.fixed_dictionaries({"uuid": st.text(max_size=4), "time": _time}), max_size=12))
def test_permutation_idempotent_and_timed_ordered(recs):
	out = analyze._chronological(recs)
	assert sorted(id(r) for r in out) == sorted(id(r) for r in recs)
	assert analyze._chronological(out) == out
	starts = [analyze._recording_start(r) for r in out]
	timed = [s for s in starts if s is not None]
	assert timed == sorted(timed)
	assert starts[: len(timed)] == timed


def test_actions_and_steps_follow_the_sorted_list():
	recs = analyze._chronological(
		[_rec("b", T0 + timedelta(seconds=1)), _rec("a", T0 + timedelta(seconds=2)), _rec("c", T0)]
	)
	actions = per_action.analyze(recs, None).actions
	assert [a["recording_uuid"] for a in actions] == _uuids(recs) == ["c", "b", "a"]
	html = analyze._build_auto_notes_list_html(recs)
	assert html.index("/c") < html.index("/b") < html.index("/a")
