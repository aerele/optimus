"""Durable refresh transitions: duplicates, stale owners, lock order and accounting.

The session counters are #72's log-and-continue counters (D2): a statement-level failure is
absorbed by the counter itself, so only a failure that ended the whole transaction reaches
the journal, and the fakes below raise to stand for that one."""

from contextlib import contextmanager
from copy import deepcopy
from importlib import import_module
from types import SimpleNamespace

import pytest


class DriverError(Exception):
	"""PyMySQL's error shape: ``args == (code, message)``."""


class DuplicateEntryError(NameError):
	"""``Document.insert`` raises ``frappe.DuplicateEntryError(doctype, name, e)`` for a
	duplicate primary key and ``frappe.UniqueValidationError`` for a unique column."""


UNIQUE = {
	"Optimus AI Refresh Run": ("run_id", "active_session"),
	"Optimus AI Refresh Attempt": ("attempt_id",),
}


class Database:
	"""Transactional rows; lock calls are observed, never treated as no-ops."""

	def __init__(self):
		self.rows = {
			"Optimus Session": {
				"fake-session-doc": {
					"name": "fake-session-doc",
					"session_uuid": "fake-session",
					"status": "Ready",
					"ai_tokens_spent": 0,
					"ai_refresh_count": 0,
				}
			}
		}
		self.locks = []
		self.calls = []  # every read: (table, name or filters, locking)
		self.duplicates = 0
		self.active = False

	@contextmanager
	def transaction(self):
		assert not self.active
		self.active = True
		before = deepcopy(self.rows)
		try:
			yield
		except BaseException:
			self.rows = before
			raise
		finally:
			self.active = False

	def get_value(self, table, name, fields="name", *, as_dict=False, for_update=False, **kw):
		self.calls.append((table, name, bool(for_update)))
		if for_update:
			assert self.active, "a row lock needs a live transaction"
			self.locks.append((table, name))
		rows = self.rows.get(table, {})
		row = (
			next((r for r in rows.values() if all(r.get(k) == v for k, v in name.items())), None)
			if isinstance(name, dict)
			else rows.get(name)
		)
		if not row:
			return None
		if fields == "*":
			return deepcopy(row)
		if isinstance(fields, str):
			return row.get(fields)
		return {k: row.get(k) for k in fields} if as_dict else tuple(row.get(k) for k in fields)

	def set_value(self, table, name, values, *, update_modified=False):
		assert self.active
		self.rows[table][name].update(values)

	def insert(self, values):
		assert self.active
		table, name = values["doctype"], values["name"]
		rows = self.rows.setdefault(table, {})
		duplicate = name in rows or any(
			values.get(field) is not None and any(r.get(field) == values[field] for r in rows.values())
			for field in UNIQUE.get(table, ())
		)
		if duplicate:
			self.duplicates += 1
			raise DuplicateEntryError(table, name, DriverError(1062, "fake duplicate entry"))
		rows[name] = deepcopy(values)

	def increment(self, name, field, value):
		assert self.active
		r = self.rows["Optimus Session"][name]
		r[field] = (r.get(field) or 0) + value

	def get_all(self, table, *, filters, fields, limit_page_length=0, **kw):
		def matches(row):
			for key, value in filters.items():
				if isinstance(value, (list, tuple)):
					actual = row.get(key)
					if value[0] == "in" and actual not in value[1]:
						return False
					if value[0] == "<=" and (actual is None or actual > value[1]):
						return False
					if value[0] not in {"in", "<="}:
						raise AssertionError("unsupported fake query operator")
				elif row.get(key) != value:
					return False
			return True

		rows = [r for r in self.rows.get(table, {}).values() if matches(r)]
		if limit_page_length:
			rows = rows[:limit_page_length]
		return [{key: row.get(key) for key in fields} for row in rows]

	def get_values(self, table, filters, fields, *, for_update=False, limit=0, **kw):
		self.calls.append((table, filters, bool(for_update)))
		if for_update:
			assert self.active
		return self.get_all(table, filters=filters, fields=fields, limit_page_length=limit)


@pytest.fixture(name="journal")
def journal(monkeypatch):
	mod = import_module("optimus.ai_refresh_store")
	db = Database()

	def doc(values):
		return SimpleNamespace(insert=lambda **kw: db.insert(values))

	monkeypatch.setattr(mod, "frappe", SimpleNamespace(db=db, get_doc=doc, get_all=db.get_all))
	monkeypatch.setattr(
		"optimus.analyze._add_ai_spend", lambda name, tokens: db.increment(name, "ai_tokens_spent", tokens)
	)
	monkeypatch.setattr(
		"optimus.analyze._bump_ai_refresh_count", lambda name: db.increment(name, "ai_refresh_count", 1)
	)
	run = {
		"doctype": mod.RUN,
		"name": "fake-run",
		"run_id": "fake-run",
		"session_name": "fake-session-doc",
		"session_uuid": "fake-session",
		"scope": "all",
		"state": "queued",
		"active_session": "fake-session-doc",
		"slice_no": 0,
		"worker_token": None,
		"lease_until": 0,
		"deadline": 3600,
		"attempted": 0,
		"completed": 0,
		"failed": 0,
		"uncertain": 0,
		"skipped": 0,
		"tokens_reported": 0,
		"usage_incomplete": 0,
		"completion_counted": 0,
	}
	db.rows[mod.RUN] = {run["name"]: run}
	db.rows[mod.CONTROL] = {"site": {"name": "site", "scope": "site"}}
	return SimpleNamespace(mod=mod, db=db, run=run)


def claim(journal, worker="worker-a", now=10):
	with journal.db.transaction():
		return journal.mod.claim(
			"fake-run",
			docname="fake-session-doc",
			slice_no=0,
			worker_token=worker,
			now=now,
			lease_seconds=300,
		)


def begin(journal, worker="worker-a", name="fake-finding"):
	with journal.db.transaction():
		return journal.mod.begin_attempt(
			"fake-run",
			docname="fake-session-doc",
			worker_token=worker,
			kind="fix",
			target=name,
			input_hash="fake-hash",
			now=11,
		)


def test_duplicate_delivery_cannot_claim_even_with_the_same_worker_token(journal):
	assert claim(journal)
	assert claim(journal) is None
	assert claim(journal, "worker-b") is None
	assert journal.run["worker_token"] == "worker-a"


