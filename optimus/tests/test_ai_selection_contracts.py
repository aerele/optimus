# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The contracts of the helpers the background refresh builds on: which findings Refresh
selects (``analyze.eligible_findings``), how the resume cutoff reads a time, and what the
light recording loader keeps in memory."""

import datetime as dt
import gc
import json
import time
import weakref
from types import SimpleNamespace

import pytest

from optimus import ai_fix, ai_prompts, analyze

V = ai_prompts.PROMPT_VERSION
ELIGIBLE_TYPE = "N+1 Query"


def row(name, stored=None, **kw):
	return SimpleNamespace(
		name=name,
		finding_type=kw.pop("finding_type", ELIGIBLE_TYPE),
		llm_fix_json=stored,
		severity=kw.pop("severity", "High"),
		estimated_impact_ms=kw.pop("estimated_impact_ms", 5),
		technical_detail_json="{}",
		**kw,
	)


def answer(**kw):
	fix = {"suggestion": "answer", "prompt_version": V, "generated_at": "2026-01-02T10:00:00+00:00"}
	fix.update(kw)
	return json.dumps(fix)


def config(*excluded):
	return SimpleNamespace(ai_excluded_finding_types=excluded)


def names(rows):
	return [r.name for r in rows]


def pick(rows, **kw):
	return names(analyze.eligible_findings(rows, config(), **kw))


@pytest.fixture
def system_tz(monkeypatch):
	"""Pretend the site's System Settings timezone is ``name``."""

	def use(name):
		monkeypatch.setattr(analyze, "_system_timezone_name", lambda: name)

	return use


# ---- one definition of "missing / outdated / current" ------------------------------------------
@pytest.mark.parametrize(
	"stored,state",
	[
		(None, "missing"),
		("", "missing"),
		("  \n", "missing"),
		("{", "outdated"),
		("[]", "outdated"),
		('"answer"', "outdated"),
		("null", "outdated"),
		("7", "outdated"),
		(b"{}", "outdated"),
		("{}", "outdated"),
		(answer(), "current"),
		(answer(prompt_version=V + 1), "current"),
		(answer(prompt_version=V - 1), "outdated"),
		(answer(prompt_version=float(V)), "outdated"),
		(answer(prompt_version=True), "outdated"),
		(answer(prompt_version=str(V)), "outdated"),
		(answer(prompt_version=None), "outdated"),
		(json.dumps({"suggestion": "x"}), "outdated"),
		(answer(error="boom"), "current"),
		(answer(suggestion=5), "outdated"),
		(answer(suggestion=["x"]), "outdated"),
		(answer(suggestion="  "), "outdated"),
		(answer(suggestion=[]), "outdated"),
		(answer(guardrail={"fallback": True}), "current"),
		({"suggestion": "x", "prompt_version": V}, "current"),
	],
)
def test_fix_state_is_the_one_definition(stored, state):
	assert ai_prompts.fix_state(stored)[0] == state


def test_fix_state_honours_a_given_version_floor():
	assert ai_prompts.fix_state(answer(prompt_version=V - 1), V - 1)[0] == "current"
	assert ai_prompts.fix_state(answer(prompt_version=V - 1), V)[0] == "outdated"


@pytest.mark.parametrize(
	"fix",
	[
		{"suggestion": "x", "prompt_version": V},
		{"suggestion": "x", "prompt_version": V + 1},
		{"suggestion": "x", "prompt_version": V - 1},
		{"suggestion": "x", "prompt_version": float(V)},
		{"suggestion": "x", "prompt_version": True},
		{"suggestion": "x", "prompt_version": str(V)},
		{"suggestion": "x"},
		{"suggestion": "x", "prompt_version": V, "guardrail": {"fallback": True}},
		{"suggestion": "x", "prompt_version": V, "error": "boom"},
		{"suggestion": "x", "prompt_version": V - 1, "error": "boom"},
	],
)
def test_the_report_and_refresh_agree_on_a_shown_answer(fix):
	"""A card the report shows is marked outdated exactly when Refresh counts it outdated."""
	from optimus.renderer.recipe_enrichment import mark_outdated_ai_fixes

	finding = {"llm_fix": dict(fix)}
	mark_outdated_ai_fixes([finding])
	refresh_picks_it = pick([row("f", json.dumps(fix))]) == ["f"]
	assert refresh_picks_it == finding["llm_fix"]["outdated"]


