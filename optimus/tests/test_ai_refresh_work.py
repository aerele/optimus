"""Worker inputs are tied to the initiating user and current SQL state."""

import json
import sys
from copy import deepcopy
from importlib import import_module
from types import ModuleType, SimpleNamespace

import pytest

from optimus import ai_fix, analyze
from optimus.tests.fixtures.ai_refresh_journal import journal as _journal_fixture  # noqa: F401
from optimus.tests.fixtures.ai_refresh_work import (  # noqa: F401
	Doc,
	_begin,
	_steps_run,
	_v15_has_permission,
)
from optimus.tests.fixtures.ai_refresh_work import (
	work as _work_fixture,
)


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


@pytest.mark.parametrize("locked", [False, True], ids=["admission", "send and save"])
def test_the_access_checks_read_no_child_table(work, locked):
	"""Given a name, Frappe v15 loads the whole session (every child table, call trees
	included) for each of the two checks, and two of the item's rechecks run inside the locked
	send and save transactions. Frappe v16 loads no child table unless the user has User
	Permissions. With no User Permission and no permission hook, the checks get the session
	built from the row the recheck has just read: every field of it, the session's own Link to
	its user included, which a User Permission check would read."""
	work.parent["user"] = "fake-owner"
	seen = _v15_has_permission(work, lambda ptype, doc, user: True)
	with work.journal.db.transaction():
		work.jobs._check_run(work.journal.run, locked=locked)
	assert work.child_loads == 0 and len(seen) == 2
	for doc in seen:
		assert doc["doctype"] == "Optimus Session" and "findings" not in doc
		assert all(doc.get(key) == value for key, value in work.parent.items())
		assert doc["user"] == "fake-owner" and doc["owner"] == "fake-owner"


@pytest.mark.parametrize(
	"user_permissions,hooks",
	[
		({"Company": [{"doc": "fake-company"}]}, {}),
		({}, {"Optimus Session": ["fake_app.permissions.session_has_permission"]}),
		({}, {"*": ["fake_app.permissions.has_permission"]}),
	],
	ids=["user-permissions", "session-hook", "every-doctype-hook"],
)
def test_the_access_checks_get_the_name_when_frappe_reads_more_than_the_row(work, user_permissions, hooks):
	"""A User Permission check also reads the Link fields of every child row, and a permission
	hook may read anything: then the checks get the session's name, and Frappe loads the
	document its own way (v15: whole; v16: lazily), exactly as before."""
	work.user_permissions, work.hooks = user_permissions, hooks
	seen = _v15_has_permission(work, lambda ptype, doc, user: True)
	work.jobs._check_run(work.journal.run)
	assert work.child_loads == 2 and len(seen) == 2
	assert all(doc.get("findings") for doc in seen)


def test_a_hook_for_another_doctype_keeps_the_row(work):
	work.hooks = {"File": ["optimus.permissions.file_has_permission"], "Note": ["fake_app.note"]}
	seen = _v15_has_permission(work, lambda ptype, doc, user: True)
	work.jobs._check_run(work.journal.run)
	assert work.child_loads == 0 and len(seen) == 2


@pytest.mark.parametrize("given", ["row", "name"])
@pytest.mark.parametrize(
	"grant,allowed",
	[("owner", True), ("read share", False), ("write share", True), ("role", True), ("none", False)],
)
def test_the_access_checks_keep_the_owner_share_and_role_rules(work, grant, allowed, given):
	"""The checks decide from the document's ``owner`` and ``name``, which the row carries: the
	owner may act, and so may a user with write (a write share, or the System Manager role);
	a read share alone may not, and nor may a user with no access. The answer is the same
	whether the checks get the row or the name."""
	if given == "name":
		work.user_permissions = {"Company": [{"doc": "fake-company"}]}
	shared = {"read share": {"read"}, "write share": {"read", "write"}}.get(grant, set())

	def decide(ptype, doc, user):
		if grant == "role":
			return True  # System Manager: read and write on every session
		if grant == "owner":
			return ptype == "read" and doc["owner"] == user  # Optimus User: read, if owner
		return ptype in shared and doc["name"] == work.parent["name"]

	if grant != "owner":
		work.parent["owner"] = "someone-else"
	_v15_has_permission(work, decide)
	if allowed:
		assert work.jobs._check_run(work.journal.run)["name"] == work.parent["name"]
	else:
		with pytest.raises(work.jobs.StopRefresh) as caught:
			work.jobs._check_run(work.journal.run)
		assert caught.value.reason == "permission"


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


