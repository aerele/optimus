"""Selection and grounding contracts shared by future background refresh jobs."""

import json
from types import SimpleNamespace

import pytest

from optimus import ai_fix, ai_prompts, analyze

pytestmark = pytest.mark.rq


def row(name, stored=None, **kw):
	return SimpleNamespace(
		name=name,
		finding_type=kw.pop("finding_type", "N+1 Query"),
		llm_fix_json=stored,
		severity=kw.pop("severity", "High"),
		estimated_impact_ms=kw.pop("estimated_impact_ms", 5),
		technical_detail_json="{}",
		**kw,
	)


def answer(**kw):
	return json.dumps(
		{
			"suggestion": "answer",
			"prompt_version": ai_prompts.PROMPT_VERSION,
			"generated_at": "2026-01-02T10:00:00+00:00",
			**kw,
		}
	)


def config(*excluded):
	return SimpleNamespace(ai_excluded_finding_types=excluded)


def names(rows):
	return [r.name for r in rows]


def test_selection_uses_missing_then_outdated_and_keeps_current():
	rows = [row("current", answer()), row("outdated", answer(prompt_version=0)), row("missing")]
	assert names(analyze.eligible_findings(rows, config())) == ["missing", "outdated"]
	assert names(analyze.eligible_findings(rows, config(), include_outdated=False)) == ["missing"]
	assert names(analyze.eligible_findings(rows, config(), regenerate_all=True)) == [
		"missing",
		"outdated",
		"current",
	]


@pytest.mark.parametrize("stored", [None, "", "  \n"])
def test_nothing_stored_is_missing(stored):
	assert names(analyze.eligible_findings([row("missing", stored)], config(), include_outdated=False)) == [
		"missing"
	]


@pytest.mark.parametrize(
	"stored",
	[
		"{}",
		'{"error": "failed"}',
		answer(suggestion=[]),
		answer(prompt_version="bad"),
		"{",
		"[]",
		'"answer"',
	],
)
def test_failed_empty_and_unparseable_records_are_outdated_not_missing(stored):
	r = row("outdated", stored)
	assert analyze.eligible_findings([r], config()) == [r]
	assert analyze.eligible_findings([r], config(), include_outdated=False) == []


def test_order_and_resume_are_timezone_aware():
	rows = [
		row("new", answer(prompt_version=0, generated_at="2026-01-02T11:00:00+00:00")),
		row("old", answer(prompt_version=0, generated_at="2026-01-02T12:00:00+05:30")),
		row("missing-low", severity="Low"),
		row("missing-high"),
	]
	assert names(analyze.eligible_findings(rows, config(), requested_at="2026-01-02T10:00:00Z")) == [
		"missing-high",
		"missing-low",
		"old",
	]


def test_bad_dates_and_impacts_do_not_crash_or_hide_work():
	rows = [
		row("a", answer(prompt_version=0, generated_at=[]), estimated_impact_ms="bad"),
		row("b", answer(prompt_version=0, generated_at="bad"), estimated_impact_ms=float("nan")),
	]
	assert names(analyze.eligible_findings(rows, config())) == ["a", "b"]


def test_excluded_types_and_pre_fix_calls_never_reach_the_model(monkeypatch):
	def forbidden_config_read():
		raise AssertionError("selection must reuse its config")

	monkeypatch.setattr("optimus.settings.get_config", forbidden_config_read)
	rows = [
		row("excluded", finding_type="Slow Query"),
		row("old-call", finding_type="Redundant Call"),
		row("index", finding_type="Missing Index"),
		row("accepted"),
	]
	assert names(analyze.eligible_findings(rows, config("Slow Query"))) == ["accepted"]


def test_dict_gate_input_is_equivalent_to_a_child_row():
	r = row("test")
	assert ai_fix.gate_input(vars(r)) == ai_fix.gate_input(r)


def test_action_map_uses_zero_based_position_not_frappe_idx():
	actions = [SimpleNamespace(idx=1, recording_uuid="fake-a"), {"idx": 2, "recording_uuid": "fake-b"}]
	actual = analyze.action_recording_map(actions)
	assert actual[0]["recording_uuid"] == "fake-a"
	assert actual[1]["recording_uuid"] == "fake-b"
	assert 2 not in actual


@pytest.fixture
def light(monkeypatch):
	loads, logs = [], []
	bundle = {
		"recordings": {
			"fake-a": {
				"rec": {"uuid": "fake-a", "calls": [], "pyi_session": "forbidden"},
				"tree_b64": "forbidden",
				"sidecar": [],
			},
			"fake-b": {"rec": {"uuid": "fake-b", "calls": []}},
		}
	}

	def load(doc):
		loads.append(doc.name)
		return bundle

	monkeypatch.setattr(analyze, "_load_recordings_bundle", load)
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **k: logs.append(k))
	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(cache=SimpleNamespace(hget=lambda *a: None)))

	def forbid(*args, **kw):
		raise AssertionError("light loading must not deserialize a tree")

	monkeypatch.setattr(analyze, "_deserialize_tree", forbid)
	monkeypatch.setattr(analyze, "_rehydrate_from_bundle", forbid)
	doc = SimpleNamespace(
		name="fake-doc",
		session_uuid="fake-session",
		recordings_file="fake-file",
		actions=[SimpleNamespace(recording_uuid="fake-a"), SimpleNamespace(recording_uuid="fake-b")],
	)
	return SimpleNamespace(doc=doc, loads=loads, bundle=bundle, logs=logs)


