# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""ai_fix.log_ai_failure (the AI-surface Error Log chokepoint) and the HTTP
layer's failure path (PR-0a).

``frappe.log_error`` and ``frappe.db`` are replaced wholesale (``frappe.db`` is
a Werkzeug Local proxy on a bench: never patch its attributes).
"""

import json
import sys
import types
from types import SimpleNamespace

import pytest
import requests

from optimus import ai_fix

KEY = "sk-live-0123456789abcdefXYZ"


class _Callbacks:
	"""``frappe.utils.CallbackManager`` semantics: functions run in the order
	they were added, each removed before it runs; ``reset`` drops them all."""

	def __init__(self):
		self.functions = []

	def add(self, fn):
		self.functions.append(fn)

	def run(self):
		while self.functions:
			self.functions.pop(0)()

	def reset(self):
		self.functions.clear()


class _FakeDB:
	"""The parts of ``frappe.database.Database`` the AI log path touches.
	``commit`` drops the rollback callbacks before committing and a full
	``rollback`` runs them after the ROLLBACK (a savepoint rollback runs
	none), as Frappe does.

	``transactional`` is the Error Log table's engine: a ROLLBACK removes the
	rows inserted since the last commit (Postgres), or leaves them (False:
	MariaDB, where Error Log is a MyISAM table)."""

	def __init__(self, docname="SESS-0001", raise_on_get=False, transactional=True, raise_on_exists=None):
		self.docname = docname
		self.raise_on_get = raise_on_get
		self.lookups = 0
		self.after_rollback = _Callbacks()
		self.transactional = transactional
		self.raise_on_exists = raise_on_exists
		self.error_logs = set()
		self.uncommitted = []
		self.inserted = 0

	def get_value(self, doctype, filters, field):
		self.lookups += 1
		if self.raise_on_get:
			raise RuntimeError("db down")
		return self.docname

	def insert_error_log(self):
		self.inserted += 1
		name = f"ERR-{self.inserted:04d}"
		self.error_logs.add(name)
		self.uncommitted.append(name)
		return name

	def exists(self, doctype, name=None, *a, **k):
		if self.raise_on_exists is not None:
			raise self.raise_on_exists
		return name if doctype == "Error Log" and name in self.error_logs else None

	def commit(self):
		self.after_rollback.reset()
		self.uncommitted.clear()

	def rollback(self, *, save_point=None, chain=False):
		if save_point:
			return
		if self.transactional:
			self.error_logs.difference_update(self.uncommitted)
		self.uncommitted.clear()
		self.after_rollback.run()


class _Flags(dict):
	"""``frappe.flags`` is a ``frappe._dict``: a flag never set reads as None."""

	__getattr__ = dict.get


def _inserted_row():
	"""What ``frappe.log_error`` returns after a direct insert: the Error Log
	document, named (the row goes into the current fake DB)."""
	import frappe

	insert = getattr(frappe.db, "insert_error_log", None)
	return SimpleNamespace(name=insert()) if insert else None


@pytest.fixture
def logs(monkeypatch):
	"""Capture frappe.log_error calls; store a key; fake the DB."""
	import frappe

	calls = []
	monkeypatch.setattr(frappe, "log_error", lambda **kw: calls.append(kw) or _inserted_row(), raising=False)
	monkeypatch.setattr(frappe, "db", _FakeDB(), raising=False)
	monkeypatch.setattr(frappe, "flags", _Flags(), raising=False)
	monkeypatch.setattr(
		"frappe.utils.password.get_decrypted_password", lambda *a, **k: KEY, raising=False
	)
	return calls


@pytest.fixture
def requeued(monkeypatch):
	"""Capture ``frappe.deferred_insert.deferred_insert`` calls as
	``(doctype, records)``."""
	queued = []
	module = types.ModuleType("frappe.deferred_insert")
	module.deferred_insert = lambda doctype, records: queued.append((doctype, records))
	monkeypatch.setitem(sys.modules, "frappe.deferred_insert", module)
	return queued


def _crumb(error_type: str) -> str:
	"""The breadcrumb for a row that may be missing. It says "may": a failed
	existence check leaves a row that may have survived (MariaDB), and a
	failed callback registration leaves a written row that a later Postgres
	rollback could remove."""
	return f"optimus ai_fix: an AI Error Log row may not have been written or re-queued: {error_type}"


@pytest.fixture(autouse=True)
def breadcrumbs(monkeypatch):
	"""Capture ``frappe.logger(...).error`` calls as ``(module, message,
	exception active while logging)``. Autouse: no test here may reach the
	real ``frappe.logger``, which opens ``../logs/<module>.log``. The fake has
	no ``warning``: a warning is dropped by Frappe's production log level
	(see ``TestTheBreadcrumbReachesTheLogInProduction``)."""
	import frappe

	lines = []

	def _logger(module=None, *a, **k):
		return SimpleNamespace(error=lambda msg, *x, **y: lines.append((module, msg, sys.exc_info()[1])))

	monkeypatch.setattr(frappe, "logger", _logger, raising=False)
	return lines


def _raise_with_local_and_chain():
	private_local = "PII-LOCAL alice@example.com"  # noqa: F841 (must not be logged)
	try:
		raise ValueError("CHAINED-CONTEXT")
	except ValueError:
		# chained on purpose: log_ai_failure must not print __context__
		raise ai_fix.AiFixError(f"provider echoed {KEY}")


class TestLogAiFailure:
	def test_message_is_explicit_scrubbed_and_referenced(self, logs):
		try:
			_raise_with_local_and_chain()
		except ai_fix.AiFixError as e:
			ai_fix.log_ai_failure("optimus ai backfill", e, session_uuid="uuid-1", finding="F1")
		assert len(logs) == 1
		row = logs[0]
		assert row["title"] == "optimus ai backfill"
		msg = row["message"]
		assert msg.startswith("optimus ai backfill\n")
		assert "session_uuid=uuid-1" in msg and "finding=F1" in msg
		assert "AiFixError: provider echoed ********" in msg
		assert KEY not in msg
		assert row["reference_doctype"] == "Optimus Session"
		assert row["reference_name"] == "SESS-0001"
		assert "defer_insert" not in row  # always inserted directly

	def test_no_frame_locals_and_no_chain(self, logs):
		try:
			_raise_with_local_and_chain()
		except ai_fix.AiFixError as e:
			ai_fix.log_ai_failure("t", e)
		msg = logs[0]["message"]
		assert "alice@example.com" not in msg
		assert "CHAINED-CONTEXT" not in msg

	def test_inserts_directly_even_in_a_request_on_a_site_whose_scheduler_runs(self, logs, requeued, monkeypatch):
		# A deferred row lands up to 15 minutes late and is lost on a cache
		# Redis restart or eviction: the row is written in the transaction.
		import frappe

		monkeypatch.setattr(frappe, "request", SimpleNamespace(path="/api/method/x"), raising=False)
		monkeypatch.setattr("frappe.utils.scheduler.is_scheduler_inactive", lambda verbose=True: False, raising=False)
		assert ai_fix.log_ai_failure("t", session_uuid="uuid-1") is True
		assert len(logs) == 1 and "defer_insert" not in logs[0]
		assert requeued == []

	def test_a_rollback_that_removes_the_row_requeues_exactly_one_record_with_the_scrubbed_message(self, logs, requeued):
		# Transactional Error Log (Postgres): frappe.throw after the log (the
		# request rollback in frappe.app) or a failing job (execute_job rolls
		# back) takes the inserted row away, so the same scrubbed row is
		# queued again, once.
		import frappe

		ai_fix.log_ai_failure("optimus ai backfill", ai_fix.AiFixError(f"echoed {KEY}"), session_uuid="uuid-1")
		frappe.db.rollback()
		assert frappe.db.error_logs == set()  # the rollback removed it
		frappe.db.rollback()
		assert len(requeued) == 1
		doctype, records = requeued[0]
		assert doctype == "Error Log"
		assert records == [{
			"error": logs[0]["message"], "method": "optimus ai backfill",
			"reference_doctype": "Optimus Session", "reference_name": "SESS-0001",
		}]
		assert "AiFixError: echoed ********" in records[0]["error"]
		assert KEY not in json.dumps(records)
		assert len(logs) == 1  # the callback never calls frappe.log_error (Sentry)

	def test_a_rollback_that_leaves_the_row_queues_nothing(self, logs, requeued, monkeypatch):
		# MariaDB: Error Log is a MyISAM table, so the ROLLBACK leaves the row
		# in place; a queued copy would be written as a second row.
		import frappe

		monkeypatch.setattr(frappe, "db", _FakeDB(transactional=False), raising=False)
		ai_fix.log_ai_failure("optimus ai backfill", ValueError("x"), session_uuid="uuid-1")
		frappe.db.rollback()
		assert frappe.db.error_logs == {"ERR-0001"}  # still there
		assert requeued == []

	def test_a_failing_existence_check_queues_nothing_and_never_raises(self, logs, requeued, monkeypatch):
		# Whether the row survived is unknown: queuing could duplicate it.
		import frappe

		db = _FakeDB(raise_on_exists=RuntimeError("connection lost"))
		monkeypatch.setattr(frappe, "db", db, raising=False)
		ai_fix.log_ai_failure("t", ValueError("x"))
		frappe.db.rollback()  # must not raise
		assert requeued == []

	@pytest.mark.parametrize("where", ["existence-check", "queue"])
	def test_a_failed_requeue_leaves_a_type_only_breadcrumb(self, logs, monkeypatch, breadcrumbs, where):
		# Postgres: the rollback removed the row and it could not be queued
		# again, so the failure is gone without a trace unless the optimus log
		# says so. Type only; written after the callback's try.
		import frappe

		boom = ConnectionError(f"lost for {KEY} alice@example.com")
		if where == "existence-check":
			monkeypatch.setattr(frappe, "db", _FakeDB(raise_on_exists=boom), raising=False)
		module = types.ModuleType("frappe.deferred_insert")
		module.deferred_insert = _raising(boom) if where == "queue" else (lambda doctype, records: None)
		monkeypatch.setitem(sys.modules, "frappe.deferred_insert", module)
		assert ai_fix.log_ai_failure("t", ValueError("x")) is True
		assert breadcrumbs == []
		frappe.db.rollback()  # must not raise
		assert len(breadcrumbs) == 1
		logger_module, message, active = breadcrumbs[0]
		assert logger_module == "optimus" and message == _crumb("ConnectionError")
		assert KEY not in message and "alice@example.com" not in message and "lost for" not in message
		assert active is None

	def test_a_failed_registration_leaves_a_type_only_breadcrumb(self, logs, monkeypatch, breadcrumbs):
		# The row is written, but no callback will queue it again if a later
		# rollback removes it (Postgres).
		import frappe

		frappe.db.after_rollback.add = _raising(RuntimeError(f"no callbacks {KEY}"))
		assert ai_fix.log_ai_failure("t", ValueError("x")) is True
		assert len(logs) == 1
		assert breadcrumbs == [("optimus", _crumb("RuntimeError"), None)]

	def test_no_breadcrumb_when_the_requeue_works_or_is_not_needed(self, logs, requeued, monkeypatch, breadcrumbs):
		import frappe

		ai_fix.log_ai_failure("t", ValueError("x"))
		frappe.db.rollback()
		assert len(requeued) == 1  # transactional: queued again
		monkeypatch.setattr(frappe, "db", _FakeDB(transactional=False), raising=False)
		ai_fix.log_ai_failure("t", ValueError("y"))
		frappe.db.rollback()
		assert len(requeued) == 1  # MyISAM: the row survived, nothing queued
		assert breadcrumbs == []

	def test_nothing_is_registered_without_a_named_row(self, logs, requeued, monkeypatch):
		# log_error returns the document only after a direct insert (no
		# database: it prints and returns None). Without its name the callback
		# could not tell whether a rollback removed it.
		import frappe

		monkeypatch.setattr(frappe, "log_error", lambda **kw: logs.append(kw), raising=False)
		assert ai_fix.log_ai_failure("t") is True
		assert frappe.db.after_rollback.functions == []

	def test_a_commit_then_a_rollback_queues_nothing(self, logs, requeued):
		import frappe

		ai_fix.log_ai_failure("t", ValueError("x"), session_uuid="uuid-1")
		frappe.db.commit()
		frappe.db.rollback()
		assert requeued == []

	def test_a_savepoint_rollback_queues_nothing(self, logs, requeued):
		# Frappe runs no rollback callbacks for a savepoint rollback.
		import frappe

		ai_fix.log_ai_failure("t")
		frappe.db.rollback(save_point="sp")
		assert requeued == []

	def test_nothing_is_registered_in_read_only_mode(self, logs, requeued, monkeypatch):
		# frappe.log_error defers the row itself when the site is read-only, so
		# a rollback cannot take it away: queuing it again would duplicate it.
		import frappe

		monkeypatch.setattr(frappe, "flags", _Flags(read_only=True), raising=False)
		ai_fix.log_ai_failure("t")
		assert frappe.db.after_rollback.functions == []

	def test_the_trace_id_and_metadata_of_the_inserted_row_are_kept(self, logs, requeued, monkeypatch):
		import frappe

		def _log_error(**kw):
			logs.append(kw)
			return SimpleNamespace(
				name=frappe.db.insert_error_log(), trace_id="trace-1", metadata='{"type": "background_job"}',
			)

		monkeypatch.setattr(frappe, "log_error", _log_error, raising=False)
		ai_fix.log_ai_failure("t")
		frappe.db.rollback()
		assert requeued[0][1][0]["trace_id"] == "trace-1"
		assert requeued[0][1][0]["metadata"] == '{"type": "background_job"}'

	def test_the_rollback_callback_never_raises_and_holds_no_key(self, logs, monkeypatch):
		import frappe

		def _broken_queue(doctype, records):
			raise ConnectionError("redis down")

		module = types.ModuleType("frappe.deferred_insert")
		module.deferred_insert = _broken_queue
		monkeypatch.setitem(sys.modules, "frappe.deferred_insert", module)
		ai_fix.log_ai_failure("t", ai_fix.AiFixError(f"echoed {KEY}"), provider="openai")
		(callback,) = frappe.db.after_rollback.functions
		held = [repr(cell.cell_contents) for cell in (callback.__closure__ or ())]
		assert held and not any(KEY in text for text in held)
		frappe.db.rollback()  # must not raise

	def test_scrub_failure_keeps_only_the_title_and_the_error_type(self, logs, monkeypatch):
		def _boom(*a, **k):
			raise RuntimeError("regex engine exploded")
		monkeypatch.setattr("optimus.redaction.scrub_secrets", _boom)
		try:
			_raise_with_local_and_chain()
		except ai_fix.AiFixError as e:
			error = e
		ai_fix.log_ai_failure("optimus ai backfill", error, session_uuid="uuid-1", finding="PII-LOCAL alice@example.com")
		assert len(logs) == 1  # the failure is still recorded
		msg = logs[0]["message"]
		assert KEY not in msg and "alice@example.com" not in msg and "provider echoed" not in msg
		assert msg == (
			"optimus ai backfill\n(details withheld: scrubbing the message failed with RuntimeError; "
			"error type AiFixError)"
		)

	def test_no_reference_without_a_resolvable_session(self, logs, monkeypatch):
		import frappe

		ai_fix.log_ai_failure("t")
		monkeypatch.setattr(frappe, "db", _FakeDB(docname=None), raising=False)
		ai_fix.log_ai_failure("t", session_uuid="gone")
		monkeypatch.setattr(frappe, "db", _FakeDB(raise_on_get=True), raising=False)
		ai_fix.log_ai_failure("t", session_uuid="u")
		assert [(r["reference_doctype"], r["reference_name"]) for r in logs] == [(None, None)] * 3

	def test_an_explicit_docname_is_the_reference_without_a_lookup(self, logs, monkeypatch):
		import frappe

		db = _FakeDB(raise_on_get=True)
		monkeypatch.setattr(frappe, "db", db, raising=False)
		ai_fix.log_ai_failure("t", session_uuid="uuid-1", docname="SESS-0042")
		assert (logs[0]["reference_doctype"], logs[0]["reference_name"]) == ("Optimus Session", "SESS-0042")
		assert db.lookups == 0
		assert "session_uuid=uuid-1" in logs[0]["message"]

	def test_never_raises(self, monkeypatch, breadcrumbs):
		import frappe

		def _boom(**kw):
			raise RuntimeError("Error Log insert failed")
		monkeypatch.setattr(frappe, "log_error", _boom, raising=False)
		ai_fix.log_ai_failure("t", ValueError("x"), session_uuid="u")  # must not raise

	def test_returns_true_only_once_log_error_returned(self, logs, monkeypatch, breadcrumbs):
		import frappe

		e = ai_fix.AiFixError("boom")
		assert ai_fix.log_ai_failure("first", e) is True
		assert ai_fix.log_ai_failure("again", e) is False  # already logged: nothing written

		def _boom(**kw):
			raise RuntimeError("Error Log insert failed")
		monkeypatch.setattr(frappe, "log_error", _boom, raising=False)
		failed = ValueError("x")
		assert ai_fix.log_ai_failure("t", failed) is False
		assert not getattr(failed, ai_fix._LOGGED_ATTR, False)  # a later caller may still log it

	def test_a_failed_write_leaves_a_type_only_breadcrumb_logged_after_the_handler(self, logs, monkeypatch, breadcrumbs):
		# The row could not be written: one error-level line in the optimus log names
		# the error TYPE only (its message could hold anything) and is written
		# with no exception being handled.
		import frappe

		def _boom(**kw):
			raise RuntimeError(f"insert failed for {KEY} alice@example.com")
		monkeypatch.setattr(frappe, "log_error", _boom, raising=False)
		ai_fix.log_ai_failure("t", ValueError("x"))
		assert len(breadcrumbs) == 1
		module, message, active = breadcrumbs[0]
		assert module == "optimus"
		assert message == _crumb("RuntimeError")
		assert KEY not in message and "alice@example.com" not in message and "insert failed" not in message
		assert active is None

	def test_a_failing_breadcrumb_is_swallowed(self, logs, monkeypatch):
		import frappe

		def _boom(*a, **kw):
			raise RuntimeError("no log file")
		monkeypatch.setattr(frappe, "log_error", _boom, raising=False)
		monkeypatch.setattr(frappe, "logger", _boom, raising=False)
		assert ai_fix.log_ai_failure("t") is False  # must not raise

	def test_no_breadcrumb_when_the_row_is_written(self, logs, breadcrumbs):
		ai_fix.log_ai_failure("t", ValueError("x"))
		assert breadcrumbs == []

	def test_only_the_scrubbed_message_is_bound_while_logging(self, logs, monkeypatch):
		# Sentry (attach_stacktrace=True) serialises the calling frame's locals
		# for the message event, so while frappe.log_error runs the
		# log_ai_failure frame must hold the scrubbed message, not the
		# unscrubbed lines it was built from.
		import sys

		import frappe

		frames = []

		def _log(**kw):
			caller = sys._getframe(1)
			frames.append((caller.f_code.co_name, set(caller.f_locals)))

		monkeypatch.setattr(frappe, "log_error", _log, raising=False)
		ai_fix.log_ai_failure("t", ValueError("x"), session_uuid="u", finding="F1")
		assert [name for name, _ in frames] == ["log_ai_failure"]
		assert "message" in frames[0][1]
		assert "lines" not in frames[0][1]

	def test_mark_logged_never_raises_when_the_flag_cannot_be_set(self):
		class _Frozen(Exception):
			def __setattr__(self, name, value):
				raise AttributeError("read-only exception")

		frozen = _Frozen("x")
		ai_fix._mark_logged(frozen)  # must not raise
		assert not getattr(frozen, ai_fix._LOGGED_ATTR, False)

	def test_an_exception_is_logged_once(self, logs):
		e = ai_fix.AiFixError("boom")
		ai_fix.log_ai_failure("first", e)
		ai_fix.log_ai_failure("second", e)
		assert [r["title"] for r in logs] == ["first"]


class TestTheBreadcrumbReachesTheLogInProduction:
	def test_it_is_logged_at_a_level_frappes_production_logger_keeps(self, logs, monkeypatch, capsys):
		"""Frappe's loggers sit at ERROR unless DEV_SERVER is set (``bench
		start``): ``frappe/utils/logger.py`` sets ``default_log_level`` to
		WARNING only for the dev server, so a warning breadcrumb would be
		dropped on every production site. This runs Frappe's real
		``get_logger`` level logic: a private copy of that module is loaded
		with DEV_SERVER unset and stream-only handlers (so no log file is
		written), ``frappe.logger`` goes through its ``get_logger``, and the
		breadcrumb must come out."""
		import importlib.util
		import logging

		import frappe

		real = pytest.importorskip("frappe.utils.logger")
		monkeypatch.delenv("DEV_SERVER", raising=False)
		monkeypatch.setattr(frappe, "_dev_server", 0, raising=False)
		monkeypatch.setenv("FRAPPE_STREAM_LOGGING", "1")
		spec = importlib.util.spec_from_file_location("_optimus_test_frappe_logger", real.__file__)
		private = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(private)
		assert private.default_log_level == logging.ERROR
		monkeypatch.setattr(frappe, "loggers", {}, raising=False)
		monkeypatch.setattr(frappe, "log_level", None, raising=False)
		monkeypatch.setattr(frappe, "logger", lambda module=None, **k: private.get_logger(module=module, **k), raising=False)
		monkeypatch.setattr(frappe, "log_error", _raising(RuntimeError("Error Log insert failed")), raising=False)

		named = logging.getLogger("optimus-all")  # what get_logger names it without a site
		saved = (list(named.handlers), named.level, named.propagate)
		try:
			assert ai_fix.log_ai_failure("t", ValueError("x")) is False
			assert named.level == logging.ERROR
		finally:
			for handler in [h for h in named.handlers if h not in saved[0]]:
				named.removeHandler(handler)
				handler.close()
			named.setLevel(saved[1])
			named.propagate = saved[2]
		err = capsys.readouterr().err
		assert _crumb("RuntimeError") in err


def _post(behaviour):
	def _fake(url, headers=None, json=None, timeout=None, auth=None):  # noqa: A002
		return behaviour()
	return _fake


class _Resp:
	def __init__(self, status_code=200, payload=None, text=""):
		self.status_code = status_code
		self._payload = payload
		self.text = text

	def json(self):
		if isinstance(self._payload, Exception):
			raise self._payload
		return self._payload


def _call():
	return ai_fix._http_post("https://x.invalid/v1/chat/completions", {}, {"model": "m"},
	                         provider="openai", where="chat/completions")


class TestHttpFailurePath:
	def _raise(self, exc):
		def _b():
			raise exc
		return _b

	def test_transport_failure_is_logged_once_and_not_chained(self, logs, monkeypatch):
		monkeypatch.setattr(requests, "post", _post(self._raise(
			requests.exceptions.ConnectionError("HTTPConnectionPool(host='x.invalid', port=443): refused"))))
		with pytest.raises(ai_fix.AiFixError) as ei:
			_call()
		assert ei.value.kind == "transport"
		assert ei.value.__context__ is None and ei.value.__cause__ is None
		assert len(logs) == 1 and logs[0]["title"] == "optimus ai_fix"
		assert "detail=ConnectionError: HTTPConnectionPool" in logs[0]["message"]
		# a caller logging the same error again writes nothing more (K16)
		ai_fix.log_ai_failure("optimus ai backfill", ei.value)
		assert len(logs) == 1

	def test_catch_all_keeps_only_the_type_name(self, logs, monkeypatch):
		# What http.client raises for a header value it cannot encode: the
		# exception's .object is the whole header, i.e. the key.
		boom = UnicodeEncodeError("latin-1", f"Bearer {KEY}\u2019", len(KEY) + 7, len(KEY) + 8, "ordinal not in range(256)")
		monkeypatch.setattr(requests, "post", _post(self._raise(boom)))
		with pytest.raises(ai_fix.AiFixError) as ei:
			_call()
		assert ei.value.kind == "transport"
		assert "UnicodeEncodeError" in str(ei.value) and KEY not in str(ei.value)
		assert ei.value.__context__ is None
		assert KEY not in logs[0]["message"]
		assert "ordinal not in range" not in logs[0]["message"]  # never the exception's message
		assert "detail=UnicodeEncodeError\n" in logs[0]["message"]  # the type name, then plain frames

	def test_catch_all_logs_plain_frames_without_locals_or_message(self, logs, monkeypatch):
		# A local programming error after the POST would otherwise leave only
		# its type: the row names where it happened, file:line:function per
		# frame, and nothing else.
		def _inner(headers):
			secret_local = f"Bearer {KEY}"  # noqa: F841 (a frame local: never logged)
			raise KeyError(f"MESSAGE-MARKER {KEY}")

		def _fake(url, headers=None, json=None, timeout=None, auth=None):  # noqa: A002
			return _inner(headers)

		monkeypatch.setattr(requests, "post", _fake)
		with pytest.raises(ai_fix.AiFixError):
			_call()
		msg = logs[0]["message"]
		detail = msg.split("detail=", 1)[1]
		assert detail.startswith("KeyError\n")
		assert f"{__file__}:{_fake.__code__.co_firstlineno + 1}:_fake" in detail
		assert f"{__file__}:{_inner.__code__.co_firstlineno + 2}:_inner" in detail
		assert f"{ai_fix.__file__}:" in detail and ":_http_post" in detail
		assert "MESSAGE-MARKER" not in msg and KEY not in msg and "secret_local" not in msg

	@pytest.mark.parametrize(
		"interrupt",
		[SystemExit(1), KeyboardInterrupt(), type("GreenletTimeout", (BaseException,), {})(5)],
		ids=["worker-timeout-SystemExit", "KeyboardInterrupt", "gevent-Timeout"],
	)
	def test_a_non_exception_interrupt_keeps_its_identity_without_the_send_frames(self, logs, monkeypatch, interrupt):
		# A gunicorn worker timeout raises SystemExit in the request thread,
		# possibly while urllib3 holds the prepared headers (the key) in its
		# locals, and Sentry's WSGI middleware ships frame locals. The same
		# instance leaves (gevent matches its Timeout by identity), with no
		# traceback below _http_post, no chain and no log.
		def _send(headers):
			prepared = {"authorization": f"Bearer {KEY}"}  # noqa: F841 what urllib3 holds
			try:
				raise UnicodeEncodeError("latin-1", f"Bearer {KEY}", 0, 1, "ordinal not in range(256)")
			except UnicodeEncodeError:
				raise interrupt  # noqa: B904 (chained to the header error on purpose)

		def _fake(url, headers=None, json=None, timeout=None, auth=None):  # noqa: A002
			return _send(headers)

		monkeypatch.setattr(requests, "post", _fake)
		with pytest.raises(BaseException) as ei:
			_call()
		assert ei.value is interrupt
		assert ei.value.__context__ is None and ei.value.__cause__ is None
		assert ei.value.__suppress_context__ is True
		codes = set()
		tb = ei.value.__traceback__
		while tb is not None:
			codes.add(tb.tb_frame.f_code)
			for name, value in tb.tb_frame.f_locals.items():
				assert KEY not in repr(value), f"{tb.tb_frame.f_code.co_name}: {name} holds the key"
			tb = tb.tb_next
		assert _send.__code__ not in codes and _fake.__code__ not in codes
		assert logs == []

	def test_the_catch_all_handler_only_records(self, logs, monkeypatch):
		# The handler keeps plain values; the AiFixError and its translated
		# message are built after the try. So even if building them fails, the
		# error that escapes chains nothing (the http.client error's .object is
		# the header, i.e. the key) and no ai_fix frame still binds that error.
		import frappe

		def _broken_translation(*a, **k):
			raise RuntimeError("translation failed")

		monkeypatch.setattr(frappe, "_", _broken_translation, raising=False)
		header = f"Bearer {KEY}’"
		monkeypatch.setattr(requests, "post", _post(self._raise(
			UnicodeEncodeError("latin-1", header, len(header) - 1, len(header), "ordinal not in range(256)"))))
		with pytest.raises(RuntimeError, match="translation failed") as ei:
			_call()
		assert ei.value.__context__ is None and ei.value.__cause__ is None
		checked = []
		tb = ei.value.__traceback__
		while tb is not None:
			if tb.tb_frame.f_code.co_filename == ai_fix.__file__:
				checked.append(tb.tb_frame.f_code.co_name)
				for name, value in tb.tb_frame.f_locals.items():
					assert KEY not in repr(value), f"{tb.tb_frame.f_code.co_name}: {name} holds the header"
			tb = tb.tb_next
		assert "_http_post" in checked

	def test_rq_job_timeout_still_stops_the_job(self, logs, monkeypatch):
		# The catch-all must not turn RQ's job timeout into a normal AI error
		# (callers would carry on to the next item); it re-raises the same type,
		# fresh, so no requests / urllib3 frame (which hold the prepared
		# headers) travels with it.
		timeouts = pytest.importorskip("rq.timeouts")
		original = timeouts.JobTimeoutException("Task exceeded maximum timeout value (60 seconds)")
		raiser = self._raise(original)
		fake_post = _post(raiser)
		monkeypatch.setattr(requests, "post", fake_post)
		with pytest.raises(timeouts.JobTimeoutException) as ei:
			_call()
		assert ei.value is not original
		assert ei.value.args == original.args
		assert ei.value.__context__ is None and ei.value.__cause__ is None
		# No frame that raised the original (the fake transport here, requests /
		# urllib3 in production, whose locals hold the auth header) travels
		# with the re-raised instance.
		codes = set()
		tb = ei.value.__traceback__
		while tb is not None:
			codes.add(tb.tb_frame.f_code)
			tb = tb.tb_next
		assert raiser.__code__ not in codes and fake_post.__code__ not in codes
		assert logs == []

	@pytest.mark.parametrize("status", [400, 404, 500])
	def test_an_echoed_key_is_masked_in_the_error_message(self, logs, monkeypatch, status):
		# The 404 and other >= 400 messages carry the provider body to the
		# operator (toast, API response, the title of Frappe's own snapshot).
		body = f'{{"error": "invalid key {KEY} for this model"}}'
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(status, {}, text=body)))
		with pytest.raises(ai_fix.AiFixError) as ei:
			_call()
		assert "invalid key ******** for this model" in str(ei.value)
		assert KEY not in str(ei.value)

	def test_the_404_message_masks_credentials_in_the_base_url(self, logs, monkeypatch):
		# A custom Base URL typed as user:password@host: the 404 message names
		# the URL, and it reaches toasts and API responses.
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(404, {}, text="")))
		with pytest.raises(ai_fix.AiFixError) as ei:
			ai_fix._call_openai_chat(
				"http://alice:hunter2-pass@llm.internal:11434/v1", "", "m", "s", [{"role": "user", "content": "x"}]
			)
		message = str(ei.value)
		assert "404 (Not Found) for http://********@llm.internal:11434/v1/chat/completions. " in message
		assert "alice" not in message and "hunter2-pass" not in message
		assert "alice" not in logs[0]["message"] and "hunter2-pass" not in logs[0]["message"]

	def test_the_404_message_masks_the_key_in_the_base_url(self, logs, monkeypatch):
		# Some gateways take the key in the path: the stored key is scrubbed
		# from the URL the message names, like any other literal.
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(404, {}, text="")))
		with pytest.raises(ai_fix.AiFixError) as ei:
			ai_fix._call_openai_chat(
				f"https://gw.internal/{KEY}/v1", "", "m", "s", [{"role": "user", "content": "x"}]
			)
		assert "404 (Not Found) for https://gw.internal/********/v1/chat/completions. " in str(ei.value)
		assert KEY not in str(ei.value)

	@pytest.mark.parametrize("fails", ["reading-the-key", "scrubbing"])
	def test_a_404_url_that_cannot_be_scrubbed_is_never_shown(self, logs, monkeypatch, fails):
		# If the URL cannot be scrubbed, the message names a placeholder, never
		# the unscrubbed URL (a custom Base URL can carry user:password@).
		target = "optimus.ai_fix._scrub_literals_for" if fails == "reading-the-key" else "optimus.redaction.scrub_secrets"
		monkeypatch.setattr(target, _raising(RuntimeError("scrub failed")))
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(404, {}, text="")))
		with pytest.raises(ai_fix.AiFixError) as ei:
			ai_fix._call_openai_chat(
				"http://alice:hunter2-pass@llm.internal:11434/v1", "", "m", "s", [{"role": "user", "content": "x"}]
			)
		message = str(ei.value)
		assert "404 (Not Found) for (the configured Base URL). Check that the Model " in message
		assert "alice" not in message and "hunter2-pass" not in message and "llm.internal" not in message
		assert "hunter2-pass" not in logs[0]["message"]

	def test_the_body_is_scrubbed_before_it_is_cut(self, logs, monkeypatch):
		# Cutting first would keep a key prefix the literal no longer matches.
		body = "x" * 290 + KEY + " tail"
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(500, {}, text=body)))
		with pytest.raises(ai_fix.AiFixError) as ei:
			_call()
		assert KEY[:10] not in str(ei.value)
		assert str(ei.value).endswith("x" * 10 + "******** t")

	@pytest.mark.parametrize(
		"old_key",
		['sk-old-0123"quoted\\escaped-XYZ', "sk-old-inflight-lowercase-words"],
		ids=["json-escapable", "lowercase-words"],
	)
	def test_the_key_the_request_used_is_scrubbed_after_a_rotation(self, logs, monkeypatch, old_key):
		# Optimus Settings gets a new key while the request is in flight, and
		# the provider's reply echoes the OLD key (the one the request carried),
		# raw and JSON-escaped. The key read from Settings at log time is the
		# new one, so only the in-flight key can mask the echo. The
		# lowercase-words key has the shape of a provider code, so only the
		# in-flight exclusion keeps it out of provider_error.
		new_key = "sk-new-9876543210ZYXwvu"
		stored = {"key": old_key}
		monkeypatch.setattr(
			"frappe.utils.password.get_decrypted_password", lambda *a, **k: stored["key"], raising=False
		)
		escaped = json.dumps(old_key)[1:-1]
		body = {"error": {"message": f"key {old_key} rejected", "type": "invalid_request_error", "code": old_key}}
		text = json.dumps(body) + f" raw={old_key} escaped={escaped}"

		def _rotated_mid_flight(url, headers=None, json=None, timeout=None, auth=None):  # noqa: A002
			stored["key"] = new_key
			return _Resp(400, body, text=text)

		monkeypatch.setattr(requests, "post", _rotated_mid_flight)
		auth = ai_fix._ApiKeyAuth("authorization", old_key, prefix="Bearer ")
		with pytest.raises(ai_fix.AiFixError) as ei:
			ai_fix._http_post("https://x.invalid/v1/chat/completions", {}, {"model": "m"},
			                  provider="openai", where="chat/completions", auth=auth)
		message, row = str(ei.value), logs[0]["message"]
		assert "raw=******** escaped=********" in message  # the echo reached the message, masked
		for form in (old_key, escaped):
			assert form not in message and form not in row
		assert "provider_error=invalid_request_error\n" in row

	def test_the_in_flight_key_is_never_bound_to_a_local_while_scrubbing(self, logs, monkeypatch):
		# The in-flight key goes from the _ApiKeyAuth straight into the
		# scrub_secrets call: while the scrubber runs, no ai_fix frame holds it
		# in a local other than api_key (the names Frappe and Sentry redact).
		# The reply does not echo it, so any hit comes from the literals.
		from optimus import redaction

		in_flight = "sk-inflight-0123456789ABCdef"
		real_scrub = redaction.scrub_secrets
		scrubbing, offenders = [], []

		def _spy(text, *, literals=()):
			# Record, never assert here: the callers swallow an error from the
			# scrubber (they fall back to "").
			frame = sys._getframe(1)
			while frame is not None:
				if frame.f_code.co_filename == ai_fix.__file__:
					scrubbing.append(frame.f_code.co_name)
					offenders.extend(
						f"{frame.f_code.co_name}: {name}" for name, value in frame.f_locals.items()
						if name != "api_key" and in_flight in repr(value)
					)
				frame = frame.f_back
			return real_scrub(text, literals=literals)

		monkeypatch.setattr(redaction, "scrub_secrets", _spy)
		body = {"error": {"message": "rejected", "type": "invalid_request_error"}}
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(400, body, text=json.dumps(body))))
		with pytest.raises(ai_fix.AiFixError):
			ai_fix._http_post("https://x.invalid/v1/chat/completions", {}, {"model": "m"}, provider="openai",
			                  where="chat/completions", auth=ai_fix._ApiKeyAuth("authorization", in_flight, prefix="Bearer "))
		assert {"_response_detail", "_provider_error_code"} <= set(scrubbing)
		assert offenders == [], f"locals holding the in-flight key while scrubbing: {offenders}"

	def test_a_json_escaped_echo_of_the_stored_key_is_masked(self, logs, monkeypatch):
		# A JSON body (or a JSON-encoded message) holds the key with its quotes
		# and backslashes escaped: the raw literal alone would miss it.
		quoted_key = 'sk-live-0123"quoted\\escaped-XYZ'
		escaped = json.dumps(quoted_key)[1:-1]
		monkeypatch.setattr("frappe.utils.password.get_decrypted_password", lambda *a, **k: quoted_key, raising=False)
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(500, {}, text=f'{{"error": "bad key {escaped}"}}')))
		with pytest.raises(ai_fix.AiFixError) as ei:
			_call()
		assert escaped not in str(ei.value) and 'bad key ********"' in str(ei.value)
		ai_fix.log_ai_failure("optimus ai backfill", ai_fix.AiFixError(f"echo {escaped} and {quoted_key}"))
		row = logs[-1]["message"]
		assert escaped not in row and quoted_key not in row
		assert "echo ******** and ********" in row

	@pytest.mark.parametrize(
		("status", "body", "expected"),
		[
			(400, {"error": {"message": "m", "type": "invalid_request_error", "code": "context_length_exceeded"}},
			 "invalid_request_error:context_length_exceeded"),
			(422, {"error": {"message": "m", "type": "invalid_request_error", "code": None}}, "invalid_request_error"),
			(429, {"error": {"message": "m", "type": "insufficient_quota", "code": "insufficient_quota"}},
			 "insufficient_quota"),
			(529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}, "overloaded_error"),
			(500, {"error": {"type": "server_error", "code": "a" * 60}}, "server_error"),
		],
		ids=["openai-type-and-code", "openai-type-only", "openai-429", "anthropic", "joined-too-long"],
	)
	def test_http_row_names_the_provider_error_code(self, logs, monkeypatch, status, body, expected):
		# The body stays out of the row (it can echo the prompt), so without the
		# provider's own error identifier a 400/422/5xx row says nothing about why.
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(status, body, text=json.dumps(body))))
		with pytest.raises(ai_fix.AiFixError):
			_call()
		assert f"provider_error={expected}\n" in logs[0]["message"]
		assert f"status={status}" in logs[0]["message"]

	@pytest.mark.parametrize(
		"error",
		[
			{"type": KEY, "code": "prompt SELECT name FROM `tabCustomer` WHERE email = 'alice@example.com'"},
			{"type": "invalid request", "code": "alice@example.com"},
			{"type": "x" * 65, "code": ["list"]},
			{"type": 400, "code": {"nested": "dict"}},
			"a plain string error",
		],
		ids=["echoed-key-and-prompt", "not-identifiers", "too-long-and-list", "non-strings", "not-an-object"],
	)
	def test_a_provider_error_code_is_kept_only_when_identifier_shaped(self, logs, monkeypatch, error):
		body = {"error": error}
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(400, body, text=json.dumps(body))))
		with pytest.raises(ai_fix.AiFixError):
			_call()
		msg = logs[0]["message"]
		assert "provider_error" not in msg
		assert KEY not in msg and "alice@example.com" not in msg and "tabCustomer" not in msg

	@pytest.mark.parametrize(
		"code",
		[
			"invalid_request_error", "rate_limit_exceeded", "authentication_error", "overloaded_error",
			"insufficient_quota", "model_not_found", "context_length_exceeded",
			"a" * 30 + "." + "b" * 33,  # 64 characters: the longest kept
		],
	)
	def test_a_lowercase_word_code_is_kept(self, logs, code):
		# Every real provider code is lowercase-letter words joined by _ . : -
		assert ai_fix._provider_error_code(_Resp(400, {"error": {"type": code}})) == code

	@pytest.mark.parametrize(
		"value",
		[
			"sk-proj-Ab3dE9fGh2IjKlMnOpQ7",
			"sk-ant-api03-abcdefghijklmnop",
			"AIzaSyDabcdefghijklmnopqrstuvwxyz",
			"Invalid_Request_Error",
			"invalid_request_error_2",
			"",
			"a" * 65,
		],
		ids=["openai-key", "anthropic-key", "google-key", "mixed-case", "digit", "empty", "65-chars"],
	)
	def test_a_key_shape_is_never_a_provider_error_code(self, logs, value):
		# None of these is the stored key, so only the shape rule can drop them:
		# the key shapes carry a digit or an upper-case letter, as most real
		# keys do. A key made only of lowercase words passes the shape rule; the
		# literal check drops it (test_the_key_the_request_used_is_scrubbed_after_a_rotation).
		assert value != KEY
		assert ai_fix._provider_error_code(_Resp(400, {"error": {"type": value, "code": value}})) == ""

	def test_an_echoed_key_code_is_dropped_but_the_type_is_kept(self, logs, monkeypatch):
		body = {"error": {"message": f"key {KEY} for alice@example.com", "type": "invalid_request_error", "code": KEY}}
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(401, body, text=json.dumps(body))))
		with pytest.raises(ai_fix.AiFixError):
			_call()
		msg = logs[0]["message"]
		assert "provider_error=invalid_request_error\n" in msg
		assert KEY not in msg and "alice@example.com" not in msg

	def test_http_error_row_never_contains_the_response_body(self, logs, monkeypatch):
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(500, {}, text="RESPONSE-BODY-MARKER")))
		with pytest.raises(ai_fix.AiFixError) as ei:
			_call()
		assert "RESPONSE-BODY-MARKER" in str(ei.value)  # still surfaced to the operator
		assert "RESPONSE-BODY-MARKER" not in logs[0]["message"]
		assert "status=500" in logs[0]["message"]

	@pytest.mark.parametrize("status", [300, 302, 307, 399])
	def test_a_redirect_with_a_json_object_body_is_a_bad_response_logged_without_its_body(
		self, logs, monkeypatch, status,
	):
		# requests follows a redirect it can; one that reaches here (no
		# Location header, or a status it does not follow) is not the
		# provider's reply, even with a JSON object body. The body stays out
		# of the message and the row (the caller's row too:
		# test_the_callers_row_never_holds_the_reply_when_the_http_row_failed).
		pii = "pii.redirect@example.com"
		body = {
			"choices": [{"message": {"content": f"REDIRECT-BODY {pii}"}}],
			"error": {"type": "moved_permanently"},
		}
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(status, body, text=json.dumps(body))))
		with pytest.raises(ai_fix.AiFixError) as ei:
			_call()
		assert ei.value.kind == "bad_response" and ei.value.status_code == status
		assert ei.value.__context__ is None
		assert f"(HTTP {status})" in str(ei.value)
		assert len(logs) == 1
		row = logs[0]["message"]
		assert f"status={status}" in row and "provider_error=moved_permanently\n" in row
		for text in (str(ei.value), row):
			assert "REDIRECT-BODY" not in text and pii not in text
		assert getattr(ei.value, ai_fix._LOG_TEXT_ATTR) == (
			f"HTTP {status} from the AI provider (where=chat/completions, provider_error=moved_permanently)"
		)
		# the HTTP layer's row was written, so the caller's log is a no-op
		assert ai_fix.log_ai_failure("optimus ai backfill", ei.value) is False
		assert len(logs) == 1

	@pytest.mark.parametrize("status", [200, 201, 299])
	def test_a_2xx_json_object_is_the_reply(self, logs, monkeypatch, status):
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(status, {"ok": 1})))
		assert _call() == {"ok": 1}
		assert logs == []

	@pytest.mark.parametrize("payload", [["a", "list"], "a string", 42, None])
	def test_non_object_json_is_a_bad_response(self, logs, monkeypatch, payload):
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(200, payload)))
		with pytest.raises(ai_fix.AiFixError) as ei:
			_call()
		assert ei.value.kind == "bad_response"
		assert len(logs) == 1

	def test_non_json_body_is_a_bad_response(self, logs, monkeypatch):
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(200, ValueError("not json"))))
		with pytest.raises(ai_fix.AiFixError, match="non-JSON") as ei:
			_call()
		assert ei.value.kind == "bad_response" and ei.value.__context__ is None

	def test_a_failed_http_row_leaves_the_error_unmarked_so_the_caller_logs_it(self, logs, monkeypatch, breadcrumbs):
		# The HTTP layer's own row could not be written: marking the error
		# logged anyway would make the caller's log_ai_failure a no-op and the
		# failure would leave no row at all.
		import frappe

		attempts = []

		def _fails_once(**kw):
			attempts.append(kw["title"])
			if len(attempts) == 1:
				raise RuntimeError("Error Log insert failed")
			logs.append(kw)

		monkeypatch.setattr(frappe, "log_error", _fails_once, raising=False)
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(500, {}, text="upstream down")))
		with pytest.raises(ai_fix.AiFixError) as ei:
			_call()
		assert not getattr(ei.value, ai_fix._LOGGED_ATTR, False)
		assert ai_fix.log_ai_failure("optimus ai backfill", ei.value) is True
		assert attempts == ["optimus ai_fix", "optimus ai backfill"]
		assert [r["title"] for r in logs] == ["optimus ai backfill"]

	@pytest.mark.parametrize("status", [302, 307, 400, 401, 403, 404, 422, 429, 500])
	def test_the_callers_row_never_holds_the_reply_when_the_http_row_failed(
		self, logs, monkeypatch, breadcrumbs, status,
	):
		# The HTTP layer's own row could not be written, so the caller logs the
		# error itself. Its message carries the provider's reply to the
		# operator, and the reply can echo the prompt: the caller's row names
		# the status, the call site and the provider's error code instead, and
		# keeps the plain frames.
		import frappe

		attempts = []

		def _fails_once(**kw):
			attempts.append(kw["title"])
			if len(attempts) == 1:
				raise RuntimeError("Error Log insert failed")
			logs.append(kw)

		monkeypatch.setattr(frappe, "log_error", _fails_once, raising=False)
		pii = "pii.reply@example.com"
		body = {"error": {"message": f"REPLY-BODY: you asked about {pii}", "type": "invalid_request_error"}}
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(status, body, text=json.dumps(body))))
		with pytest.raises(ai_fix.AiFixError) as ei:
			_call()
		if status >= 400 and status not in (401, 403, 429):
			assert f"REPLY-BODY: you asked about {pii}" in str(ei.value)  # the operator's message is unchanged
		assert ai_fix.log_ai_failure("optimus ai backfill", ei.value) is True
		assert attempts == ["optimus ai_fix", "optimus ai backfill"]
		row = logs[0]["message"]
		assert pii not in row and "REPLY-BODY" not in row and "you asked" not in row
		assert f"HTTP {status} " in row
		assert "where=chat/completions" in row and "provider_error=invalid_request_error" in row
		assert "Traceback (most recent call last):" in row and ", in _http_post\n" in row  # the plain frames stay

	def test_http_row_references_the_marked_session(self, logs, monkeypatch):
		import frappe

		monkeypatch.setattr(frappe.local, "_optimus_spend_session", "uuid-9", raising=False)
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(401, {})))
		with pytest.raises(ai_fix.AiFixError):
			_call()
		assert logs[0]["reference_name"] == "SESS-0001"
		assert "session_uuid=uuid-9" in logs[0]["message"]


class TestNothingIsLoggedOrSentWhileAnExceptionIsActive:
	"""``frappe.log_error`` calls Sentry's ``capture_exception``, which ships
	the ACTIVE exception's frame locals (requests / urllib3 frames hold the
	prepared headers, i.e. the key), and a ``raise`` inside an ``except``
	chains that exception as ``__context__``. The HTTP layer therefore logs
	and raises after its ``try``, and the OpenAI temperature retry (a second
	request, which logs its own failure) is sent after its ``try`` too."""

	_TEMPERATURE_400 = '{"error":{"message":"invalid temperature: only 1 is allowed for this model"}}'

	@pytest.fixture
	def active_at_log(self, logs, monkeypatch):
		"""The exception being handled at each ``frappe.log_error`` call."""
		import sys

		import frappe

		seen = []

		def _log(**kw):
			seen.append(sys.exc_info()[1])
			logs.append(kw)

		monkeypatch.setattr(frappe, "log_error", _log, raising=False)
		return seen

	@staticmethod
	def _sequence(*results):
		"""Successive ``requests.post`` results; records the exception being
		handled when each request is sent."""
		import sys

		it = iter(results)
		active_at_send = []

		def _fake(url, headers=None, json=None, timeout=None, auth=None):  # noqa: A002
			active_at_send.append(sys.exc_info()[1])
			result = next(it)
			if isinstance(result, BaseException):
				raise result
			return result

		_fake.active_at_send = active_at_send
		return _fake

	@pytest.mark.parametrize(
		"result",
		[
			requests.exceptions.Timeout("read timed out"),
			requests.exceptions.ConnectionError("refused"),
			UnicodeEncodeError("latin-1", "Bearer x’", 8, 9, "ordinal not in range(256)"),
			_Resp(500, {}, text="upstream down"),
			_Resp(200, ValueError("not json")),
			_Resp(200, ["a", "list"]),
		],
		ids=["timeout", "transport", "catch-all", "http-500", "non-json", "non-object"],
	)
	def test_http_failures_are_logged_and_raised_with_no_active_exception(
		self, active_at_log, monkeypatch, result
	):
		monkeypatch.setattr(requests, "post", self._sequence(result))
		with pytest.raises(ai_fix.AiFixError) as ei:
			_call()
		assert active_at_log == [None]
		assert ei.value.__context__ is None and ei.value.__cause__ is None

	def test_the_temperature_retry_is_sent_with_no_active_exception(self, logs, monkeypatch):
		ok = {"choices": [{"message": {"content": "done"}}]}
		fake = self._sequence(_Resp(400, {}, text=self._TEMPERATURE_400), _Resp(200, ok))
		monkeypatch.setattr(requests, "post", fake)
		text = ai_fix._call_openai_chat(
			"https://x.invalid/v1", "", "kimi-k2", "s", [{"role": "user", "content": "x"}]
		)
		assert text == "done"
		assert fake.active_at_send == [None, None]

	def test_a_failed_temperature_retry_is_logged_and_raised_unchained(self, active_at_log, monkeypatch):
		fake = self._sequence(
			_Resp(400, {}, text=self._TEMPERATURE_400), _Resp(500, {}, text="upstream down")
		)
		monkeypatch.setattr(requests, "post", fake)
		with pytest.raises(ai_fix.AiFixError) as ei:
			ai_fix._call_openai_chat(
				"https://x.invalid/v1", "", "kimi-k2", "s", [{"role": "user", "content": "x"}]
			)
		assert ei.value.status_code == 500
		assert ei.value.__context__ is None and ei.value.__cause__ is None
		# one row per failed attempt, each written with no exception active
		assert active_at_log == [None, None]


# ---------------------------------------------------------------------------
# The never-raise helpers of the key read, the Error Log path and the HTTP
# failure path never swallow an RQ job timeout (other best-effort helpers in
# ai_fix.py, such as the spend recorder and the config readers, are not
# covered here)
# ---------------------------------------------------------------------------

class _JobTimeout(Exception):
	"""Stands in for rq's ``JobTimeoutException``: an ``Exception`` subclass
	raised asynchronously (SIGALRM), so it can land inside any helper."""


_TIMEOUT_TEXT = "Task exceeded maximum timeout value (60 seconds)"


@pytest.fixture
def job_timeout(monkeypatch):
	monkeypatch.setattr(ai_fix, "_job_timeout_types", lambda: (_JobTimeout,))
	return _JobTimeout(_TIMEOUT_TEXT)


def _raising(exc, holds=None):
	"""A callable that raises ``exc`` while a local holds ``holds`` (what the
	interrupted frame held: decrypted key bytes, unscrubbed text)."""
	def _raise(*a, **k):
		held = holds  # noqa: F841
		raise exc
	return _raise


def _assert_fresh_and_clean(ei, original, *raisers):
	"""The timeout still leaves (the job must stop), as a fresh instance of
	its own type with no chain, no frame of the code it interrupted and no
	local holding the key or unscrubbed text on its way out."""
	assert type(ei.value) is _JobTimeout
	assert ei.value is not original and ei.value.args == (_TIMEOUT_TEXT,)
	assert ei.value.__context__ is None and ei.value.__cause__ is None
	codes = set()
	tb = ei.value.__traceback__
	while tb is not None:
		codes.add(tb.tb_frame.f_code)
		for name, value in tb.tb_frame.f_locals.items():
			if name == "api_key":  # redacted by name by Frappe and Sentry
				continue
			assert KEY not in repr(value), f"{tb.tb_frame.f_code.co_name}: {name} holds the key"
			assert "UNSCRUBBED" not in repr(value), f"{tb.tb_frame.f_code.co_name}: {name} holds unscrubbed text"
		tb = tb.tb_next
	for raiser in raisers:
		assert raiser.__code__ not in codes


class TestAJobTimeoutIsNeverSwallowed:
	"""RQ raises ``JobTimeoutException`` from a SIGALRM handler, wherever the
	job happens to be. A helper that swallows it lets the job overrun, and
	``_current_key_or_empty`` would answer "" and send the request
	unauthenticated. Each helper tested here lets it through as a fresh
	instance raised after its ``try``, so the frames it interrupted (Fernet's
	decrypt frames hold the key bytes; the scrubber's hold the unscrubbed
	text) never reach ``execute_job``'s with-context log."""

	def test_current_key_or_empty(self, job_timeout, monkeypatch):
		raiser = _raising(job_timeout, holds=KEY.encode())
		monkeypatch.setattr("frappe.utils.password.get_decrypted_password", raiser, raising=False)
		with pytest.raises(_JobTimeout) as ei:
			ai_fix._current_key_or_empty()
		_assert_fresh_and_clean(ei, job_timeout, raiser)

	def test_current_key_or_empty_with_the_real_rq_timeout(self, monkeypatch):
		timeouts = pytest.importorskip("rq.timeouts")
		original = timeouts.JobTimeoutException(_TIMEOUT_TEXT)
		monkeypatch.setattr("frappe.utils.password.get_decrypted_password", _raising(original), raising=False)
		with pytest.raises(timeouts.JobTimeoutException) as ei:
			ai_fix._current_key_or_empty()
		assert ei.value is not original and ei.value.__context__ is None

	def test_scrubbed_message(self, job_timeout, monkeypatch):
		raiser = _raising(job_timeout, holds=f"UNSCRUBBED {KEY}")
		monkeypatch.setattr("optimus.redaction.scrub_secrets", raiser)
		monkeypatch.setattr("frappe.utils.password.get_decrypted_password", lambda *a, **k: KEY, raising=False)
		with pytest.raises(_JobTimeout) as ei:
			ai_fix._scrubbed_message("t", [f"UNSCRUBBED {KEY}"], None)
		_assert_fresh_and_clean(ei, job_timeout, raiser)

	def test_log_ai_failure_while_scrubbing(self, logs, job_timeout, monkeypatch, breadcrumbs):
		raiser = _raising(job_timeout, holds=f"UNSCRUBBED {KEY}")
		monkeypatch.setattr("optimus.redaction.scrub_secrets", raiser)
		failed = ai_fix.AiFixError("x")
		with pytest.raises(_JobTimeout) as ei:
			ai_fix.log_ai_failure("t", failed, finding="F1")
		_assert_fresh_and_clean(ei, job_timeout, raiser)
		assert logs == [] and breadcrumbs == []

	def test_log_ai_failure_while_writing(self, logs, job_timeout, monkeypatch, breadcrumbs):
		import frappe

		raiser = _raising(job_timeout)
		monkeypatch.setattr(frappe, "log_error", raiser, raising=False)
		failed = ai_fix.AiFixError("x")
		with pytest.raises(_JobTimeout) as ei:
			ai_fix.log_ai_failure("t", failed)
		_assert_fresh_and_clean(ei, job_timeout, raiser)
		assert not getattr(failed, ai_fix._LOGGED_ATTR, False)
		assert breadcrumbs == []

	def test_log_ai_failure_while_looking_up_the_session(self, logs, job_timeout, monkeypatch, breadcrumbs):
		import frappe

		db = _FakeDB()
		raiser = _raising(job_timeout)
		db.get_value = raiser
		monkeypatch.setattr(frappe, "db", db, raising=False)
		with pytest.raises(_JobTimeout) as ei:
			ai_fix.log_ai_failure("t", session_uuid="uuid-1")
		_assert_fresh_and_clean(ei, job_timeout, raiser)
		assert logs == [] and breadcrumbs == []

	def test_log_ai_failure_while_registering_the_rollback_callback(self, logs, job_timeout, monkeypatch, breadcrumbs):
		import frappe

		raiser = _raising(job_timeout)
		frappe.db.after_rollback.add = raiser
		with pytest.raises(_JobTimeout) as ei:
			ai_fix.log_ai_failure("t")
		_assert_fresh_and_clean(ei, job_timeout, raiser)
		assert breadcrumbs == []  # a timeout is not a failed write

	def test_the_breadcrumb(self, logs, job_timeout, monkeypatch):
		import frappe

		monkeypatch.setattr(frappe, "log_error", _raising(RuntimeError("insert failed")), raising=False)
		raiser = _raising(job_timeout)
		monkeypatch.setattr(frappe, "logger", raiser, raising=False)
		with pytest.raises(_JobTimeout) as ei:
			ai_fix.log_ai_failure("t")
		_assert_fresh_and_clean(ei, job_timeout, raiser)

	def test_the_rollback_callback(self, logs, job_timeout, monkeypatch):
		import frappe

		raiser = _raising(job_timeout)
		module = types.ModuleType("frappe.deferred_insert")
		module.deferred_insert = raiser
		monkeypatch.setitem(sys.modules, "frappe.deferred_insert", module)
		ai_fix.log_ai_failure("t")
		with pytest.raises(_JobTimeout) as ei:
			frappe.db.rollback()
		_assert_fresh_and_clean(ei, job_timeout, raiser)

	def test_the_rollback_callback_while_checking_the_row(self, logs, job_timeout, monkeypatch, requeued):
		import frappe

		monkeypatch.setattr(frappe, "db", _FakeDB(raise_on_exists=job_timeout), raising=False)
		ai_fix.log_ai_failure("t")
		with pytest.raises(_JobTimeout) as ei:
			frappe.db.rollback()
		_assert_fresh_and_clean(ei, job_timeout, _FakeDB.exists)
		assert requeued == []

	def test_mark_logged(self, job_timeout):
		class _Sticky(Exception):
			def __setattr__(self, name, value):
				raise job_timeout

		with pytest.raises(_JobTimeout) as ei:
			ai_fix._mark_logged(_Sticky("x"))
		_assert_fresh_and_clean(ei, job_timeout, _Sticky.__setattr__)

	def test_log_http_error_while_reading_the_session_marker(self, logs, job_timeout, monkeypatch):
		import frappe

		class _Local:
			def __getattr__(self, name):
				raise job_timeout

		monkeypatch.setattr(frappe, "local", _Local(), raising=False)
		with pytest.raises(_JobTimeout) as ei:
			ai_fix._log_http_error("openai", "chat/completions", 500)
		_assert_fresh_and_clean(ei, job_timeout, _Local.__getattr__)
		assert logs == []

	def test_response_detail(self, job_timeout, monkeypatch):
		raiser = _raising(job_timeout, holds=f"UNSCRUBBED {KEY}")
		monkeypatch.setattr("optimus.redaction.scrub_secrets", raiser)
		monkeypatch.setattr("frappe.utils.password.get_decrypted_password", lambda *a, **k: KEY, raising=False)
		with pytest.raises(_JobTimeout) as ei:
			ai_fix._response_detail(_Resp(400, {}, text=f"UNSCRUBBED echo of {KEY}"))
		_assert_fresh_and_clean(ei, job_timeout, raiser)

	def test_provider_error_code(self, job_timeout, monkeypatch):
		raiser = _raising(job_timeout, holds=f"UNSCRUBBED {KEY}")
		resp = _Resp(400, {}, text="")
		resp.json = raiser
		with pytest.raises(_JobTimeout) as ei:
			ai_fix._provider_error_code(resp)
		_assert_fresh_and_clean(ei, job_timeout, raiser)

	def test_provider_error_code_while_checking_an_echoed_key(self, job_timeout, monkeypatch):
		# The timeout lands while the echoed key (in ``code``) is being checked.
		# Only a code of lowercase-letter words reaches that check, so the
		# stored key here has that shape.
		word_key = "sk-echoed-lowercase-words-only"

		def _scrub(text, literals=()):
			if word_key in text:
				raise job_timeout
			return text

		monkeypatch.setattr("optimus.redaction.scrub_secrets", _scrub)
		monkeypatch.setattr("frappe.utils.password.get_decrypted_password", lambda *a, **k: word_key, raising=False)
		resp = _Resp(400, {"error": {"message": f"UNSCRUBBED {KEY}", "type": "invalid_request_error", "code": word_key}})
		with pytest.raises(_JobTimeout) as ei:
			ai_fix._provider_error_code(resp)
		_assert_fresh_and_clean(ei, job_timeout, _scrub)
		checked = []
		tb = ei.value.__traceback__
		while tb is not None:
			if tb.tb_frame.f_code.co_filename == ai_fix.__file__:
				checked.append(tb.tb_frame.f_code.co_name)
				for name, value in tb.tb_frame.f_locals.items():
					if name != "api_key":  # redacted by name by Frappe and Sentry
						assert word_key not in repr(value), f"{tb.tb_frame.f_code.co_name}: {name} holds the echoed key"
			tb = tb.tb_next
		assert checked == ["_provider_error_code"]

	def test_the_404_url_scrub(self, logs, job_timeout, monkeypatch):
		# The timeout lands inside json.dumps while the key's literals are
		# built for the URL the 404 message names: json's frames hold the key
		# as ``obj`` and ``o``, names no sanitizer redacts. It leaves fresh,
		# and no frame on its way out holds the key under any name. It fires
		# once, as RQ's SIGALRM does: a helper that swallowed it would let
		# the job run on.
		real_encode = json.JSONEncoder.encode
		fired = []

		def _encode(self, o):
			if o == KEY and not fired:
				fired.append(True)
				raise job_timeout
			return real_encode(self, o)

		monkeypatch.setattr(json.JSONEncoder, "encode", _encode)
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(404, {}, text="")))
		with pytest.raises(_JobTimeout) as ei:
			ai_fix._http_post("https://llm.internal/v1/chat/completions", {}, {"model": "m"}, provider="openai",
			                  where="chat/completions", auth=ai_fix._ApiKeyAuth("authorization", KEY, prefix="Bearer "))
		_assert_fresh_and_clean(ei, job_timeout, _encode)
		walked = []
		tb = ei.value.__traceback__
		while tb is not None:
			walked.append(tb.tb_frame.f_code.co_name)
			for name, value in tb.tb_frame.f_locals.items():
				assert KEY not in repr(value), f"{tb.tb_frame.f_code.co_name}: {name} holds the key"
			tb = tb.tb_next
		assert "_http_post" in walked
		assert logs == []

	def test_token_count(self, job_timeout):
		class _Count:
			def __int__(self):
				raise job_timeout

		with pytest.raises(_JobTimeout) as ei:
			ai_fix._usage_from_openai({"usage": {"prompt_tokens": _Count()}})
		_assert_fresh_and_clean(ei, job_timeout, _Count.__int__)


