"""Phase 2 delivery uses SQL ownership, never an HTTP inline fallback."""

from copy import deepcopy
from importlib import import_module
from types import SimpleNamespace

import pytest

from optimus.tests.test_ai_refresh_journal import Database

pytestmark = pytest.mark.rq


@pytest.fixture
def phase2(monkeypatch):
	mod = import_module("optimus.line_profile.jobs")
	from optimus import ai_refresh_store
	db = Database()
	db.rows[ai_refresh_store.CONTROL] = {"site": {"name": "site", "generation": 0}}
	db.rows[mod.TABLE] = {"fake-child": {
		"name": "fake-child", "parent": "fake-session-doc", "run_uuid": "fake-phase2",
		"status": "Failed", "analyze_attempts": 0,
	}}
	fake = SimpleNamespace(db=db, ValidationError=ValueError)
	monkeypatch.setattr(mod, "frappe", fake)
	monkeypatch.setattr(ai_refresh_store, "frappe", fake)
	monkeypatch.setattr(mod, "_authorized", lambda *a: True)
	monkeypatch.setattr(mod.ai_jobs, "_touch_session", lambda name: db.set_value("Optimus Session", name, {"modified": "changed"}))
	return SimpleNamespace(mod=mod, db=db, row=lambda: db.rows[mod.TABLE]["fake-child"])


def admit(env, **kw):
	with env.db.transaction():
		return env.mod._admit("fake-session-doc", "fake-session", "fake-phase2", "fake-owner",
			generation=kw.pop("generation", "generation-1"), now=kw.pop("now", 100), **kw)


def claim(env, generation="generation-1", token="worker-1", now=101):
	with env.db.transaction():
		return env.mod._claim("fake-session-doc", "fake-phase2", generation, token, now=now)


def test_admission_reserves_against_ai_and_commits_one_generation(phase2):
	out = admit(phase2)
	assert out["status"] == "queued"
	assert phase2.row()["status"] == "Analyzing"
	assert phase2.row()["analyze_attempts"] == 1
	assert phase2.row()["analyze_requested_by"] == "fake-owner"
	assert phase2.db.locks[:2] == [("Optimus AI Refresh Control", "site"), ("Optimus Session", "fake-session-doc")]
	assert admit(phase2, generation="generation-2")["status"] == "already_running"
	assert phase2.row()["analyze_generation"] == "generation-1"


@pytest.mark.parametrize("status", ["Recording", "Ready"])
def test_retry_cannot_overwrite_recording_or_finished_results(phase2, status):
	phase2.row()["status"] = status
	assert admit(phase2)["reason"] == "not_retryable"
	assert phase2.row()["status"] == status


def test_stopping_recording_uses_same_durable_queue(phase2):
	phase2.row()["status"] = "Recording"
	assert admit(phase2, from_recording=True)["status"] == "queued"


def test_duplicate_delivery_including_same_token_cannot_claim_twice(phase2):
	admit(phase2)
	assert claim(phase2)
	assert claim(phase2) is None
	assert claim(phase2, token="worker-2") is None


def test_expired_and_old_generation_deliveries_never_claim(phase2):
	admit(phase2)
	assert claim(phase2, now=2000) is None
	assert admit(phase2, generation="generation-2", now=2001)["status"] == "queued"
	assert claim(phase2, now=2002) is None
	assert claim(phase2, generation="generation-2", now=2002)


def test_retry_budget_survives_worker_crashes(phase2):
	for count in range(4):
		assert admit(phase2, generation=f"g-{count}", now=2000 * count)["status"] == "queued"
	assert admit(phase2, now=9000)["reason"] == "retry_limit"
	assert phase2.row()["analyze_attempts"] == 4


@pytest.mark.parametrize("change", ["generation", "lease", "status", "parent", "permission"])
def test_stale_worker_cannot_append_findings(phase2, monkeypatch, change):
	admit(phase2)
	run = claim(phase2)
	if change == "generation":
		phase2.row()["analyze_generation"] = "new-generation"
	elif change == "lease":
		phase2.row()["analyze_lease_until"] = 101
	elif change == "status":
		phase2.row()["status"] = "Ready"
	elif change == "parent":
		phase2.db.rows["Optimus Session"]["fake-session-doc"]["status"] = "Analyzing"
	else:
		monkeypatch.setattr(phase2.mod, "_authorized", lambda *a: False)
	writes = []
	with phase2.db.transaction():
		assert phase2.mod._complete(run, now=102, persist=lambda: writes.append(True)) is False
	assert not writes


