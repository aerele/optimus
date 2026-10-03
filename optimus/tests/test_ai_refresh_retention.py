"""Session deletion owns journal retention and fences late optional work."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from optimus import ai_jobs
from optimus.tests.test_ai_refresh_journal import journal as _journal_fixture  # noqa: F401

pytestmark = pytest.mark.rq


@pytest.fixture
def retention(journal, monkeypatch):
	db, store = journal.db, journal.mod
	db.rows[store.RUN]["foreign-run"] = {"name": "foreign-run", "session_name": "another-session"}
	db.rows[store.ATTEMPT] = {
		"uncertain": {"name": "uncertain", "session_name": "fake-session-doc", "state": "uncertain"},
		"foreign": {"name": "foreign", "session_name": "another-session", "state": "uncertain"},
	}
	def delete(table, filters):
		assert db.active and ("Optimus Session", "fake-session-doc") in db.locks
		for name, row in list(db.rows.get(table, {}).items()):
			if all(row.get(key) == value for key, value in filters.items()):
				del db.rows[table][name]
	db.delete = delete
	db.table_exists = lambda table: table in db.rows
	return journal


def test_delete_removes_only_that_sessions_private_history(retention):
	db, store = retention.db, retention.mod
	with db.transaction():
		store.delete_session_journal("fake-session-doc", "fake-session")
	assert list(db.rows[store.RUN]) == ["foreign-run"]
	assert list(db.rows[store.ATTEMPT]) == ["foreign"]
	assert "fake-session-doc" in db.rows["Optimus Session"]  # framework deletes parent in this same transaction


def test_failed_deletion_restores_uncertain_history(retention, monkeypatch):
	db, store = retention.db, retention.mod
	before = deepcopy(db.rows)
	delete = db.delete
	def fail_second(table, filters):
		if table == store.RUN:
			raise RuntimeError("fake SQL failure")
		delete(table, filters)
	monkeypatch.setattr(db, "delete", fail_second)
	with pytest.raises(RuntimeError, match="fake SQL failure"), db.transaction():
		store.delete_session_journal("fake-session-doc", "fake-session")
	assert db.rows == before


def test_stale_parent_identity_cannot_delete_another_journal(retention):
	db, store = retention.db, retention.mod
	before = deepcopy(db.rows)
	with pytest.raises(ValueError), db.transaction():
		store.delete_session_journal("fake-session-doc", "wrong-uuid")
	assert db.rows == before


def test_capture_cleanup_waits_for_parent_commit_and_does_not_commit_itself(retention, monkeypatch):
	from optimus.line_profile import capture
	callbacks, stopped, cleaned = [], [], []
	db = retention.db
	db.after_commit = SimpleNamespace(add=callbacks.append)
	doc = SimpleNamespace(name="fake-session-doc", session_uuid="fake-session", user="legacy-user",
		phase_2_runs=[SimpleNamespace(run_uuid="fake-capture", recording_user="actual-user"),
			SimpleNamespace(run_uuid="legacy-capture")])
	monkeypatch.setattr(ai_jobs, "frappe", SimpleNamespace(db=db))
	monkeypatch.setattr(capture, "stop_line_profile_pass", lambda run, user: stopped.append((run, user)))
	monkeypatch.setattr(capture, "cleanup_run", lambda run: cleaned.append(run))
	with db.transaction():
		ai_jobs.delete_session_state(doc)
		assert not stopped and not cleaned
	assert len(callbacks) == 1
	callbacks[0]()
	assert stopped == [("fake-capture", "actual-user"), ("legacy-capture", "legacy-user")]
	assert cleaned == ["fake-capture", "legacy-capture"]


def test_session_controller_runs_the_retention_fence(monkeypatch):
	from optimus.optimus.doctype.optimus_session.optimus_session import OptimusSession
	seen = []
	monkeypatch.setattr(ai_jobs, "delete_session_state", lambda doc: seen.append(doc), raising=False)
	doc = SimpleNamespace(name="fake-session-doc")
	OptimusSession.on_trash(doc)
	assert seen == [doc]


def test_journal_schema_has_no_prompt_reply_key_or_recording_body_fields():
	import json
	from pathlib import Path
	root = Path(__file__).resolve().parents[1] / "optimus" / "doctype"
	for folder in ("optimus_ai_refresh_run", "optimus_ai_refresh_attempt"):
		schema = json.loads((root / folder / (folder + ".json")).read_text())
		assert schema["permissions"] == []
		assert all(field["fieldtype"] not in {"Text", "Small Text", "Long Text", "Code", "JSON", "Password"}
			for field in schema["fields"])
		assert not {"prompt", "response", "api_key", "query", "recording", "source_code"}.intersection(
			field["fieldname"] for field in schema["fields"])


@pytest.mark.parametrize("field", ["ai_tokens_spent", "ai_refresh_count", "ai_steps_tokens"])
def test_session_save_cannot_overwrite_worker_accounting(field):
	from optimus.optimus.doctype.optimus_session.optimus_session import OptimusSession
	old = {field: 17}
	before = SimpleNamespace(get=old.get, phase_2_runs=[])
	doc = SimpleNamespace(get=lambda key: 0 if key == field else None,
		get_doc_before_save=lambda: before, phase_2_runs=[])
	with pytest.raises(Exception, match="AI usage is managed by the server"):
		OptimusSession.validate(doc)


def test_unrelated_session_edit_preserves_existing_accounting():
	from optimus.optimus.doctype.optimus_session.optimus_session import OptimusSession
	values = {"ai_tokens_spent": 17, "ai_refresh_count": 2, "ai_steps_tokens": 5}
	before = SimpleNamespace(get=values.get, phase_2_runs=[])
	doc = SimpleNamespace(get=values.get, get_doc_before_save=lambda: before, phase_2_runs=[])
	OptimusSession.validate(doc)


@pytest.mark.parametrize("logger_failure", [False, True])
def test_deleted_capture_cleanup_is_best_effort_and_counts_only(monkeypatch, logger_failure):
	from optimus.line_profile import capture
	seen, cleaned = [], []
	def fail_stop(run, user):
		raise ConnectionError("private transport detail")
	def warning(message, *args):
		seen.append(message % args)
		if logger_failure:
			raise OSError("fake log failure")
	monkeypatch.setattr(capture, "stop_line_profile_pass", fail_stop)
	monkeypatch.setattr(capture, "cleanup_run", cleaned.append)
	monkeypatch.setattr(ai_jobs, "frappe", SimpleNamespace(logger=lambda name: SimpleNamespace(warning=warning)))
	ai_jobs._cleanup_deleted_captures((("private-run", "private-user"),))
	assert len(seen) == 1 and "failed=1" in seen[0]
	assert "private" not in seen[0] and "new capture input" in seen[0]
	# A failed stop must not delete input that could still be in use.
	assert not cleaned


def test_deleted_capture_cleanup_preserves_fresh_job_timeout(monkeypatch):
	from optimus.line_profile import capture
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	interrupt = Timeout("fake timeout")
	def fail(*a):
		raise interrupt
	monkeypatch.setattr(capture, "stop_line_profile_pass", fail)
	with pytest.raises(Timeout) as caught:
		ai_jobs._cleanup_deleted_captures((("fake-run", "fake-user"),))
	assert caught.value is not interrupt and caught.value.__context__ is None
