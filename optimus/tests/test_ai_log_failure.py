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
	``rollback`` runs them (a savepoint rollback runs none), as Frappe does."""

	def __init__(self, docname="SESS-0001", raise_on_get=False):
		self.docname = docname
		self.raise_on_get = raise_on_get
		self.lookups = 0
		self.after_rollback = _Callbacks()

	def get_value(self, doctype, filters, field):
		self.lookups += 1
		if self.raise_on_get:
			raise RuntimeError("db down")
		return self.docname

	def commit(self):
		self.after_rollback.reset()

	def rollback(self, *, save_point=None, chain=False):
		if not save_point:
			self.after_rollback.run()


class _Flags(dict):
	"""``frappe.flags`` is a ``frappe._dict``: a flag never set reads as None."""

	__getattr__ = dict.get


@pytest.fixture
def logs(monkeypatch):
	"""Capture frappe.log_error calls; store a key; fake the DB."""
	import frappe

	calls = []
	monkeypatch.setattr(frappe, "log_error", lambda **kw: calls.append(kw), raising=False)
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


@pytest.fixture
def breadcrumbs(monkeypatch):
	"""Capture ``frappe.logger(...).warning`` calls as ``(module, message,
	exception active while logging)``."""
	import frappe

	lines = []

	def _logger(module=None, *a, **k):
		return SimpleNamespace(warning=lambda msg, *x, **y: lines.append((module, msg, sys.exc_info()[1])))

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

	def test_a_rollback_requeues_exactly_one_record_with_the_scrubbed_message(self, logs, requeued):
		# frappe.throw after the log (the request rollback in frappe.app) or a
		# failing job (execute_job rolls back) takes the inserted row away: the
		# same scrubbed row is queued again, once.
		import frappe

		ai_fix.log_ai_failure("optimus ai backfill", ai_fix.AiFixError(f"echoed {KEY}"), session_uuid="uuid-1")
		frappe.db.rollback()
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

		row = SimpleNamespace(trace_id="trace-1", metadata='{"type": "background_job"}')
		monkeypatch.setattr(frappe, "log_error", lambda **kw: logs.append(kw) or row, raising=False)
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
		# The row could not be written: one warning line in the optimus log names
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
		assert "RuntimeError" in message
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

	def test_an_exception_is_logged_once(self, logs):
		e = ai_fix.AiFixError("boom")
		ai_fix.log_ai_failure("first", e)
		ai_fix.log_ai_failure("second", e)
		assert [r["title"] for r in logs] == ["first"]


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

	def test_the_body_is_scrubbed_before_it_is_cut(self, logs, monkeypatch):
		# Cutting first would keep a key prefix the literal no longer matches.
		body = "x" * 290 + KEY + " tail"
		monkeypatch.setattr(requests, "post", _post(lambda: _Resp(500, {}, text=body)))
		with pytest.raises(ai_fix.AiFixError) as ei:
			_call()
		assert KEY[:10] not in str(ei.value)
		assert str(ei.value).endswith("x" * 10 + "******** t")

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
# The never-raise helpers never swallow an RQ job timeout
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
	unauthenticated. Each helper lets it through as a fresh instance raised
	after its ``try``, so the frames it interrupted (Fernet's decrypt frames
	hold the key bytes; the scrubber's hold the unscrubbed text) never reach
	``execute_job``'s with-context log."""

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

	def test_log_ai_failure_while_looking_up_the_session(self, logs, job_timeout, monkeypatch):
		import frappe

		db = _FakeDB()
		raiser = _raising(job_timeout)
		db.get_value = raiser
		monkeypatch.setattr(frappe, "db", db, raising=False)
		with pytest.raises(_JobTimeout) as ei:
			ai_fix.log_ai_failure("t", session_uuid="uuid-1")
		_assert_fresh_and_clean(ei, job_timeout, raiser)
		assert logs == []

	def test_log_ai_failure_while_registering_the_rollback_callback(self, logs, job_timeout, monkeypatch):
		import frappe

		raiser = _raising(job_timeout)
		frappe.db.after_rollback.add = raiser
		with pytest.raises(_JobTimeout) as ei:
			ai_fix.log_ai_failure("t")
		_assert_fresh_and_clean(ei, job_timeout, raiser)

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
		def _scrub(text, literals=()):
			if KEY in text:
				raise job_timeout
			return text

		monkeypatch.setattr("optimus.redaction.scrub_secrets", _scrub)
		monkeypatch.setattr("frappe.utils.password.get_decrypted_password", lambda *a, **k: KEY, raising=False)
		resp = _Resp(400, {"error": {"message": f"UNSCRUBBED {KEY}", "type": "invalid_request_error", "code": KEY}})
		with pytest.raises(_JobTimeout) as ei:
			ai_fix._provider_error_code(resp)
		_assert_fresh_and_clean(ei, job_timeout, _scrub)

	def test_token_count(self, job_timeout):
		class _Count:
			def __int__(self):
				raise job_timeout

		with pytest.raises(_JobTimeout) as ei:
			ai_fix._usage_from_openai({"usage": {"prompt_tokens": _Count()}})
		_assert_fresh_and_clean(ei, job_timeout, _Count.__int__)


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
