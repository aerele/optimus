# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Refresh AI suggestions writes each answer with its spend in one short transaction.

MariaDB 11.8 turns ``innodb_snapshot_isolation`` on by default: a locking UPDATE of a row
another transaction committed after this one's read view fails with 1020 ("Record has changed
since last read") and InnoDB rolls back the WHOLE transaction, like a deadlock (1213); Frappe
raises both as ``frappe.QueryDeadlockError``. A refresh that kept every answer in one
transaction until its end lost the earlier answers to one such rollback while it still counted
them as added. Each answer, its spend and the commit are now one short transaction, tried again
on ``QueryDeadlockError``; ``added`` counts only committed answers, and no transaction stays open
across a provider call.

These run the real ``_run_ai_backfill``, ``suggest_fix``, ``_add_ai_spend`` and
``_increment_session_counter`` against a fake transactional database."""

import json
import sys
from types import SimpleNamespace

import pytest

from optimus import ai_fix, ai_prompts, analyze

pytestmark = pytest.mark.rq

_PROVIDER = {
	"name": "fake", "protocol": "openai", "base_url": "https://fake.invalid/v1", "model": "fake",
	"needs_key": False, "context_tokens": 128000,
}
_ANSWER = "## Diagnosis\nRepeated work.\n## Fix\nBatch the call.\n## Why it works\nFewer calls.\n## Verify\nProfile again."
_TOKENS = 1000


class _Deadlock(Exception):
	"""Stands for ``frappe.QueryDeadlockError``: MariaDB 1213, or 1020 under snapshot isolation."""


class _TxnDb:
	"""A transactional ``frappe.db``: writes stay pending until ``commit``; a "whole" failure
	rolls every pending write back (the server ended the transaction) and drops the savepoint, so
	``rollback(save_point=)`` then fails as MariaDB's 1305 does; a "statement" failure leaves both."""

	def __init__(
		self, *, fail_updates=None, fail_writes=None, fail_commit_of=None, fail_after_commit=(),
		deadlock_commits=False,
	):
		self.fail_updates = dict(fail_updates or {})  # n-th counter UPDATE -> "whole" | "statement"
		self.fail_writes = dict(fail_writes or {})  # n-th Finding write -> "whole" | "other"
		self.fail_commit_of = fail_commit_of  # the first COMMIT that would make this finding durable fails
		# the n-th COMMIT succeeds, then an after_commit callback raises (Frappe runs them after the SQL COMMIT)
		self.fail_after_commit = set(fail_after_commit)
		self.deadlock_commits = deadlock_commits  # every COMMIT with writes is chosen as a deadlock victim
		self.updates = self.writes = self.commits = 0
		self.pending_tokens, self.pending = 0, {}
		self.tokens, self.answers = 0, {}
		self.savepoint_alive = False
		self.rollbacks = 0
		self.charged = []
		self.view_open = self.changed_since_view = False
		self.fresh_reads = 0

	def savepoint(self, name):
		self.savepoint_alive = True

	def release_savepoint(self, name):
		self.savepoint_alive = False

	def rollback(self, *, save_point=None, **kw):
		if save_point:
			if not self.savepoint_alive:
				raise RuntimeError("1305 SAVEPOINT optimus_ai_counter does not exist")
			self.savepoint_alive = False
			return
		self._end()
		self.rollbacks += 1

	def _end(self):
		self.pending_tokens, self.pending, self.savepoint_alive = 0, {}, False
		self.view_open = self.changed_since_view = False

	def read_during_the_call(self, *, other_writer_commits):
		"""The provider call reads Optimus Settings (a consistent read opens a read view); another
		writer may commit the session row before the reply comes back."""
		self.view_open = True
		self.changed_since_view = other_writer_commits

	def increment(self, key, field, n, *, by="name"):
		def run():
			self.updates += 1
			mode = self.fail_updates.get(self.updates)
			if self.view_open and self.changed_since_view:
				mode = "whole"  # 1020 under innodb_snapshot_isolation
			if mode == "whole":
				self._end()
				raise _Deadlock("(1020) Record has changed since last read; the transaction was rolled back")
			if mode == "statement":
				raise RuntimeError("(1205) Lock wait timeout exceeded")
			assert field == "ai_tokens_spent"
			self.charged.append((key, by))
			self.pending_tokens += n

		return SimpleNamespace(run=run)

	def set_value(self, doctype, name, field, value=None, *args, **kwargs):
		assert doctype == "Optimus Finding" and field == "llm_fix_json"
		self.writes += 1
		mode = self.fail_writes.get(self.writes)
		if mode == "whole":
			self._end()
			raise _Deadlock("(1213) Deadlock found when trying to get lock")
		if mode == "other":
			raise RuntimeError("(1406) Data too long")
		self.pending[name] = value

	def commit(self):
		self.commits += 1
		if self.deadlock_commits and self.open_writes:
			self._end()
			raise _Deadlock("(1213) Deadlock found when trying to get lock; the transaction was rolled back")
		self.view_open = self.changed_since_view = False
		if self.fail_commit_of is not None and self.fail_commit_of in self.pending:
			self.fail_commit_of = None
			raise RuntimeError("(2013) Lost connection during COMMIT")
		self.tokens += self.pending_tokens
		self.answers.update(self.pending)
		self.pending_tokens, self.pending = 0, {}
		if self.commits in self.fail_after_commit:
			raise ConnectionError("Timeout reading from socket (an after_commit callback)")

	def get_value(self, doctype, name, field, *args, **kwargs):
		"""A fresh read of what is committed."""
		assert (doctype, field) == ("Optimus Finding", "llm_fix_json") and not kwargs.get("cache")
		self.fresh_reads += 1
		return self.answers.get(name)

	@property
	def open_writes(self):
		return self.pending_tokens or self.pending


