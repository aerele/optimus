"""Worker inputs are tied to the initiating user and current SQL state."""

from copy import deepcopy
from importlib import import_module
from types import SimpleNamespace

import pytest

from optimus import ai_fix, analyze
from optimus.tests.test_ai_refresh_journal import journal as _journal_fixture  # noqa: F401


class Doc(dict):
	def __getattr__(self, key):
		return self.get(key)

	def as_dict(self):
		return dict(self)


@pytest.fixture
def work(journal, monkeypatch):
	jobs = import_module("optimus.ai_jobs")
	run = journal.run
	run.update(
		requested_by="fake-owner",
		include_fixes=1,
		include_steps=0,
		cap=20,
		regenerate_all=0,
		requested_at_epoch=10,
		retry_uncertain=0,
	)
	parent = journal.db.rows["Optimus Session"]["fake-session-doc"]
	parent.update(owner="fake-owner", notes="", title="fake title", recordings_file="", actions=[])
	row = Doc(
		name="finding-1",
		parent=parent["name"],
		parenttype="Optimus Session",
		parentfield="findings",
		finding_type="N+1 Query",
		severity="High",
		estimated_impact_ms=10,
		title="fake finding",
		technical_detail_json="{}",
		llm_fix_json="",
		action_ref="0",
	)
	journal.db.rows["Optimus Finding"] = {row.name: row}
	journal.db.rows["User"] = {"fake-owner": {"name": "fake-owner", "enabled": 1}}
	checks, sends = [], []
	env = SimpleNamespace(
		jobs=jobs,
		journal=journal,
		parent=parent,
		row=row,
		checks=checks,
		sends=sends,
		can_read=True,
		can_write=False,
		roles=["Optimus User"],
	)

	def get_doc(table, name):
		assert table == "Optimus Session" and name == parent["name"]
		return Doc(**deepcopy(parent), findings=[Doc(deepcopy(row))])

	def permission(table, kind, doc, *, user):
		checks.append((kind, user))
		return env.can_read if kind == "read" else env.can_write

	monkeypatch.setattr(
		jobs,
		"frappe",
		SimpleNamespace(
			db=journal.db,
			get_doc=get_doc,
			get_all=journal.db.get_all,
			has_permission=permission,
			get_roles=lambda user: env.roles,
		),
	)
	monkeypatch.setattr(jobs, "_now", lambda: 20)
	monkeypatch.setattr("optimus.settings.get_config", lambda: SimpleNamespace(ai_excluded_finding_types=[]))
	monkeypatch.setattr(ai_fix, "is_available", lambda **kw: True)
	monkeypatch.setattr(
		jobs,
		"_build_payload",
		lambda row, cache, grounding, *, principal: {"title": row.title, "principal": principal},
	)
	monkeypatch.setattr(analyze, "_phase2_index_for", lambda doc: {})
	monkeypatch.setattr(analyze, "load_recordings_light", lambda *a, **kw: [])
	monkeypatch.setattr(
		ai_fix,
		"suggest_fix",
		lambda payload, **kw: (
			sends.append((payload, kw))
			or {"suggestion": "fake answer", "tokens": {"total_tokens": 7}, "usage_complete": True}
		),
	)

	def transaction(fn):
		with journal.db.transaction():
			return fn()

	monkeypatch.setattr(jobs, "_transaction", transaction)
	with journal.db.transaction():
		journal.mod.claim("fake-run", slice_no=0, worker_token="worker", now=10, lease_seconds=300)
	return env


def test_worker_checks_requesting_user_instead_of_worker_identity(work):
	work.jobs._check_run(work.journal.run)
	assert work.checks == [("read", "fake-owner"), ("write", "fake-owner")]