def test_manual_refresh_settles_an_incomplete_answer_once_and_never_loops(work):
	from optimus import ai_prompts

	answer = {"suggestion": "Direction only", "prompt_version": ai_prompts.PROMPT_VERSION,
		"finish_reason": "length", "tokens": {"total_tokens": 3186}, "usage_complete": True}
	work.row["llm_fix_json"] = json.dumps(answer)
	work.journal.run["scope"] = "fixes_missing"
	assert work.jobs._next_item(work.journal.run, {}) is None
	work.journal.run["scope"] = "all"
	item = work.jobs._next_item(work.journal.run, {})
	assert item.target == work.row.name
	attempt = _begin(work, item)
	item.send(10)
	for _ in range(2):  # a replayed settlement must neither charge nor save twice
		with work.journal.db.transaction():
			work.journal.mod.settle_attempt(
				"fake-run", attempt["name"], docname="fake-session-doc", worker_token="worker",
				outcome="succeeded", tokens=3186, usage_complete=True, now=20,
				persist=lambda: item.persist(answer),
			)
	assert work.jobs._next_item(work.journal.run, {}) is None
	assert len(work.sends) == 1
	assert work.journal.run["tokens_reported"] == 3186
	assert json.loads(work.row["llm_fix_json"])["finish_reason"] == "length"


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


def test_presave_user_disable_check_is_the_first_plain_read_after_the_locks(work):
	"""The requester is read without a lock, so its read must open the read view: every
	journal transaction starts fresh (``_retry_sql``, ``_settle``) and locks first, so the
	User read is current when it runs and a user disabled before it stops the save."""
	item = work.jobs._next_item(work.journal.run, {})
	work.journal.db.rows["User"]["fake-owner"]["enabled"] = 0
	work.journal.db.calls.clear()
	with work.journal.db.transaction():
		assert item.valid() is False
	first_plain = next(c for c in work.journal.db.calls if not c[2])
	assert first_plain == ("User", "fake-owner", False)


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
	monkeypatch.setattr(analyze, "actions_for_humanizer", lambda recordings: [{"label": "fake action"}])

	def humanize(*a, usage_out, **kw):
		usage_out.begin()
		usage_out.observe(True)
		usage_out.update(total_tokens=7)
		return "fake steps"

	def fail(*a):
		raise ValueError("fake rendering failure")

	monkeypatch.setattr(ai_fix, "humanize_steps", humanize)
	monkeypatch.setattr(analyze, "assemble_humanized_notes", fail)
	item = work.jobs._next_item(work.journal.run, {})
	with pytest.raises(ai_fix.AiFixError) as caught:
		item.send(10)
	assert caught.value.usage["total_tokens"] == 7 and caught.value.usage_complete is True


@pytest.mark.parametrize("total,stored", [(7, 7), (2**31 - 1, 2**31 - 1), (3 * (2**31 - 1) + 5, 2**31 - 1)])
def test_the_steps_token_count_is_clamped_to_the_32_bit_field(work, monkeypatch, total, stored):
	"""The field stays an Int; a sum of valid provider counts that exceeds it is stored clamped."""
	work.journal.run.update(include_fixes=0, include_steps=1)
	monkeypatch.setattr(analyze, "actions_for_humanizer", lambda recordings: [{"label": "fake action"}])
	item = work.jobs._next_item(work.journal.run, {})
	touched = []
	monkeypatch.setattr(work.jobs, "_touch_session", lambda name, **values: touched.append(values))
	with work.journal.db.transaction():
		assert item.persist({"notes": "fake notes", "tokens": {"total_tokens": total}}) is True
	assert touched == [{"notes": "fake notes", "ai_steps_tokens": stored, "auto_notes_sha256": None}]


@pytest.mark.parametrize("complete", [True, False])
def test_the_steps_send_returns_its_notes_its_tokens_and_whether_they_are_complete(
	work, monkeypatch, complete
):
	_steps_run(work, monkeypatch)
	monkeypatch.setattr(analyze, "assemble_humanized_notes", lambda text: f"assembled {text}")

	def humanize(*a, usage_out, **kw):
		usage_out.begin()
		usage_out.observe(complete)
		usage_out.update(total_tokens=9)
		return "fake steps"

	monkeypatch.setattr(ai_fix, "humanize_steps", humanize)
	item = work.jobs._next_item(work.journal.run, {})
	assert item.kind == "steps"
	assert item.send(10) == {
		"notes": "assembled fake steps",
		"tokens": {"total_tokens": 9},
		"usage_complete": complete,
	}