def test_results_and_completion_roll_back_together(phase2):
	admit(phase2)
	run = claim(phase2)
	before = deepcopy(phase2.db.rows)
	def broken_save():
		phase2.db.rows["Optimus Session"]["fake-session-doc"]["findings"] = ["fake-result"]
		raise ValueError("fake child save failure")
	with pytest.raises(ValueError), phase2.db.transaction():
		phase2.mod._complete(run, now=102, persist=broken_save)
	assert phase2.db.rows == before


def test_committed_result_rejects_duplicate_save_and_late_failure(phase2):
	admit(phase2)
	run = claim(phase2)
	writes = []
	with phase2.db.transaction():
		assert phase2.mod._complete(run, now=102, persist=lambda: writes.append(True))
	with phase2.db.transaction():
		assert not phase2.mod._complete(run, now=103, persist=lambda: writes.append(True))
		assert not phase2.mod._fail(run, now=103, reason="failed")
	assert writes == [True] and phase2.row()["status"] == "Ready"
	assert phase2.row()["analyze_render_pending"] == 1


def test_failure_records_fixed_reason_and_keeps_generation_for_fencing(phase2):
	admit(phase2)
	run = claim(phase2)
	with phase2.db.transaction():
		assert phase2.mod._fail(run, now=102, reason="input_missing")
	assert phase2.row()["status"] == "Failed"
	assert "input" in phase2.row()["warnings_json"].lower()
	assert phase2.row()["analyze_generation"] == "generation-1"


@pytest.fixture
def worker(phase2, monkeypatch):
	from optimus import ai_jobs
	from optimus.line_profile import analyzer, capture
	mod, db = phase2.mod, phase2.db
	now = [100]
	monkeypatch.setattr(mod, "_now", lambda: now[0], raising=False)
	def transaction(operation):
		with db.transaction():
			return operation()
	monkeypatch.setattr(ai_jobs, "_retry_sql", transaction)
	monkeypatch.setattr(ai_jobs, "_transaction", transaction)
	db.rollback = lambda: None
	mod.frappe.local = SimpleNamespace(optimus_analyzing=False)
	calls = SimpleNamespace(compute=[], persist=[], render=[], cleanup=[], logs=[], events=[])
	monkeypatch.setattr(analyzer, "_compute_run", lambda *a: calls.compute.append(a) or ([], SimpleNamespace(findings=[]), 0), raising=False)
	monkeypatch.setattr(analyzer, "_persist_run", lambda *a: calls.persist.append(a))
	def publish(event, payload):
		assert not db.active, "publish only after the state transaction commits"
		calls.events.append((event, payload))
	monkeypatch.setattr(analyzer, "_publish", publish)
	monkeypatch.setattr(mod, "_render", lambda *a: calls.render.append(a), raising=False)
	monkeypatch.setattr(mod, "_log", lambda *a: calls.logs.append(a), raising=False)
	monkeypatch.setattr(capture, "cleanup_run", lambda *a: calls.cleanup.append(a))
	return SimpleNamespace(**vars(phase2), calls=calls, now=now, analyzer=analyzer)


def run_worker(env):
	env.mod.run("fake-session", "fake-phase2", generation="generation-1")


def test_worker_duplicate_delivery_never_computes_or_persists_twice(worker):
	admit(worker)
	run_worker(worker)
	run_worker(worker)
	assert len(worker.calls.compute) == len(worker.calls.persist) == 1
	assert worker.row()["status"] == "Ready"
	assert not worker.mod.frappe.local.optimus_analyzing


def test_render_failure_keeps_phase2_findings_ready(worker, monkeypatch):
	admit(worker)
	def broken(*a):
		raise OSError("fake report file failure")
	monkeypatch.setattr(worker.mod, "_render", broken)
	run_worker(worker)
	assert worker.row()["status"] == "Ready"
	assert worker.row()["analyze_render_pending"] == 1
	assert len(worker.calls.persist) == 1 and worker.calls.logs
	run_worker(worker)
	assert len(worker.calls.persist) == 1


def test_failed_input_does_not_become_an_empty_success(worker, monkeypatch):
	admit(worker)
	def missing(*a):
		raise worker.mod.MissingInput()
	monkeypatch.setattr(worker.analyzer, "_compute_run", missing)
	with pytest.raises(worker.mod.Phase2Failed):
		run_worker(worker)
	assert worker.row()["status"] == "Failed" and not worker.calls.persist
	assert not worker.calls.cleanup


