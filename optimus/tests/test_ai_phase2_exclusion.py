"""AI admission and Phase 2 serialize on the same SQL mutex and parent."""

import pytest

from optimus.tests.test_ai_refresh_journal import admit
from optimus.tests.test_ai_refresh_journal import journal as _journal_fixture  # noqa: F401


@pytest.mark.parametrize("status", ["Recording", "Analyzing"])
def test_phase2_started_after_precheck_still_refuses_ai_admission(journal, status):
	journal.db.rows[journal.mod.RUN] = {}
	journal.db.rows["Optimus Phase Two Run"] = {
		"phase2-row": {"name": "phase2-row", "parent": "fake-session-doc", "status": status},
	}
	assert admit(journal) == {"status": "refused", "reason": "phase2"}
	assert not journal.db.rows[journal.mod.RUN]


def test_phase2_lock_refuses_an_active_ai_generation(journal):
	with journal.db.transaction():
		assert journal.mod.lock_phase2_parent("fake-session-doc") == "ai_refresh"
	assert journal.db.locks[:2] == [(journal.mod.CONTROL, "site"), ("Optimus Session", "fake-session-doc")]


def test_phase2_lock_changes_shared_mutex_to_reject_old_postgres_admission_snapshot(journal):
	journal.db.rows[journal.mod.RUN] = {}
	with journal.db.transaction():
		assert journal.mod.lock_phase2_parent("fake-session-doc") is None
		assert journal.db.active  # caller still owns the locks until its phase-2 row commits
	assert journal.db.rows[journal.mod.CONTROL]["site"]["generation"] == 1


def test_phase2_lock_refuses_a_parent_that_is_no_longer_ready(journal):
	journal.db.rows[journal.mod.RUN] = {}
	journal.db.rows["Optimus Session"]["fake-session-doc"]["status"] = "Analyzing"
	with journal.db.transaction():
		assert journal.mod.lock_phase2_parent("fake-session-doc") == "not_ready"



def test_retry_analyze_fences_an_ai_call_and_resets_parent_atomically(journal):
	parent = journal.db.rows["Optimus Session"]["fake-session-doc"]
	parent["status"] = "Failed"
	journal.run.update(state="running", worker_token="fake-worker", lease_until=200)
	journal.db.rows[journal.mod.RUN]["fake-run"] = journal.run
	with journal.db.transaction():
		assert journal.mod.prepare_analyze_retry("fake-session-doc", "fake-session", requested_by="fake-owner", now=100)
	assert parent["status"] == "Stopping"
	run = journal.db.rows[journal.mod.RUN]["fake-run"]
	assert run["state"] == "cancelled" and run["active_session"] is None
	assert run["cancelled_by"] == "fake-owner"


def test_retry_analyze_rechecks_parent_status_under_lock(journal):
	with journal.db.transaction():
		assert not journal.mod.prepare_analyze_retry("fake-session-doc", "fake-session", requested_by="fake-owner", now=100)
	assert journal.db.rows["Optimus Session"]["fake-session-doc"]["status"] == "Ready"
	assert journal.run["state"] == "queued"


def test_retry_analyze_returns_exact_owned_captures_for_postcommit_stop(journal):
	journal.db.rows["Optimus Session"]["fake-session-doc"].update(status="Failed", user="legacy-user")
	journal.db.rows["Optimus Phase Two Run"] = {
		"one": {"name": "one", "parent": "fake-session-doc", "run_uuid": "one-run", "status": "Recording", "recording_user": "capture-user"},
		"two": {"name": "two", "parent": "fake-session-doc", "run_uuid": "two-run", "status": "Recording"},
		"three": {"name": "three", "parent": "fake-session-doc", "run_uuid": "three-run", "status": "Analyzing"},
	}
	captures = []
	with journal.db.transaction():
		assert journal.mod.prepare_analyze_retry("fake-session-doc", "fake-session", requested_by="fake-owner", now=100, stopped_captures=captures)
	assert captures == [("one-run", "capture-user"), ("two-run", "legacy-user")]