@pytest.mark.parametrize(
	"change,reason",
	[
		("status", "not_ready"),
		("owner", "permission"),
		("read", "permission"),
		("role", "permission"),
		("disabled", "permission"),
		("phase2", "phase2"),
	],
)
def test_invalid_state_or_access_prevents_call(work, change, reason):
	if change == "status":
		work.parent["status"] = "Analyzing"
	elif change == "owner":
		work.parent["owner"] = "different-owner"
	elif change == "read":
		work.can_read = False
	elif change == "role":
		work.roles = []
	elif change == "disabled":
		work.journal.db.rows["User"]["fake-owner"]["enabled"] = 0
	else:
		work.journal.db.rows["Optimus Phase Two Run"] = {
			"fake-phase2": {"parent": work.parent["name"], "status": "Recording"}
		}
	with pytest.raises(work.jobs.StopRefresh) as caught:
		work.jobs._check_run(work.journal.run)
	assert caught.value.reason == reason


def test_prepared_work_carries_explicit_session_and_principal(work):
	item = work.jobs._next_item(work.journal.run, {})
	assert item.kind == "fix" and not work.sends
	item.send(10)
	payload, kwargs = work.sends[0]
	assert payload["principal"] == "fake-owner"
	assert kwargs["session_uuid"] == "fake-session" and kwargs["docname"] == "fake-session-doc"


@pytest.mark.parametrize("field", ["title", "technical_detail_json", "llm_fix_json"])
def test_changed_finding_invalidates_prepared_answer(work, field):
	item = work.jobs._next_item(work.journal.run, {})
	work.row[field] = "changed input"
	with work.journal.db.transaction():
		assert not item.valid()
		assert item.persist({"suggestion": "obsolete"}) is False
	assert work.row["llm_fix_json"] != '{"suggestion": "obsolete"}'


def test_prior_uncertain_call_requires_explicit_retry_for_unchanged_input(work):
	item = work.jobs._next_item(work.journal.run, {})
	work.journal.db.rows[work.journal.mod.ATTEMPT] = {
		"older-attempt": {
			"run_id": "older-run",
			"session_name": work.parent["name"],
			"kind": "fix",
			"target_name": item.target,
			"input_hash": item.input_hash,
			"state": "uncertain",
		}
	}
	assert work.jobs._next_item(work.journal.run, {}) is None
	work.journal.run["retry_uncertain"] = 1
	assert work.jobs._next_item(work.journal.run, {}).target == item.target


def test_already_attempted_finding_does_not_repeat_on_next_slice(work):
	work.journal.db.rows[work.journal.mod.ATTEMPT] = {
		"previous": {
			"run_id": "fake-run",
			"session_name": work.parent["name"],
			"kind": "fix",
			"target_name": work.row.name,
			"input_hash": "fake",
			"state": "failed",
		}
	}
	assert work.jobs._next_item(work.journal.run, {}) is None


def test_auto_refresh_preserves_human_written_steps(work):
	work.journal.run.update(scope="fixes_missing", include_steps=1, include_fixes=0)
	work.parent["notes"] = "Human authored reproduction notes"
	assert work.jobs._next_item(work.journal.run, {}) is None
	assert work.parent["notes"] == "Human authored reproduction notes"


def test_resumed_saved_steps_do_not_make_another_provider_call(work, monkeypatch):
	work.journal.run.update(include_steps=1, include_fixes=0, steps_state="carried", render_pending=1)

	def repeated(*args):
		raise AssertionError("a saved Steps result must survive resume")

	monkeypatch.setattr(work.jobs, "_steps_item", repeated)
	assert work.jobs._next_item(work.journal.run, {}) is None
	assert work.journal.run["steps_state"] == "carried" and work.journal.run["render_pending"] == 1


def test_presave_phase2_check_uses_a_current_read_after_waiting_for_parent(work, monkeypatch):
	item = work.jobs._next_item(work.journal.run, {})
	work.journal.db.rows["Optimus Phase Two Run"] = {
		"fake-phase2": {"name": "fake-phase2", "parent": work.parent["name"], "status": "Analyzing"}
	}
	current = work.journal.db.get_values

	def snapshot(table, filters, fields, *, for_update=False, **kw):
		if table == "Optimus Phase Two Run" and not for_update:
			return []
		return current(table, filters, fields, for_update=for_update, **kw)

	monkeypatch.setattr(work.journal.db, "get_values", snapshot)
	with work.journal.db.transaction():
		assert item.valid() is False