class _Reply:
	def __init__(self, content, total=_TOKENS):
		self.status_code = 200
		self.headers = {}
		self.payload = {
			"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
			"usage": {"prompt_tokens": total - 1, "completion_tokens": 1, "total_tokens": total},
		}
		self.text = json.dumps(self.payload)

	def json(self):
		return self.payload


def _safe_commit(db):
	"""``optimus.safe_commit`` on the fake: a failed COMMIT is rolled back and raised again."""

	def commit():
		try:
			db.commit()
		except Exception:
			db.rollback()
			raise

	return commit


@pytest.fixture
def refresh(monkeypatch):
	"""``install(db, replies)`` wires the fake database and provider; ``run()`` refreshes PS-1."""
	import frappe

	state = SimpleNamespace(rows=[], logs=[], posts=[], open_at_post=[])

	def install(db, replies, n=None, other_writer_commits=False):
		monkeypatch.setattr(analyze, "frappe", SimpleNamespace(
			db=db, QueryDeadlockError=_Deadlock, local=frappe.local, log_error=lambda *a, **k: None,
		))
		monkeypatch.setattr(analyze, "_session_increment_query", db.increment)
		monkeypatch.setattr(analyze, "safe_commit", _safe_commit(db))
		pending = iter(replies)

		def post(url, **kw):
			# no transaction of this refresh holds a write (and so a row lock) across a provider call
			state.open_at_post.append(bool(db.open_writes))
			state.posts.append(kw["json"].get("metadata"))
			db.read_during_the_call(other_writer_commits=other_writer_commits)
			reply = next(pending)
			if isinstance(reply, BaseException):
				raise reply
			return reply

		monkeypatch.setattr(ai_fix.requests, "post", post)
		state.rows = [
			SimpleNamespace(
				name=f"F{i}", finding_type="N+1 Query", severity="High", title=f"F{i}", customer_description="d",
				estimated_impact_ms=100.0 - i, affected_count=1, action_ref="0", technical_detail_json="{}",
				llm_fix_json=None,
			)
			for i in range(len(replies) if n is None else n)
		]
		state.db = db

	def log(title, exc=None, **context):
		state.logs.append((title, type(exc).__name__ if exc is not None else None, context, sys.exc_info()[0]))
		ai_fix._mark_logged(exc, f"ERR-{len(state.logs)}")
		return True

	monkeypatch.setattr(frappe.local, "_optimus_spend_session", None, raising=False)
	monkeypatch.setattr(ai_fix, "log_ai_failure", log)
	monkeypatch.setattr(ai_fix, "_log_http_error", lambda *a, **k: None)
	monkeypatch.setattr(ai_fix, "_provider_config", lambda: dict(_PROVIDER))
	monkeypatch.setattr(ai_fix, "_resolve_display_threshold_ms", lambda: 1000)
	monkeypatch.setattr(ai_fix, "_get_api_key", lambda *a: "")
	monkeypatch.setattr(ai_fix, "_current_key_or_empty", lambda: "")
	monkeypatch.setattr(ai_fix, "_reask_enabled", lambda: False)
	monkeypatch.setattr(ai_fix, "is_available", lambda section=None: True)
	monkeypatch.setattr(ai_fix, "is_finding_type_excluded", lambda *a, **k: False)
	monkeypatch.setattr(ai_fix, "llm_gate_note", lambda finding: None)
	monkeypatch.setattr(analyze, "_phase2_index_for", lambda doc: {})
	monkeypatch.setattr(analyze, "_ai_evidence_scope", lambda: (None, None))
	monkeypatch.setattr(analyze, "_ai_payload_for_finding", lambda row, *a, **k: {"finding_type": row.finding_type})

	def run(name="PS-1"):
		doc = SimpleNamespace(name=name, session_uuid="uuid-1", findings=state.rows)
		return analyze._run_ai_backfill(doc, cap=0, regenerate_all=True, time_budget=1e9)

	state.install, state.run = install, run
	return state


