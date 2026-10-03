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


@pytest.mark.parametrize("stored", [None, "", "{", "[]", '"answer"'])
def test_unusable_json_is_missing(stored):
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
		answer(guardrail={"fallback": True}),
	],
)
def test_failed_empty_and_fallback_records_are_outdated_not_missing(stored):
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
	monkeypatch.setattr(analyze, "_rehydrate_from_bundle", forbid, raising=False)
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


def test_rq_timeout_during_live_read_escapes_fresh(light, monkeypatch):
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = Timeout("fake timeout")

	def fail(*a):
		raise original

	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(cache=SimpleNamespace(hget=fail)))
	with pytest.raises(Timeout) as caught:
		analyze.load_recordings_light(light.doc)
	assert caught.value is not original and caught.value.__context__ is None


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
		SimpleNamespace(get_all=failed, get_doc=failed, log_error=lambda **kw: seen.append(sys.exc_info()[0])),
	)
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **kw: seen.append(sys.exc_info()[0]))
	doc = SimpleNamespace(name="fake-doc", recordings_file="/private/files/fake.json.gz", session_uuid="fake-session")
	if interrupt:
		with pytest.raises(Timeout) as caught:
			analyze._load_recordings_bundle(doc)
		assert caught.value is not original and caught.value.__context__ is None
		assert seen == []
	else:
		assert analyze._load_recordings_bundle(doc) is None
		assert seen == [None]


def test_selection_handles_malformed_severity():
	r = row("fake", severity=[])
	assert analyze.eligible_findings([r], config()) == [r]



@pytest.mark.parametrize("cap", [0, 1, 20])
def test_confirmation_plan_uses_actual_worker_eligibility_and_manual_cap(monkeypatch, cap):
	from optimus import ai_jobs
	cfg = config("Slow Query")
	cfg.ai_enabled = cfg.ai_suggest_findings = cfg.ai_humanize_steps = True
	cfg.ai_refresh_max_findings = cap
	monkeypatch.setattr("optimus.settings.get_config", lambda: cfg)
	monkeypatch.setattr(ai_jobs, "_now", lambda: 1780000000)
	findings = [row("missing"), row("outdated", answer(prompt_version=0)), row("current", answer()),
		row("recipe", finding_type="Missing Index"), row("excluded", finding_type="Slow Query")]
	plan = ai_jobs.refresh_plan({"findings": findings, "actions": [{"recording_uuid": "fake"}]})
	assert plan == {"pending": 2, "total": 3, "cap": cap, "selected": min(2, cap) if cap else 2,
		"selected_all": min(3, cap) if cap else 3, "steps": True}


def test_missing_worker_notice_names_the_queue_to_start(monkeypatch):
	from optimus import ai_jobs
	monkeypatch.setattr(ai_jobs, "ai_queue", lambda: "fake-ai-queue")
	assert "fake-ai-queue" in ai_jobs.admission_message("no_worker")