# ---- eligible_findings: outdated is "not is_current" ---------------------------------------------
def test_a_newer_answer_is_never_rebilled_or_downgraded():
	rows = [row("newer", answer(prompt_version=V + 1))]
	assert pick(rows) == [] and pick(rows, include_outdated=False) == []
	assert pick(rows, regenerate_all=True) == ["newer"]


@pytest.mark.parametrize("version", [float(V), True, str(V), None])
def test_a_version_is_current_does_not_accept_is_outdated(version):
	assert not ai_prompts.is_current({"prompt_version": version})
	assert pick([row("x", answer(prompt_version=version))]) == ["x"]


def test_empty_records_are_retried_but_a_fallback_or_error_keyed_answer_is_not():
	rows = [
		row("empty", answer(suggestion="")),
		row("error-key", answer(error="boom")),
		row("fallback", answer(guardrail={"fallback": True})),
	]
	assert pick(rows) == ["empty"]


@pytest.mark.parametrize("stored", ["{", "[]", '"answer"', "null"])
def test_unparseable_text_is_outdated_not_missing(stored):
	assert pick([row("x", stored)], include_outdated=False) == []
	assert pick([row("x", stored)]) == ["x"]


# ---- order: missing, outdated, current; then severity, impact, age -------------------------------
def test_order_is_state_then_severity_then_impact_then_age():
	old, new = "2026-01-01T00:00:00+00:00", "2026-01-03T00:00:00+00:00"
	rows = [
		row("current-high", answer(), severity="High", estimated_impact_ms=900),
		row("outdated-high", answer(prompt_version=V - 1), severity="High", estimated_impact_ms=900),
		row("missing-low", severity="Low", estimated_impact_ms=1),
		row("missing-med-small", severity="Medium", estimated_impact_ms=1),
		row("missing-med-big", severity="Medium", estimated_impact_ms=50),
		row("outdated-new", answer(prompt_version=V - 1, generated_at=new), severity="Medium", estimated_impact_ms=7),
		row("outdated-old", answer(prompt_version=V - 1, generated_at=old), severity="Medium", estimated_impact_ms=7),
	]
	assert pick(rows, regenerate_all=True) == [
		"missing-med-big",
		"missing-med-small",
		"missing-low",
		"outdated-high",
		"outdated-old",
		"outdated-new",
		"current-high",
	]


def test_a_blank_severity_ranks_as_low_and_an_unrecognised_one_goes_last():
	rows = [
		row("junk", severity="Urgent"),
		row("list", severity=[]),
		row("none", severity=None),
		row("empty", severity=""),
		row("low", severity="Low"),
		row("high", severity="High"),
	]
	got = pick(rows)
	assert got[0] == "high" and got[1:4] == ["none", "empty", "low"]
	assert set(got[4:]) == {"junk", "list"}


def test_a_bigger_impact_goes_first_and_a_bad_impact_counts_as_zero():
	rows = [
		row("nan", estimated_impact_ms=float("nan")),
		row("inf", estimated_impact_ms=float("inf")),
		row("small", estimated_impact_ms=10),
		row("big", estimated_impact_ms=50),
		row("junk", estimated_impact_ms="junk"),
	]
	assert pick(rows) == ["big", "small", "nan", "inf", "junk"]


# ---- selection counts -----------------------------------------------------------------------------
def test_the_selection_carries_the_gated_and_excluded_counts():
	rows = [
		row("ok"),
		row("excluded", finding_type="Slow Query"),
		row("gated-n1", finding_type="Framework N+1"),
		row("old-call", finding_type="Redundant Call"),
		row("index", finding_type="Missing Index"),
		row("infra", finding_type="Memory Pressure"),
	]
	out = analyze.eligible_findings(rows, config("Slow Query"))
	assert names(out) == ["ok"]
	assert (out.gated, out.excluded) == (2, 1)
	assert out == list(out)