def test_late_worker_does_not_fail_or_clean_input_of_a_new_retry(worker, monkeypatch):
	admit(worker)
	def expired_during_compute(*a):
		worker.now[0] = 2001
		assert admit(worker, generation="generation-2", now=2001)["status"] == "queued"
		return [], SimpleNamespace(findings=[]), 0
	monkeypatch.setattr(worker.analyzer, "_compute_run", expired_during_compute)
	run_worker(worker)
	assert worker.row()["status"] == "Analyzing"
	assert worker.row()["analyze_generation"] == "generation-2"
	assert not worker.calls.persist and not worker.calls.cleanup


@pytest.mark.rq
def test_hard_timeout_after_results_commit_keeps_ready(worker, monkeypatch):
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	admit(worker)
	def timeout(*a):
		raise Timeout("fake report timeout")
	monkeypatch.setattr(worker.mod, "_render", timeout)
	with pytest.raises(Timeout):
		run_worker(worker)
	assert worker.row()["status"] == "Ready" and worker.row()["analyze_render_pending"] == 1
	assert len(worker.calls.persist) == 1


def test_queue_dispatch_failure_leaves_durable_intent(worker, monkeypatch):
	monkeypatch.setattr(worker.mod, "_enqueue", lambda *a: (_ for _ in ()).throw(ConnectionError("fake Redis outage")), raising=False)
	out = worker.mod.request("fake-session-doc", "fake-session", "fake-phase2", "fake-owner")
	assert out["status"] == "Analyzing" and out["ran_inline"] is False
	assert worker.row()["analyze_dispatch_pending"] == 1
	assert worker.calls.logs and not worker.calls.compute


def test_recovery_never_repeats_claimed_compute_or_deletes_captured_input(worker):
	admit(worker)
	claim(worker)
	worker.now[0] = 2001
	with worker.db.transaction():
		assert worker.mod._recover("fake-session-doc", "fake-phase2", now=2001)
	assert worker.row()["status"] == "Failed"
	assert not worker.calls.compute and not worker.calls.cleanup


@pytest.mark.parametrize("samples,picks", [([], []), ([[{"fake": 1}]], []), ({}, []), ([], {}), ("malformed", [])])
def test_missing_or_malformed_input_never_reaches_classifier(monkeypatch, samples, picks):
	from optimus.line_profile import analyzer, capture, jobs
	monkeypatch.setattr(capture, "read_all_samples", lambda *a: samples)
	monkeypatch.setattr(capture, "read_picks_meta", lambda *a: picks)
	def forbidden(*a, **kw):
		raise AssertionError("missing input must not save empty findings")
	monkeypatch.setattr(analyzer, "analyze", forbidden)
	with pytest.raises(jobs.MissingInput):
		analyzer._compute_run("fake-doc", "fake-run")


def test_valid_picks_with_no_invocations_still_produce_real_diagnostics(monkeypatch):
	from optimus.line_profile import analyzer, capture
	monkeypatch.setattr(capture, "read_all_samples", lambda *a: [])
	monkeypatch.setattr(capture, "read_picks_meta", lambda *a: [{"dotted_path": "fake.app.fn", "file": "fake.py", "qualname": "fn", "first_lineno": 1, "source_lines": [{"lineno": 1, "content": "def fn(): pass"}]}])
	monkeypatch.setattr(capture, "budget_was_hit", lambda *a: False)
	monkeypatch.setattr(analyzer, "frappe", SimpleNamespace(get_doc=lambda *a: SimpleNamespace(actions=[])))
	results, result, total_ms = analyzer._compute_run("fake-doc", "fake-run")
	assert results and result.warnings and total_ms == 0


def test_legacy_entrypoint_delegates_to_fenced_worker(monkeypatch):
	from optimus.line_profile import analyzer, jobs
	calls = []
	monkeypatch.setattr(jobs, "run", lambda *a, **kw: calls.append((a, kw)))
	analyzer.run_analyze("fake-session", "fake-run")
	assert calls == [(("fake-session", "fake-run"), {"generation": None})]