def test_bundle_is_loaded_only_once_per_slice_and_never_unpickled(light):
	memo = {}
	a = analyze.load_recordings_light(light.doc, ["fake-a"], memo=memo)
	b = analyze.load_recordings_light(light.doc, ["fake-b"], memo=memo)
	assert [r["uuid"] for r in a + b] == ["fake-a", "fake-b"]
	assert light.loads == ["fake-doc"]
	assert "pyi_session" not in a[0] and "sidecar" not in a[0] and "tree_b64" not in a[0]
	assert light.bundle["recordings"]["fake-a"]["rec"]["pyi_session"] == "forbidden"


def test_live_recordings_avoid_the_bundle(light, monkeypatch):
	monkeypatch.setattr(
		analyze,
		"frappe",
		SimpleNamespace(
			cache=SimpleNamespace(
				hget=lambda _, uuid: {"uuid": uuid, "calls": []},
			)
		),
	)
	assert len(analyze.load_recordings_light(light.doc)) == 2
	assert light.loads == []


def test_empty_requested_set_does_not_reload_everything(light):
	assert analyze.load_recordings_light(light.doc, []) == []
	assert light.loads == []


def test_memo_cannot_return_another_sessions_bundle(light):
	memo = {}
	analyze.load_recordings_light(light.doc, memo=memo)
	other = SimpleNamespace(**{**vars(light.doc), "name": "fake-other", "session_uuid": "fake-other-session"})
	analyze.load_recordings_light(other, memo=memo)
	assert light.loads == ["fake-doc", "fake-other"]


def test_missing_bundle_and_malformed_entries_are_empty(light):
	light.bundle["recordings"] = {"fake-a": None, "fake-b": {"rec": []}}
	assert analyze.load_recordings_light(light.doc) == []


def _logger_that_stops_the_job(seen):
	"""A ``log_ai_failure`` stand-in that records the call (and whether an exception was being
	handled) and, like the real one, raises an RQ job timeout again as a fresh instance."""
	import sys

	def log(title, exc=None, **kw):
		seen.append((title, sys.exc_info()[0]))
		guard = ai_fix._InterruptGuard()
		guard.note(exc)
		if guard.pending():
			raise guard.interrupt()

	return log


def test_rq_timeout_during_live_read_escapes_fresh(light, monkeypatch):
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = Timeout("fake timeout")

	def fail(*a):
		raise original

	seen = []
	monkeypatch.setattr(ai_fix, "log_ai_failure", _logger_that_stops_the_job(seen))
	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(cache=SimpleNamespace(hget=fail)))
	with pytest.raises(Timeout) as caught:
		analyze.load_recordings_light(light.doc)
	assert caught.value is not original and caught.value.__context__ is None
	assert seen == [("optimus recording cache read", None)]


def test_cache_failure_is_logged_outside_except_and_bundle_still_works(light, monkeypatch):
	import sys

	calls, active = [], []

	def failed(*a):
		calls.append(True)
		raise RuntimeError("fake redis failure")

	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(cache=SimpleNamespace(hget=failed)))
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **kw: active.append(sys.exc_info()[0]))
	assert len(analyze.load_recordings_light(light.doc)) == 2
	assert calls == [True] and active == [None]


@pytest.mark.parametrize("interrupt", [False, True])
def test_bundle_read_logs_outside_except_and_never_swallows_rq(monkeypatch, interrupt):
	import sys

	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException if interrupt else RuntimeError
	original = Timeout("fake file failure")

	def failed(*a, **kw):
		raise original

	seen = []
	monkeypatch.setattr(
		analyze,
		"frappe",
		SimpleNamespace(get_doc=failed, log_error=lambda **kw: seen.append(sys.exc_info()[0])),
	)
	logged = []
	monkeypatch.setattr(ai_fix, "log_ai_failure", _logger_that_stops_the_job(logged))
	doc = SimpleNamespace(recordings_file="fake-file", session_uuid="fake-session")
	if interrupt:
		with pytest.raises(Timeout) as caught:
			analyze._load_recordings_bundle(doc)
		assert caught.value is not original and caught.value.__context__ is None
	else:
		assert analyze._load_recordings_bundle(doc) is None
	# the row is written outside the handler, for the timeout too; frappe.log_error itself is never called
	assert logged == [("optimus load recordings bundle", None)] and seen == []


def test_selection_handles_malformed_severity():
	r = row("fake", severity=[])
	assert analyze.eligible_findings([r], config()) == [r]