def test_an_empty_selection_still_has_counts():
	out = analyze.eligible_findings([], config())
	assert out == [] and (out.gated, out.excluded) == (0, 0)


# ---- the resume cutoff -------------------------------------------------------------------------
def test_answers_generated_since_the_request_are_skipped_even_at_the_same_instant():
	cutoff = "2026-01-02T10:00:00+00:00"
	rows = [
		row("at", answer(prompt_version=V - 1, generated_at=cutoff)),
		row("after", answer(prompt_version=V - 1, generated_at="2026-01-02T10:00:01+00:00")),
		row("before", answer(prompt_version=V - 1, generated_at="2026-01-02T09:59:59+00:00")),
		row("missing"),
	]
	assert pick(rows, requested_at=cutoff) == ["missing", "before"]


def test_a_naive_request_time_is_read_in_the_system_timezone(system_tz):
	system_tz("Asia/Kolkata")  # 12:00 here is 06:30Z
	rows = [
		row("after-request", answer(prompt_version=V - 1, generated_at="2026-10-09T07:00:00+00:00")),
		row("before-request", answer(prompt_version=V - 1, generated_at="2026-10-09T06:00:00+00:00")),
	]
	naive = dt.datetime(2026, 10, 9, 12, 0, 0)
	assert pick(rows, requested_at=naive) == ["before-request"]
	assert pick(rows, requested_at="2026-10-09 12:00:00") == ["before-request"]
	assert pick(rows, requested_at="2026-10-09T12:00:00.250000") == ["before-request"]


def test_a_naive_request_time_west_of_utc(system_tz):
	system_tz("America/New_York")  # 02:00 EDT is 06:00Z
	rows = [
		row("after", answer(prompt_version=V - 1, generated_at="2026-10-09T06:30:00+00:00")),
		row("before", answer(prompt_version=V - 1, generated_at="2026-10-09T05:30:00+00:00")),
	]
	assert pick(rows, requested_at=dt.datetime(2026, 10, 9, 2, 0, 0)) == ["before"]


def test_an_aware_request_time_ignores_the_system_timezone(system_tz):
	system_tz("Asia/Kolkata")
	rows = [row("after", answer(prompt_version=V - 1, generated_at="2026-10-09T07:00:00+00:00"))]
	assert pick(rows, requested_at=dt.datetime(2026, 10, 9, 6, 30, tzinfo=dt.timezone.utc)) == []
	assert pick(rows, requested_at="2026-10-09T12:00:00+05:30") == []
	assert pick(rows, requested_at="2026-10-09T06:30:00Z") == []
	assert pick(rows, requested_at="2026-10-09T07:30:00Z") == ["after"]


def test_a_naive_stored_time_is_utc_whatever_the_machine_and_site_timezone(system_tz, monkeypatch):
	system_tz("Asia/Kolkata")
	monkeypatch.setenv("TZ", "America/Los_Angeles")
	time.tzset()
	try:
		rows = [row("x", answer(prompt_version=V - 1, generated_at="2026-01-02T10:00:00"))]
		assert pick(rows, requested_at="2026-01-02T10:00:00+00:00") == []
		assert pick(rows, requested_at="2026-01-02T10:00:01+00:00") == ["x"]
	finally:
		monkeypatch.undo()
		time.tzset()


def test_without_a_resolvable_system_timezone_a_naive_request_time_is_utc(system_tz):
	system_tz("Not/AZone")
	rows = [row("x", answer(prompt_version=V - 1, generated_at="2026-10-09T06:00:00+00:00"))]
	assert pick(rows, requested_at=dt.datetime(2026, 10, 9, 6, 0, 0)) == []


def test_a_missing_answer_is_never_skipped_by_the_cutoff(system_tz):
	system_tz("UTC")
	assert pick([row("missing")], requested_at="1960-01-01T00:00:00+00:00") == ["missing"]


@pytest.mark.parametrize("none", [None, ""])
def test_no_request_time_means_no_cutoff(none):
	rows = [row("x", answer(prompt_version=V - 1, generated_at="2999-01-01T00:00:00+00:00"))]
	assert pick(rows, requested_at=none) == ["x"]


