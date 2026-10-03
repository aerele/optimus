"""Durable refresh transitions: duplicates, stale owners and atomic accounting."""

from contextlib import contextmanager
from copy import deepcopy
from importlib import import_module
from types import SimpleNamespace

import pytest


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
		assert name not in rows, "duplicate journal identity"
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
		return journal.mod.claim("fake-run", slice_no=0, worker_token=worker, now=now, lease_seconds=300)


def begin(journal, worker="worker-a", name="fake-finding"):
	with journal.db.transaction():
		return journal.mod.begin_attempt(
			"fake-run", worker_token=worker, kind="fix", target=name, input_hash="fake-hash", now=11
		)


def test_duplicate_delivery_cannot_claim_even_with_the_same_worker_token(journal):
	assert claim(journal)
	assert claim(journal) is None
	assert claim(journal, "worker-b") is None
	assert journal.run["worker_token"] == "worker-a"


def test_claim_locks_parent_before_run_and_rejects_stale_slice(journal):
	with journal.db.transaction():
		assert (
			journal.mod.claim("fake-run", slice_no=1, worker_token="worker-a", now=10, lease_seconds=300)
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
				worker_token="worker-a",
				outcome="succeeded",
				tokens=17,
				usage_complete=True,
				now=12,
			)
	assert journal.run["tokens_reported"] == 17 and journal.run["completed"] == 1
	assert journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_tokens_spent"] == 17


def test_result_and_usage_roll_back_together(journal, monkeypatch):
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
		journal.mod.interrupt("fake-run", now=400, reason="worker_lost")
	assert journal.run["state"] == "interrupted" and journal.run["active_session"] is None
	assert journal.db.rows[journal.mod.ATTEMPT][attempt["name"]]["state"] == "uncertain"
	assert journal.run["uncertain"] == 1 and journal.run["usage_incomplete"] == 1
	assert claim(journal, now=401) is None


def test_late_known_outcome_can_account_but_cannot_write_stale_answer(journal):
	claim(journal)
	attempt = begin(journal)
	with journal.db.transaction():
		journal.mod.interrupt("fake-run", now=400, reason="worker_lost")
	written = []
	with journal.db.transaction():
		journal.mod.settle_attempt(
			"fake-run",
			attempt["name"],
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
				worker_token="worker-a",
				kind="fix",
				target="another-finding",
				input_hash="fake",
				now=13,
			)
			is None
		)


def test_completion_counter_is_atomic_and_idempotent(journal, monkeypatch):
	claim(journal)
	journal.run["completed"] = 1

	def failed(*a):
		raise RuntimeError("fake completion accounting failure")

	with monkeypatch.context() as patch:
		patch.setattr("optimus.analyze._bump_ai_refresh_count", failed)
		with pytest.raises(RuntimeError), journal.db.transaction():
			journal.mod.finish("fake-run", worker_token="worker-a", now=12, reason="complete")
	assert journal.db.rows[journal.mod.RUN]["fake-run"]["completion_counted"] == 0
	for _ in range(2):
		with journal.db.transaction():
			journal.mod.finish("fake-run", worker_token="worker-a", now=12, reason="complete")
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
		assert journal.mod.continue_run("fake-run", worker_token="worker-a", now=12)
	assert journal.run["slice_no"] == 1 and journal.run["state"] == "queued"
	assert journal.run["deadline"] == 3600 and journal.run["dispatch_pending"] == 1
	assert claim(journal) is None
	with journal.db.transaction():
		assert journal.mod.claim("fake-run", slice_no=1, worker_token="worker-b", now=13, lease_seconds=300)
	assert begin(journal) is None


def test_continuation_never_repeats_an_unsettled_call(journal):
	claim(journal)
	begin(journal)
	with pytest.raises(RuntimeError), journal.db.transaction():
		journal.mod.continue_run("fake-run", worker_token="worker-a", now=12)


def test_cancellation_releases_admission_without_repeating_or_losing_usage(journal):
	claim(journal)
	attempt = begin(journal)
	with journal.db.transaction():
		journal.mod.cancel("fake-run", now=12)
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


