# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""optimus.error_log_mask: the Error Log ``before_insert`` hook that masks the
stored AI key (and the key shapes ``scrub_secrets`` knows) in every Error Log
row as Frappe inserts it, the direct ``frappe.log_error`` insert and the
records Frappe's ``save_to_db`` takes from the deferred-insert queue alike.

``frappe`` is the real module on a bench host and the conftest stub on CI:
the hook only reads ``frappe.flags`` and ``frappe.logger``, which each test
replaces (``env``). ``ai_fix._current_key_or_empty`` is replaced too, so no
database is read.
"""

import ast
import importlib
import json
import sys
import types
from types import SimpleNamespace

import pytest

from optimus import error_log_mask, maintenance

KEY = "sk-live-0123456789abcdefXYZ"
LEAKY = (
	'File "apps/optimus/optimus/ai_fix.py", line 1347, in _call_openai_chat\n'
	f"    headers = {{'content-type': 'application/json', 'authorization': 'Bearer {KEY}'}}\n"
	f"    provider = {{'name': 'OpenAI', 'api_key': '{KEY}'}}\n"
	'  File "env/lib/python3.14/site-packages/urllib3/connection.py", line 499, in request\n'
	f"      value = '{KEY}'\n"
)
# Frappe's with-context traceback of an ordinary ERPNext error: its value
# lines are the user's data, not a header.
ERP_TB = (
	"Traceback (most recent call last):\n"
	'  File "apps/erpnext/erpnext/stock/doctype/stock_entry/stock_entry.py", line 88, in validate\n'
	"    self.set_value(fieldname, value)\n"
	"      fieldname = 'customer'\n"
	"      value = 'Acme Traders: 12 units'\n"
	"      values = ['Acme Traders', 'Beta Stores']\n"
)
HANDLER = "optimus.error_log_mask.mask_error_log"
FIELDS = ("error", "method", "metadata")


class _Doc:
	"""Enough of Frappe's ``Document`` for the hook and for Frappe's own hook
	dispatcher (``Document.hook``): ``get`` / ``set`` over the instance
	dict, ``doctype``. ``sets`` logs every field the hook set."""

	def __init__(self, **fields):
		self.__dict__["doctype"] = "Error Log"
		self.__dict__["sets"] = []
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def set(self, key, value):
		self.__dict__["sets"].append(key)
		self.__dict__[key] = value

	def fields(self) -> dict:
		return {f: self.get(f) for f in FIELDS if self.get(f) is not None}


class JobTimeoutException(Exception):
	"""rq.timeouts.JobTimeoutException: RQ's job timeout (an ``Exception``)."""


@pytest.fixture
def env(monkeypatch):
	"""The hook's Frappe surface, recorded: ``reads`` logs each key read with
	whether Frappe's messages were muted during it; ``lines`` the ``optimus``
	log lines; ``inserts`` any ``frappe.log_error`` / ``frappe.get_doc`` call
	(the hook must make none). A fake ``rq`` provides the job timeout."""
	import frappe

	rec = SimpleNamespace(reads=[], lines=[], inserts=[], key=KEY)
	flags = SimpleNamespace(mute_messages=False)

	def _read():
		rec.reads.append(flags.mute_messages)
		return rec.key

	def _logger(module=None, *a, **k):
		def _level(level):
			return lambda msg, *a, **k: rec.lines.append((module, level, msg))
		return SimpleNamespace(**{level: _level(level) for level in ("debug", "info", "warning", "error")})

	monkeypatch.setattr(frappe, "flags", flags, raising=False)
	monkeypatch.setattr(frappe, "logger", _logger, raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda *a, **k: rec.inserts.append(("log_error", a, k)), raising=False)
	monkeypatch.setattr(frappe, "get_doc", lambda *a, **k: rec.inserts.append(("get_doc", a, k)), raising=False)
	monkeypatch.setattr("optimus.ai_fix._current_key_or_empty", _read)
	rq = types.ModuleType("rq")
	timeouts = types.ModuleType("rq.timeouts")
	timeouts.BaseTimeoutException = JobTimeoutException
	rq.timeouts = timeouts
	monkeypatch.setitem(sys.modules, "rq", rq)
	monkeypatch.setitem(sys.modules, "rq.timeouts", timeouts)
	rec.flags = flags
	return rec