def test_claim_locks_parent_before_run_and_rejects_stale_slice(journal):
	with journal.db.transaction():
		assert (
			journal.mod.claim(
				"fake-run",
				docname="fake-session-doc",
				slice_no=1,
				worker_token="worker-a",
				now=10,
				lease_seconds=300,
			)
			is None
		)
	assert journal.db.locks == [("Optimus Session", "fake-session-doc"), (journal.mod.RUN, "fake-run")]


def test_pre_send_attempt_is_durable_and_unique(journal):
	claim(journal)
	attempt = begin(journal)
	assert attempt["state"] == "calling" and attempt["worker_token"] == "worker-a"
	assert begin(journal) is None
	assert journal.run["attempted"] == 1


def test_stale_owner_cannot_start_a_call(journal):
	claim(journal)
	assert begin(journal, "worker-b") is None
	assert journal.run["attempted"] == 0


def test_duplicate_outcome_counts_usage_and_completion_once(journal):
	claim(journal)
	attempt = begin(journal)
	for _ in range(2):
		with journal.db.transaction():
			journal.mod.settle_attempt(
				"fake-run",
				attempt["name"],
				docname="fake-session-doc",
				worker_token="worker-a",
				outcome="succeeded",
				tokens=17,
				usage_complete=True,
				now=12,
			)
	assert journal.run["tokens_reported"] == 17 and journal.run["completed"] == 1
	assert journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_tokens_spent"] == 17


def test_a_counter_failure_that_ends_the_transaction_rolls_the_result_back(journal, monkeypatch):
	claim(journal)
	attempt = begin(journal)
	writes = journal.db.rows.setdefault("fake-result", {})

	def persist():
		writes["answer"] = "fake-answer"

	def failed(*a):
		raise RuntimeError("fake counter failure")

	monkeypatch.setattr("optimus.analyze._add_ai_spend", failed)
	with pytest.raises(RuntimeError), journal.db.transaction():
		journal.mod.settle_attempt(
			"fake-run",
			attempt["name"],
			docname="fake-session-doc",
			worker_token="worker-a",
			outcome="succeeded",
			tokens=17,
			usage_complete=True,
			now=12,
			persist=persist,
		)
	assert journal.db.rows["fake-result"] == {}
	assert journal.db.rows[journal.mod.ATTEMPT][attempt["name"]]["state"] == "calling"
	assert journal.db.rows[journal.mod.RUN]["fake-run"]["completed"] == 0


def test_unknown_interrupted_call_is_not_requeued(journal):
	claim(journal)
	attempt = begin(journal)
	with journal.db.transaction():
		journal.mod.interrupt("fake-run", docname="fake-session-doc", now=400, reason="worker_lost")
	assert journal.run["state"] == "interrupted" and journal.run["active_session"] is None
	assert journal.db.rows[journal.mod.ATTEMPT][attempt["name"]]["state"] == "uncertain"
	assert journal.run["uncertain"] == 1 and journal.run["usage_incomplete"] == 1
	assert claim(journal, now=401) is None


def test_late_known_outcome_can_account_but_cannot_write_stale_answer(journal):
	claim(journal)
	attempt = begin(journal)
	with journal.db.transaction():
		journal.mod.interrupt("fake-run", docname="fake-session-doc", now=400, reason="worker_lost")
	written = []
	with journal.db.transaction():
		journal.mod.settle_attempt(
			"fake-run",
			attempt["name"],
			docname="fake-session-doc",
			worker_token="worker-a",
			outcome="succeeded",
			tokens=17,
			usage_complete=True,
			now=401,
			persist=lambda: written.append(True),
		)
	assert written == []
	assert journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_tokens_spent"] == 17
	assert journal.db.rows[journal.mod.ATTEMPT][attempt["name"]]["state"] == "discarded"


@pytest.mark.parametrize("expiry", ["lease_until", "deadline"])
def test_expiry_fences_answer_even_before_recovery_observes_the_crash(journal, expiry):
	claim(journal)
	attempt = begin(journal)
	journal.run[expiry] = 12
	written = []
	with journal.db.transaction():
		journal.mod.settle_attempt(
			"fake-run",
			attempt["name"],
			docname="fake-session-doc",
			worker_token="worker-a",
			outcome="succeeded",
			tokens=7,
			usage_complete=True,
			now=13,
			persist=lambda: written.append(True),
		)
	assert written == [] and journal.run["completed"] == 0
	assert journal.run["tokens_reported"] == 7
	with journal.db.transaction():
		assert (
			journal.mod.begin_attempt(
				"fake-run",
				docname="fake-session-doc",
				worker_token="worker-a",
				kind="fix",
				target="another-finding",
				input_hash="fake",
				now=13,
			)
			is None
		)


def test_a_completion_count_failure_that_ends_the_transaction_keeps_the_run_and_counts_once(
	journal, monkeypatch
):
	claim(journal)
	journal.run["completed"] = 1

	def failed(*a):
		raise RuntimeError("fake completion accounting failure")

	with monkeypatch.context() as patch:
		patch.setattr("optimus.analyze._bump_ai_refresh_count", failed)
		with pytest.raises(RuntimeError), journal.db.transaction():
			journal.mod.finish(
				"fake-run", docname="fake-session-doc", worker_token="worker-a", now=12, reason="complete"
			)
	assert journal.db.rows[journal.mod.RUN]["fake-run"]["completion_counted"] == 0
	for _ in range(2):
		with journal.db.transaction():
			journal.mod.finish(
				"fake-run", docname="fake-session-doc", worker_token="worker-a", now=12, reason="complete"
			)
	assert journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_refresh_count"] == 1


def test_invalid_queued_reservation_cannot_be_claimed(journal):
	journal.run["active_session"] = None
	assert claim(journal) is None


def test_missing_worker_identity_never_owns_a_corrupt_run(journal):
	journal.run.update(state="running", worker_token=None, lease_until=100)
	assert begin(journal, worker=None) is None


@pytest.mark.parametrize("tokens,complete", [(True, True), (-1, True), ("4", True), (1, "false")])
def test_invalid_usage_cannot_be_recorded_as_known(journal, tokens, complete):
	claim(journal)
	attempt = begin(journal)
	with pytest.raises(ValueError), journal.db.transaction():
		journal.mod.settle_attempt(
			"fake-run",
			attempt["name"],
			docname="fake-session-doc",
			worker_token="worker-a",
			outcome="succeeded",
			tokens=tokens,
			usage_complete=complete,
			now=12,
		)
	assert journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_tokens_spent"] == 0