class TestAnInterruptWhileDecryptingTheKey:
	"""A non-``Exception`` interrupt (``SystemExit`` from a gunicorn worker
	timeout, ``KeyboardInterrupt``, a gevent ``Timeout``) can land while
	Frappe decrypts the key, when Fernet's and ``cstr``'s frames hold the
	plaintext in their locals, and Sentry's WSGI middleware ships frame
	locals. ``_current_key_or_empty`` lets it leave as the same instance
	(gevent matches its Timeout by identity), without those frames and
	unchained, as ``_http_post`` does."""

	@pytest.mark.parametrize(
		"interrupt",
		[SystemExit(1), KeyboardInterrupt(), type("GreenletTimeout", (BaseException,), {})(5)],
		ids=["worker-timeout-SystemExit", "KeyboardInterrupt", "gevent-Timeout"],
	)
	def test_it_leaves_as_the_same_instance_without_the_decrypt_frames(self, monkeypatch, interrupt):
		def _decrypt(*a, **k):
			plaintext = KEY.encode()  # noqa: F841 what Fernet's frame holds
			try:
				raise UnicodeDecodeError("utf-8", KEY.encode(), 0, 1, "invalid start byte")
			except UnicodeDecodeError:
				raise interrupt  # noqa: B904 (chained to a key-bearing error on purpose)

		monkeypatch.setattr("frappe.utils.password.get_decrypted_password", _decrypt, raising=False)
		with pytest.raises(BaseException) as ei:
			ai_fix._current_key_or_empty()
		assert ei.value is interrupt
		assert ei.value.__context__ is None and ei.value.__cause__ is None
		assert ei.value.__suppress_context__ is True
		codes = set()
		tb = ei.value.__traceback__
		while tb is not None:
			codes.add(tb.tb_frame.f_code)
			for name, value in tb.tb_frame.f_locals.items():
				assert KEY not in repr(value), f"{tb.tb_frame.f_code.co_name}: {name} holds the key"
			tb = tb.tb_next
		assert _decrypt.__code__ not in codes
		assert ai_fix._current_key_or_empty.__code__ in codes

	def test_any_other_error_still_answers_empty(self, monkeypatch):
		monkeypatch.setattr(
			"frappe.utils.password.get_decrypted_password", _raising(RuntimeError("bad token")), raising=False
		)
		assert ai_fix._current_key_or_empty() == ""