def test_admission_serializes_caps_before_parent_lock(journal):
	journal.db.rows[journal.mod.RUN] = {}
	out = admit(journal)
	assert out["status"] == "queued"
	assert journal.db.locks[:2] == [(journal.mod.CONTROL, "site"), ("Optimus Session", "fake-session-doc")]
	run = journal.db.rows[journal.mod.RUN]["new-run"]
	assert run["dispatch_pending"] == 1 and run["deadline"] == 3610
	assert run["active_session"] == "fake-session-doc" and run["requested_by"] == "fake-owner"


def test_admission_changes_the_mutex_row_so_postgres_rejects_an_older_snapshot(journal):
	journal.db.rows[journal.mod.RUN] = {}
	assert admit(journal)["status"] == "queued"
	assert journal.db.rows[journal.mod.CONTROL]["site"]["generation"] == 1
	with journal.db.transaction():
		journal.mod.cancel("new-run", now=12)
	assert admit(journal, run_id="second-run")["status"] == "queued"
	assert journal.db.rows[journal.mod.CONTROL]["site"]["generation"] == 2


def test_admission_attaches_to_an_existing_run_without_creating_another(journal):
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
	with pytest.raises(journal.mod.JournalUnavailable):
		admit(journal)
	assert len(journal.db.rows[journal.mod.RUN]) == 1


def test_admission_counts_current_rows_even_with_an_older_transaction_snapshot(journal, monkeypatch):
	journal.db.rows[journal.mod.RUN] = {
		f"other-{i}": {
			**journal.run,
			"name": f"other-{i}",
			"active_session": f"other-session-{i}",
			"requested_by": "fake-owner",
		}
		for i in range(2)
	}
	current = journal.db.get_values

	def snapshot_or_current(*a, for_update=False, **kw):
		return current(*a, for_update=True, **kw) if for_update else []

	monkeypatch.setattr(journal.db, "get_values", snapshot_or_current)
	monkeypatch.setattr(journal.mod.frappe, "get_all", lambda *a, **kw: [])
	assert admit(journal) == {"status": "refused", "reason": "site_cap"}


def test_empty_refresh_does_not_count_as_an_updated_session(journal):
	claim(journal)
	with journal.db.transaction():
		journal.mod.finish("fake-run", worker_token="worker-a", now=12, reason="complete")
	assert journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_refresh_count"] == 0
	assert journal.run["completion_counted"] == 1


def test_stale_input_rejection_keeps_usage_but_not_completed_count(journal):
	claim(journal)
	attempt = begin(journal)
	with journal.db.transaction():
		journal.mod.settle_attempt(
			"fake-run",
			attempt["name"],
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
	with pytest.raises(journal.mod.JournalUnavailable), journal.db.transaction():
		journal.mod.cancel("fake-run", now=12)
	assert journal.db.rows[journal.mod.ATTEMPT][attempt["name"]]["state"] == "calling"


def test_corrupt_session_reservation_cannot_be_claimed(journal):
	journal.run["active_session"] = "other-session"
	with pytest.raises(journal.mod.JournalUnavailable):
		claim(journal)


def test_dispatch_reservation_is_throttled_and_cannot_resurrect_finished_run(journal):
	journal.run.update(lease_until=1800, dispatch_pending=1, dispatched_at_epoch=0)
	with journal.db.transaction():
		assert journal.mod.prepare_dispatch("fake-run", now=50)
	with journal.db.transaction():
		assert journal.mod.prepare_dispatch("fake-run", now=51) is None
	assert journal.run["dispatch_pending"] == 1
	claim(journal, now=52)
	with journal.db.transaction():
		assert journal.mod.prepare_dispatch("fake-run", now=100) is None


def test_explicit_worker_abandon_marks_unknown_without_waiting_for_lease(journal):
	claim(journal)
	attempt = begin(journal)
	with journal.db.transaction():
		assert journal.mod.abandon("fake-run", worker_token="worker-b", now=12) is None
	with journal.db.transaction():
		assert journal.mod.abandon("fake-run", worker_token="worker-a", now=12)
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
		journal.mod.interrupt("fake-run", now=50, reason="deadline")
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
		journal.mod.cancel("new-run", now=12)
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
		journal.mod.cancel("new-run", now=12)
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