def _run(doc):
	error_log_mask.mask_error_log(doc, "before_insert")
	return doc


def _one_line(env) -> str:
	"""The one ``optimus`` log line, at ERROR (Frappe's production level),
	holding no row text and no key."""
	assert len(env.lines) == 1, env.lines
	module, level, line = env.lines[0]
	assert (module, level) == ("optimus", "error")
	assert KEY not in line and "Traceback" not in line and "ai_fix.py" not in line
	return line


class TestMasking:
	def test_an_ai_record_is_masked_in_error_method_and_metadata(self, env):
		meta = json.dumps({"form_dict": {"doc": f'{{"ai_api_key": "{KEY}"}}'}})
		doc = _run(_Doc(error=LEAKY, method=f"The AI provider returned an error (HTTP 401): bad key {KEY}", metadata=meta))
		assert KEY not in json.dumps(doc.fields())
		assert "'authorization': 'Bearer ********'" in doc.error and "      value = ********\n" in doc.error
		assert doc.method.endswith("bad key ********")
		assert json.loads(doc.metadata)  # still valid JSON
		assert sorted(doc.sets) == ["error", "metadata", "method"]
		assert env.reads == [True] and env.inserts == [] and env.lines == []

	def test_the_key_only_in_the_metadata_is_masked_and_makes_it_an_ai_record(self, env):
		meta = json.dumps({"form_dict": {"ai_api_key": KEY}})
		doc = _run(_Doc(error=ERP_TB, method="Stock Entry failed", metadata=meta))
		assert KEY not in doc.metadata and json.loads(doc.metadata)
		# the key makes it an AI record: its value lines are masked too
		assert "Acme" not in doc.error and "      value = ********\n" in doc.error
		assert doc.method == "Stock Entry failed" and "method" not in doc.sets

	def test_a_shape_the_long_title_and_the_error_complete_together_is_masked(self, env):
		# The title ends in "Bearer" and the error starts with the token: only
		# the joined "<title>\n<error>" (v16's validate joins them) holds it.
		title = "x" * 140 + " Bearer"
		doc = _run(_Doc(error="abcdefghij rest", method=title))
		assert doc.error == f"{title}\n******** rest"
		assert doc.method == title[:140]

	def test_a_long_title_goes_in_front_of_the_error_as_v16_validate_does(self, env):
		title = ("T" * 150 + f" key {KEY} tail ").ljust(300, "z")
		doc = _run(_Doc(error="Traceback ...", method=title))
		full = title.replace(KEY, "********")
		assert doc.error == f"{full}\nTraceback ..."  # the full masked title is kept
		assert doc.method == full[:140]  # v15's length check passes
		assert _v16_validate(_Doc(**doc.fields())).fields() == doc.fields()  # v16's validate has nothing left

	def test_an_erpnext_style_record_is_left_untouched(self, env):
		doc = _run(_Doc(error=ERP_TB, method="Stock Entry failed", metadata=json.dumps({"user": "a@b.c"})))
		assert doc.sets == [] and doc.error == ERP_TB
		assert env.reads == [True] and env.lines == []

	@pytest.mark.parametrize("field", ["error", "method", "metadata"])
	@pytest.mark.parametrize("shape", ["dict", "list"])
	def test_a_non_str_value_holding_the_key_is_masked_as_the_text_the_row_stores(self, env, field, shape):
		# No AI frame and no other text: only str() of the value (what the
		# row stores) shows the key.
		value = {"note": f"bad key {KEY}"} if shape == "dict" else [f"bad key {KEY}"]
		doc = _run(_Doc(**{field: value}))
		assert isinstance(doc.get(field), str) and KEY not in doc.get(field)
		assert doc.get(field) == str(value).replace(KEY, "********")

	def test_a_non_str_value_without_a_key_is_left_as_it_is(self, env):
		value = {"note": "nothing secret"}
		doc = _run(_Doc(error="e", metadata=value))
		assert doc.metadata is value and doc.sets == []

	def test_a_field_the_doc_does_not_have_is_never_set(self, env):
		# Frappe v15's Error Log has no metadata field.
		doc = _run(_Doc(error=LEAKY, method="optimus ai_fix"))
		assert "metadata" not in doc.sets and doc.get("metadata") is None

	def test_it_is_idempotent(self, env):
		doc = _run(_Doc(error=LEAKY, method=("x" * 140 + " Bearer"), metadata=json.dumps({"k": KEY})))
		once = doc.fields()
		doc.sets.clear()
		assert _run(doc).fields() == once and doc.sets == []

	def test_the_key_is_read_once_per_insert_and_never_cached(self, env):
		_run(_Doc(error="a"))
		env.key = "sk-live-9999999999abcdefQRS"
		doc = _run(_Doc(error=f"api_key={env.key}"))
		assert env.reads == [True, True]
		assert "9999999999" not in doc.error

	def test_messages_are_muted_only_while_the_key_is_read(self, env):
		# An undecryptable key makes Frappe's decrypt call frappe.throw, which
		# would add "Encryption key is invalid" to the reply of every request
		# that logs an error.
		env.flags.mute_messages = "caller's value"
		_run(_Doc(error="a"))
		assert env.reads == [True] and env.flags.mute_messages == "caller's value"

	def test_the_flag_is_restored_when_the_read_fails(self, env, monkeypatch):
		def _read():
			raise RuntimeError("boom")
		monkeypatch.setattr("optimus.ai_fix._current_key_or_empty", _read)
		_run(_Doc(error=LEAKY))
		assert env.flags.mute_messages is False