def _real_reply(status_code: int, body: str) -> requests.Response:
	"""The provider's reply as a REAL ``requests.Response``, so a frame that
	holds it renders as Frappe's formatter and Sentry render it
	(``<Response [400]>``); only a local bound to its text or its parsed JSON
	shows the body."""
	resp = requests.Response()
	resp.status_code = status_code
	resp._content = body.encode("utf-8")
	resp.encoding = "utf-8"
	return resp


class TestAnInterruptWhileReadingTheKeyForAReply:
	"""``_response_detail`` and ``_provider_error_code`` read the stored key
	(a database read, where a gunicorn worker timeout's ``SystemExit`` can
	land) BEFORE they bind the provider's reply, which can echo the key.
	Nothing there catches an interrupt that is not an ``Exception``, so it
	leaves with their frames, and Sentry's WSGI middleware ships frame
	locals: none of them may hold the reply yet."""

	@pytest.mark.parametrize("reader", ["_response_detail", "_provider_error_code"])
	def test_no_frame_holds_the_echoed_key(self, monkeypatch, reader):
		interrupt = SystemExit(1)

		def _decrypt(*a, **k):
			raise interrupt

		monkeypatch.setattr("frappe.utils.password.get_decrypted_password", _decrypt, raising=False)
		resp = _real_reply(400, json.dumps({
			"error": {"message": f"invalid key {KEY}", "type": "invalid_request_error", "code": KEY},
		}))
		with pytest.raises(SystemExit) as ei:
			getattr(ai_fix, reader)(resp, ai_fix._ApiKeyAuth("authorization", KEY, prefix="Bearer "))
		assert ei.value is interrupt
		walked = []
		tb = ei.value.__traceback__
		while tb is not None:
			walked.append(tb.tb_frame.f_code.co_name)
			for name, value in tb.tb_frame.f_locals.items():
				assert KEY not in repr(value), f"{tb.tb_frame.f_code.co_name}: {name} holds the echoed key"
			tb = tb.tb_next
		assert reader in walked and "_current_key_or_empty" in walked