def test_parent_leaving_ready_discards_result_but_retains_reported_usage(journal):
	claim(journal)
	attempt = begin(journal)
	journal.db.rows["Optimus Session"]["fake-session-doc"]["status"] = "Analyzing"
	written = []
	with journal.db.transaction():
		journal.mod.settle_attempt(
			"fake-run",
			attempt["name"],
			docname="fake-session-doc",
			worker_token="worker-a",
			outcome="succeeded",
			tokens=11,
			usage_complete=True,
			now=12,
			persist=lambda: written.append(True),
		)
	assert written == []
	assert journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_tokens_spent"] == 11


def test_continuation_fences_old_slice_and_preserves_budget(journal):
	claim(journal)
	with journal.db.transaction():
		assert journal.mod.continue_run(
			"fake-run", docname="fake-session-doc", worker_token="worker-a", now=12
		)
	assert journal.run["slice_no"] == 1 and journal.run["state"] == "queued"
	assert journal.run["deadline"] == 3600 and journal.run["dispatch_pending"] == 1
	assert claim(journal) is None
	with journal.db.transaction():
		assert journal.mod.claim(
			"fake-run",
			docname="fake-session-doc",
			slice_no=1,
			worker_token="worker-b",
			now=13,
			lease_seconds=300,
		)
	assert begin(journal) is None


def test_continuation_never_repeats_an_unsettled_call(journal):
	claim(journal)
	begin(journal)
	with pytest.raises(RuntimeError), journal.db.transaction():
		journal.mod.continue_run("fake-run", docname="fake-session-doc", worker_token="worker-a", now=12)


def test_cancellation_releases_admission_without_repeating_or_losing_usage(journal):
	claim(journal)
	attempt = begin(journal)
	with journal.db.transaction():
		journal.mod.cancel("fake-run", docname="fake-session-doc", now=12)
	assert journal.run["state"] == "cancelled" and journal.run["active_session"] is None
	assert journal.db.rows[journal.mod.ATTEMPT][attempt["name"]]["state"] == "uncertain"
	assert claim(journal) is None


def admit(journal, **kwargs):
	defaults = {
		"run_id": "new-run",
		"docname": "fake-session-doc",
		"session_uuid": "fake-session",
		"requested_by": "fake-owner",
		"scope": "all",
		"now": 10,
		"max_seconds": 3600,
		"site_cap": 2,
		"user_cap": 2,
		"cap": 20,
		"include_fixes": True,
		"include_steps": True,
		"regenerate_all": False,
	}
	with journal.db.transaction():
		return journal.mod.admit(**{**defaults, **kwargs})


def test_admission_locks_the_session_before_the_site_mutex(journal):
	"""Q4: the session row first, so an admission waiting on its own session never holds the
	site mutex and stalls every other session's admission."""
	journal.db.rows[journal.mod.RUN] = {}
	out = admit(journal)
	assert out["status"] == "queued"
	assert journal.db.locks[:2] == [("Optimus Session", "fake-session-doc"), (journal.mod.CONTROL, "site")]
	run = journal.db.rows[journal.mod.RUN]["new-run"]
	assert run["dispatch_pending"] == 1 and run["deadline"] == 3610
	assert run["active_session"] == "fake-session-doc" and run["requested_by"] == "fake-owner"


def test_admission_changes_the_mutex_row_so_postgres_rejects_an_older_snapshot(journal):
	journal.db.rows[journal.mod.RUN] = {}
	assert admit(journal)["status"] == "queued"
	assert journal.db.rows[journal.mod.CONTROL]["site"]["generation"] == 1
	with journal.db.transaction():
		journal.mod.cancel("new-run", docname="fake-session-doc", now=12)
	assert admit(journal, run_id="second-run")["status"] == "queued"
	assert journal.db.rows[journal.mod.CONTROL]["site"]["generation"] == 2


def test_admission_attaches_to_an_existing_run_without_creating_another(journal):
	journal.run["lease_until"] = 1800  # a live queued run (an expired one is taken over)
	out = admit(journal)
	assert out["status"] == "already_running" and out["run"]["name"] == "fake-run"
	assert len(journal.db.rows[journal.mod.RUN]) == 1


@pytest.mark.parametrize(
	"count,site_cap,user_cap,owner,reason",
	[
		(2, 2, 2, "other-owner", "site_cap"),
		(2, 4, 2, "fake-owner", "user_cap"),
	],
)
def test_admission_enforces_site_and_requester_limits(journal, count, site_cap, user_cap, owner, reason):
	journal.db.rows[journal.mod.RUN] = {
		f"other-{i}": {
			**journal.run,
			"name": f"other-{i}",
			"active_session": f"other-session-{i}",
			"requested_by": owner,
		}
		for i in range(count)
	}
	out = admit(journal, site_cap=site_cap, user_cap=user_cap)
	assert out == {"status": "refused", "reason": reason}
	assert len(journal.db.rows[journal.mod.RUN]) == count


def test_analyze_time_runs_use_only_the_session_reservation(journal):
	journal.db.rows[journal.mod.RUN] = {
		f"other-{i}": {
			**journal.run,
			"name": f"other-{i}",
			"active_session": f"other-session-{i}",
			"requested_by": "fake-owner",
		}
		for i in range(2)
	}
	assert admit(journal, scope="fixes_missing")["status"] == "queued"


def test_absent_admission_mutex_fails_closed(journal):
	journal.db.rows[journal.mod.CONTROL] = {}
	with pytest.raises(journal.mod.JournalUnavailable, match="initialize_ai_refresh_store.execute"):
		admit(journal)
	assert len(journal.db.rows[journal.mod.RUN]) == 1


def test_admission_reads_runs_only_after_its_locks_and_never_locks_them_by_filter(journal):
	"""The capacity count and the reservation probe are plain reads taken after the session and
	site mutex locks: the read view opens after every admission that could change the count
	has committed (admissions serialize on the mutex, and Postgres's older snapshot fails on
	the mutex row the winner changed). A FOR UPDATE by filter took next-key locks on the
	state and active_session indexes and deadlocked another session's finishing worker."""
	journal.db.rows[journal.mod.RUN] = {
		f"other-{i}": {
			**journal.run,
			"name": f"other-{i}",
			"active_session": f"other-session-{i}",
			"requested_by": "other-owner",
		}
		for i in range(2)
	}
	journal.db.calls.clear()
	assert admit(journal, site_cap=2) == {"status": "refused", "reason": "site_cap"}
	run_reads = [c for c in journal.db.calls if c[0] == journal.mod.RUN]
	assert run_reads and not [c for c in run_reads if c[2]]
	control = journal.db.calls.index((journal.mod.CONTROL, "site", True))
	assert control < journal.db.calls.index(run_reads[0])


