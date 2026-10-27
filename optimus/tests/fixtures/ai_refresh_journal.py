"""Durable refresh transitions: duplicates, stale owners, lock order and accounting.

The session counters are #72's log-and-continue counters: a statement-level failure is
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

	db_type = "mariadb"  # ``frappe.db.db_type``; a test sets "postgres"
	lock_timeout = "0"  # what Postgres' ``SHOW lock_timeout`` prints; Frappe sets none

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
		self.began = 0  # the index in ``calls`` where the live transaction began

	@contextmanager
	def transaction(self):
		assert not self.active
		self.active = True
		self.began = len(self.calls)
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

	def sql(self, query, values=(), *, as_dict=False):
		"""A statement, recorded as ``("SQL", (query, values), first)``: ``first`` when it is the
		first statement of its transaction. ``SHOW lock_timeout`` answers ``lock_timeout``; any
		other statement returns no rows."""
		assert self.active, "a statement needs a live transaction"
		self.calls.append(("SQL", (query, tuple(values)), len(self.calls) == self.began))
		if query == "SHOW lock_timeout":
			return [{"lock_timeout": self.lock_timeout}] if as_dict else [(self.lock_timeout,)]
		return []

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
					if value[0] == ">" and (actual is None or actual <= value[1]):
						return False
					if value[0] not in {"in", "<=", ">"}:
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
	from datetime import datetime

	monkeypatch.setattr("frappe.utils.now_datetime", lambda: datetime(2026, 10, 14, 12))
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
		"lease_until": 1800,  # a live queued lease: an expired run is not claimed
		"deadline": 3600,
		"attempted": 0,
		"completed": 0,
		"failed": 0,
		"uncertain": 0,
		"skipped": 0,
		"tokens_reported": 0,
		"usage_incomplete": 0,
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


def _count(journal):
	return journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_refresh_count"]


def _end(journal, how):
	"""End the claimed fixture run (two answers done, a call open for some endings) one way."""
	mod, db = journal.mod, journal.db
	journal.run["completed"] = 2
	with db.transaction():
		if how in {"complete", "breaker", "deadline", "disabled", "uncertain"}:
			mod.finish("fake-run", docname="fake-session-doc", worker_token="worker-a", now=12, reason=how)
		elif how == "cancel":
			mod.cancel("fake-run", docname="fake-session-doc", now=12)
		elif how == "interrupt":
			mod.interrupt("fake-run", docname="fake-session-doc", now=400, reason="lease_expired")
		elif how == "abandon":
			mod.abandon("fake-run", docname="fake-session-doc", worker_token="worker-a", now=12)
		else:
			assert how == "no_progress"
			journal.run["idle_slices"] = mod.IDLE_SLICE_LIMIT - 1
			mod.continue_run(
				"fake-run", docname="fake-session-doc", worker_token="worker-a", now=12, progressed=False
			)
	assert journal.run["state"] != "running"


def _refuse_cap(journal, reason):
	journal.db.rows[journal.mod.RUN] = {
		f"other-{i}": {
			**journal.run,
			"name": f"other-{i}",
			"active_session": f"other-session-{i}",
			"requested_by": "other-owner" if reason == "site_cap" else "fake-owner",
		}
		for i in range(2)
	}
	return {"site_cap": 2 if reason == "site_cap" else 4, "user_cap": 2}


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


def _other_sessions_runs(journal, *, owner="other-owner", count=2, **values):
	"""Two user refreshes of other sessions, claimed by workers. Nothing has ended them: no
	click on their sessions, no sweep."""
	journal.db.rows[journal.mod.RUN] = {
		f"other-{i}": {
			**journal.run,
			"name": f"other-{i}",
			"run_id": f"other-{i}",
			"session_name": f"other-session-{i}",
			"active_session": f"other-session-{i}",
			"state": "running",
			"worker_token": f"worker-{i}",
			"requested_by": owner,
			**values,
		}
		for i in range(count)
	}


class LockUnavailable(Exception):
	"""What Frappe raises for a NOWAIT conflict: ``QueryTimeoutError(e)`` around 1205."""


def _fail(journal, name, kind):
	attempt = begin(journal, name=name)
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


def _crashed(journal, **values):
	"""The fixture run, claimed at 10 (lease until 310) and mid-call, by a worker that died.
	It had already answered two findings."""
	claim(journal)
	attempt = begin(journal)
	journal.run.update(completed=2, **values)
	return attempt


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


def _settle_uncertain(journal, attempt, signals, now=12):
	with journal.db.transaction():
		return journal.mod.settle_attempt(
			"fake-run",
			attempt["name"],
			docname="fake-session-doc",
			worker_token="worker-a",
			outcome="uncertain",
			tokens=0,
			usage_complete=False,
			now=now,
			error_kind="timeout",
			signals=signals,
		)


def _claimed(journal, lease_seconds=300):
	with journal.db.transaction():
		return journal.mod.claim(
			"fake-run",
			docname="fake-session-doc",
			slice_no=0,
			worker_token="worker-a",
			now=10,
			lease_seconds=lease_seconds,
		)