# ---------------------------------------------------------------------------
# analyze.py / api.py call sites: one row per failure, with a session reference
# ---------------------------------------------------------------------------

def _backfill_env(monkeypatch):
	import frappe

	from optimus import analyze
	from optimus import settings as _settings

	monkeypatch.setattr(analyze, "frappe", frappe)
	monkeypatch.setattr(frappe.local, "_optimus_spend_session", None, raising=False)
	cfg = _settings.OptimusConfig(ai_enabled=True, ai_provider="OpenAI")
	monkeypatch.setattr("optimus.settings.get_config", lambda: cfg)
	monkeypatch.setattr(analyze, "_ai_payload_for_finding",
	                    lambda *a, **k: {"finding_type": "N+1 Query", "title": "t", "technical_detail": {}})
	monkeypatch.setattr(analyze, "_phase2_index_for", lambda *a, **k: {})
	rows = [
		SimpleNamespace(name=n, finding_type="N+1 Query", severity="High", estimated_impact_ms=1, llm_fix_json=None)
		for n in ("F1", "F2")
	]
	return analyze, SimpleNamespace(session_uuid="uuid-7", findings=rows)


class TestCallSitesLogOnce:
	def test_http_failure_during_backfill_writes_one_referenced_row_each(self, logs, monkeypatch):
		analyze, doc = _backfill_env(monkeypatch)
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(500, {}, text="upstream down")))
		out = analyze._run_ai_backfill(doc, cap=0)
		assert out["failed"] == 2
		assert len(logs) == 2  # K16: one row per failure, not HTTP layer + caller
		assert {r["title"] for r in logs} == {"optimus ai_fix"}
		assert all(r["reference_name"] == "SESS-0001" for r in logs)
		assert all("session_uuid=uuid-7" in r["message"] for r in logs)

	def test_non_http_failure_is_logged_by_the_caller(self, logs, monkeypatch):
		analyze, doc = _backfill_env(monkeypatch)
		empty = {"choices": [{"message": {"content": "   "}}]}
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(200, empty)))
		analyze._run_ai_backfill(doc, cap=0)
		assert [r["title"] for r in logs] == ["optimus ai backfill", "optimus ai backfill"]
		assert "finding=F1" in logs[0]["message"] and "empty response" in logs[0]["message"]
		assert logs[0]["reference_name"] == "SESS-0001"