def test_analyze_time_admission_never_touches_the_site_mutex(journal):
	journal.db.rows[journal.mod.RUN] = {}
	journal.db.calls.clear()
	assert admit(journal, scope="fixes_missing")["status"] == "queued"
	assert not [c for c in journal.db.calls if c[0] == journal.mod.CONTROL]
	assert journal.db.rows[journal.mod.CONTROL]["site"].get("generation") is None


class LockUnavailable(Exception):
	"""What Frappe raises for a NOWAIT conflict: ``QueryTimeoutError(e)`` around 1205."""


@pytest.mark.parametrize(
	"error",
	[
		LockUnavailable(DriverError(1205, "fake lock wait timeout")),
		DriverError(1205, "fake raw lock wait timeout"),
	],
	ids=["frappe-wrapped", "raw"],
)
def test_a_held_site_mutex_is_a_busy_refusal_not_an_error(journal, monkeypatch, error):
	journal.db.rows[journal.mod.RUN] = {}
	current = journal.db.get_value

	def nowait(table, name, *a, wait=True, **kw):
		if table == journal.mod.CONTROL:
			assert wait is False, "the site mutex is taken with NOWAIT"
			raise error
		return current(table, name, *a, **kw)

	monkeypatch.setattr(journal.db, "get_value", nowait)
	assert admit(journal) == {"status": "refused", "reason": "busy"}
	assert not journal.db.rows[journal.mod.RUN]


def test_a_site_mutex_failure_that_is_not_contention_is_raised(journal, monkeypatch):
	journal.db.rows[journal.mod.RUN] = {}
	current = journal.db.get_value

	def broken(table, name, *a, **kw):
		if table == journal.mod.CONTROL:
			raise RuntimeError("fake storage failure")
		return current(table, name, *a, **kw)

	monkeypatch.setattr(journal.db, "get_value", broken)
	with pytest.raises(RuntimeError):
		admit(journal)
	assert not journal.db.rows[journal.mod.RUN]


@pytest.mark.parametrize("scope", ["all", "fixes_missing"])
def test_the_unique_reservation_detects_a_run_the_snapshot_missed(journal, monkeypatch, scope):
	"""On Postgres a waiting admission's snapshot can predate the run another admission of
	the same session committed; the unique active_session key still refuses the second run."""
	journal.run.update(scope=scope)
	current = journal.db.get_value

	def snapshot(table, name, *a, for_update=False, **kw):
		if table == journal.mod.RUN and isinstance(name, dict) and not for_update:
			return None
		return current(table, name, *a, for_update=for_update, **kw)

	monkeypatch.setattr(journal.db, "get_value", snapshot)
	assert admit(journal, scope=scope) == {"status": "already_running", "run": None}
	assert list(journal.db.rows[journal.mod.RUN]) == ["fake-run"]
	assert journal.db.duplicates == 1
	assert journal.db.rows[journal.mod.CONTROL]["site"].get("generation") is None


def test_empty_refresh_does_not_count_as_an_updated_session(journal):
	claim(journal)
	with journal.db.transaction():
		journal.mod.finish(
			"fake-run", docname="fake-session-doc", worker_token="worker-a", now=12, reason="complete"
		)
	assert journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_refresh_count"] == 0
	assert journal.run["completion_counted"] == 1


def test_stale_input_rejection_keeps_usage_but_not_completed_count(journal):
	claim(journal)
	attempt = begin(journal)
	with journal.db.transaction():
		journal.mod.settle_attempt(
			"fake-run",
			attempt["name"],
			docname="fake-session-doc",
			worker_token="worker-a",
			outcome="succeeded",
			tokens=17,
			usage_complete=True,
			now=12,
			persist=lambda: False,
		)
	assert journal.db.rows[journal.mod.ATTEMPT][attempt["name"]]["state"] == "discarded"
	assert journal.run["completed"] == 0 and journal.run["skipped"] == 1
	assert journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_tokens_spent"] == 17


def test_corrupt_active_pointer_cannot_mutate_another_runs_attempt(journal):
	claim(journal)
	attempt = begin(journal)
	journal.db.rows[journal.mod.ATTEMPT][attempt["name"]]["run_id"] = "other-run"
	with journal.db.transaction():
		out = journal.mod.cancel("fake-run", docname="fake-session-doc", now=12)
	# E2: the run is force-released; the other run's attempt is never written.
	assert out["inconsistent"] == "attempt" and out["active_session"] is None
	assert journal.db.rows[journal.mod.ATTEMPT][attempt["name"]]["state"] == "calling"


def test_corrupt_session_reservation_cannot_be_claimed(journal):
	journal.run["active_session"] = "other-session"
	with pytest.raises(journal.mod.JournalUnavailable):
		claim(journal)


def test_dispatch_reservation_is_throttled_and_cannot_resurrect_finished_run(journal):
	journal.run.update(lease_until=1800, dispatch_pending=1, dispatched_at_epoch=0)
	with journal.db.transaction():
		assert journal.mod.prepare_dispatch("fake-run", docname="fake-session-doc", now=50)
	with journal.db.transaction():
		assert journal.mod.prepare_dispatch("fake-run", docname="fake-session-doc", now=51) is None
	assert journal.run["dispatch_pending"] == 1
	claim(journal, now=52)
	with journal.db.transaction():
		assert journal.mod.prepare_dispatch("fake-run", docname="fake-session-doc", now=100) is None


def test_explicit_worker_abandon_marks_unknown_without_waiting_for_lease(journal):
	claim(journal)
	attempt = begin(journal)
	with journal.db.transaction():
		assert (
			journal.mod.abandon("fake-run", docname="fake-session-doc", worker_token="worker-b", now=12)
			is None
		)
	with journal.db.transaction():
		assert journal.mod.abandon("fake-run", docname="fake-session-doc", worker_token="worker-a", now=12)
	assert journal.run["state"] == "interrupted"
	assert journal.db.rows[journal.mod.ATTEMPT][attempt["name"]]["state"] == "uncertain"


@pytest.mark.parametrize(
	"kind,stop", [("auth", True), ("quota", True), ("rate_limited", True), ("timeout", False)]
)
def test_breaker_is_committed_with_attempt_accounting(journal, kind, stop):
	claim(journal)
	attempt = begin(journal)
	with journal.db.transaction():
		journal.mod.settle_attempt(
			"fake-run",
			attempt["name"],
			docname="fake-session-doc",
			worker_token="worker-a",
			outcome="failed",
			tokens=0,
			usage_complete=False,
			now=12,
			error_kind=kind,
		)
	assert journal.run["consecutive_failures"] == 1
	assert bool(journal.run.get("breaker_kind")) is stop