@pytest.mark.parametrize("bad", [5, 5.5, True, dt.date(2026, 10, 5), b"2026-10-05", [], {}])
def test_a_request_time_of_the_wrong_type_is_a_type_error(bad):
	with pytest.raises(TypeError):
		analyze.eligible_findings([row("x")], config(), requested_at=bad)


@pytest.mark.parametrize("bad", ["garbage", "2026-13-45", "9999-12-31 23:59:59+99:99"])
def test_an_unreadable_request_time_is_a_value_error_not_a_silent_no_cutoff(bad, system_tz):
	system_tz("UTC")
	with pytest.raises(ValueError):
		analyze.eligible_findings([row("x")], config(), requested_at=bad)


def test_a_bad_stored_time_never_hides_work():
	rows = [
		row("junk", answer(prompt_version=V - 1, generated_at="garbage")),
		row("int", answer(prompt_version=V - 1, generated_at=123)),
		row("list", answer(prompt_version=V - 1, generated_at=[])),
	]
	assert pick(rows, requested_at="2026-01-01T00:00:00Z") == ["junk", "int", "list"]


def test_the_system_timezone_comes_from_frappes_helper(monkeypatch):
	import sys
	import types

	fake = types.ModuleType("frappe.utils")
	fake.get_system_timezone = lambda: "Asia/Kolkata"
	monkeypatch.setitem(sys.modules, "frappe.utils", fake)
	assert analyze._system_timezone_name() == "Asia/Kolkata"
	fake.get_system_timezone = lambda: ""
	assert analyze._system_timezone_name() is None


def test_an_unreadable_system_timezone_falls_back_to_utc_with_one_log_line_outside_an_except(monkeypatch):
	import sys

	lines = []
	monkeypatch.setattr(analyze.safe_call, "log_error_line", lambda m, **k: lines.append((m, sys.exc_info()[0])))
	monkeypatch.setattr(analyze, "_system_timezone_name", lambda: None)
	assert analyze._system_zone() is dt.timezone.utc
	monkeypatch.setattr(analyze, "_system_timezone_name", lambda: "Not/AZone")
	assert analyze._system_zone() is dt.timezone.utc
	assert len(lines) == 2 and all("timezone" in m and active is None for m, active in lines)
	monkeypatch.setattr(analyze, "_system_timezone_name", lambda: "Asia/Kolkata")
	assert str(analyze._system_zone()) == "Asia/Kolkata" and len(lines) == 2


# ---- the light recording loader keeps only the recordings ---------------------------------------
class Heavy(str):
	"""A tree blob stand-in that can be weakly referenced."""


@pytest.fixture
def light(monkeypatch):
	heavy = [Heavy("x" * 64), Heavy("y" * 64)]
	bundle = {
		"recordings": {
			"fake-a": {
				"rec": {"uuid": "fake-a", "calls": [1], "sidecar": ["s"], "tree_b64": "t", "pyi_session": "p"},
				"tree_b64": heavy[0],
				"sidecar": ["big"],
			},
			"fake-b": {"rec": {"uuid": "fake-b", "calls": []}, "tree_b64": heavy[1]},
		}
	}
	holder = {"bundle": bundle}
	del bundle
	loads = []

	def load(doc):
		loads.append(doc.name)
		return holder["bundle"]

	monkeypatch.setattr(analyze, "_load_recordings_bundle", load)
	monkeypatch.setattr(analyze, "_deserialize_tree", lambda uuid, blob: None)
	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(cache=SimpleNamespace(hget=lambda *a: None)))
	doc = SimpleNamespace(
		name="fake-doc",
		session_uuid="fake-session",
		recordings_file="fake-file",
		actions=[SimpleNamespace(recording_uuid="fake-a"), SimpleNamespace(recording_uuid="fake-b")],
	)
	return SimpleNamespace(doc=doc, loads=loads, holder=holder, heavy=[weakref.ref(h) for h in heavy], raw=heavy)