class TestLogAiStepFailure:
	"""``analyze._log_ai_step_failure``: how ``analyze.run`` logs a failed AI
	step (``run`` itself must not reference ai_fix names)."""

	def test_threads_the_title_and_the_session(self, monkeypatch):
		from optimus import analyze

		calls = []
		monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **k: calls.append((a, k)) or True)
		error = RuntimeError("step broke")
		analyze._log_ai_step_failure("optimus ai auto-suggest (outer)", error, "uuid-3")
		assert calls == [(("optimus ai auto-suggest (outer)", error), {"session_uuid": "uuid-3"})]

	def test_writes_one_referenced_row(self, logs):
		from optimus import analyze

		analyze._log_ai_step_failure("optimus ai index-suggest (outer)", RuntimeError("step broke"), "uuid-3")
		assert len(logs) == 1
		assert logs[0]["title"] == "optimus ai index-suggest (outer)"
		assert logs[0]["reference_name"] == "SESS-0001"
		assert "session_uuid=uuid-3" in logs[0]["message"]
		assert "RuntimeError: step broke" in logs[0]["message"]

	def test_never_raises(self, logs, monkeypatch, breadcrumbs):
		import frappe

		from optimus import analyze

		monkeypatch.setattr(frappe, "log_error", _raising(RuntimeError("Error Log insert failed")), raising=False)
		monkeypatch.setattr(frappe, "db", _FakeDB(raise_on_get=True), raising=False)
		analyze._log_ai_step_failure("t", RuntimeError("x"), "uuid-3")  # must not raise

	def test_a_job_timeout_propagates_as_a_fresh_instance(self, logs, job_timeout, monkeypatch):
		# The one exception to "never raises": the job must still stop, and
		# run()'s outer handler re-raises it.
		import frappe

		from optimus import analyze

		raiser = _raising(job_timeout)
		monkeypatch.setattr(frappe, "log_error", raiser, raising=False)
		with pytest.raises(_JobTimeout) as ei:
			analyze._log_ai_step_failure("optimus ai auto-suggest (outer)", RuntimeError("x"), "uuid-3")
		_assert_fresh_and_clean(ei, job_timeout, raiser)