class TestFailOpen:
	def test_a_key_read_that_raises_leaves_the_doc_as_it_was(self, env, monkeypatch):
		def _read():
			raise RuntimeError(f"cannot read {KEY}")
		monkeypatch.setattr("optimus.ai_fix._current_key_or_empty", _read)
		doc = _run(_Doc(error=LEAKY, method="optimus ai_fix"))
		assert doc.sets == [] and doc.error == LEAKY
		assert _one_line(env).endswith("stored as it was: RuntimeError")
		assert env.inserts == []

	def test_a_process_with_stale_optimus_modules_leaves_the_doc_as_it_was(self, env, monkeypatch):
		# A process started before the upgrade holds develop's optimus.redaction
		# (no SECRET_PLACEHOLDER, no scrub_secrets). The new hooks reach it
		# through the Redis cache: Frappe resolves the handler (this module
		# needs only the standard library to import), and the handler's
		# import of optimus.maintenance fails inside its guard.
		import optimus

		stale = types.ModuleType("optimus.redaction")
		stale.redact_sensitive = lambda payload, **kw: payload
		monkeypatch.setitem(sys.modules, "optimus.redaction", stale)
		for name in ("maintenance", "error_log_mask"):
			monkeypatch.delitem(sys.modules, f"optimus.{name}", raising=False)
			monkeypatch.delattr(optimus, name, raising=False)
		handler = _resolve(HANDLER)
		doc = _Doc(error=LEAKY, method="optimus ai_fix")
		assert handler(doc, "before_insert") is None
		assert doc.sets == [] and doc.error == LEAKY
		assert _one_line(env).endswith("stored as it was: ImportError")

	def test_masking_that_fails_on_a_record_that_is_not_ai_leaves_it_as_it_was(self, env, monkeypatch):
		monkeypatch.setattr(maintenance, "_mask", _boom)
		doc = _run(_Doc(error=ERP_TB, method="Stock Entry failed"))
		assert doc.sets == [] and doc.error == ERP_TB
		assert _one_line(env).endswith("stored as it was: its masking failed")

	@pytest.mark.parametrize(
		("fields", "method"),
		[
			# an ai_fix.py frame in the error: the error is withheld, the
			# title (no frame, no key) kept
			({"error": LEAKY, "method": "optimus ai_fix"}, "optimus ai_fix"),
			# the key in the title only
			({"error": "Traceback ...", "method": f"bad key {KEY}"}, error_log_mask.WITHHELD_TITLE),
			# a long title that holds neither: cut, as v16's validate cuts it
			({"error": LEAKY, "method": "t" * 200}, "t" * 140),
		],
		ids=["frame_in_error", "key_in_title", "long_plain_title"],
	)
	def test_masking_that_fails_on_an_ai_record_withholds_its_text(self, env, monkeypatch, fields, method):
		monkeypatch.setattr(maintenance, "_mask", _boom)
		doc = _run(_Doc(**fields, metadata=json.dumps({"doc": KEY})))
		assert doc.error == error_log_mask.WITHHELD
		assert doc.method == method
		assert doc.metadata == error_log_mask.WITHHELD  # it held the key
		assert KEY not in json.dumps(doc.fields()) and "ai_fix.py" not in json.dumps(doc.fields())
		assert _one_line(env).endswith("withheld: its masking failed")

	def test_metadata_without_a_frame_or_the_key_is_kept_when_the_text_is_withheld(self, env, monkeypatch):
		monkeypatch.setattr(maintenance, "_mask", _boom)
		meta = json.dumps({"user": "a@b.c"})
		doc = _run(_Doc(error=LEAKY, method="optimus ai_fix", metadata=meta))
		assert doc.error == error_log_mask.WITHHELD and doc.metadata == meta

	def test_a_record_whose_ai_check_fails_too_is_withheld(self, env, monkeypatch):
		# The masking failed and so did the check that would tell an AI
		# record from another: nothing shows the text is safe.
		monkeypatch.setattr(maintenance, "_is_ai_record", _boom)
		doc = _run(_Doc(error=ERP_TB, method="Stock Entry failed", metadata=json.dumps({"user": "a@b.c"})))
		assert doc.fields() == {
			"error": error_log_mask.WITHHELD, "method": error_log_mask.WITHHELD_TITLE, "metadata": error_log_mask.WITHHELD,
		}
		assert _one_line(env).endswith("withheld: its masking failed")

	def test_only_the_joined_pass_failing_withholds_the_text_never_the_joined_raw_text(self, env, monkeypatch):
		# The first pass masks each field; the pass over the joined
		# "<title>\n<error>" fails. The joined text (whose join completes
		# "Bearer <token>") must not be stored.
		title = "optimus/ai_fix.py " + "x" * 130 + " Bearer"
		real = maintenance._mask_row

		def _mask_row(row, text_fields, api_key, **kw):
			if text_fields == ("error",):
				return None
			return real(row, text_fields, api_key, **kw)
		monkeypatch.setattr(maintenance, "_mask_row", _mask_row)
		doc = _run(_Doc(error="abcdefghij rest", method=title))
		assert doc.error == error_log_mask.WITHHELD and "abcdefghij" not in json.dumps(doc.fields())
		assert doc.method == error_log_mask.WITHHELD_TITLE  # it holds the frame path

	def test_it_never_writes_an_error_log(self, env, monkeypatch):
		# It runs inside an Error Log insert: a log_error of its own would
		# insert another Error Log, and run this hook again.
		for doc in (_Doc(error=LEAKY), _Doc(error=ERP_TB)):
			_run(doc)
		monkeypatch.setattr(maintenance, "_mask", _boom)
		_run(_Doc(error=LEAKY))

		def _read():
			raise RuntimeError("boom")
		monkeypatch.setattr("optimus.ai_fix._current_key_or_empty", _read)
		_run(_Doc(error=LEAKY))
		assert env.inserts == []

	def test_a_breadcrumb_that_fails_is_swallowed(self, env, monkeypatch):
		import frappe

		def _logger(*a, **k):
			raise OSError("disk full")
		monkeypatch.setattr(frappe, "logger", _logger, raising=False)
		monkeypatch.setattr(maintenance, "_mask", _boom)
		doc = _run(_Doc(error=ERP_TB))
		assert doc.error == ERP_TB