def test_presave_user_disable_check_uses_a_current_read(work, monkeypatch):
	item = work.jobs._next_item(work.journal.run, {})
	work.journal.db.rows["User"]["fake-owner"]["enabled"] = 0
	current = work.journal.db.get_value

	def snapshot(table, *a, for_update=False, **kw):
		if table == "User" and not for_update:
			return 1
		return current(table, *a, for_update=for_update, **kw)

	monkeypatch.setattr(work.journal.db, "get_value", snapshot)
	with work.journal.db.transaction():
		assert item.valid() is False


def test_progress_counts_targets_and_exposes_prior_uncertainty(work):
	item = work.jobs._next_item(work.journal.run, {})
	assert work.journal.run["total_items"] == 1
	work.journal.db.rows[work.journal.mod.ATTEMPT] = {
		"older": {
			"run_id": "old-run",
			"session_name": work.parent["name"],
			"kind": "fix",
			"target_name": item.target,
			"input_hash": item.input_hash,
			"state": "uncertain",
		}
	}
	assert work.jobs._next_item(work.journal.run, {}) is None
	assert work.journal.run["blocked_uncertain"] == 1
	assert work.jobs.public_state(work.journal.run)["blocked_uncertain"] == 1


def test_steps_without_input_reports_a_reason_without_provider_call(work):
	work.journal.run.update(include_fixes=0, include_steps=1)
	assert work.jobs._next_item(work.journal.run, {}) is None
	assert work.journal.run["steps_state"] == "no_input" and not work.sends


def test_successful_result_updates_finding_and_invalidates_stale_form(work, monkeypatch):
	item = work.jobs._next_item(work.journal.run, {})
	monkeypatch.setattr("frappe.utils.now_datetime", lambda: "fake-new-modified")
	with work.journal.db.transaction():
		assert item.persist({"suggestion": "new fake answer"}) is True
	assert "new fake answer" in work.row["llm_fix_json"]
	assert work.parent["modified"] == "fake-new-modified"


def test_fingerprint_ignores_framework_bookkeeping_and_normalizes_sql_numbers(work):
	from decimal import Decimal

	row = dict(work.row)
	doc_row = {
		**row,
		"estimated_impact_ms": 10.0,
		"affected_count": 0,
		"__onload": {"fake": True},
		"doctype": "Optimus Finding",
	}
	db_row = {
		**row,
		"estimated_impact_ms": Decimal("10.000000000"),
		"affected_count": None,
		"_comments": None,
		"_assign": None,
		"_liked_by": None,
	}
	assert work.jobs._signature(work.parent, doc_row) == work.jobs._signature(work.parent, db_row)


def test_steps_postprocessing_failure_keeps_reported_usage(work, monkeypatch):
	work.journal.run.update(include_fixes=0, include_steps=1)
	monkeypatch.setattr(analyze, "_actions_for_humanizer", lambda recordings: [{"label": "fake action"}])

	def humanize(*a, usage_out, **kw):
		usage_out.begin()
		usage_out.observe(True)
		usage_out.update(total_tokens=7)
		return "fake steps"

	def fail(*a):
		raise ValueError("fake rendering failure")

	monkeypatch.setattr(ai_fix, "humanize_steps", humanize)
	monkeypatch.setattr(analyze, "_assemble_humanized_notes", fail)
	item = work.jobs._next_item(work.journal.run, {})
	with pytest.raises(ai_fix.AiFixError) as caught:
		item.send(10)
	assert caught.value.usage["total_tokens"] == 7 and caught.value.usage_complete is True