def test_a_steps_answer_is_not_saved_over_a_session_saved_since_it_was_prepared(work, monkeypatch):
	"""The save rechecks the item: a Desk save of the session since the item was prepared
	makes the answer stale, and the session's notes are left as they are."""
	_steps_run(work, monkeypatch)
	work.parent["modified"] = "t1"
	item = work.jobs._next_item(work.journal.run, {})
	touched = []
	monkeypatch.setattr(work.jobs, "_touch_session", lambda name, **values: touched.append(values))
	work.parent["modified"] = "t2"
	with work.journal.db.transaction():
		assert item.persist({"notes": "fake notes", "tokens": {"total_tokens": 3}}) is False
	assert touched == []


def test_steps_switched_off_in_the_settings_are_recorded_and_never_sent(work, monkeypatch):
	_steps_run(work, monkeypatch)
	monkeypatch.setattr(ai_fix, "is_available", lambda section=None: section != "humanize")
	assert work.jobs._next_item(work.journal.run, {}) is None
	assert work.journal.run["steps_state"] == "toggle_off" and not work.sends


@pytest.mark.parametrize("scope,kept", [("fixes_missing", True), ("all", False)])
def test_notes_a_person_wrote_are_kept_by_an_analyze_time_run_only(work, monkeypatch, scope, kept):
	"""An analyze-time run never replaces notes a person wrote; a user refresh does."""
	_steps_run(work, monkeypatch, scope=scope)
	work.parent["notes"] = "Human authored reproduction notes"
	item = work.jobs._next_item(work.journal.run, {})
	if kept:
		assert item is None and work.journal.run["steps_state"] == "kept"
	else:
		assert item.kind == "steps" and work.journal.run["steps_state"] == "pending"


@pytest.mark.parametrize("scope,selected", [("all", True), ("fixes_missing", False)])
def test_an_outdated_answer_is_refreshed_by_a_user_refresh_only(work, scope, selected):
	"""A stored answer from an older prompt version is outdated: a user refresh replaces it, an
	analyze-time run only fills findings with no answer."""
	from optimus import ai_prompts

	work.journal.run.update(scope=scope, requested_at_epoch=1_800_000_000)  # after the answer
	work.row["llm_fix_json"] = json.dumps(
		{
			"suggestion": "an older answer",
			"prompt_version": ai_prompts.PROMPT_VERSION - 1,
			"generated_at": "2026-01-02T10:00:00+00:00",
		}
	)
	item = work.jobs._next_item(work.journal.run, {})
	assert (item is not None and item.target == work.row.name) is selected


@pytest.mark.parametrize("count,stops", [(10000, False), (10001, True)])
@pytest.mark.parametrize("where", ["chain", "uncertain"])
def test_more_than_ten_thousand_attempts_to_weigh_stop_the_run(work, where, count, stops):
	"""``history_limit``: one selection weighs at most 10000 attempts of the run's chain and
	10000 uncertain attempts of its session."""
	work.journal.db.rows[work.journal.mod.ATTEMPT] = {
		f"attempt-{i}": {
			"run_id": "fake-run" if where == "chain" else "older-run",
			"session_name": work.parent["name"],
			"kind": "fix",
			"target_name": f"target-{i}",
			"input_hash": "fake",
			"state": "failed" if where == "chain" else "uncertain",
		}
		for i in range(count)
	}
	if stops:
		with pytest.raises(work.jobs.StopRefresh) as caught:
			work.jobs._attempts(work.journal.run)
		assert caught.value.reason == "history_limit"
	else:
		tried, uncertain, _spent = work.jobs._attempts(work.journal.run)
		assert len(tried) + len(uncertain) == count


def test_a_retry_of_uncertain_calls_does_not_weigh_them(work):
	work.journal.run["retry_uncertain"] = 1
	work.journal.db.rows[work.journal.mod.ATTEMPT] = {
		f"attempt-{i}": {
			"run_id": "older-run",
			"session_name": work.parent["name"],
			"kind": "fix",
			"target_name": f"target-{i}",
			"input_hash": "fake",
			"state": "uncertain",
		}
		for i in range(10001)
	}
	assert work.jobs._attempts(work.journal.run) == ([], set(), 0)


