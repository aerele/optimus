# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Behavioural canary (PR-0a): the API key never reaches any log, traceback,
error-tracker event or response, and the prompt never reaches an Error Log
row, in any of the AI failure scenarios below.

A fake key and a PII marker are pushed through every AI entry point while
``requests.post`` fails in each way seen in the field. ``frappe.log_error``
is replaced by a recorder that is a superset of what really gets stored:

* ``stored``: what an Error Log row would hold. The explicit ``message``,
  or, when ``message`` is empty, a dump of every frame's locals (repr, plus
  the public attributes of a plain object, as Frappe's formatter prints
  them; see ``_dump_local``) of the active exception, walking
  ``__cause__`` / ``__context__`` the way Python and Sentry do. That is Frappe's ``get_traceback(with_context=True)`` minus
  its weak redaction. An exception an entry point lets out is stored too
  when Frappe would store it: a non-``AiFixError`` (the 500 snapshot, or
  ``execute_job``'s log for an RQ job timeout) and, in the
  ``developer_mode`` scenario, every one, ``frappe.throw`` included
  (``frappe/app.py`` snapshots every exception in developer mode, with
  ``str(exc)`` as the title).
* ``sentry``: at every ``log_error`` call made while an exception is being
  handled, the dump of that exception, which is what
  ``frappe.utils.sentry.capture_exception`` sends (``include_local_variables``)
  even when a message is given. It must stay EMPTY: every AI-surface log
  call runs outside an ``except`` block.
* ``stack``: at EVERY ``log_error`` call, the locals of the optimus frames on
  the current call stack. Frappe initialises Sentry with
  ``attach_stacktrace=True``, so even a message-only event carries them.
* ``escaped``: the dump of any exception an entry point lets out.
* ``returned``: return values and escaped exception reprs (what the UI shows).
* ``wire``: the ``headers`` / ``json`` arguments handed to ``requests.post``.
* ``pending``: what each ``frappe.db.after_rollback`` callback holds (its
  closure). ``log_ai_failure`` registers one per row; after the entry points
  ran, a rollback runs them and each record they re-queue through
  ``frappe.deferred_insert`` is a ``stored`` row too. The fake DB models a
  transactional Error Log table (Postgres), where that rollback removes the
  rows, so every row is re-queued and checked. On MariaDB the table is
  MyISAM, the rows survive and nothing is re-queued
  (``test_ai_log_failure.py`` covers that).

A non-``Exception`` interrupt (``system_exit``: a gunicorn worker timeout) is
not snapshotted by Frappe (``frappe.app`` catches ``Exception``), so it only
lands in ``escaped``, which Sentry's WSGI middleware would ship.

The only redaction applied is the one Frappe and Sentry both really apply:
a local variable named exactly ``api_key``.

The key must appear in none of them. The PII marker must not appear in a
stored row, except in the dump of an escaped RQ job timeout and in a
developer-mode snapshot: frame locals legitimately hold the prompt, so those
dumps (like ``stack``, i.e. Sentry's) can contain it. That residual is
documented in SECURITY.md.

Entry points are resolved with ``getattr`` when this module is imported, so
a renamed or deleted entry point is a collection error, never a silently
skipped check: a PR that removes one removes it here, and a PR that adds an
AI entry point adds it here.
"""

import inspect
import json
import os
import sys
import types
from types import SimpleNamespace

import pytest
import requests

from optimus import ai_fix, analyze, api
from optimus import settings as _settings

KEY = "sk-CANARY-7f3a9c1e5b2d4f6a8c0e"
NON_LATIN_KEY = "sk-CANARY-7f3a9c1e\u20195b2d4f6a8c0e"  # pasted smart quote
# The last mark is the ASCII segment both keys share: it still matches when the
# non-latin key is written with its smart quote escaped (``json.dumps`` with
# ``ensure_ascii``, ``ascii()``), which the two whole-key marks would miss.
KEY_MARKS = ("CANARY-7f3a9c1e5b2d4f6a8c0e", "CANARY-7f3a9c1e\u20195b2d4f6a8c0e", "CANARY-7f3a9c1e")
PII = "pii.canary@example.com"
PII_SQL = f"SELECT name FROM `tabCustomer` WHERE email_id = '{PII}'"
ECHO_MARK = "PROVIDER-ECHO"
SUGGESTION_MARK = "CANARY-SUGGESTION"
_NAME_REDACTED = frozenset({"api_key"})
_OPTIMUS_DIR = os.sep + "optimus" + os.sep
_TESTS_DIR = os.sep + "tests" + os.sep
_json_dumps = json.dumps  # the requests.post fake has a parameter named json

_FINDING = {
	"finding_type": "N+1 Query", "severity": "High", "title": "Customer lookup in a loop",
	"technical_detail": {"normalized_query": PII_SQL, "example_queries": [PII_SQL]},
}
_TABLE_PAYLOAD = {"table": "tabCustomer", "doctype": "Customer", "sample_queries": [PII_SQL]}
_ACTIONS = [{"label": f"Open Customer {PII}", "cmd": "frappe.desk.form.load.getdoc", "duration_ms": 12}]


class _Thrown(Exception):
	"""What the ``frappe.throw`` fake raises (a ValidationError: a 417, not
	snapshotted outside developer mode)."""


def _session_doc():
	breakdown = [{"table": "tabCustomer", "recommended_index": {"columns": ["email_id"]}}]
	row = SimpleNamespace(
		name="FIND-1", finding_type="N+1 Query", severity="High", title="t",
		customer_description="d", estimated_impact_ms=100, affected_count=3, action_ref="0",
		technical_detail_json=json.dumps({"normalized_query": PII_SQL}), llm_fix_json=None,
	)
	return SimpleNamespace(
		name="SESS-CANARY", session_uuid="uuid-canary", findings=[row], actions=[],
		table_breakdown_json=json.dumps(breakdown),
	)


def _ctx():
	return SimpleNamespace(
		session_uuid="uuid-canary", docname="SESS-CANARY", warnings=[], actions=[],
		findings=[{
			"finding_type": "N+1 Query", "severity": "High", "title": "t",
			"technical_detail_json": json.dumps({"normalized_query": PII_SQL}),
		}],
		aggregate={"table_breakdown": [{"table": "tabCustomer", "recommended_index": {"columns": ["email_id"]}}]},
	)


def _entry(module, name: str, args_factory, *, unwrap: bool = False):
	"""Resolve ``module.name`` NOW (a missing symbol fails collection)."""
	fn = getattr(module, name)
	return (inspect.unwrap(fn) if unwrap else fn), args_factory


# name -> (callable, () -> (args, kwargs)). Whitelisted endpoints are unwrapped
# from their rate-limit / type-check decorators; their permission gates are
# faked in the fixture.
_ENTRY_POINTS = {
	"ai_fix.suggest_fix": _entry(ai_fix, "suggest_fix", lambda: ((json.loads(json.dumps(_FINDING)),), {})),
	"ai_fix.humanize_steps": _entry(ai_fix, "humanize_steps", lambda: (([dict(x) for x in _ACTIONS],), {"session_title": "t"})),
	"ai_fix.suggest_index": _entry(ai_fix, "suggest_index", lambda: ((dict(_TABLE_PAYLOAD),), {})),
	"ai_fix.test_connection": _entry(ai_fix, "test_connection", lambda: ((), {})),
	"analyze._enrich_findings_with_ai_suggestions": _entry(
		analyze, "_enrich_findings_with_ai_suggestions", lambda: ((_ctx(),), {"recordings": []})),
	"analyze._run_ai_backfill": _entry(analyze, "_run_ai_backfill", lambda: ((_session_doc(),), {"cap": 0})),
	"analyze._enrich_table_breakdown_with_ai_suggestions": _entry(
		analyze, "_enrich_table_breakdown_with_ai_suggestions", lambda: ((_ctx(), []), {})),
	"analyze._build_humanized_notes_html": _entry(
		analyze, "_build_humanized_notes_html", lambda: (([],), {"session_title": "t"})),
	"analyze._run_table_index_ai_backfill": _entry(
		analyze, "_run_table_index_ai_backfill", lambda: ((_session_doc(),), {"table_name": "tabCustomer"})),
	"api._refill_indexes_for_doc": _entry(api, "_refill_indexes_for_doc", lambda: ((_session_doc(),), {})),
	"api._humanize_steps_core": _entry(api, "_humanize_steps_core", lambda: ((_session_doc(),), {"title": "t"})),
	"api.suggest_fix": _entry(api, "suggest_fix", lambda: (("uuid-canary", "FIND-1"), {}), unwrap=True),
	"api.suggest_index": _entry(api, "suggest_index", lambda: (("uuid-canary", "tabCustomer"), {}), unwrap=True),
}


def _dump_local(name, value) -> list[str]:
	"""``name = repr(value)``. For a value whose type keeps the default
	``object.__repr__``, also ``name.attr = repr(...)`` for each public
	non-callable attribute: Frappe's formatter (traceback_with_variables,
	``objects_details=1``) prints such an object by its public attributes,
	one level deep, so a plain object holding the key must be caught too."""
	try:
		parts = [f"{name} = {value!r}"]
	except Exception:
		parts = [f"{name} = <unrepr>"]
	if type(value).__repr__ != object.__repr__:
		return parts
	try:
		attrs = [a for a in dir(value) if not a.startswith("_")]
	except Exception:
		return parts + [f"{name}.<dir> = <undir>"]
	for attr in attrs:
		try:
			attr_value = getattr(value, attr)
			if callable(attr_value):
				continue
			parts.append(f"{name}.{attr} = {attr_value!r}")
		except Exception:
			parts.append(f"{name}.{attr} = <unrepr>")
	return parts


def _dump_exception(exc) -> str:
	parts = []
	seen = set()
	while exc is not None and id(exc) not in seen:
		seen.add(id(exc))
		parts.append(repr(exc))
		tb = exc.__traceback__
		while tb is not None:
			for name, value in list(tb.tb_frame.f_locals.items()):
				if name in _NAME_REDACTED:
					continue
				parts += _dump_local(name, value)
			tb = tb.tb_next
		exc = exc.__cause__ if exc.__suppress_context__ else exc.__context__
	return "\n".join(parts)


def _dump_stack(frame) -> str:
	"""Locals of the optimus (non-test) frames from ``frame`` outwards."""
	parts = []
	while frame is not None:
		path = frame.f_code.co_filename
		if _OPTIMUS_DIR in path and _TESTS_DIR not in path:
			for name, value in list(frame.f_locals.items()):
				if name in _NAME_REDACTED:
					continue
				parts += _dump_local(name, value)
		frame = frame.f_back
	return "\n".join(parts)


class _Sinks:
	"""Every channel is a list of (entry point, text) so a failure names the
	entry point that leaked. ``stored`` items carry a third field: whether the
	PII check applies to them."""

	def __init__(self):
		self.stored, self.sentry, self.stack, self.escaped, self.returned, self.wire = [], [], [], [], [], []
		self.pending = []
		self.escaped_types = []
		self.posts = 0
		self.registered = 0
		self.requeued = 0
		self.entry = ""

	def __repr__(self):
		# The harness sits in the test frames' locals; a custom repr keeps
		# _dump_local from copying the collected channels into later dumps,
		# where they would satisfy the positive controls on their own.
		return "<canary sinks>"

	def log_error(self, title=None, message=None, reference_doctype=None, reference_name=None, **kw):
		active = sys.exc_info()[1]
		self.stored.append((self.entry, f"{title}\n{message if message else _dump_exception(active)}", True))
		if active is not None:
			self.sentry.append((self.entry, _dump_exception(active)))
		self.stack.append((self.entry, _dump_stack(sys._getframe(1))))
		# What log_error returns after a direct insert: the named document.
		import frappe

		return SimpleNamespace(name=frappe.db.insert_error_log())

	def deferred_insert(self, doctype, records):
		for record in records:
			self.requeued += 1
			self.stored.append((self.entry, f"{record.get('method')}\n{record.get('error')}\n{record!r}", True))


class _Callbacks:
	"""``frappe.utils.CallbackManager`` (``frappe.db.after_rollback``): run in
	order, each removed before it runs. Every callback added is recorded in the
	``pending`` channel by what its closure holds."""

	def __init__(self, sinks):
		self.sinks = sinks
		self.functions = []

	def __repr__(self):
		return "<canary callbacks>"

	def add(self, fn):
		self.sinks.registered += 1
		held = [repr(cell.cell_contents) for cell in (getattr(fn, "__closure__", None) or ())]
		self.sinks.pending.append((self.sinks.entry, "\n".join(held)))
		self.functions.append(fn)

	def run(self):
		while self.functions:
			self.functions.pop(0)()

	def reset(self):
		self.functions.clear()


class _Flags(dict):
	"""``frappe.flags`` (a ``frappe._dict``): a flag never set reads as None."""

	__getattr__ = dict.get


class _FakeDB:
	"""``commit`` drops the rollback callbacks, a full ``rollback`` runs them
	after the ROLLBACK, as ``frappe.database.Database`` does. The Error Log
	table is transactional (Postgres): a ROLLBACK removes the rows inserted
	since the last commit."""

	def __init__(self, sinks):
		self.after_rollback = _Callbacks(sinks)
		self.error_logs = set()
		self.uncommitted = []
		self.inserted = 0

	def __repr__(self):
		return "<canary db>"

	def get_value(self, doctype, filters=None, fieldname=None, *a, as_dict=False, **k):
		if as_dict:
			return {"name": "SESS-CANARY", "user": "Administrator", "status": "Ready", "title": "t"}
		return "SESS-CANARY"

	def set_value(self, *a, **k):
		pass

	def sql(self, *a, **k):
		return []

	def insert_error_log(self):
		self.inserted += 1
		name = f"ERR-{self.inserted:04d}"
		self.error_logs.add(name)
		self.uncommitted.append(name)
		return name

	def exists(self, doctype, name=None, *a, **k):
		return name if doctype == "Error Log" and name in self.error_logs else None

	def commit(self):
		self.after_rollback.reset()
		self.uncommitted.clear()

	def rollback(self, *, save_point=None, chain=False):
		if save_point:
			return
		self.error_logs.difference_update(self.uncommitted)
		self.uncommitted.clear()
		self.after_rollback.run()


def _reply(status_code: int, body: str) -> requests.Response:
	"""The provider's reply as a REAL ``requests.Response`` (what ``_http_post``
	holds in its ``resp`` local), so the dumps render it exactly as Frappe's
	formatter and Sentry do (``<Response [400]>``), never by a stand-in's own
	attributes. ``_http_post`` reads only ``status_code``, ``text`` and
	``json()``."""
	resp = requests.Response()
	resp.status_code = status_code
	resp._content = body.encode("utf-8")
	resp.encoding = "utf-8"
	return resp


def _wire_headers(url, headers, auth):
	# What urllib3 holds in its frame locals while sending: the prepared
	# headers AFTER the auth object ran.
	return dict(requests.Request("POST", url, headers=dict(headers or {}), auth=auth).prepare().headers)


# Scenarios whose provider reply is an error body that echoes the key and the
# PII marker, with its HTTP status.
_ECHO_STATUS = {
	"http_400_echo": 400, "http_404_echo": 404, "http_500_echo": 500,
	"developer_mode": 500, "scrub_raises": 500,
}


def _scenario_post(scenario, sinks, job_timeout):
	def _post(url, headers=None, json=None, timeout=None, auth=None, allow_redirects=True):  # noqa: A002
		sinks.posts += 1
		sinks.wire.append((sinks.entry, repr(headers) + repr(json)))
		wire_headers = _wire_headers(url, headers, auth)
		if scenario == "connection_error":
			raise requests.exceptions.ConnectionError(
				"HTTPConnectionPool(host='llm.invalid', port=443): Max retries exceeded"
			)
		if scenario == "unicode_encode_error":
			value = next(iter(v for k, v in wire_headers.items() if k.lower() in ("authorization", "x-api-key")))
			raise UnicodeEncodeError("latin-1", value, 0, 1, "ordinal not in range(256)")
		if scenario == "rq_timeout":
			raise job_timeout("Task exceeded maximum timeout value (60 seconds)")
		if scenario == "system_exit":
			# A gunicorn worker timeout while urllib3 sends: this frame holds
			# the auth-applied headers (wire_headers), i.e. the key.
			raise SystemExit(1)
		if scenario == "non_str_text":
			# A 200 reply whose text is not a string (a dict, then a list): it
			# counts as no text, and the marker inside never reaches the
			# operator. OpenAI's equivalent: ``message.content`` a dict, or a
			# list of parts whose text is a dict.
			if url.endswith("/chat/completions"):
				content = {"echo": ECHO_MARK} if sinks.posts % 2 else [{"type": "text", "text": {"echo": ECHO_MARK}}]
				return _reply(200, _json_dumps({"choices": [{"message": {"content": content}}]}))
			text = {"echo": ECHO_MARK} if sinks.posts % 2 else [ECHO_MARK, "list"]
			return _reply(200, _json_dumps({"content": [{"type": "text", "text": text}]}))
		if scenario == "malformed_usage":
			# A usable suggestion whose usage block is not what the parsers expect.
			usage = "x" if sinks.posts % 2 else {
				"prompt_tokens": "abc", "input_tokens": "abc", "completion_tokens": -3, "output_tokens": -3,
			}
			text = f"{SUGGESTION_MARK}: batch the lookup"
			if url.endswith("/chat/completions"):
				return _reply(200, _json_dumps({"choices": [{"message": {"content": text}}], "usage": usage}))
			return _reply(200, _json_dumps({"content": [{"type": "text", "text": text}], "usage": usage}))
		if scenario == "http_400_echo":
			# OpenAI's error object: the identifier-shaped type reaches the row,
			# the echoed key in ``code`` and the message never do.
			return _reply(400, _json_dumps({"error": {
				"message": f"{ECHO_MARK}: key {KEY} rejected for {PII}",
				"type": "invalid_request_error", "code": KEY,
			}}))
		if scenario == "http_401":
			return _reply(401, _json_dumps({"error": f"Incorrect API key {KEY} for {PII}"}))
		if scenario in _ECHO_STATUS:
			return _reply(_ECHO_STATUS[scenario], _json_dumps({"error": f"{ECHO_MARK}: key {KEY} rejected for {PII}"}))
		if scenario == "non_dict_json":
			return _reply(200, _json_dumps(["unexpected", "list"]))
		raise AssertionError(f"requests.post must not be called in scenario {scenario!r}")
	return _post


@pytest.fixture
def canary(monkeypatch, request):
	"""Wire the fakes; returns (sinks, scenario, job_timeout)."""
	import frappe

	scenario, provider = request.param
	job_timeout = None
	if scenario == "rq_timeout":
		job_timeout = pytest.importorskip("rq.timeouts").JobTimeoutException
	sinks = _Sinks()
	monkeypatch.setattr(analyze, "frappe", frappe)
	monkeypatch.setattr(api, "frappe", frappe)
	monkeypatch.setattr(frappe, "log_error", sinks.log_error, raising=False)
	monkeypatch.setattr(frappe, "db", _FakeDB(sinks), raising=False)
	monkeypatch.setattr(frappe, "flags", _Flags(), raising=False)
	queue = types.ModuleType("frappe.deferred_insert")
	queue.deferred_insert = sinks.deferred_insert
	monkeypatch.setitem(sys.modules, "frappe.deferred_insert", queue)
	monkeypatch.setattr(frappe.local, "_optimus_spend_session", None, raising=False)
	stored_key = NON_LATIN_KEY if scenario == "non_latin_key" else KEY
	monkeypatch.setattr(
		"frappe.utils.password.get_decrypted_password", lambda *a, **k: stored_key, raising=False
	)
	cfg = _settings.OptimusConfig(
		ai_enabled=True, ai_provider=provider, ai_auto_suggest=True, ai_auto_suggest_max=0,
	)
	monkeypatch.setattr("optimus.settings.get_config", lambda: cfg)
	monkeypatch.setattr(requests, "post", _scenario_post(scenario, sinks, job_timeout))
	if scenario == "scrub_raises":
		def _scrub_fails(*a, **k):
			raise RuntimeError("scrubber broke")
		monkeypatch.setattr("optimus.redaction.scrub_secrets", _scrub_fails)
	# Keep the analyze helpers off the source-reading / Redis paths: the payload
	# builders return the PII-bearing inputs directly.
	monkeypatch.setattr(analyze, "_ai_payload_for_finding", lambda *a, **k: json.loads(json.dumps(_FINDING)))
	monkeypatch.setattr(analyze, "_ai_payload_for_table", lambda *a, **k: dict(_TABLE_PAYLOAD))
	monkeypatch.setattr(analyze, "_actions_for_humanizer", lambda *a, **k: [dict(x) for x in _ACTIONS])
	monkeypatch.setattr(analyze, "_phase2_index_for", lambda *a, **k: {})
	monkeypatch.setattr(analyze, "_fetch_recordings", lambda *a, **k: [])
	monkeypatch.setattr(analyze, "_load_recordings_bundle", lambda *a, **k: None)
	# A successful AI call (malformed_usage) re-renders the report: not an AI path.
	monkeypatch.setattr(analyze, "_render_and_attach_reports", lambda *a, **k: None)
	# The whitelisted endpoints' gates: the caller owns the Ready session.
	monkeypatch.setattr(api, "_require_profiler_user", lambda: "Administrator")
	monkeypatch.setattr(api, "_require_session_permission", lambda *a, **k: "SESS-CANARY")
	monkeypatch.setattr(frappe, "get_roles", lambda *a, **k: ["System Manager"], raising=False)
	monkeypatch.setattr(frappe, "get_doc", lambda *a, **k: _session_doc(), raising=False)

	def _throw(msg=None, exc=None, **kw):
		raise (exc or _Thrown)(msg)
	monkeypatch.setattr(frappe, "throw", _throw, raising=False)
	return sinks, scenario, job_timeout


def _drive(name, fn, args_factory, sinks, scenario, job_timeout):
	sinks.entry = name
	args, kwargs = args_factory()
	try:
		sinks.returned.append((name, repr(fn(*args, **kwargs))))
	except BaseException as exc:  # noqa: BLE001
		dump = _dump_exception(exc)
		sinks.escaped.append((name, dump))
		sinks.escaped_types.append(type(exc))
		sinks.returned.append((name, repr(exc)))
		if scenario == "developer_mode":
			sinks.stored.append((name, f"{exc}\n{dump}", False))  # every exception, title str(exc)
		elif job_timeout is not None and isinstance(exc, job_timeout):
			sinks.stored.append((name, dump, False))  # execute_job's with-context log
		elif isinstance(exc, Exception) and not isinstance(exc, ai_fix.AiFixError | _Thrown):
			sinks.stored.append((name, dump, True))  # 500 snapshot (frappe.app catches Exception only)


_SCENARIOS = (
	"connection_error", "unicode_encode_error", "http_401", "http_400_echo", "http_404_echo",
	"http_500_echo", "non_dict_json", "non_latin_key", "rq_timeout", "developer_mode", "scrub_raises",
	"system_exit", "malformed_usage", "non_str_text",
)
_PROVIDERS = ("OpenAI", "Anthropic")
# Nothing is logged: nothing failed (malformed_usage), or the interrupt must
# leave untouched (system_exit).
_NO_ROW_SCENARIOS = ("system_exit", "malformed_usage")


@pytest.mark.parametrize(
	"canary", [(s, p) for s in _SCENARIOS for p in _PROVIDERS], indirect=True,
	ids=[f"{s}-{p}" for s in _SCENARIOS for p in _PROVIDERS],
)
def test_no_key_or_prompt_leaks_on_any_ai_failure_path(canary):
	import frappe

	sinks, scenario, job_timeout = canary
	for name, (fn, args_factory) in _ENTRY_POINTS.items():
		_drive(name, fn, args_factory, sinks, scenario, job_timeout)
	# Every row still in the transaction is queued again by its rollback
	# callback (frappe.throw's request rollback, execute_job's rollback).
	rows_written = len(sinks.stack)
	sinks.entry = "(rollback)"
	frappe.db.rollback()

	if scenario == "non_latin_key":
		assert sinks.posts == 0, "a key that cannot be sent must fail before any HTTP call"
	else:
		assert sinks.posts > 0, "the scenario never reached requests.post: the canary would prove nothing"
	if scenario in _NO_ROW_SCENARIOS:
		assert sinks.stored == [], f"{scenario}: no Error Log row may be written"
	else:
		assert sinks.stored, "no Error Log row was written: failures must still be logged"
	# Each row written registered exactly one re-queue callback, and the
	# rollback re-queued rows, so their records were checked below too.
	assert sinks.registered == rows_written
	if rows_written:
		assert sinks.requeued, "no row was re-queued by the rollback: the re-queued records went unchecked"

	channels = {
		"stored": [(e, t) for e, t, _ in sinks.stored], "sentry": sinks.sentry, "stack": sinks.stack,
		"escaped": sinks.escaped, "returned": sinks.returned, "wire": sinks.wire, "pending": sinks.pending,
	}
	for channel, items in channels.items():
		for entry, text in items:
			for mark in KEY_MARKS:
				assert mark not in text, f"API key leaked via {channel} from {entry} ({scenario})"
	for entry, text, pii_checked in sinks.stored:
		if pii_checked:
			assert PII not in text, f"prompt data stored in the Error Log by {entry} ({scenario})"
	# Every log call runs outside an except block, so Sentry never gets an
	# active exception (and its frame locals) to ship.
	assert sinks.sentry == [], (
		f"frappe.log_error ran while an exception was being handled: {[e for e, _ in sinks.sentry]} ({scenario})"
	)

	# Positive controls: each scenario really exercised the path it names.
	if scenario not in _NO_ROW_SCENARIOS:
		assert any(PII in t for _, t in sinks.stack), "the stack channel saw no prompt: it would prove nothing"
	returned = "\n".join(t for _, t in sinks.returned)
	if scenario == "system_exit":
		# Every request that was sent let the SystemExit out (none swallowed or
		# turned into an AI error); each dump walked down to the HTTP layer's
		# frame (its ``where`` argument) and the prompt-bearing frames, so the
		# key check above covered them.
		assert sum(1 for _, t in sinks.returned if t == "SystemExit(1)") == sinks.posts, "a SystemExit did not escape"
		assert len(sinks.escaped) == sinks.posts and all(
			"where = 'chat/completions'" in t or "where = 'messages'" in t for _, t in sinks.escaped
		), "an escaped dump never reached the HTTP layer's frame: the key check proved nothing"
		assert any(PII in t for _, t in sinks.escaped), "no escaped dump held the prompt: it would prove nothing"
	if scenario == "malformed_usage":
		assert sinks.escaped == [], f"malformed usage broke a good reply: {[e for e, _ in sinks.escaped]}"
		for ep in ("ai_fix.suggest_fix", "ai_fix.humanize_steps", "ai_fix.suggest_index", "api.suggest_fix"):
			assert any(e == ep and SUGGESTION_MARK in t for e, t in sinks.returned), f"{ep}: the suggestion was lost"
	if scenario == "non_str_text":
		# Only AI errors (and the endpoints' frappe.throw) left the entry
		# points: nothing escaped as a 500 whose snapshot holds the prompt.
		assert sinks.escaped_types, "nothing failed: the non-string text was never read"
		assert all(issubclass(t, ai_fix.AiFixError | _Thrown) for t in sinks.escaped_types), (
			f"a non-string text escaped as {[t.__name__ for t in sinks.escaped_types]}"
		)
		assert "empty response" in returned or "didn't contain any text" in returned, "the no-text path never ran"
		assert ECHO_MARK not in returned, "a non-string text reached the operator"
	if scenario == "http_400_echo":
		assert any("provider_error=invalid_request_error\n" in t for _, t, _ in sinks.stored), (
			"the provider's error type never reached the row: the code parser went unchecked"
		)
	if scenario in ("http_400_echo", "http_404_echo", "http_500_echo", "developer_mode"):
		assert ECHO_MARK in returned, "the provider body never reached the operator: nothing was checked"
	if scenario == "scrub_raises":
		assert ECHO_MARK not in returned, "a body that could not be scrubbed must be dropped"
		assert any("details withheld" in t for _, t, _ in sinks.stored), "a failed scrub must still write a row"
	if scenario == "unicode_encode_error":
		# The catch-all names the error type. Without an auth header to encode,
		# the fake would raise something else and the scenario would test nothing.
		assert "UnicodeEncodeError" in returned, "no header failed to encode: the scenario tested nothing"
	if scenario == "rq_timeout":
		assert any(job_timeout.__name__ in t for _, t in sinks.returned), "no RQ timeout escaped"
	if scenario == "developer_mode":
		assert any(ECHO_MARK in t for _, t, checked in sinks.stored if not checked), "no snapshot was stored"
		# The whitelisted endpoints call frappe.throw inside their
		# ``except AiFixError`` block, so the thrown error chains that AiFixError.
		# Each endpoint's escaped dump must hold it, as a chained exception
		# (_dump_exception writes each exception's repr on its own line). An
		# escape for any other reason, such as a throw before the AI call, leaves
		# the chained dump unchecked.
		for ep in ("api.suggest_fix", "api.suggest_index"):
			assert any(
				e == ep and any(line.startswith("AiFixError(") for line in t.splitlines())
				for e, t in sinks.escaped
			), f"{ep}: the frappe.throw-inside-except path never ran, so its chained dump went unchecked"