def test_three_failures_stop_across_slice_boundaries(journal):
	claim(journal)
	for i in range(3):
		attempt = begin(journal, name=f"finding-{i}")
		with journal.db.transaction():
			journal.mod.settle_attempt(
				"fake-run",
				attempt["name"],
				docname="fake-session-doc",
				worker_token="worker-a",
				outcome="failed",
				tokens=0,
				usage_complete=False,
				now=12,
				error_kind="server",
			)
	assert journal.run["breaker_kind"] == "server"
	assert begin(journal, name="fourth-finding") is None


def test_success_resets_failure_streak_and_marks_render_owed(journal):
	claim(journal)
	journal.run["consecutive_failures"] = 2
	attempt = begin(journal)
	with journal.db.transaction():
		journal.mod.settle_attempt(
			"fake-run",
			attempt["name"],
			docname="fake-session-doc",
			worker_token="worker-a",
			outcome="succeeded",
			tokens=0,
			usage_complete=True,
			now=12,
		)
	assert journal.run["consecutive_failures"] == 0 and journal.run["render_pending"] == 1


def test_deadline_expiry_releases_a_queued_reservation_before_its_queue_lease(journal):
	journal.run.update(deadline=40, lease_until=1800)
	with journal.db.transaction():
		journal.mod.interrupt("fake-run", docname="fake-session-doc", now=50, reason="deadline")
	assert journal.run["state"] == "interrupted" and journal.run["active_session"] is None


def test_resume_cannot_reference_another_sessions_run(journal):
	journal.db.rows[journal.mod.RUN] = {
		"other-run": {
			**journal.run,
			"name": "other-run",
			"state": "interrupted",
			"active_session": None,
			"session_name": "other-session",
			"requested_at_epoch": 1,
		}
	}
	with pytest.raises(ValueError):
		admit(journal, resume_from="other-run")
	assert "new-run" not in journal.db.rows[journal.mod.RUN]


def test_resume_preserves_original_selection_time_and_has_a_bounded_retry_count(journal):
	journal.db.rows[journal.mod.RUN] = {
		"old-run": {
			**journal.run,
			"name": "old-run",
			"state": "interrupted",
			"active_session": None,
			"requested_at_epoch": 1,
			"retry_no": 2,
		}
	}
	out = admit(journal, resume_from="old-run")
	assert out["run"]["requested_at_epoch"] == 1 and out["run"]["retry_no"] == 3
	with journal.db.transaction():
		journal.mod.cancel("new-run", docname="fake-session-doc", now=12)
	assert admit(journal, run_id="newer-run", resume_from="new-run") == {
		"status": "refused",
		"reason": "retry_limit",
	}


@pytest.mark.parametrize("now", [None, float("nan"), float("inf"), True, "10", -1, 10**20])
def test_nonfinite_or_malformed_clock_cannot_make_an_immortal_reservation(journal, now):
	journal.db.rows[journal.mod.RUN] = {}
	with pytest.raises(ValueError):
		admit(journal, now=now)
	assert not journal.db.rows[journal.mod.RUN]


@pytest.mark.parametrize(
	"field,value",
	[
		("requested_at_epoch", None),
		("requested_at_epoch", float("nan")),
		("requested_at_epoch", 10**20),
		("retry_no", -1),
		("retry_no", "2"),
		("cap", -1),
		("include_fixes", "0"),
		("regenerate_all", 2),
	],
)
def test_corrupt_resume_state_cannot_change_the_budget_or_selection(journal, field, value):
	journal.run.update(state="interrupted", active_session=None, requested_at_epoch=1, retry_no=0)
	journal.run[field] = value
	with pytest.raises(journal.mod.JournalUnavailable):
		admit(journal, resume_from="fake-run")
	assert "new-run" not in journal.db.rows[journal.mod.RUN]


def test_resuming_an_older_run_again_cannot_reset_the_retry_chain(journal):
	journal.run.update(state="interrupted", active_session=None, requested_at_epoch=1, retry_no=0)
	assert admit(journal, resume_from="fake-run")["status"] == "queued"
	with journal.db.transaction():
		journal.mod.cancel("new-run", docname="fake-session-doc", now=12)
	assert admit(journal, run_id="another-run", resume_from="fake-run") == {
		"status": "refused",
		"reason": "already_resumed",
	}
	assert "another-run" not in journal.db.rows[journal.mod.RUN]


@pytest.mark.parametrize("steps_state", ["updated", "carried"])
def test_resume_carries_saved_steps_and_the_unfinished_report(journal, steps_state):
	journal.run.update(
		state="interrupted",
		active_session=None,
		requested_at_epoch=1,
		retry_no=0,
		steps_state=steps_state,
		render_pending=1,
		completed=1,
	)
	out = admit(journal, resume_from="fake-run")["run"]
	assert out["steps_state"] == "carried" and out["render_pending"] == 1
	assert out["include_steps"] == 1
	assert out["completed"] == 0 and out["tokens_reported"] == 0


def _settle_setup(journal):
	claim(journal)
	return begin(journal)["name"]


TRANSITIONS = {
	"claim": (
		lambda journal: None,
		lambda m, doc, _: m.claim(
			"fake-run", docname=doc, slice_no=0, worker_token="worker-b", now=10, lease_seconds=300
		),
	),
	"begin_attempt": (
		claim,
		lambda m, doc, _: m.begin_attempt(
			"fake-run", docname=doc, worker_token="worker-a", kind="fix", target="t", input_hash="h", now=11
		),
	),
	"settle_attempt": (
		_settle_setup,
		lambda m, doc, attempt: m.settle_attempt(
			"fake-run",
			attempt,
			docname=doc,
			worker_token="worker-a",
			outcome="succeeded",
			tokens=3,
			usage_complete=True,
			now=12,
		),
	),
	"finish": (
		claim,
		lambda m, doc, _: m.finish(
			"fake-run", docname=doc, worker_token="worker-a", now=12, reason="complete"
		),
	),
	"interrupt": (
		claim,
		lambda m, doc, _: m.interrupt("fake-run", docname=doc, now=400, reason="lease_expired"),
	),
	"continue_run": (
		claim,
		lambda m, doc, _: m.continue_run("fake-run", docname=doc, worker_token="worker-a", now=12),
	),
	"cancel": (claim, lambda m, doc, _: m.cancel("fake-run", docname=doc, now=12)),
	"abandon": (claim, lambda m, doc, _: m.abandon("fake-run", docname=doc, worker_token="worker-a", now=12)),
	"prepare_dispatch": (
		lambda journal: journal.run.update(lease_until=1800, dispatch_pending=1, dispatched_at_epoch=0),
		lambda m, doc, _: m.prepare_dispatch("fake-run", docname=doc, now=50),
	),
	"checkpoint": (
		claim,
		lambda m, doc, _: m.checkpoint(
			"fake-run", docname=doc, worker_token="worker-a", now=12, total_items=3
		),
	),
}