def _answers(n):
	return [_Reply(_ANSWER) for _ in range(n)]


def test_a_whole_transaction_rollback_on_a_charge_never_drops_an_earlier_answer(refresh):
	"""The cycle-2 probe (snapshot_1020.py): six answers, the second charge hits 1020."""
	db = _TxnDb(fail_updates={2: "whole"})
	refresh.install(db, _answers(6))
	out = refresh.run()
	assert sorted(db.answers) == [f"F{i}" for i in range(6)]
	assert db.tokens == 6 * _TOKENS
	assert out["added"] == len(db.answers) == 6 and out["failed"] == 0
	assert not db.open_writes


def test_added_counts_only_committed_answers(refresh):
	db = _TxnDb(fail_commit_of="F1")
	refresh.install(db, _answers(3))
	out = refresh.run()
	assert sorted(db.answers) == ["F0", "F2"]
	assert out["added"] == 2 and out["failed"] == 1
	# the answer that did not commit is logged once, after the try, with what it cost
	[(title, error, context, active)] = [log for log in refresh.logs if log[2].get("finding") == "F1"]
	assert title == "optimus ai backfill" and error == "RuntimeError" and active is None
	assert context["attempts"] == 1 and context["tokens"] == _TOKENS
	assert [row.llm_fix_json is not None for row in refresh.rows] == [True, False, True]


def test_each_answer_commits_with_its_own_spend_before_the_next_provider_call(refresh):
	db = _TxnDb()
	refresh.install(db, _answers(4))
	out = refresh.run()
	assert out["added"] == 4 and db.tokens == 4 * _TOKENS
	assert refresh.open_at_post == [False] * 4
	assert db.commits >= 4


def test_the_short_write_is_tried_again_after_a_whole_transaction_rollback(refresh):
	db = _TxnDb(fail_writes={1: "whole"})
	refresh.install(db, _answers(2))
	out = refresh.run()
	# the rolled-back charge is made again with the answer, so it is counted exactly once
	assert out["added"] == 2 and db.tokens == 2 * _TOKENS and sorted(db.answers) == ["F0", "F1"]
	assert db.updates == 3
	assert refresh.logs == []


def test_a_retried_charge_appends_its_outcome_to_the_counter_row(refresh):
	db = _TxnDb(fail_updates={1: "whole"})
	refresh.install(db, _answers(1))
	out = refresh.run()
	assert out["added"] == 1 and db.tokens == _TOKENS
	# the counter logged its failed savepoint rollback; the retry's outcome follows in that row
	assert [(log[0], log[2].get("savepoint_rollback"), log[2].get("outcome")) for log in refresh.logs] == [
		("optimus ai spend", "failed: RuntimeError", None),  # the 1305 of the gone savepoint
		("optimus ai backfill", None, "committed on attempt 2"),
	]


def test_exhausted_retries_fail_only_that_finding_and_log_it_once(refresh):
	attempts = analyze.AI_WRITE_ATTEMPTS
	db = _TxnDb(fail_updates={n: "whole" for n in range(2, 2 + attempts)})
	refresh.install(db, _answers(3))
	out = refresh.run()
	assert sorted(db.answers) == ["F0", "F2"] and db.tokens == 2 * _TOKENS
	assert out["added"] == 2 and out["failed"] == 1
	rows = [log for log in refresh.logs if log[0] == "optimus ai backfill"]
	assert len(rows) == 1
	assert rows[0][2]["attempts"] == attempts and rows[0][2]["finding"] == "F1" and rows[0][3] is None