def _boom(*a, **k):
	raise ValueError(f"catastrophic backtracking near {KEY}")


class TestJobTimeout:
	"""An RQ job timeout (an ``Exception`` subclass) must still stop the job.
	It leaves as a fresh instance raised after the ``try``, so the frames it
	interrupted (the masking's, which hold the record text, and the key
	read's) never travel with it to the job's failure log."""

	@pytest.mark.parametrize("where", ["key_read", "masking", "ai_check"])
	def test_it_is_re_raised_as_a_fresh_instance(self, env, monkeypatch, where):
		raised = []

		def _timeout(*a, **k):
			held = KEY  # noqa: F841 (a frame local that must not travel)
			exc = JobTimeoutException("Task exceeded maximum timeout value (180 seconds)")
			raised.append(exc)
			raise exc
		if where == "key_read":
			monkeypatch.setattr("optimus.ai_fix._current_key_or_empty", _timeout)
		elif where == "masking":
			monkeypatch.setattr(maintenance, "_mask", _timeout)
		else:
			monkeypatch.setattr(maintenance, "_is_ai_record", _timeout)
		with pytest.raises(JobTimeoutException) as ei:
			_run(_Doc(error=LEAKY, method="optimus ai_fix"))
		exc = ei.value
		assert raised and exc is not raised[0]
		assert exc.args == ("Task exceeded maximum timeout value (180 seconds)",)
		assert exc.__context__ is None and exc.__cause__ is None
		names = []
		tb = exc.__traceback__
		while tb is not None:
			names.append(tb.tb_frame.f_code.co_name)
			tb = tb.tb_next
		assert names[-1] == "mask_error_log" and "_timeout" not in names and "_mask_row" not in names
		assert env.lines == []

	def test_a_timeout_during_the_breadcrumb_stops_the_job_too(self, env, monkeypatch):
		import frappe

		def _logger(*a, **k):
			raise JobTimeoutException("Task exceeded maximum timeout value (180 seconds)")
		monkeypatch.setattr(frappe, "logger", _logger, raising=False)
		monkeypatch.setattr(maintenance, "_mask", _boom)
		with pytest.raises(JobTimeoutException) as ei:
			_run(_Doc(error=ERP_TB))
		assert ei.value.__context__ is None

	def test_the_masking_lets_a_timeout_through(self, env, monkeypatch):
		# maintenance's own guards (the scrub's per-row fail-safe) must not
		# swallow it: the hook runs this masking inside RQ jobs.
		def _timeout(*a, **k):
			raise JobTimeoutException("t")
		monkeypatch.setattr(maintenance, "_mask", _timeout)
		with pytest.raises(JobTimeoutException):
			maintenance._mask_row({"error": "x"}, ("error",), KEY)
		with pytest.raises(JobTimeoutException):
			maintenance._masked_record({"error": "x"}, KEY)
		monkeypatch.setattr(maintenance, "_is_ai_record", _timeout)
		with pytest.raises(JobTimeoutException):
			maintenance._masked_record({"error": "x"}, KEY)