def test_phase2_persistence_never_commits_outside_ownership_transaction(monkeypatch):
	from optimus.line_profile import analyzer
	child = SimpleNamespace(run_uuid="fake-run")
	findings = []
	parent = SimpleNamespace(phase_2_runs=[child], flags=SimpleNamespace(),
		append=lambda table, row: findings.append(row), save=lambda **kw: None)
	monkeypatch.setattr(analyzer, "frappe", SimpleNamespace(get_doc=lambda *a: parent,
		utils=SimpleNamespace(now_datetime=lambda: "fake-time")))
	def forbidden():
		raise AssertionError("phase2 save must participate in the ownership transaction")
	monkeypatch.setattr(analyzer, "safe_commit", forbidden, raising=False)
	result = SimpleNamespace(findings=[{"title": "fake finding"}], warnings=[])
	analyzer._persist_run("fake-doc", "fake-run", [], result, 5)
	assert child.status == "Ready" and len(findings) == 1


JOURNAL_FIELDS = {
	"analyze_generation": "Data", "analyze_requested_by": "Link", "analyze_worker_token": "Data",
	"analyze_lease_until": "Float", "analyze_dispatch_at": "Float", "analyze_dispatch_pending": "Check",
	"analyze_attempts": "Int", "analyze_render_pending": "Check",
}


def test_phase2_durable_fields_exist_and_are_hidden_read_only():
	import json
	from pathlib import Path
	meta = json.loads((Path(__file__).parents[1] / "optimus/doctype/optimus_phase_two_run/optimus_phase_two_run.json").read_text())
	fields = {field["fieldname"]: field for field in meta["fields"]}
	for name, kind in JOURNAL_FIELDS.items():
		assert fields[name]["fieldtype"] == kind
		assert fields[name]["hidden"] == fields[name]["read_only"] == 1
		assert fields[name]["no_copy"] == 1


@pytest.mark.parametrize("mutation", ["create", "change", "remove"])
def test_parent_save_cannot_forge_or_erase_phase2_journal(mutation):
	from optimus.line_profile.jobs import validate_parent_journal
	original = {"name": "fake-child", "analyze_generation": "fake-generation"}
	old = SimpleNamespace(phase_2_runs=[original])
	row = dict(original)
	if mutation == "create":
		old.phase_2_runs = []
	elif mutation == "change":
		row["analyze_attempts"] = -1
	doc = SimpleNamespace(phase_2_runs=[] if mutation == "remove" else [row], get_doc_before_save=lambda: old)
	with pytest.raises(Exception, match="Phase 2 analysis state is managed by the server"):
		validate_parent_journal(doc)


def test_parent_save_can_edit_unrelated_content_without_changing_journal():
	from optimus.line_profile.jobs import validate_parent_journal
	row = {"name": "fake-child", "analyze_generation": "fake-generation", "analyze_attempts": 1}
	old = SimpleNamespace(phase_2_runs=[dict(row)])
	doc = SimpleNamespace(phase_2_runs=[dict(row)], get_doc_before_save=lambda: old)
	validate_parent_journal(doc)


@pytest.mark.parametrize("change", ["status", "generation", "modified"])
def test_legacy_janitor_cannot_fail_a_newer_generation(phase2, change):
	row = phase2.row()
	row.update(status="Analyzing", modified=1)
	if change == "status":
		row["status"] = "Ready"
	elif change == "generation":
		row.update(analyze_generation="new", analyze_lease_until=3000)
	else:
		row["modified"] = 100
	with phase2.db.transaction():
		assert not phase2.mod.expire_legacy("fake-session-doc", "fake-phase2", status="Analyzing", cutoff=50)
	assert row["status"] != "Failed"


def test_expired_legacy_analysis_is_failed_without_new_compute(phase2):
	phase2.row().update(status="Analyzing", modified=1)
	with phase2.db.transaction():
		assert phase2.mod.expire_legacy("fake-session-doc", "fake-phase2", status="Analyzing", cutoff=50)
	assert phase2.row()["status"] == "Failed"
	assert "Retry" in phase2.row()["warnings_json"]


def test_phase2_recovery_is_registered_on_the_scheduler():
	from optimus import hooks
	assert "optimus.line_profile.jobs.recover_pending" in hooks.scheduler_events["cron"]["* * * * *"]