@pytest.mark.parametrize("name", TRANSITIONS)
def test_every_transition_opens_its_transaction_with_the_session_lock(journal, name):
	"""No plain read precedes the locks: on MariaDB 11.8 a read view opened before the
	session lock turns a lock on a row the holder changed into 1020 (snapshot isolation)."""
	setup, transition = TRANSITIONS[name]
	attempt = setup(journal)
	journal.db.calls.clear()
	with journal.db.transaction():
		assert transition(journal.mod, "fake-session-doc", attempt)
	assert journal.db.calls[:2] == [
		("Optimus Session", "fake-session-doc", True),
		(journal.mod.RUN, "fake-run", True),
	]


@pytest.mark.parametrize("name", TRANSITIONS)
def test_a_transition_for_another_session_changes_nothing(journal, name):
	setup, transition = TRANSITIONS[name]
	attempt = setup(journal)
	journal.db.rows["Optimus Session"]["other-session-doc"] = {
		**journal.db.rows["Optimus Session"]["fake-session-doc"],
		"name": "other-session-doc",
	}
	before = deepcopy(journal.db.rows)
	with journal.db.transaction():
		assert not transition(journal.mod, "other-session-doc", attempt)
	assert journal.db.rows == before


def test_an_open_attempt_blocks_a_call_for_any_other_target(journal):
	claim(journal)
	assert begin(journal, name="first-finding")
	assert begin(journal, name="second-finding") is None
	assert journal.run["attempted"] == 1
	assert len(journal.db.rows[journal.mod.ATTEMPT]) == 1


def test_a_settled_target_is_never_called_again_in_the_same_run(journal):
	"""The unique attempt key, not a locking pre-read, rejects the repeat: the INSERT hits
	the existing key and that means already attempted."""
	attempt = _settle_setup(journal)
	with journal.db.transaction():
		assert journal.mod.settle_attempt(
			"fake-run",
			attempt,
			docname="fake-session-doc",
			worker_token="worker-a",
			outcome="failed",
			tokens=0,
			usage_complete=True,
			now=12,
		)
	settled = deepcopy(journal.db.rows[journal.mod.ATTEMPT][attempt])
	assert begin(journal) is None
	assert journal.db.duplicates == 1
	assert journal.run["attempted"] == 1 and not journal.run.get("active_attempt")
	assert journal.db.rows[journal.mod.ATTEMPT][attempt] == settled


def test_begin_inserts_the_attempt_without_a_locking_read_of_its_key(journal):
	"""A locking read of a key that does not exist yet takes a gap lock on MariaDB
	(REPEATABLE READ); two runs beginning together then deadlock on each other's INSERT."""
	claim(journal)
	journal.db.calls.clear()
	attempt = begin(journal)
	assert attempt and not [c for c in journal.db.calls if c[0] == journal.mod.ATTEMPT]


def test_begin_raises_any_insert_failure_that_is_not_a_duplicate(journal, monkeypatch):
	claim(journal)

	def fail(values):
		raise RuntimeError("fake storage failure")

	monkeypatch.setattr(journal.db, "insert", fail)
	with pytest.raises(RuntimeError), journal.db.transaction():
		journal.mod.begin_attempt(
			"fake-run",
			docname="fake-session-doc",
			worker_token="worker-a",
			kind="fix",
			target="t",
			input_hash="h",
			now=11,
		)
	assert journal.run["attempted"] == 0


@pytest.mark.parametrize(
	"error",
	[
		LockUnavailable(DriverError(1205, "fake lock wait timeout")),
		DriverError(1205, "fake raw lock wait timeout"),
		type("LockNotAvailable", (Exception,), {"pgcode": "55P03"})("fake postgres lock_timeout"),
	],
	ids=["frappe-wrapped", "raw", "postgres"],
)
def test_a_lock_wait_timeout_on_the_session_row_is_a_busy_refusal(journal, monkeypatch, error):
	"""F-5: an HTTP admission waits for its session row once; a lock wait timeout there is
	the Q4 busy refusal, so the caller never waits a second timeout."""
	journal.db.rows[journal.mod.RUN] = {}
	current = journal.db.get_value
	waits = []

	def held(table, name, *a, for_update=False, **kw):
		if table == "Optimus Session" and for_update:
			waits.append(name)
			raise error
		return current(table, name, *a, for_update=for_update, **kw)

	monkeypatch.setattr(journal.db, "get_value", held)
	assert admit(journal) == {"status": "refused", "reason": "busy"}
	assert waits == ["fake-session-doc"] and not journal.db.rows[journal.mod.RUN]


# F2: recovery without the scheduler, forced release, dispatch and progress.


def _crashed(journal, **values):
	"""The fixture run, claimed at 10 (lease until 310) and mid-call, by a worker that died.
	It had already answered two findings."""
	claim(journal)
	attempt = begin(journal)
	journal.run.update(completed=2, **values)
	return attempt


@pytest.mark.parametrize(
	"values,reason",
	[({}, "lease_expired"), ({"lease_until": 5000, "deadline": 350}, "deadline")],
	ids=["lease", "deadline"],
)
def test_an_admission_takes_over_an_expired_run_in_its_own_transaction(journal, values, reason):
	"""A2: crash recovery must not depend on the (unscheduled) sweep. An admission that finds
	its session reserved by a run whose lease or deadline has passed ends that run as
	interrupted, its open call uncertain, and admits the new run in the same transaction."""
	attempt = _crashed(journal, **values)
	journal.db.locks.clear()
	out = admit(journal, now=400)
	assert out["status"] == "queued" and out["run"]["name"] == "new-run"
	old = journal.db.rows[journal.mod.RUN]["fake-run"]
	assert old["state"] == "interrupted" and old["end_reason"] == reason
	assert old["active_session"] is None and old["worker_token"] is None
	assert old["uncertain"] == 1 and old["usage_incomplete"] == 1 and not old.get("active_attempt")
	called = journal.db.rows[journal.mod.ATTEMPT][attempt["name"]]
	assert called["state"] == "uncertain" and not called.get("settled")
	assert out["taken_over"]["name"] == "fake-run" and not out["taken_over"].get("inconsistent")
	# Q1: the interrupted run is not a refresh the session counts; the new click is.
	assert journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_refresh_count"] == 0
	# Every lock: the session, the site mutex, then the stale run and its attempt.
	assert journal.db.locks == [
		("Optimus Session", "fake-session-doc"),
		(journal.mod.CONTROL, "site"),
		(journal.mod.RUN, "fake-run"),
		(journal.mod.ATTEMPT, attempt["name"]),
	]