@pytest.fixture
def run_env(monkeypatch):
	"""The smallest set of fakes ``analyze.run`` needs for one pass over one
	recording with no analyzers. Each stubbed step records ``run``'s
	``ai_error`` local at the moment it is called, in ``trail`` next to the
	``log_ai_failure`` calls, so a test sees whether the logged error is
	still bound afterwards. ``status`` holds the session status writes, a
	rollback and any non-AI ``frappe.log_error``."""
	import frappe

	from optimus import analyze

	trail, status = [], []

	def _step(name):
		def _fn(*a, **k):
			caller = sys._getframe(1)
			if caller.f_code is analyze.run.__code__:
				trail.append(("call", name, caller.f_locals.get("ai_error")))
		return _fn

	class _DB:
		def get_value(self, *a, **k):
			return "SESS-RUN"

		def set_value(self, doctype, name, field, value=None, *a, **k):
			if field == "status":
				status.append(value)

		def rollback(self, *a, **k):
			status.append("rollback")

	monkeypatch.setattr(analyze, "frappe", frappe)
	monkeypatch.setattr(frappe, "db", _DB(), raising=False)
	monkeypatch.setattr(frappe, "conf", {"optimus_analyze_gc_collect": False}, raising=False)
	monkeypatch.setattr(frappe, "cache", SimpleNamespace(get_value=lambda *a, **k: None), raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda *a, **k: status.append("non-AI log_error"), raising=False)
	monkeypatch.setattr(analyze, "is_scheduler_disabled", lambda: True)
	monkeypatch.setattr(analyze, "safe_commit", lambda: None)
	monkeypatch.setattr(analyze, "session", SimpleNamespace(
		get_recordings=lambda *a, **k: ["rec-1"], get_session_meta=lambda *a, **k: {},
		delete_session_state=lambda *a, **k: None,
	))
	monkeypatch.setattr(analyze, "_bg_wait_for_pending_jobs", lambda *a, **k: 0)
	monkeypatch.setattr(analyze, "_acquire_singleflight", lambda *a, **k: True)
	monkeypatch.setattr(analyze, "_fetch_recordings", lambda *a, **k: iter([{"uuid": "rec-1"}]))
	monkeypatch.setattr(analyze, "_enrich_recordings", lambda *a, **k: [])
	monkeypatch.setattr(analyze, "_get_analyzers", lambda: [])
	monkeypatch.setattr("optimus.api._read_frontend_data", lambda *a, **k: {"xhr": [], "vitals": []})
	for name in (
		"_touch_singleflight", "_release_singleflight", "_publish_session_event", "_publish_progress",
		"_mark_ai_spend_session", "_enrich_findings_with_source_snippets",
		"_enrich_findings_with_ai_suggestions", "_enrich_table_breakdown_with_ai_suggestions",
		"_persist", "_render_and_attach_reports", "_persist_recordings_file", "_cleanup_redis",
		"_auto_arm_phase2",
	):
		monkeypatch.setattr(analyze, name, _step(name))
	monkeypatch.setattr(
		ai_fix, "log_ai_failure", lambda title, exc=None, **kw: trail.append(("log", title, exc, kw)) or True
	)
	return SimpleNamespace(analyze=analyze, trail=trail, status=status)