def test_a_statement_level_counter_failure_keeps_the_answer(refresh):
	"""D2: a lock wait timeout on the counter rolls back only the counter, logs it and keeps the
	billed answer (its tokens are in that row)."""
	db = _TxnDb(fail_updates={1: "statement"})
	refresh.install(db, _answers(2))
	out = refresh.run()
	assert out["added"] == 2 and sorted(db.answers) == ["F0", "F1"]
	assert db.tokens == _TOKENS
	assert [(log[0], log[2]["amount"], log[2]["savepoint_rollback"]) for log in refresh.logs] == [
		("optimus ai spend", _TOKENS, "done"),
	]


def test_a_billed_failure_records_its_spend_and_leaves_nothing_open(refresh):
	"""V3: a reply that was billed and then unusable is charged and committed at once, so the
	session row is not locked into the next step (the Steps call)."""
	db = _TxnDb()
	refresh.install(db, [_Reply("<think>unfinished"), _Reply(_ANSWER)])
	out = refresh.run()
	assert (out["added"], out["failed"]) == (1, 1)
	assert db.tokens == 2 * _TOKENS and sorted(db.answers) == ["F1"]
	assert refresh.open_at_post == [False, False] and not db.open_writes


def test_a_failure_that_billed_nothing_writes_nothing(refresh):
	import requests

	db = _TxnDb()
	refresh.install(db, [requests.exceptions.ConnectionError("down")])
	out = refresh.run()
	assert (out["added"], out["failed"]) == (0, 1)
	assert db.updates == db.writes == 0 and db.tokens == 0


def test_the_refresh_attributes_its_calls_to_the_session_explicitly(refresh, monkeypatch):
	"""The refresh records its own spend, so ai_fix must not charge the ambient session too."""
	import frappe

	monkeypatch.setattr(frappe.local, "_optimus_spend_session", "uuid-OTHER", raising=False)
	ambient = []
	monkeypatch.setattr(ai_fix, "_record_session_spend", lambda tokens: ambient.append(tokens))
	db = _TxnDb()
	refresh.install(db, _answers(2))
	refresh.run()
	assert ambient == [] and db.tokens == 2 * _TOKENS
	assert db.charged == [("PS-1", "name")] * 2
	assert refresh.posts == [None, None]  # a provider without session metadata gets none


def test_nothing_of_the_refresh_is_left_uncommitted(refresh):
	db = _TxnDb()
	refresh.install(db, _answers(3))
	refresh.run()
	assert not db.open_writes
	assert all(json.loads(blob)["prompt_version"] == ai_prompts.PROMPT_VERSION for blob in db.answers.values())


def test_an_rq_timeout_in_the_short_write_stops_the_refresh_fresh(refresh):
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = Timeout("fake timeout")
	db = _TxnDb()

	def write(*a, **k):
		raise original

	refresh.install(db, _answers(2))
	db.set_value = write
	with pytest.raises(Timeout) as caught:
		refresh.run()
	assert caught.value is not original and caught.value.__context__ is None


def test_the_short_write_starts_after_the_read_view_of_the_provider_call(refresh):
	"""Another writer commits the session row while the provider answers (a Phase 2 run, a Desk
	save). The write commits the read-only transaction first, so its UPDATE runs without the old
	read view and MariaDB has no 1020 to raise: no retry, no Error Log row."""
	db = _TxnDb()
	refresh.install(db, _answers(3), other_writer_commits=True)
	out = refresh.run()
	assert out["added"] == 3 and db.tokens == 3 * _TOKENS
	assert db.updates == 3 and refresh.logs == []


def test_a_failed_answer_write_rolls_its_spend_back_and_logs_the_tokens(refresh):
	"""The answer and its spend commit together or not at all: a Finding write that fails for
	another reason (not retried) takes the charge with it, and the row says what was lost."""
	db = _TxnDb(fail_writes={1: "other"})
	refresh.install(db, _answers(2))
	out = refresh.run()
	assert sorted(db.answers) == ["F1"] and db.tokens == _TOKENS
	assert (out["added"], out["failed"]) == (1, 1)
	[(title, error, context, active)] = refresh.logs
	assert (title, error, context["tokens"], context["attempts"], active) == (
		"optimus ai backfill", "RuntimeError", _TOKENS, 1, None,
	)