class TestKeyFrames:
	"""The hook's frames hold the key only under the names Frappe's traceback
	sanitizer and Sentry redact (``api_key``, ``secret``)."""

	@staticmethod
	def _holds(value) -> bool:
		if isinstance(value, str):
			return value in (KEY, json.dumps(KEY)[1:-1])
		if isinstance(value, (tuple, list)):
			return any(isinstance(v, str) and v in (KEY, json.dumps(KEY)[1:-1]) for v in value)
		return False

	@pytest.mark.parametrize("masking_fails", [False, True])
	def test_no_frame_holds_the_key_under_another_name(self, env, monkeypatch, masking_fails):
		if masking_fails:
			monkeypatch.setattr(maintenance, "_mask", _boom)
		files = {error_log_mask.__file__, maintenance.__file__}
		offenders, seen = set(), set()

		def _profile(frame, event, arg):
			if frame.f_code.co_filename not in files or event not in ("call", "return"):
				return
			seen.add(frame.f_code.co_name)
			for name, value in frame.f_locals.items():
				if name not in ("api_key", "secret") and self._holds(value):
					offenders.add(f"{frame.f_code.co_name}.{name}")
		sys.setprofile(_profile)
		try:
			_run(_Doc(error=LEAKY, method=("x" * 150) + f" {KEY}", metadata=json.dumps({"k": KEY})))
		finally:
			sys.setprofile(None)
		assert offenders == set()
		assert {"mask_error_log", "_masked_record"} <= seen  # positive control