class TestRunLogsAFailedAiStepAndCarriesOn:
	"""``analyze.run`` with one AI step raising: the analyze still completes,
	the failure is logged once through ``log_ai_failure`` with the step's
	outer title, and the error is unbound right after (a later non-AI
	failure is logged by ``run``'s outer handler with frame locals, and a
	prompt builder's error can carry prompt text)."""

	@pytest.mark.parametrize(
		("step", "title"),
		[
			("_enrich_findings_with_ai_suggestions", "optimus ai auto-suggest (outer)"),
			("_enrich_table_breakdown_with_ai_suggestions", "optimus ai index-suggest (outer)"),
		],
		ids=["auto-suggest", "index-suggest"],
	)
	def test_the_step_is_logged_once_and_unbound(self, run_env, monkeypatch, step, title):
		error = RuntimeError("PROMPT-TEXT step broke")
		monkeypatch.setattr(run_env.analyze, step, _raising(error))
		assert run_env.analyze.run("uuid-run") is None
		assert run_env.status == ["Analyzing", "Ready"]  # completed: no rollback, no failure row
		logged = [entry for entry in run_env.trail if entry[0] == "log"]
		assert logged == [("log", title, error, {"session_uuid": "uuid-run"})]
		after = run_env.trail[run_env.trail.index(logged[0]) + 1:]
		assert after, "run() called nothing after logging the step: the unbinding went unchecked"
		assert all(entry[2] is None for entry in after), f"the logged AI error was still bound: {after[0]}"