@pytest.mark.rq
def test_interrupt_is_not_replaced_by_a_secondary_log_failure(worker, monkeypatch):
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	admit(worker)
	def timeout(*a):
		raise Timeout("fake report timeout")
	def log_failure(*a):
		raise OSError("fake log storage outage")
	monkeypatch.setattr(worker.mod, "_render", timeout)
	monkeypatch.setattr(worker.mod, "_log", log_failure)
	# Trigger cleanup logging as well as the original timeout.
	worker.db.rollback = lambda: (_ for _ in ()).throw(ConnectionError("fake rollback outage"))
	with pytest.raises(Timeout):
		run_worker(worker)
	assert worker.row()["status"] == "Ready"


def test_missing_child_cannot_promote_orphaned_phase2_findings(monkeypatch):
	from optimus.line_profile import analyzer
	writes = []
	parent = SimpleNamespace(phase_2_runs=[], flags=SimpleNamespace(),
		append=lambda *a: writes.append(a), save=lambda **kw: writes.append("save"))
	monkeypatch.setattr(analyzer, "frappe", SimpleNamespace(get_doc=lambda *a: parent))
	with pytest.raises(ValueError, match="Phase 2 result row is missing"):
		analyzer._persist_run("fake-parent", "fake-run", [], SimpleNamespace(findings=[{"title": "fake"}], warnings=[]), 0)
	assert not writes



def test_worker_publishes_committed_start_and_completion(worker):
	admit(worker)
	run_worker(worker)
	assert [event for event, _ in worker.calls.events] == ["phase_2_run_analyzing", "phase_2_run_ready"]
	assert all(payload["user"] == "fake-owner" for _, payload in worker.calls.events)


def test_worker_publishes_failed_state_without_exception_text(worker, monkeypatch):
	admit(worker)
	def malformed(*a):
		raise ValueError("fake sensitive input detail")
	monkeypatch.setattr(worker.analyzer, "_compute_run", malformed)
	with pytest.raises(worker.mod.Phase2Failed):
		run_worker(worker)
	assert worker.calls.events[-1][0] == "phase_2_run_failed"
	assert "fake sensitive" not in str(worker.calls.events)


def test_legacy_delivery_adopts_once_and_cannot_repeat_a_completed_run(worker):
	worker.row().update(status="Analyzing")
	worker.db.rows["Optimus Session"]["fake-session-doc"]["owner"] = "fake-owner"
	worker.mod.run("fake-session", "fake-phase2")
	worker.mod.run("fake-session", "fake-phase2")
	assert len(worker.calls.compute) == len(worker.calls.persist) == 1
	assert worker.row()["status"] == "Ready" and worker.row()["analyze_attempts"] == 1


def test_dispatch_throttle_never_acknowledges_before_a_worker_claim(worker):
	admit(worker)
	with worker.db.transaction():
		assert worker.mod._prepare_dispatch(worker.row(), now=100)
	with worker.db.transaction():
		assert not worker.mod._prepare_dispatch(worker.row(), now=129)
		assert worker.mod._prepare_dispatch(worker.row(), now=130)
	assert worker.row()["analyze_dispatch_pending"] == 1
	claim(worker, now=131)
	with worker.db.transaction():
		assert not worker.mod._prepare_dispatch(worker.row(), now=200)


@pytest.mark.parametrize("user,enabled,roles,owner,read,write,expected", [
	("Guest", 1, ["System Manager"], "Guest", True, True, False),
	("fake-owner", 0, ["Optimus User"], "fake-owner", True, True, False),
	("fake-owner", 1, [], "fake-owner", True, True, False),
	("fake-owner", 1, ["Optimus User"], "fake-owner", False, True, False),
	("fake-sharee", 1, ["Optimus User"], "fake-owner", True, False, False),
	("fake-sharee", 1, ["Optimus User"], "fake-owner", True, True, True),
	("fake-owner", 1, ["Optimus User"], "fake-owner", True, False, True),
	("Administrator", 1, [], "fake-owner", True, True, True),
])
def test_worker_uses_current_actor_permissions(monkeypatch, user, enabled, roles, owner, read, write, expected):
	from optimus.line_profile import jobs
	fake = SimpleNamespace(db=SimpleNamespace(get_value=lambda *a: enabled), get_roles=lambda *a: roles,
		has_permission=lambda doctype, ptype, docname, **kw: read if ptype == "read" else write)
	monkeypatch.setattr(jobs, "frappe", fake)
	assert jobs._authorized({"name": "fake-doc", "owner": owner}, user) is expected