class TestRegistration:
	def test_hooks_register_it_as_the_error_log_before_insert_event(self):
		from optimus import hooks

		assert hooks.doc_events["Error Log"] == {"before_insert": HANDLER}
		assert hooks.doc_events["User"] == {"validate": "optimus.install.on_user_role_change"}
		assert _resolve(HANDLER) is error_log_mask.mask_error_log

	def test_the_module_imports_only_the_standard_library_at_import_time(self):
		# Frappe resolves the handler (frappe.get_attr) outside any try, in
		# every process, old ones included: a failing import would fail
		# every Error Log insert there.
		tree = ast.parse(open(error_log_mask.__file__, encoding="utf-8").read())
		top = set()
		for node in tree.body:
			if isinstance(node, ast.Import):
				top |= {alias.name.split(".")[0] for alias in node.names}
			elif isinstance(node, ast.ImportFrom):
				top.add((node.module or "").split(".")[0])
		assert top <= set(sys.stdlib_module_names) | {"__future__"}, top


# ---------------------------------------------------------------------------
# Frappe's own insert paths, replayed
# ---------------------------------------------------------------------------


def _resolve(method_string: str):
	"""The resolution half of ``frappe.get_attr`` (``get_module`` is
	``importlib.import_module``)."""
	module, name = method_string.rsplit(".", 1)
	return getattr(importlib.import_module(module), name)


def _doc_events() -> dict:
	"""``frappe.get_doc_hooks()`` for Optimus alone: its real ``doc_events``,
	each handler listified as Frappe's ``append_hook`` does."""
	from optimus import hooks

	return {
		doctype: {event: handler if isinstance(handler, list) else [handler] for event, handler in events.items()}
		for doctype, events in hooks.doc_events.items()
	}


class CharacterLengthExceededError(Exception):
	"""Frappe's ``_validate_length``: a Data value past 140 characters."""


def _v16_validate(doc):
	"""Frappe v16's ``ErrorLog.validate``."""
	doc.__dict__["method"], doc.__dict__["error"] = str(doc.get("method")), str(doc.get("error"))
	if len(doc.method) > 140:
		doc.__dict__["error"] = f"{doc.method}\n{doc.error}"
		doc.__dict__["method"] = doc.method[:140]
	return doc


def _frappe_insert(record: dict, rows: list, v15: bool) -> None:
	"""``frappe.get_doc(record).insert()`` for an Error Log record, in
	``Document.insert``'s order (frappe/model/document.py, v15 and v16):
	``run_method("before_insert")``, whose ``Document.hook`` composer runs
	every ``doc_events`` handler for the doctype; then ``validate`` (v16's
	ErrorLog has one, v15's none); then ``_validate_length``; then
	``db_insert``, which stores the row in ``rows``."""
	doc = _Doc(**{k: v for k, v in record.items() if k != "doctype"})
	for handler in _doc_events().get(record["doctype"], {}).get("before_insert", []):
		_resolve(handler)(doc, "before_insert")
	if not v15:
		_v16_validate(doc)
	if len(str(doc.get("method") or "")) > 140:
		raise CharacterLengthExceededError("Value too big")
	rows.append(doc.fields())