def test_a_takeover_rolls_back_with_an_admission_that_fails(journal, monkeypatch):
	_crashed(journal)

	def fail(values):
		raise RuntimeError("fake storage failure")

	monkeypatch.setattr(journal.db, "insert", fail)
	with pytest.raises(RuntimeError):
		admit(journal, now=400)
	old = journal.db.rows[journal.mod.RUN]["fake-run"]
	assert old["state"] == "running" and old["active_session"] == "fake-session-doc"


def test_an_admission_attaches_to_a_live_run_and_never_takes_it_over(journal):
	attempt = _crashed(journal)
	out = admit(journal, now=300)
	assert out["status"] == "already_running" and out["run"]["name"] == "fake-run"
	assert "taken_over" not in out
	assert journal.db.rows[journal.mod.ATTEMPT][attempt["name"]]["state"] == "calling"
	assert list(journal.db.rows[journal.mod.RUN]) == ["fake-run"]


def test_resuming_an_expired_run_takes_it_over_first(journal):
	_crashed(journal, requested_at_epoch=1, retry_no=0)
	out = admit(journal, now=400, resume_from="fake-run")
	assert out["status"] == "queued" and out["run"]["resume_from"] == "fake-run"
	assert out["run"]["retry_no"] == 1 and out["run"]["requested_at_epoch"] == 1
	assert journal.db.rows[journal.mod.RUN]["fake-run"]["state"] == "interrupted"


@pytest.mark.parametrize("corrupt", ["missing", "another run's"])
def test_a_takeover_force_releases_a_run_whose_open_attempt_is_inconsistent(journal, corrupt):
	"""E2: the row it cannot vouch for is never written; the run counts an uncertain call of
	unknown usage (spend fails closed) and the session is released (release fails open)."""
	attempt = _crashed(journal)
	attempts = journal.db.rows[journal.mod.ATTEMPT]
	if corrupt == "missing":
		del attempts[attempt["name"]]
	else:
		attempts[attempt["name"]]["run_id"] = "other-run"
	before = deepcopy(attempts)
	out = admit(journal, now=400)
	assert out["status"] == "queued" and out["taken_over"]["inconsistent"] == "attempt"
	assert journal.db.rows[journal.mod.ATTEMPT] == before
	old = journal.db.rows[journal.mod.RUN]["fake-run"]
	assert old["state"] == "interrupted" and old["active_session"] is None
	assert old["uncertain"] == 1 and old["usage_incomplete"] == 1
	assert journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_refresh_count"] == 0


@pytest.mark.parametrize("operation", ["cancel", "interrupt"])
@pytest.mark.parametrize("corrupt", ["reservation", "attempt"])
def test_cancel_and_recovery_force_release_an_inconsistent_run(journal, operation, corrupt):
	"""E2: a corrupt row could neither be cancelled nor recovered, so it held its session and
	a site-cap slot for good. It is released now; its spend stays unknown."""
	attempt = _crashed(journal)
	if corrupt == "reservation":
		journal.run["active_session"] = "other-session"
	else:
		del journal.db.rows[journal.mod.ATTEMPT][attempt["name"]]
	with journal.db.transaction():
		if operation == "cancel":
			out = journal.mod.cancel(
				"fake-run", docname="fake-session-doc", now=400, cancelled_by="fake-owner"
			)
		else:
			out = journal.mod.interrupt(
				"fake-run", docname="fake-session-doc", now=400, reason="lease_expired"
			)
	assert out["inconsistent"] == corrupt and out["active_session"] is None
	assert out["state"] == ("cancelled" if operation == "cancel" else "interrupted")
	assert out["uncertain"] == 1 and out["usage_incomplete"] == 1
	session = journal.db.rows["Optimus Session"]["fake-session-doc"]
	assert session["ai_tokens_spent"] == 0 and session["ai_refresh_count"] == 0
	if corrupt == "reservation":
		assert journal.db.rows[journal.mod.ATTEMPT][attempt["name"]]["state"] == "uncertain"


def test_a_consistent_cancel_reports_nothing_inconsistent(journal):
	_crashed(journal)
	with journal.db.transaction():
		out = journal.mod.cancel("fake-run", docname="fake-session-doc", now=12)
	assert out["state"] == "cancelled" and not out.get("inconsistent")


@pytest.mark.parametrize("operation", ["interrupt", "prepare_dispatch"])
def test_the_sweep_takes_the_session_row_without_waiting(journal, monkeypatch, operation):
	"""R1: a dead transaction can hold a session row for hours; the sweep's NOWAIT read is
	refused at once (the caller counts it busy) instead of waiting a lock timeout per run."""
	journal.run.update(state="running", lease_until=5, dispatch_pending=1, dispatched_at_epoch=0)
	if operation == "prepare_dispatch":
		journal.run.update(state="queued", lease_until=1800)
	current = journal.db.get_value
	waits = []

	def held(table, name, *a, for_update=False, wait=True, **kw):
		if for_update:
			waits.append((table, wait))
		if table == "Optimus Session" and for_update and not wait:
			raise LockUnavailable(DriverError(1205, "fake NOWAIT conflict"))
		return current(table, name, *a, for_update=for_update, **kw)

	monkeypatch.setattr(journal.db, "get_value", held)
	with pytest.raises(LockUnavailable), journal.db.transaction():
		if operation == "interrupt":
			journal.mod.interrupt(
				"fake-run", docname="fake-session-doc", now=50, reason="lease_expired", wait=False
			)
		else:
			journal.mod.prepare_dispatch("fake-run", docname="fake-session-doc", now=50, wait=False)
	assert waits == [("Optimus Session", False)]