def test_the_memo_holds_only_the_recording_dicts_never_the_parsed_bundle(light):
	memo = {}
	out = analyze.load_recordings_light(light.doc, memo=memo)
	assert [r["uuid"] for r in out] == ["fake-a", "fake-b"]
	assert len(memo) == 1
	(held,) = memo.values()
	assert set(held) == {"fake-a", "fake-b"}
	assert all(isinstance(rec, dict) and "tree_b64" not in rec.get("rec", {}) for rec in held.values())
	assert held["fake-a"]["uuid"] == "fake-a"
	# Nothing in the memo reaches the tree blobs: drop every other reference and they are freed.
	light.holder.clear()
	del light.raw[:]
	gc.collect()
	assert [ref() for ref in light.heavy] == [None, None]


def test_a_new_recordings_file_on_the_same_session_is_read_again(light):
	memo = {}
	analyze.load_recordings_light(light.doc, ["fake-a"], memo=memo)
	light.doc.recordings_file = "fake-file-2"
	analyze.load_recordings_light(light.doc, ["fake-a"], memo=memo)
	assert light.loads == ["fake-doc", "fake-doc"]


def test_a_second_call_with_the_memo_does_not_read_the_file_again(light):
	memo = {}
	analyze.load_recordings_light(light.doc, ["fake-a"], memo=memo)
	analyze.load_recordings_light(light.doc, ["fake-b"], memo=memo)
	assert light.loads == ["fake-doc"]


@pytest.mark.parametrize("dropped", ["sidecar", "tree_b64", "pyi_session"])
def test_the_returned_recording_drops_trees_and_sidecars(light, dropped):
	(first, _second) = analyze.load_recordings_light(light.doc)
	assert dropped not in first
	assert first["calls"] == [1] and first["uuid"] == "fake-a"


def test_the_returned_recording_is_a_copy_the_memo_keeps_whole(light):
	memo = {}
	first = analyze.load_recordings_light(light.doc, ["fake-a"], memo=memo)[0]
	first["mutated"] = True
	again = analyze.load_recordings_light(light.doc, ["fake-a"], memo=memo)[0]
	assert "mutated" not in again


def test_a_legacy_bare_uuid_map_is_read_like_rehydrate_reads_it(light, monkeypatch):
	bare = light.holder["bundle"]["recordings"]
	monkeypatch.setattr(analyze, "_load_recordings_bundle", lambda doc: bare)
	assert [r["uuid"] for r in analyze.load_recordings_light(light.doc)] == ["fake-a", "fake-b"]
	assert analyze._rehydrate_from_bundle(bare, "fake-b")["uuid"] == "fake-b"


def test_light_and_rehydrate_share_one_shape_reader(light, monkeypatch):
	seen = []
	real = analyze._bundle_entries
	monkeypatch.setattr(analyze, "_bundle_entries", lambda bundle: seen.append(bundle) or real(bundle))
	analyze.load_recordings_light(light.doc)
	analyze._rehydrate_from_bundle(light.holder["bundle"], "fake-a")
	assert len(seen) == 2


@pytest.mark.parametrize("bundle", [None, [], "x", {"recordings": []}, {"recordings": {"u": None}}, {"u": {"rec": []}}])
def test_a_bundle_of_any_other_shape_yields_nothing(light, monkeypatch, bundle):
	monkeypatch.setattr(analyze, "_load_recordings_bundle", lambda doc: bundle)
	light.doc.actions = [SimpleNamespace(recording_uuid="u")]
	assert analyze.load_recordings_light(light.doc) == []


# ---- the shared row accessor ---------------------------------------------------------------------
def test_there_is_one_row_accessor():
	assert not hasattr(analyze, "_row_get")
	from optimus.analyzers.base import row_get

	assert row_get({"a": 1}, "a") == 1 and row_get(SimpleNamespace(a=2), "a") == 2
	assert row_get({}, "a", 9) == 9 and row_get(SimpleNamespace(), "a", 9) == 9
	assert ai_fix.gate_input({"finding_type": "T"}) == ai_fix.gate_input(SimpleNamespace(finding_type="T"))