def test_an_unattributed_refresh_commits_its_ambient_charges_before_it_returns(refresh, monkeypatch):
	"""A session without a docname keeps the ambient charge ai_fix makes per reply; a billed
	failure there writes nothing, so the refresh's final commit is what releases the row (V3)."""
	db = _TxnDb()
	refresh.install(db, [_Reply("<think>unfinished")])
	out = refresh.run(name="")
	assert (out["added"], out["failed"]) == (0, 1)
	assert db.charged == [("uuid-1", "session_uuid")]
	assert db.tokens == _TOKENS and not db.open_writes


def test_every_failed_counter_row_learns_that_a_retry_committed(refresh):
	"""Two whole-transaction failures at the counter (two rows saying savepoint_rollback=failed),
	then a commit on attempt 3: each of those rows gets the outcome, not only the last."""
	db = _TxnDb(fail_updates={1: "whole", 2: "whole"})
	refresh.install(db, _answers(1))
	out = refresh.run()
	assert out["added"] == 1 and db.tokens == _TOKENS
	spend_rows = [log for log in refresh.logs if log[0] == "optimus ai spend"]
	outcomes = [log[2].get("outcome") for log in refresh.logs if log[0] == "optimus ai backfill"]
	assert len(spend_rows) == 2 and outcomes == ["committed on attempt 3"] * 2


def _final_commit_of(n_findings_before):
	"""The number of the COMMIT that makes a finding's write durable: each finding's write commits
	twice (the provider call's read view first), so finding k's write ends with the (2k + 2)-th."""
	return 2 * n_findings_before + 2


def test_an_error_after_the_commit_still_counts_the_committed_answer(refresh):
	"""An after_commit callback that raises (a redis timeout) makes ``safe_commit`` raise although
	the answer and its spend are committed. A fresh read of the Finding finds the answer, so it
	counts as added, and the row says it was saved, not that its tokens were lost."""
	db = _TxnDb(fail_after_commit={_final_commit_of(1)})
	refresh.install(db, _answers(3))
	out = refresh.run()
	assert (out["added"], out["failed"]) == (3, 0)
	assert sorted(db.answers) == ["F0", "F1", "F2"] and db.tokens == 3 * _TOKENS
	assert refresh.rows[1].llm_fix_json == db.answers["F1"] and db.fresh_reads == 1
	[(title, error, context, active)] = refresh.logs
	assert (title, error, context["finding"], active) == ("optimus ai backfill", "ConnectionError", "F1", None)
	assert context["outcome"].startswith("committed") and "tokens" not in context and "attempts" not in context


def test_an_error_before_the_commit_is_still_a_failure_without_a_read(refresh):
	db = _TxnDb(fail_writes={1: "other"})
	refresh.install(db, _answers(1))
	out = refresh.run()
	assert (out["added"], out["failed"]) == (0, 1) and db.fresh_reads == 0


def test_an_error_after_the_commit_of_a_billed_failure_says_the_tokens_may_be_recorded(refresh):
	"""A billed failure writes only its spend, so there is no answer to read back: its row says the
	error came after the COMMIT and the tokens may well be recorded."""
	db = _TxnDb(fail_after_commit={_final_commit_of(0)})
	refresh.install(db, [_Reply("<think>unfinished")])
	out = refresh.run()
	assert (out["added"], out["failed"]) == (0, 1) and db.tokens == _TOKENS
	[context] = [log[2] for log in refresh.logs if "outcome" in log[2]]
	assert "may" in context["outcome"] and context["tokens"] == _TOKENS


def test_an_error_after_a_retrys_commit_also_clears_the_earlier_counter_row(refresh):
	db = _TxnDb(fail_updates={1: "whole"}, fail_after_commit={2})
	refresh.install(db, _answers(1))
	out = refresh.run()
	assert out["added"] == 1 and db.tokens == _TOKENS
	outcomes = sorted(log[2]["outcome"].split(":")[0] for log in refresh.logs if log[0] == "optimus ai backfill")
	assert outcomes == ["committed", "committed on attempt 2"]


def test_a_deadlock_at_the_commit_is_not_taken_for_an_error_after_it(refresh):
	"""A COMMIT that the database answers with a deadlock rolled everything back: nothing to read
	back, and the row says the tokens were not recorded (no "may be recorded" outcome)."""
	db = _TxnDb(deadlock_commits=True)
	refresh.install(db, [_Reply("<think>unfinished")])
	out = refresh.run()
	assert (out["added"], out["failed"]) == (0, 1) and db.tokens == 0 and db.fresh_reads == 0
	[context] = [log[2] for log in refresh.logs if "attempts" in log[2]]
	assert context["attempts"] == analyze.AI_WRITE_ATTEMPTS and "outcome" not in context