def test_realtime_timeout_remains_a_fresh_interrupt(monkeypatch):
	from optimus.line_profile import analyzer
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = Timeout("fake realtime timeout")
	def timeout(*a, **kw):
		raise original
	monkeypatch.setattr(analyzer, "frappe", SimpleNamespace(publish_realtime=timeout))
	with pytest.raises(Timeout) as caught:
		analyzer._publish("fake-event", {"user": "fake-owner"})
	assert caught.value is not original and caught.value.__context__ is None


def test_many_running_jobs_cannot_starve_pending_queue_recovery(worker, monkeypatch):
	pending = {"parent": "fake-session-doc", "run_uuid": "pending", "analyze_generation": "g"}
	busy = [{"parent": "fake-session-doc", "run_uuid": str(i), "analyze_generation": "g"} for i in range(100)]
	queries, dispatched = [], []
	def get_all(table, **kw):
		queries.append(kw)
		assert kw["limit_page_length"] <= 100
		if kw["filters"].get("analyze_dispatch_pending") == 1:
			return [pending]
		if kw["filters"].get("analyze_lease_until") == ["<=", 100]:
			return []
		return busy
	monkeypatch.setattr(worker.mod.frappe, "get_all", get_all, raising=False)
	monkeypatch.setattr(worker.mod, "_dispatch", lambda row: dispatched.append(row["run_uuid"]))
	worker.mod.recover_pending()
	assert dispatched == ["pending"]
	assert len(queries) == 2


def test_classification_settings_timeout_stops_the_worker(monkeypatch):
	from optimus import settings
	from optimus.line_profile import analyzer

	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = Timeout("fake classification timeout")
	def interrupted():
		raise original
	monkeypatch.setattr(settings, "get_config", interrupted)
	with pytest.raises(Timeout) as caught:
		analyzer._classify_hot_line(1000, 1000)
	assert caught.value is not original
	assert caught.value.__context__ is None


@pytest.mark.parametrize("field,value", [
	("analyze_attempts", 0), ("analyze_dispatch_pending", 1), ("analyze_worker_token", "forged"),
	("status", "Failed"), ("run_uuid", "another-run"), ("parent", "another-parent"),
	("results_json", "forged result"),
])
def test_direct_rest_child_save_cannot_change_journal_or_finished_results(field, value):
	from optimus.optimus.doctype.optimus_phase_two_run.optimus_phase_two_run import OptimusPhaseTwoRun
	before = {"analyze_generation": "fake-generation", "analyze_attempts": 1,
		"status": "Ready", "run_uuid": "fake-run", "parent": "fake-parent"}
	values = {**before, field: value}
	doc = SimpleNamespace(get=values.get, get_doc_before_save=lambda: before)
	with pytest.raises(Exception, match="Phase 2 analysis state is managed by the server"):
		OptimusPhaseTwoRun.validate(doc)


def test_direct_rest_child_delete_cannot_erase_retry_accounting():
	from optimus.optimus.doctype.optimus_phase_two_run.optimus_phase_two_run import OptimusPhaseTwoRun
	doc = SimpleNamespace(get={"analyze_generation": "fake-generation"}.get)
	with pytest.raises(Exception, match="Phase 2 analysis state is managed by the server"):
		OptimusPhaseTwoRun.on_trash(doc)


def test_parent_save_cannot_force_a_finished_run_back_into_retry():
	from optimus.line_profile.jobs import validate_parent_journal
	row = {"name": "fake-child", "analyze_generation": "fake-generation", "status": "Ready"}
	old = SimpleNamespace(phase_2_runs=[dict(row)])
	doc = SimpleNamespace(phase_2_runs=[{**row, "status": "Failed"}], get_doc_before_save=lambda: old)
	with pytest.raises(Exception, match="Phase 2 analysis state is managed by the server"):
		validate_parent_journal(doc)


def test_only_fenced_result_transaction_can_change_managed_results(phase2):
	admit(phase2)
	run = claim(phase2)
	row = dict(phase2.row())
	old = SimpleNamespace(phase_2_runs=[row])
	doc = SimpleNamespace(phase_2_runs=[{**row, "status": "Ready", "results_json": "fake result"}],
		get_doc_before_save=lambda: old)
	with phase2.db.transaction():
		assert phase2.mod._complete(run, now=102, persist=lambda: phase2.mod.validate_parent_journal(doc))
	with pytest.raises(Exception, match="Phase 2 analysis state is managed by the server"):
		phase2.mod.validate_parent_journal(doc)