def test_a_continuation_is_dispatched_at_once_however_soon_it_follows(journal):
	"""R3 / C2 / P5: the 30 s throttle is per slice. A yield seconds after the slice was
	dispatched was held back, and only the unscheduled sweep could deliver it."""
	journal.run.update(lease_until=1800, dispatch_pending=1, dispatched_at_epoch=0)
	with journal.db.transaction():
		assert journal.mod.prepare_dispatch("fake-run", docname="fake-session-doc", now=10)
	claim(journal, now=11)
	with journal.db.transaction():
		assert journal.mod.continue_run(
			"fake-run", docname="fake-session-doc", worker_token="worker-a", now=15
		)
	with journal.db.transaction():
		assert journal.mod.prepare_dispatch("fake-run", docname="fake-session-doc", now=16)
	assert journal.run["slice_no"] == 1 and journal.run["dispatched_at_epoch"] == 16


def _idle_slice(journal, slice_no, *, progressed=False):
	with journal.db.transaction():
		journal.mod.claim(
			"fake-run",
			docname="fake-session-doc",
			slice_no=slice_no,
			worker_token=f"worker-{slice_no}",
			now=20 + slice_no,
			lease_seconds=300,
		)
	with journal.db.transaction():
		return journal.mod.continue_run(
			"fake-run",
			docname="fake-session-doc",
			worker_token=f"worker-{slice_no}",
			now=20 + slice_no,
			progressed=progressed,
		)


def test_a_run_that_cannot_begin_any_item_stops_after_three_idle_slices(journal):
	"""O5: a slice that could not begin its item yields; the next slice selects the same item
	again. Three such slices in a row end the run instead of respinning until its deadline."""
	assert _idle_slice(journal, 0)["state"] == "queued"
	assert _idle_slice(journal, 1)["state"] == "queued"
	out = _idle_slice(journal, 2)
	assert out["state"] == "stopped" and out["end_reason"] == "no_progress"
	assert out["active_session"] is None and not out.get("dispatch_pending")


def test_a_slice_that_made_progress_resets_the_idle_streak(journal):
	_idle_slice(journal, 0)
	_idle_slice(journal, 1)
	_idle_slice(journal, 2, progressed=True)
	_idle_slice(journal, 3)
	assert _idle_slice(journal, 4)["state"] == "queued"
	assert journal.run["idle_slices"] == 2


def test_a_run_stopped_because_ai_was_switched_off_counts_no_refresh(journal):
	"""E7 / C4: ending as complete told the user the refresh finished, and counted it."""
	claim(journal)
	journal.run["completed"] = 2
	with journal.db.transaction():
		out = journal.mod.finish(
			"fake-run", docname="fake-session-doc", worker_token="worker-a", now=12, reason="disabled"
		)
	assert out["state"] == "stopped" and out["end_reason"] == "disabled"
	assert journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_refresh_count"] == 0


def test_finish_refuses_while_a_provider_call_is_unsettled(journal):
	"""M15: ending the run would release the session while a paid answer is still owed."""
	attempt = _crashed(journal)
	with pytest.raises(RuntimeError, match="unsettled"), journal.db.transaction():
		journal.mod.finish(
			"fake-run", docname="fake-session-doc", worker_token="worker-a", now=12, reason="complete"
		)
	assert journal.run["state"] == "running" and journal.run["active_session"] == "fake-session-doc"
	assert journal.db.rows[journal.mod.ATTEMPT][attempt["name"]]["state"] == "calling"


def test_a_late_known_outcome_moves_the_call_out_of_uncertain(journal):
	"""M11: recovery counted the call uncertain and its usage incomplete; the original
	worker's late, known outcome replaces both, so nothing is counted twice."""
	attempt = _crashed(journal)
	with journal.db.transaction():
		journal.mod.interrupt("fake-run", docname="fake-session-doc", now=400, reason="lease_expired")
	assert journal.run["uncertain"] == 1 and journal.run["usage_incomplete"] == 1
	with journal.db.transaction():
		assert journal.mod.settle_attempt(
			"fake-run",
			attempt["name"],
			docname="fake-session-doc",
			worker_token="worker-a",
			outcome="failed",
			tokens=5,
			usage_complete=True,
			now=401,
		)
	assert journal.run["uncertain"] == 0 and journal.run["usage_incomplete"] == 0
	assert journal.run["failed"] == 1 and journal.run["tokens_reported"] == 5


def test_a_takeover_force_releases_another_sessions_run_holding_this_reservation(journal):
	"""E2: a corrupt run of another session holds this session's reservation; it is released
	(the admission goes on), reported as inconsistent and counts no refresh."""
	_crashed(journal)
	journal.db.rows["Optimus Session"]["other-session-doc"] = {
		**journal.db.rows["Optimus Session"]["fake-session-doc"],
		"name": "other-session-doc",
	}
	journal.run["session_name"] = "other-session-doc"
	out = admit(journal, now=400)
	assert out["status"] == "queued" and out["taken_over"]["inconsistent"] == "reservation"
	assert journal.db.rows[journal.mod.RUN]["fake-run"]["active_session"] is None


def test_a_reservation_released_since_the_plain_read_is_not_reported_as_running(journal, monkeypatch):
	"""The takeover locks the run it read and checks it again: a reservation released in
	between (the plain read was older than the lock) is free, not already running."""
	_crashed(journal)
	older = deepcopy(journal.run)
	journal.run.update(state="cancelled", active_session=None)
	current = journal.db.get_value

	def snapshot(table, name, *a, for_update=False, **kw):
		if table == journal.mod.RUN and isinstance(name, dict) and not for_update:
			return deepcopy(older)
		return current(table, name, *a, for_update=for_update, **kw)

	monkeypatch.setattr(journal.db, "get_value", snapshot)
	out = admit(journal, now=400)
	assert out["status"] == "queued" and "taken_over" not in out
	assert journal.db.rows[journal.mod.RUN]["fake-run"]["state"] == "cancelled"


def test_a_refused_admission_still_reports_the_run_it_released(journal):
	"""F3: the takeover commits with the refusal, so the caller still gets ``taken_over``."""
	attempt = _crashed(journal)
	del journal.db.rows[journal.mod.ATTEMPT][attempt["name"]]
	journal.db.rows[journal.mod.RUN]["other"] = {
		**journal.run,
		"name": "other",
		"active_session": "other-session",
		"state": "running",
		"lease_until": 900,
	}
	out = admit(journal, now=400, site_cap=1)
	assert out["status"] == "refused" and out["reason"] == "site_cap"
	assert out["taken_over"]["name"] == "fake-run" and out["taken_over"]["inconsistent"] == "attempt"
	assert journal.db.rows[journal.mod.RUN]["fake-run"]["state"] == "interrupted"