# ---------------------------------------------------------------------------
# api.py log sites: each failure is logged with its title and session, and the
# endpoint still returns normally
# ---------------------------------------------------------------------------

class _ApiDB:
	"""``frappe.db`` for the api.py paths below."""

	def __init__(self, set_value_error=None):
		self.set_value_error = set_value_error
		self.writes = []

	def get_value(self, doctype, filters=None, fieldname=None, *a, as_dict=False, **k):
		if as_dict:
			return {"name": "SESS-0001", "user": "Administrator", "status": "Ready"}
		return "SESS-0001"

	def set_value(self, *a, **k):
		if self.set_value_error is not None:
			raise self.set_value_error
		self.writes.append(a)


@pytest.fixture
def api_env(monkeypatch):
	"""Fake the gates and data the api.py AI paths read; capture every
	``log_ai_failure`` call as ``(title, exc, kwargs)``."""
	import inspect

	import frappe

	from optimus import analyze, api
	from optimus import settings as _settings

	calls = []
	monkeypatch.setattr(api, "frappe", frappe)
	monkeypatch.setattr(analyze, "frappe", frappe)
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda title, exc=None, **kw: calls.append((title, exc, kw)) or True)
	monkeypatch.setattr(api, "_require_profiler_user", lambda: "Administrator")
	monkeypatch.setattr(api, "_require_session_permission", lambda *a, **k: "SESS-0001")
	monkeypatch.setattr(frappe, "get_roles", lambda *a, **k: ["System Manager"], raising=False)
	monkeypatch.setattr(frappe, "db", _ApiDB(), raising=False)
	monkeypatch.setattr(frappe.local, "_optimus_spend_session", None, raising=False)
	finding = SimpleNamespace(name="FIND-1", finding_type="N+1 Query", llm_fix_json=None, action_ref="")
	doc = SimpleNamespace(
		name="SESS-0001", session_uuid="uuid-5", findings=[finding],
		actions=[SimpleNamespace(recording_uuid="rec-1", idx=1)],
	)
	monkeypatch.setattr(frappe, "get_doc", lambda *a, **k: doc, raising=False)
	cfg = _settings.OptimusConfig(ai_enabled=True, ai_provider="OpenAI", ai_suggest_findings=True)
	monkeypatch.setattr("optimus.settings.get_config", lambda: cfg)
	monkeypatch.setattr(analyze, "_load_recordings_bundle", lambda *a, **k: None)
	monkeypatch.setattr(analyze, "_fetch_recordings", lambda *a, **k: [])
	monkeypatch.setattr(analyze, "_backfill_ai_suggestions", lambda *a, **k: None)
	monkeypatch.setattr(analyze, "_render_and_attach_reports", lambda *a, **k: None)
	monkeypatch.setattr(analyze, "_ai_payload_for_finding", lambda *a, **k: {"finding_type": "N+1 Query"})
	monkeypatch.setattr(analyze, "_phase2_index_for", lambda *a, **k: {})
	monkeypatch.setattr("optimus.pdf_export.clear_cached_pdf", lambda *a, **k: None)
	monkeypatch.setattr(ai_fix, "is_available", lambda *a, **k: True)
	monkeypatch.setattr(ai_fix, "suggest_fix", lambda payload: {"suggestion": "batch it", "model": "m"})
	return SimpleNamespace(
		calls=calls, doc=doc, api=api, analyze=analyze,
		regenerate_reports=inspect.unwrap(api.regenerate_reports),
		suggest_fix=inspect.unwrap(api.suggest_fix),
	)


class TestApiLogSites:
	def test_regenerate_reports_fetch_error(self, api_env, monkeypatch):
		error = RuntimeError("redis down")
		monkeypatch.setattr(api_env.analyze, "_fetch_recordings", _raising(error))
		out = api_env.regenerate_reports("uuid-5")
		assert api_env.calls == [("optimus regenerate_reports fetch", error, {"session_uuid": "uuid-5"})]
		assert out["regenerated"] is True and out["recordings_available"] == 0

	def test_regenerate_reports_backfill_error(self, api_env, monkeypatch):
		error = RuntimeError("backfill broke")
		monkeypatch.setattr(api_env.analyze, "_backfill_ai_suggestions", _raising(error))
		out = api_env.regenerate_reports("uuid-5")
		assert api_env.calls == [("optimus regenerate ai backfill", error, {"session_uuid": "uuid-5"})]
		assert out["regenerated"] is True

	def test_suggest_fix_persist_error_on_set_value(self, api_env, monkeypatch):
		import frappe

		error = RuntimeError("write failed")
		monkeypatch.setattr(frappe, "db", _ApiDB(set_value_error=error), raising=False)
		monkeypatch.setattr(api_env.api, "safe_commit", lambda: None)
		out = api_env.suggest_fix("uuid-5", "FIND-1")
		assert api_env.calls == [
			("optimus suggest_fix persist", error, {"session_uuid": "uuid-5", "finding": "FIND-1"}),
		]
		assert out["ok"] is True and out["cached"] is False and out["suggestion"] == "batch it"

	def test_suggest_fix_persist_error_on_commit(self, api_env, monkeypatch):
		error = RuntimeError("commit failed")
		monkeypatch.setattr(api_env.api, "safe_commit", _raising(error))
		out = api_env.suggest_fix("uuid-5", "FIND-1")
		assert api_env.calls == [
			("optimus suggest_fix persist", error, {"session_uuid": "uuid-5", "finding": "FIND-1"}),
		]
		assert out["ok"] is True and out["suggestion"] == "batch it"

	def test_humanize_steps_core_fetch_error(self, api_env, monkeypatch):
		error = RuntimeError("redis down")
		monkeypatch.setattr(api_env.analyze, "_fetch_recordings", _raising(error))
		out = api_env.api._humanize_steps_core(api_env.doc, title="t")
		assert api_env.calls == [("optimus humanize_steps fetch", error, {"session_uuid": "uuid-5"})]
		assert out["updated"] is False and out["reason"]