@pytest.mark.parametrize("field,value", [
	("recording_user", "forged@example.com"), ("recording_user", None),
	("run_uuid", "forged-run"), ("status", "Ready"), ("picks_json", "[]"),
])
def test_capture_actor_and_input_identity_cannot_be_forged(field, value):
	from optimus.line_profile import jobs
	before = {"recording_user": "capture@example.com", "run_uuid": "fake-run", "status": "Recording",
		"parent": "fake-parent", "picks_json": "fake-input"}
	with pytest.raises(Exception, match="managed by the server"):
		jobs._validate_journal_row({**before, field: value}, before)


def test_capture_creation_scope_is_exact_and_resets():
	from optimus.line_profile import jobs
	row = {"recording_user": "capture@example.com", "run_uuid": "fake-run", "status": "Recording",
		"parent": "fake-parent", "parenttype": "Optimus Session", "parentfield": "phase_2_runs"}
	with pytest.raises(Exception, match="managed by the server"):
		jobs._validate_journal_row(row, {})
	with jobs.capture_creation("fake-parent", "fake-run", "capture@example.com"):
		jobs._validate_journal_row(row, {})
		for mutation in ({"run_uuid": "other"}, {"recording_user": "other"}, {"analyze_attempts": 1},
			{"status": "Ready"}, {"results_json": "forged"}, {"parenttype": "Other"}):
			with pytest.raises(Exception, match="managed by the server"):
				jobs._validate_journal_row({**row, **mutation}, {})
	with pytest.raises(Exception, match="managed by the server"):
		jobs._validate_journal_row(row, {})


def test_missing_capture_data_is_an_explicit_input_failure(monkeypatch):
	from optimus.line_profile import analyzer, capture, jobs
	def corrupt(*args):
		raise capture.CaptureInputError("invalid fake source")
	monkeypatch.setattr(capture, "read_all_samples", corrupt)
	with pytest.raises(jobs.MissingInput):
		analyzer._compute_run("fake-parent", "fake-run")


@pytest.mark.parametrize("actor,status,expected", [
	("fake-capture-user", "Recording", True), ("different-user", "Recording", False),
	("fake-capture-user", "Analyzing", False), ("fake-capture-user", "Ready", False),
])
def test_force_stop_rechecks_actual_capture_actor_and_current_status(phase2, actor, status, expected):
	phase2.row().update(recording_user=actor, status=status)
	with phase2.db.transaction():
		assert phase2.mod._force_stop_capture("fake-session-doc", "fake-phase2", "fake-capture-user") is expected
	assert phase2.row()["status"] == ("Failed" if expected else status)


def test_force_stop_legacy_capture_checks_parent_recording_user(phase2):
	phase2.row()["status"] = "Recording"
	phase2.db.rows["Optimus Session"]["fake-session-doc"]["user"] = "legacy-user"
	with phase2.db.transaction():
		assert not phase2.mod._force_stop_capture("fake-session-doc", "fake-phase2", "other")
		assert phase2.mod._force_stop_capture("fake-session-doc", "fake-phase2", "legacy-user")


@pytest.mark.parametrize("raced", [False, True])
def test_force_stop_preserves_input_if_analysis_claims_it(phase2, monkeypatch, raced):
	from optimus.line_profile import capture
	phase2.row().update(recording_user="capture-user", status="Recording")
	def transaction(operation):
		with phase2.db.transaction():
			return operation()
	monkeypatch.setattr(phase2.mod.ai_jobs, "_transaction", transaction)
	monkeypatch.setattr(phase2.mod.ai_jobs, "_retry_sql", transaction)
	monkeypatch.setattr(phase2.mod, "_capture_candidates", lambda user: [{"parent": "fake-session-doc", "run_uuid": "fake-phase2"}])
	def active(user, *, fresh=False):
		assert fresh
		if raced:
			phase2.row()["status"] = "Analyzing"
		return "fake-phase2"
	monkeypatch.setattr(capture, "is_active", active)
	def stop(run_uuid, user):
		assert not phase2.db.active
		return not raced
	monkeypatch.setattr(capture, "stop_line_profile_pass", stop)
	monkeypatch.setattr(capture, "cleanup_run", lambda *a: pytest.fail("deleted input used by a possible retry"))
	out = phase2.mod.force_stop_captures("capture-user")
	assert out["rows_marked_failed"] == int(not raced)
	assert phase2.row()["status"] == ("Analyzing" if raced else "Failed")