@pytest.mark.parametrize(
	"field,value",
	[("parent", "other-session-doc"), ("parenttype", "Other Doctype"), ("parentfield", "other_table")],
)
def test_a_finding_row_that_is_not_one_of_the_sessions_findings_is_never_answered(work, field, value):
	"""The locked Finding row must be a row of this session's ``findings`` table, even when its
	fingerprint matches: an answer is never written to another session's or table's row."""
	work.row[field] = value
	item = work.jobs._next_item(work.journal.run, {})
	assert item.target == work.row.name
	with work.journal.db.transaction():
		assert item.valid() is False
		assert item.persist({"suggestion": "fake answer"}) is False
	assert work.row["llm_fix_json"] == ""


def test_locked_recheck_reads_the_requester_without_locking_the_user_row(work):
	"""A FOR UPDATE on tabUser blocked the requester's own User writes (last_active) under the
	session, run and attempt locks, and added a cross-table lock order. A plain read is enough
	for an enabled check; all locking reads come first, so the read view opens after them."""
	work.journal.db.calls.clear()
	with work.journal.db.transaction():
		work.jobs._check_run(work.journal.run, locked=True)
	assert [c for c in work.journal.db.calls if c[0] == "User"] == [("User", "fake-owner", False)]
	locking = [c[2] for c in work.journal.db.calls]
	assert locking == sorted(locking, reverse=True)


def test_input_recheck_takes_its_row_locks_before_any_plain_read(work):
	item = work.jobs._next_item(work.journal.run, {})
	work.journal.db.calls.clear()
	with work.journal.db.transaction():
		assert item.valid()
	locking = [c[2] for c in work.journal.db.calls]
	assert False in locking and locking == sorted(locking, reverse=True)
	assert ("Optimus Finding", "finding-1", True) in work.journal.db.calls


def test_the_send_intent_transaction_takes_every_lock_before_its_first_plain_read(work):
	"""In the transaction that writes the Calling attempt, the Finding row is locked
	before the requester's plain User read opens the read view (no 1020 on MariaDB 11.8
	when a Finding writer that skips the session lock commits in between)."""
	item = work.jobs._next_item(work.journal.run, {})
	work.journal.db.calls.clear()
	assert _begin(work, item)["state"] == "calling"
	calls = work.journal.db.calls
	first_plain = next(i for i, c in enumerate(calls) if not c[2])
	assert calls.index(("Optimus Finding", "finding-1", True)) < first_plain
	assert all(c[2] for c in calls[:first_plain])


def test_the_send_intent_transaction_still_ends_a_run_that_must_stop(work):
	item = work.jobs._next_item(work.journal.run, {})
	work.journal.db.rows["User"]["fake-owner"]["enabled"] = 0
	with pytest.raises(work.jobs.StopRefresh) as caught:
		_begin(work, item)
	assert caught.value.reason == "permission"
	assert work.journal.run["attempted"] == 0


def test_the_send_intent_transaction_yields_on_a_changed_input(work):
	item = work.jobs._next_item(work.journal.run, {})
	work.row["title"] = "changed input"
	assert _begin(work, item) is None
	assert work.journal.run["attempted"] == 0


def test_selection_stops_the_run_when_ai_is_switched_off(work, monkeypatch):
	"""With the provider off nothing was selected, so the run ended complete."""
	monkeypatch.setattr(ai_fix, "is_available", lambda **kw: False)
	with pytest.raises(work.jobs.StopRefresh) as caught:
		work.jobs._next_item(work.journal.run, {})
	assert caught.value.reason == "disabled" and not work.sends


@pytest.mark.parametrize("sent", [False, True])
def test_the_fix_send_counts_its_calls_in_the_engines_own_usage_boundary(work, monkeypatch, sent):
	"""A failure is a known zero only when the call count proves nothing was sent;
	one after a begun call, with no usage reported, stays unknown."""

	def suggest_fix(payload, *, usage_out=None, **kw):
		if sent and usage_out is not None:
			usage_out.begin()
		raise ai_fix.AiFixError("fake failure", kind="timeout" if sent else "context")

	monkeypatch.setattr(ai_fix, "suggest_fix", suggest_fix)
	item = work.jobs._next_item(work.journal.run, {})
	with pytest.raises(ai_fix.AiFixError) as caught:
		item.send(10)
	assert caught.value.usage_complete is (not sent)