def _frappe_save_to_db(queues: dict, rows: list, dropped: list, v15: bool) -> None:
	"""``frappe.deferred_insert.save_to_db`` with ``insert_record``: every
	``insert_queue_for_<doctype>`` queue, entries popped from the left, each
	record inserted with its doctype set, a failing insert logged and
	dropped; at most about 500 records a run on v15, 10,000 on v16."""
	cap = 500 if v15 else 10_000
	for name in [n for n in queues if n.startswith("insert_queue_for_")]:
		doctype = name.split("insert_queue_for_")[1]
		count = 0
		while queues[name] and count <= cap:
			records = json.loads(queues[name].pop(0).decode("utf-8"))
			for record in [records] if isinstance(records, dict) else records:
				count += 1
				try:
					record.update({"doctype": doctype})
					_frappe_insert(record, rows, v15)
				except Exception as e:
					dropped.append(type(e).__name__)


class TestFrappesInsertPaths:
	@pytest.mark.parametrize("v15", [False, True], ids=["v16", "v15"])
	def test_queued_leaky_records_are_inserted_masked_exactly_once(self, env, v15):
		leaky = {"error": LEAKY, "method": f"The AI provider returned an error (HTTP 401): bad key {KEY}"}
		if not v15:
			leaky["metadata"] = json.dumps({"form_dict": {"doc": f'{{"ai_api_key": "{KEY}"}}'}})
		long_title = {"error": "Traceback ...", "method": ("T" * 150) + f" Bearer {KEY}"}
		plain = {"error": ERP_TB, "method": "Stock Entry failed"}
		queues = {
			"insert_queue_for_Error Log": [
				json.dumps(leaky).encode(), json.dumps([long_title, plain]).encode(),
			],
		}
		rows, dropped = [], []
		_frappe_save_to_db(queues, rows, dropped, v15)
		_frappe_save_to_db(queues, rows, dropped, v15)  # the next scheduler run finds nothing
		assert dropped == [] and queues["insert_queue_for_Error Log"] == []
		assert len(rows) == 3  # each once
		assert not [row for row in rows if KEY in json.dumps(row)]
		assert "'authorization': 'Bearer ********'" in rows[0]["error"]
		# the long title is moved in front of the error on v15 too, so its
		# insert no longer fails the length check and is dropped
		assert rows[1]["error"].startswith("T" * 150 + " Bearer ********\n") and len(rows[1]["method"]) == 140
		assert rows[2] == (plain if v15 else _v16_validate(_Doc(**plain)).fields())

	def test_a_direct_log_error_insert_is_masked_too(self, env):
		# frappe.log_error builds the doc and calls insert() at once (no
		# queue): the same before_insert event.
		rows = []
		_frappe_insert({"doctype": "Error Log", "error": LEAKY, "method": "optimus ai_fix"}, rows, v15=False)
		assert KEY not in json.dumps(rows)

	def test_other_doctypes_are_not_touched(self, env):
		rows = []
		record = {"doctype": "Route History", "error": f"api_key={KEY}"}
		doc = _Doc(**record)
		for handler in _doc_events().get("Route History", {}).get("before_insert", []):
			_resolve(handler)(doc, "before_insert")
		assert doc.sets == [] and env.reads == []
		assert rows == []

	def test_frappes_own_hook_dispatcher_runs_it(self, env, monkeypatch):
		# The real Document.hook composer (frappe/model/document.py), with
		# get_doc_hooks and get_attr standing in for their site lookups.
		document = pytest.importorskip("frappe.model.document", exc_type=ImportError)
		import frappe

		monkeypatch.setattr(frappe, "get_doc_hooks", _doc_events)
		monkeypatch.setattr(frappe, "get_attr", _resolve)
		monkeypatch.setattr(frappe, "db", SimpleNamespace(_disable_transaction_control=0), raising=False)
		ran = []

		def before_insert(self, *args, **kwargs):
			ran.append("controller")
		doc = _Doc(error=LEAKY, method="optimus ai_fix")
		document.Document.hook(before_insert)(doc)
		assert ran == ["controller"] and "error" in doc.sets and KEY not in doc.error
