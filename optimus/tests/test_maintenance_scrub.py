# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""optimus.maintenance: scrub / purge of the AI Error Log rows written before
the key-leak fix, and the v0_12 patch that runs the scrub on migrate.

``maintenance.frappe`` is replaced wholesale by an in-memory fake that
implements just the ORM calls the module makes (``get_all`` with LIKE / = / >
/ <= filters, ``or_filters``, ``order_by="name asc"``, ``limit_start``,
``limit_page_length``; ``db.set_value`` with a field dict; ``db.count``;
``db.sql`` (the bounded count); ``db.has_column``; ``db.delete``;
``db.get_single_value``; savepoints; ``get_doc(...).insert``, which the
scrub must never call). ``cache`` records every Redis call the scrub makes.
"""

import fnmatch
import importlib
import inspect
import json
import re
import sys
import time
import tracemalloc
from types import SimpleNamespace

import pytest

from optimus import maintenance

KEY = "sk-live-0123456789abcdefXYZ"
LEAKY = (
	'File "apps/optimus/optimus/ai_fix.py", line 1347, in _call_openai_chat\n'
	f"    headers = {{'content-type': 'application/json', 'authorization': 'Bearer {KEY}'}}\n"
	f"    provider = {{'name': 'OpenAI', 'api_key': '{KEY}'}}\n"
)
CLEAN_AI = 'File "apps/optimus/optimus/ai_fix.py", line 10\n    provider = {\'api_key\': \'\'}\n'
UNRELATED = "File \"apps/frappe/frappe/app.py\"\n    Bearer sk-other-0000000000000000\n"  # not an AI row
# Frappe's with-context traceback of a pasted smart quote in an Anthropic key
# (observed shape): the header value sits bare in urllib3 / http.client locals.
ANTHROPIC_KEY = "sk-ant-api03-0123\u2019456789abcdef"
ANTHROPIC_TB = (
	'  File "apps/optimus/optimus/ai_fix.py", line 1290, in _http_post\n'
	"      headers = {'content-type': 'application/json', 'x-api-key': '"
	+ ANTHROPIC_KEY + "'}\n"
	'  File "env/lib/python3.14/site-packages/urllib3/connection.py", line 499, in request\n'
	"    self.putheader(header, value)\n"
	"      header = 'x-api-key'\n"
	f"      value = '{ANTHROPIC_KEY}'\n"
	'  File "http/client.py", line 1331, in putheader\n'
	"    values[i] = one_value.encode('latin-1')\n"
	f"      values = ['{ANTHROPIC_KEY}']\n"
	"      i = 0\n"
	f"      one_value = '{ANTHROPIC_KEY}'\n"
)


def _like(value, pattern):
	"""SQL ``LIKE`` with the escape rules of MariaDB and Postgres: ``%`` and
	``_`` are wildcards and a backslash (the default escape) makes the next
	character literal, so an unescaped backslash in a pattern never matches
	one in the text. It is case-sensitive, stricter than a MariaDB ``_ci``
	collation or the ILIKE Frappe runs on Postgres, so the real candidate set
	is a superset of the one these tests see."""
	rx, chars = "", iter(pattern)
	for ch in chars:
		if ch == "\\":
			rx += re.escape(next(chars, "\\"))
		elif ch == "%":
			rx += ".*"
		elif ch == "_":
			rx += "."
		else:
			rx += re.escape(ch)
	return re.fullmatch(rx, value or "", re.S) is not None


def _match(row, flt):
	field, op, val = flt
	if op == "like":
		return _like(row.get(field), val)
	if op == "=":
		return row.get(field) == val
	if op == ">":
		return (row.get(field) or "") > val
	if op == "<=":
		return (row.get(field) or "") <= val
	raise AssertionError(op)


def _unlike(pattern: str) -> str:
	"""A LIKE pattern's literal text: each escaped character kept as itself."""
	out, chars = "", iter(pattern)
	for ch in chars:
		out += next(chars, "\\") if ch == "\\" else ch
	return out


def _key_runs_in(value: str, key: str, run: int) -> list[str]:
	"""Every ``run``-character stretch of ``key`` that ``value`` (a LIKE
	pattern, un-escaped first) holds, raw or JSON-escaped."""
	text = _unlike(value)
	found = []
	for i in range(len(key) - run + 1):
		piece = key[i:i + run]
		if piece in text or json.dumps(piece)[1:-1] in text:
			found.append(piece)
	return found


class _FakeTxn:
	"""Savepoints as Postgres keeps them: SAVEPOINT pushes (a reused name
	nests), ROLLBACK TO keeps the savepoint and drops later ones, RELEASE
	drops it and later ones, COMMIT and a full ROLLBACK drop all. ROLLBACK TO
	or RELEASE of a name that is not set raises, as on both databases.
	``max_depth`` is the deepest nesting seen."""

	def __init__(self, log):
		self.log = log
		self.open = []
		self.max_depth = 0

	def _index(self, name):
		if name not in self.open:
			raise RuntimeError(f"SAVEPOINT {name} does not exist")
		return len(self.open) - 1 - self.open[::-1].index(name)

	def savepoint(self, name):
		self.open.append(name)
		self.max_depth = max(self.max_depth, len(self.open))
		self.log.append(("savepoint", name))

	def rollback(self, save_point=None):
		if save_point is None:
			self.open.clear()
		else:
			del self.open[self._index(save_point) + 1:]
		self.log.append(("rollback", save_point))

	def release_savepoint(self, name):
		del self.open[self._index(name):]
		self.log.append(("release", name))

	def commit(self):
		self.open.clear()


class _RecordingCache:
	"""``frappe.cache`` (and Frappe v16's ``frappe.client_cache``): every
	method call is logged in ``calls`` as ``(name, *args)`` and answers
	None."""

	def __init__(self):
		self.calls = []

	def __getattr__(self, name):
		if name.startswith("__"):
			raise AttributeError(name)
		return lambda *a, **k: self.calls.append((name, *a))


class DoesNotExistError(Exception):
	"""``frappe.DoesNotExistError``: ``get_single_value`` of a DocType that
	is not installed (``frappe.get_meta`` finds no such DocType)."""


class _FakeFrappe:
	"""``has_metadata=False`` models Frappe v15, whose Error Log has no
	``metadata`` column: a statement naming it fails. ``v15_like=True``
	models v15's ``db_query``, which doubles every backslash of a LIKE value
	before it reaches SQL. ``method`` is Data (varchar(140)) and a longer
	write fails as strict mode does. ``cache`` records the Redis calls, and
	``get_doc(record).insert()`` adds a row (``inserted`` keeps each record;
	the scrub never inserts). ``singles`` holds the Singles values
	``db.get_single_value`` reads (``single_reads`` logs each read); a
	Single whose DocType is not in ``installed_singles`` raises
	``DoesNotExistError``, as on a site Optimus was uninstalled from, and
	``single_error`` makes every read raise it instead."""

	DoesNotExistError = DoesNotExistError

	def __init__(self, error_logs, deleted_docs=(), has_metadata=True, v15_like=False):
		self.v15_like = v15_like
		self.cache = _RecordingCache()
		# Frappe v16 keeps the hooks in client_cache; v15 has none (None here)
		# and keeps them in frappe.cache.
		self.client_cache = _RecordingCache()
		# get_hooks() reloads: each call logs the Redis calls made before it
		self.hook_loads = []
		self.local = SimpleNamespace(doc_events_hooks={"User": {}})
		self.inserted = []
		self.singles = {}
		self.installed_singles = {"Optimus Settings"}
		self.single_error = None
		self.single_reads = []
		self.tables = {
			"Error Log": {n: {"name": n, "error": e, "method": "t", "metadata": "{}"} for n, e in error_logs},
			"Deleted Document": {
				n: {"name": n, "deleted_doctype": "Error Log", "data": d} for n, d in deleted_docs
			},
		}
		self.has_metadata = has_metadata
		self.reads = 0
		self.statements = []  # one SimpleNamespace per get_all, with the names it returned
		self.writes = []
		self.deletes = []
		self.fail_writes = set()
		self.commits = []
		self.db_log = []  # savepoint / release / rollback / write, in order
		self.txn = _FakeTxn(self.db_log)
		self.db = SimpleNamespace(
			set_value=self._set_value, delete=self._delete, count=self._count, has_column=self._has_column,
			savepoint=self.txn.savepoint, release_savepoint=self.txn.release_savepoint,
			rollback=self.txn.rollback, sql=self._sql, get_single_value=self._get_single_value,
		)

	def get_hooks(self, *a, **k):
		self.hook_loads.append((list(self.client_cache.calls if self.client_cache else self.cache.calls), self.reads))
		return {}

	def _get_single_value(self, doctype, fieldname, cache=True):
		self.single_reads.append((doctype, fieldname))
		if self.single_error is not None:
			raise self.single_error
		if doctype not in self.installed_singles:
			raise DoesNotExistError(f"DocType {doctype} not found")
		return self.singles.get((doctype, fieldname))

	def commit(self):
		self.commits.append(1)
		self.txn.commit()

	def get_doc(self, record):
		def _insert(ignore_permissions=False):
			self.inserted.append(dict(record))
			name = f"q{len(self.inserted):03d}"
			self.tables["Error Log"][name] = {
				"name": name, "error": record.get("error"), "method": record.get("method") or "t",
				"metadata": record.get("metadata") or "{}",
			}
		return SimpleNamespace(insert=_insert)

	def _sql(self, query, *a, **k):
		raise AssertionError(f"the scrub runs no raw SQL: {query}")

	def _has_column(self, doctype, column):
		return self.has_metadata or (doctype, column) != ("Error Log", "metadata")

	def get_all(
		self, doctype, filters=None, or_filters=None, fields=None, order_by=None, limit_start=0, limit_page_length=0,
	):
		assert order_by == "name asc"
		self.reads += 1
		assert self.reads <= 2000, "runaway scan: the pagination never advances"
		statement = SimpleNamespace(
			doctype=doctype, filters=list(filters or []), or_filters=list(or_filters or []), fields=list(fields),
			limit_start=limit_start, limit_page_length=limit_page_length, returned=[],
		)
		self.statements.append(statement)
		named = set(fields) | {f[0] for f in statement.filters + statement.or_filters}
		if doctype == "Error Log" and "metadata" in named and not self.has_metadata:
			raise RuntimeError("(1054, \"Unknown column 'metadata' in 'SELECT'\")")
		if self.v15_like:
			def _sql(flts):
				return [[f, op, v.replace("\\", "\\\\")] if op == "like" else [f, op, v] for f, op, v in flts or []]
			filters, or_filters = _sql(filters), _sql(or_filters)
		rows = [
			r for r in self.tables[doctype].values()
			if all(_match(r, f) for f in filters or [])
			and (not or_filters or any(_match(r, f) for f in or_filters))
		]
		rows.sort(key=lambda r: r["name"])
		rows = rows[limit_start:]
		if limit_page_length:
			rows = rows[:limit_page_length]
		statement.returned = [r["name"] for r in rows]
		return [{k: r.get(k) for k in fields} for r in rows]

	def filter_values(self):
		"""Every value handed to the ORM as a filter (what reaches SQL)."""
		return [
			f[2] for s in self.statements for f in s.filters + s.or_filters if isinstance(f[2], str)
		]

	def _set_value(self, doctype, name, values, update_modified=True):
		assert update_modified is False and isinstance(values, dict)
		self.db_log.append(("write", name))
		if name in self.fail_writes:
			raise RuntimeError("Lock wait timeout exceeded")
		if doctype == "Error Log" and len(values.get("method") or "") > 140:
			raise RuntimeError("(1406, \"Data too long for column 'method' at row 1\")")
		for field, value in values.items():
			self.writes.append((doctype, name, field))
			self.tables[doctype][name][field] = value

	def _count(self, doctype, filters=None):
		return sum(1 for r in self.tables[doctype].values() if all(r.get(k) == v for k, v in (filters or {}).items()))

	def _delete(self, doctype, filters):
		names = filters["name"][1]
		self.deletes.append((doctype, tuple(names)))
		for n in names:
			self.tables[doctype].pop(n, None)


# The scrub's result when nothing was found: every count 0, the key readable.
_OUT = {
	"candidates": 0, "changed": 0, "deleted_docs_changed": 0, "residual": 0, "failed": 0, "key_unreadable": False,
}


@pytest.fixture
def fake(monkeypatch):
	def _make(error_logs, deleted_docs=(), current_key=KEY, has_metadata=True, v15_like=False):
		f = _FakeFrappe(error_logs, deleted_docs, has_metadata=has_metadata, v15_like=v15_like)
		monkeypatch.setattr(maintenance, "frappe", f)
		monkeypatch.setattr(maintenance, "safe_commit", f.commit)
		monkeypatch.setattr("optimus.ai_fix._current_key_or_empty", lambda: current_key)
		return f
	return _make


class TestScrubErrorLogSecrets:
	def test_dry_run_counts_without_writing(self, fake):
		f = fake([("a", LEAKY), ("b", CLEAN_AI), ("c", UNRELATED)], [("d1", json.dumps({"error": LEAKY}))])
		out = maintenance.scrub_error_log_secrets(dry_run=True)
		# b has an ai_fix.py frame and the api_key marker, so it is a
		# candidate, but nothing in it changes.
		assert out == {**_OUT, "candidates": 2, "changed": 1, "deleted_docs_changed": 1}
		assert f.writes == [] and f.commits == [] and f.cache.calls == []

	def test_scrubs_error_log_and_deleted_document_copies(self, fake):
		f = fake([("a", LEAKY), ("b", CLEAN_AI), ("c", UNRELATED)], [("d1", json.dumps({"error": LEAKY}))])
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert out == {**_OUT, "candidates": 2, "changed": 1, "deleted_docs_changed": 1}
		assert _queue_calls(f) == []  # the Error Log hook masks queued records as Frappe inserts them
		assert KEY not in f.tables["Error Log"]["a"]["error"]
		assert "'authorization': 'Bearer ********'" in f.tables["Error Log"]["a"]["error"]
		assert KEY not in f.tables["Deleted Document"]["d1"]["data"]
		assert json.loads(f.tables["Deleted Document"]["d1"]["data"])  # still valid JSON
		assert f.tables["Error Log"]["b"]["error"] == CLEAN_AI  # unchanged row never written
		assert f.tables["Error Log"]["c"]["error"] == UNRELATED  # not an AI row
		assert ("Error Log", "b", "error") not in f.writes

	def test_second_run_changes_nothing(self, fake):
		fake([("a", LEAKY), ("b", CLEAN_AI)], [("d1", LEAKY)])
		maintenance.scrub_error_log_secrets(dry_run=False)
		again = maintenance.scrub_error_log_secrets(dry_run=False)
		assert (again["changed"], again["deleted_docs_changed"], again["residual"]) == (0, 0, 0)

	def test_batches_by_name_with_a_commit_per_chunk(self, fake):
		f = fake([(f"r{i:03d}", LEAKY) for i in range(5)])
		out = maintenance.scrub_error_log_secrets(dry_run=False, batch_size=2)
		assert out["candidates"] == 5 and out["changed"] == 5
		assert len(f.commits) == 3  # chunks of 2, 2, 1 (nothing left for the other passes)
		assert all(KEY not in r["error"] for r in f.tables["Error Log"].values())
		# every write's savepoint is released, so two writes in a chunk never
		# nest (a re-issued SAVEPOINT opens a nested subtransaction on Postgres)
		assert f.txn.max_depth == 1

	def test_a_failing_row_does_not_stop_the_others(self, fake):
		f = fake([("a", LEAKY), ("b", LEAKY), ("c", LEAKY)])
		f.fail_writes = {"b"}
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert (out["changed"], out["failed"]) == (2, 1)
		assert KEY not in f.tables["Error Log"]["a"]["error"] + f.tables["Error Log"]["c"]["error"]
		assert f.tables["Error Log"]["b"]["error"] == LEAKY  # rolled back to its savepoint only
		i = f.db_log.index(("write", "b"))
		assert f.db_log[i - 1] == ("savepoint", "optimus_scrub_row")  # set before the failing write
		assert f.db_log[i + 1] == ("rollback", "optimus_scrub_row")  # the fake raises for an unset one
		assert f.txn.max_depth == 1  # released after the rollback too

	def test_a_row_that_cannot_be_masked_is_counted_and_skipped(self, fake, monkeypatch):
		f = fake([("a", LEAKY), ("b", LEAKY + "BOOM\n")])
		real_mask = maintenance._mask

		def _mask(text, api_key, **kw):
			if "BOOM" in text:
				raise ValueError("catastrophic backtracking")
			return real_mask(text, api_key, **kw)
		monkeypatch.setattr(maintenance, "_mask", _mask)
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert (out["changed"], out["failed"]) == (1, 1)
		assert KEY not in f.tables["Error Log"]["a"]["error"]

	def test_header_value_lines_of_a_smart_quote_key_are_masked(self, fake):
		f = fake([("a", ANTHROPIC_TB)], [("d1", json.dumps({"error": ANTHROPIC_TB}))], current_key="")
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert (out["changed"], out["deleted_docs_changed"], out["residual"]) == (1, 1, 0)
		row = f.tables["Error Log"]["a"]["error"]
		assert "456789abcdef" not in row
		assert "      value = ********\n" in row and "      one_value = ********\n" in row
		assert "      values = ********\n" in row and "      i = 0\n" in row  # non-string locals stay
		data = f.tables["Deleted Document"]["d1"]["data"]
		assert "456789abcdef" not in data
		assert json.loads(data)["error"].count("********") == 4

	def test_rows_holding_todays_key_in_the_title_or_metadata_are_masked(self, fake):
		# Frappe's 500 snapshot uses str(exception) as the title; a request's
		# form data lands in metadata. Neither row has an ai_fix.py frame.
		f = fake([("s1", "Traceback ...\n"), ("s2", "Traceback ...\n"), ("s3", "Traceback ...\n")])
		f.tables["Error Log"]["s1"]["method"] = f"The AI provider returned an error (HTTP 401): bad key {KEY}"
		f.tables["Error Log"]["s2"]["metadata"] = json.dumps({"form_dict": {"doc": f'{{"ai_api_key": "{KEY}"}}'}})
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert (out["candidates"], out["changed"], out["residual"]) == (2, 2, 0)
		assert all(KEY not in json.dumps(r) for r in f.tables["Error Log"].values())
		assert ("Error Log", "s3", "error") not in f.writes

	def test_a_json_escaped_smart_quote_key_is_found_without_an_ai_frame(self, fake):
		# Today's key holds a smart quote; JSON text stores it as the six
		# characters \u2019. The
		# Deleted Document copy and the metadata below have no ai_fix.py frame
		# and no marker, so only the stored-key LIKE pass can select them, and
		# its pattern must escape that backslash (LIKE's default escape).
		escaped = json.dumps(ANTHROPIC_KEY)[1:-1]
		assert "\\u2019" in escaped
		data = json.dumps({"doctype": "Error Log", "method": f"HTTP 401: bad key {ANTHROPIC_KEY}"})
		meta = json.dumps({"form_dict": {"doc": f"key {ANTHROPIC_KEY}"}})
		assert escaped in data and ANTHROPIC_KEY not in data and escaped in meta
		f = fake([("e1", "Traceback ...\n")], [("d1", data)], current_key=ANTHROPIC_KEY)
		f.tables["Error Log"]["e1"]["metadata"] = meta
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert (out["candidates"], out["changed"], out["deleted_docs_changed"], out["residual"]) == (1, 1, 1, 0)
		for text in (f.tables["Deleted Document"]["d1"]["data"], f.tables["Error Log"]["e1"]["metadata"]):
			assert escaped not in text and "456789abcdef" not in text
			assert json.loads(text)  # still valid JSON

	@pytest.mark.parametrize(
		"shape", ["sk-proj-AAAABBBBCCCCDDDDEEEE", "gsk_AAAABBBBCCCCDDDDEEEE", "AIzaSyA-0123456789abcdefghijABCDEFGHIJ"],
	)
	def test_residual_counts_a_key_shape_the_masking_misses(self, fake, shape):
		# An older key (not the one stored today) in plain prose: no marker
		# shape matches it, the independent detector does.
		prose = CLEAN_AI + f"    note = 'rotated {shape}'\n"
		f = fake([("a", prose)])
		out = maintenance.scrub_error_log_secrets(dry_run=True)
		assert (out["changed"], out["residual"]) == (0, 1)
		assert f.writes == []

	def test_batch_size_defaults_to_the_module_batch(self):
		sig = inspect.signature(maintenance.scrub_error_log_secrets)
		assert sig.parameters["batch_size"].default == maintenance._BATCH

	def test_a_full_length_title_with_url_credentials_is_masked_and_written(self, fake):
		# Error Log.method is varchar(140): "u:p" becomes "********", so the
		# masked title is longer than the column. It is cut to 140 characters
		# instead of failing the whole row (error text included).
		title = "POST " + "x" * 90 + " via http://u:p@proxy.example/v1 "
		title += "y" * (140 - len(title))
		assert len(title) == 140
		f = fake([("a", LEAKY)])
		f.tables["Error Log"]["a"]["method"] = title
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert (out["changed"], out["failed"]) == (1, 0)
		row = f.tables["Error Log"]["a"]
		assert "u:p@" not in row["method"] and "://********@proxy" in row["method"]
		assert len(row["method"]) == 140 and row["method"].startswith(title[:100])
		assert KEY not in row["error"]

	def test_runs_where_error_log_has_no_metadata_column(self, fake):
		# Frappe v15's Error Log has no metadata column: the scrub neither
		# selects nor filters on it there.
		f = fake([("a", LEAKY), ("b", CLEAN_AI), ("m", "Traceback ...\n")], has_metadata=False)
		f.tables["Error Log"]["m"]["method"] = f"bad key {KEY}"
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert out == {**_OUT, "candidates": 3, "changed": 2}
		assert KEY not in f.tables["Error Log"]["a"]["error"] + f.tables["Error Log"]["m"]["method"]
		assert f.statements  # the fake raises on any statement naming metadata


def _mid8(key: str) -> str:
	start = (len(key) - 8) // 2
	return key[start:start + 8]


class TestStoredKeyPass:
	"""The pass that finds rows holding the key stored today. SQL gets only
	an 8-character fragment from the middle of the key; the full key is
	checked in Python."""

	@pytest.mark.parametrize("key", [KEY, ANTHROPIC_KEY], ids=["ascii", "smart_quote"])
	def test_no_filter_value_holds_the_key(self, fake, key):
		data = json.dumps({"doctype": "Error Log", "method": f"HTTP 401: bad key {key}"})
		f = fake([("m", "Traceback ...\n")], [("d1", data)], current_key=key)
		f.tables["Error Log"]["m"]["method"] = f"HTTP 401: bad key {key}"
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		# positive control: only the stored-key pass can select these two rows
		assert (out["candidates"], out["changed"], out["deleted_docs_changed"]) == (1, 1, 1)
		values = f.filter_values()
		assert values
		assert [v for v in values if _key_runs_in(v, key, 9)] == []
		assert [v for v in values if _key_runs_in(v, key, 8)]  # an 8-character fragment is what is sent

	@pytest.mark.parametrize("v15_like", [False, True], ids=["v16", "v15"])
	@pytest.mark.parametrize(
		("key", "fragment"),
		[
			# the centre window holds "_", "%" or a smart quote; the nearest
			# window of letters, digits and "-" is sent (not the first one)
			("sk-proj-AbCd_EfGh_IjKlMnOpQr_StUvWx", "IjKlMnOp"),
			("sk%live-9Qx7Kp2Z%%rTy5W%m3Nb8Lc1Hvx", "9Qx7Kp2Z"),
			(ANTHROPIC_KEY, "i03-0123"),
		],
		ids=["underscore", "percent", "smart_quote"],
	)
	def test_the_fragment_is_the_clean_window_nearest_the_centre(self, fake, key, fragment, v15_like):
		assert fragment in key and key.index(fragment) != 0
		data = json.dumps({"doctype": "Error Log", "method": f"HTTP 401: bad key {key}"})
		f = fake([("m", "Traceback ...\n")], [("d1", data)], current_key=key, v15_like=v15_like)
		f.tables["Error Log"]["m"]["method"] = f"HTTP 401: bad key {key}"
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		# no escaping needed, so v15's backslash doubling cannot break it
		key_likes = {v for v in f.filter_values() if _key_runs_in(v, key, 8)}
		assert key_likes == {f"%{fragment}%"}
		assert (out["candidates"], out["changed"], out["deleted_docs_changed"]) == (1, 1, 1)
		assert key not in f.tables["Error Log"]["m"]["method"]

	@pytest.mark.parametrize(
		("key", "patterns"),
		[
			# no 8 letters, digits or "-" in a row: key[5:13], LIKE-escaped
			("a_b%c_d%e_f%g_h%i_j", {"%\\_d\\%e\\_f\\%g%"}),
			# and its JSON-escaped form, for the Deleted Document copy
			("a_b’c_d’e_f’g_h’i_j", {"%\\_d’e\\_f’g%", "%\\_d\\\\u2019e\\_f\\\\u2019g%"}),
		],
		ids=["metacharacters", "smart_quotes"],
	)
	def test_a_key_without_a_clean_window_falls_back_to_the_escaped_centre(self, fake, key, patterns):
		data = json.dumps({"doctype": "Error Log", "method": f"HTTP 401: bad key {key}"})
		f = fake([("m", "Traceback ...\n")], [("d1", data)], current_key=key)
		f.tables["Error Log"]["m"]["method"] = f"HTTP 401: bad key {key}"
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		key_likes = {v for v in f.filter_values() if "\\" in v}
		assert key_likes == patterns
		assert (out["candidates"], out["changed"], out["deleted_docs_changed"]) == (1, 1, 1)
		assert [v for v in f.filter_values() if _key_runs_in(v, key, 9)] == []

	def test_a_fragment_match_without_the_key_is_neither_counted_nor_changed(self, fake):
		frag = _mid8(KEY)
		# holds the fragment and a maskable token, but not the key
		near = f"GET /x?ref={frag} Authorization: Bearer abcdefghijklmnop"
		assert frag in near and KEY not in near
		f = fake(
			[("a", "Traceback ...\n"), ("fp", "Traceback ...\n")],
			[("dfp", json.dumps({"doctype": "Error Log", "error": near}))],
		)
		f.tables["Error Log"]["a"]["method"] = f"bad key {KEY}"
		f.tables["Error Log"]["fp"]["method"] = near
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert (out["candidates"], out["changed"], out["deleted_docs_changed"], out["residual"]) == (1, 1, 0, 0)
		assert f.tables["Error Log"]["fp"]["method"] == near
		assert [w for w in f.writes if w[1] in ("fp", "dfp")] == []
		# the SQL did return them: the full-key check in Python dropped them
		assert {"fp", "dfp"} <= {n for s in f.statements for n in s.returned}

	@pytest.mark.parametrize(("length", "searched"), [(15, False), (16, True)])
	def test_a_key_shorter_than_16_is_not_searched_by_value(self, fake, length, searched):
		key = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ"[:length]
		f = fake([("a", LEAKY.replace(KEY, key) + f"    raw = {key}\n"), ("m", "Traceback ...\n")], current_key=key)
		f.tables["Error Log"]["m"]["method"] = f"bad key {key}"
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert key not in f.tables["Error Log"]["a"]["error"]  # marker pass, masked by value
		assert (key in f.tables["Error Log"]["m"]["method"]) is not searched
		assert out["candidates"] == 1 + searched
		assert bool([v for v in f.filter_values() if _key_runs_in(v, key, 8)]) is searched


class TestDryRunCoercion:
	"""``bench execute --kwargs`` or a hand-written call can pass a string or
	a number. Only explicit values are accepted, so a typo raises instead of
	running for real or silently doing nothing."""

	FALSE = [False, 0, "False", "false", "0", "no", " NO ", "\tfalse\n"]
	TRUE = [True, 1, "True", "true", "1", "yes", " Yes "]
	BAD = ["off", "", 2, "maybe", "on", -1, "   "]

	@pytest.mark.parametrize("value", FALSE, ids=repr)
	def test_a_false_value_writes(self, fake, value):
		f = fake([("a", LEAKY)], [("d1", LEAKY)])
		out = maintenance.scrub_error_log_secrets(dry_run=value)
		assert (out["changed"], out["deleted_docs_changed"]) == (1, 1)
		assert KEY not in f.tables["Error Log"]["a"]["error"] and _queue_calls(f) == []

	@pytest.mark.parametrize("value", [*TRUE, None], ids=repr)
	def test_a_true_value_or_none_writes_nothing(self, fake, value):
		f = fake([("a", LEAKY)], [("d1", LEAKY)])
		out = maintenance.scrub_error_log_secrets(dry_run=value)
		assert (out["changed"], out["deleted_docs_changed"]) == (1, 1)
		assert f.writes == [] and f.commits == [] and f.cache.calls == []

	def test_the_default_is_a_dry_run(self, fake):
		f = fake([("a", LEAKY), ("c", UNRELATED)])
		assert maintenance.scrub_error_log_secrets()["changed"] == 1
		assert maintenance.purge_ai_error_logs() == {"error_logs": 1, "deleted_documents": 0}
		assert f.writes == [] and f.deletes == [] and f.cache.calls == []

	@pytest.mark.parametrize("value", BAD, ids=repr)
	@pytest.mark.parametrize("func", ["scrub_error_log_secrets", "purge_ai_error_logs"])
	def test_anything_else_raises_before_reading_a_row(self, fake, func, value):
		f = fake([("a", LEAKY)], [("d1", LEAKY)])
		with pytest.raises(ValueError) as ei:
			getattr(maintenance, func)(dry_run=value)
		message = str(ei.value)
		for allowed in ("True", "False", "1", "0", '"true"', '"false"', '"yes"', '"no"'):
			assert allowed in message, allowed
		assert f.statements == [] and f.cache.calls == [] and f.writes == [] and f.deletes == []

	@pytest.mark.parametrize("value", [*TRUE, None], ids=repr)
	def test_purge_counts_only_for_a_true_value_or_none(self, fake, value):
		f = fake([("a", LEAKY), ("c", UNRELATED)])
		assert maintenance.purge_ai_error_logs(dry_run=value) == {"error_logs": 1, "deleted_documents": 0}
		assert f.deletes == []

	@pytest.mark.parametrize("value", FALSE, ids=repr)
	def test_purge_deletes_for_a_false_value(self, fake, value):
		f = fake([("a", LEAKY), ("c", UNRELATED)])
		assert maintenance.purge_ai_error_logs(dry_run=value) == {"error_logs": 1, "deleted_documents": 0}
		assert set(f.tables["Error Log"]) == {"c"}


class TestWindowedScan:
	"""Error Log is MyISAM: a statement holds a table read lock while it
	runs. Each LIKE statement reads at most one window of 1000 names."""

	NAMES = [f"r{i:04d}" for i in range(2500)]
	# window edges (the 1000th / 1001st names) and both ends of the table
	HITS = ("r0000", "r0421", "r0999", "r1000", "r1999", "r2000", "r2499")

	def _fake(self, fake):
		return fake([(n, LEAKY if n in self.HITS else "Traceback ...\n") for n in self.NAMES])

	def _assert_windows(self, f):
		likes = 0
		for s in f.statements:
			if not any(x[1] == "like" for x in s.filters + s.or_filters):
				# the lookup of the window's last name: primary key only
				assert s.fields == ["name"] and s.or_filters == []
				assert [x[:2] for x in s.filters] == [["name", ">"]]
				assert (s.limit_start, s.limit_page_length) == (999, 1)
				continue
			likes += 1
			lo = [x[2] for x in s.filters if x[:2] == ["name", ">"]]
			hi = [x[2] for x in s.filters if x[:2] == ["name", "<="]]
			assert len(lo) == 1 and len(hi) <= 1
			window = [n for n in f.tables[s.doctype] if n > lo[0] and (not hi or n <= hi[0])]
			assert len(window) <= 1000, (lo, hi, len(window))
		assert likes

	def test_every_match_is_found_once_and_no_statement_spans_more_than_a_window(self, fake):
		f = self._fake(fake)
		out = maintenance.scrub_error_log_secrets(dry_run=True, batch_size=2)
		assert out["candidates"] == len(self.HITS) and out["changed"] == len(self.HITS)
		self._assert_windows(f)

	def test_chunks_yield_each_match_once_in_batches(self, fake):
		f = self._fake(fake)
		chunks = list(maintenance._chunks("Error Log", [["error", "like", "%ai_fix.py%"]], None, ["name"], 3))
		names = [r["name"] for c in chunks for r in c]
		assert names == sorted(self.HITS)
		assert [len(c) for c in chunks] == [3, 3, 1]
		self._assert_windows(f)

	def test_a_chunk_never_gathers_more_than_batch_size_rows(self, fake):
		# One match in the first window, five in the second. The statement
		# after the carried-over match asks for batch_size - 1 rows, so no
		# more than batch_size rows are ever held, and the window is read on
		# until a statement returns fewer rows than it asked for.
		hits = ("r0100", "r1100", "r1101", "r1102", "r1103", "r1104")
		f = fake([(n, LEAKY if n in hits else "Traceback ...\n") for n in self.NAMES])
		chunks = list(maintenance._chunks("Error Log", [["error", "like", "%ai_fix.py%"]], None, ["name"], 3))
		assert [r["name"] for c in chunks for r in c] == list(hits)
		assert [len(c) for c in chunks] == [3, 3]
		likes = [s for s in f.statements if any(x[1] == "like" for x in s.filters)]
		assert [s.limit_page_length for s in likes] == [3, 2, 3, 3, 3]
		self._assert_windows(f)

	def test_purge_is_windowed_too(self, fake):
		f = self._fake(fake)
		assert maintenance.purge_ai_error_logs(dry_run=False) == {"error_logs": len(self.HITS), "deleted_documents": 0}
		assert not set(self.HITS) & set(f.tables["Error Log"])
		assert len(f.tables["Error Log"]) == len(self.NAMES) - len(self.HITS)
		self._assert_windows(f)


class QueryTimeoutError(Exception):
	"""Stands in for a driver's timeout (its type name is what is kept)."""


class InFailedSqlTransaction(Exception):
	"""Postgres: every statement after a failed one, until a rollback."""


class TestScrubScanSize:
	"""count(Error Log) + a bounded exact count of Deleted Document. The
	Deleted Document count reads at most MIGRATE_SCAN_LIMIT + 1 rows: exact
	below the limit, above it once the table is larger, and portable (no
	estimate: v15's is not scoped to the site's schema, Postgres answers -1
	for a table never analysed, InnoDB's is about a fifth low). The Error
	Log queue is not part of it: the scrub never reads it (the Error Log
	hook masks queued records as Frappe inserts them)."""

	BOUNDED = "SELECT COUNT(*) FROM (SELECT 1 FROM `tabDeleted Document` LIMIT %s) t"

	def _frappe(self, monkeypatch, fail=None, values=None, deleted_documents=20):
		"""``fail`` maps a part to the exception it raises; ``values``
		overrides what a part returns. ``sql`` emulates the LIMIT."""
		calls = []
		fail = fail or {}
		values = values or {}

		def part(name, compute):
			def call(*a, **k):
				calls.append((name, a, k))
				if name in fail:
					raise fail[name](f"{name} failed: row text {KEY}")
				return values[name] if name in values else compute(*a)
			return call

		def _sql(query, params):
			return ((min(deleted_documents, params[0]),),)
		db = SimpleNamespace(count=part("count", lambda doctype: 10), sql=part("sql", _sql))
		cache = SimpleNamespace(llen=part("llen", lambda key: 3))
		monkeypatch.setattr(maintenance, "frappe", SimpleNamespace(db=db, cache=cache))
		return calls

	def test_counts_error_logs_and_every_deleted_document_but_not_the_queue(self, monkeypatch):
		calls = self._frappe(monkeypatch)
		assert maintenance.scrub_scan_size() == 30
		assert maintenance.measure_scan_size() == (30, None)
		# every Deleted Document: deleted_doctype is not indexed, so the
		# passes read the whole table; the LIMIT is a bound parameter. The
		# queue adds no table-scan work (the scrub never reads it), so its
		# length is never read.
		assert calls == [
			("count", ("Error Log",), {}),
			("sql", (self.BOUNDED, (maintenance.MIGRATE_SCAN_LIMIT + 1,)), {}),
		] * 2

	def test_a_huge_queue_never_skips_the_scan_of_a_small_table(self, monkeypatch):
		self._frappe(monkeypatch, values={"llen": maintenance.MIGRATE_SCAN_LIMIT * 10})
		assert maintenance.measure_scan_size() == (30, None)

	def test_a_queue_that_cannot_be_read_leaves_the_size_known(self, monkeypatch):
		self._frappe(monkeypatch, fail={"llen": ConnectionError})
		assert maintenance.measure_scan_size() == (30, None)

	def test_a_deleted_document_table_past_the_limit_is_counted_only_up_to_it(self, monkeypatch):
		self._frappe(monkeypatch, deleted_documents=5_000_000)
		size = maintenance.scrub_scan_size()
		assert size == 10 + maintenance.MIGRATE_SCAN_LIMIT + 1
		assert size > maintenance.MIGRATE_SCAN_LIMIT  # the migrate skips it

	def test_a_deleted_document_table_at_the_limit_is_exact(self, monkeypatch):
		self._frappe(monkeypatch, deleted_documents=maintenance.MIGRATE_SCAN_LIMIT - 10)
		assert maintenance.scrub_scan_size() == maintenance.MIGRATE_SCAN_LIMIT

	@pytest.mark.parametrize("broken", ["count", "sql"])
	def test_a_part_that_cannot_be_read_makes_the_size_unknown(self, monkeypatch, broken):
		# An unmeasured table must not look small: the migrate skips the
		# scrub and prints the command instead. Only the TYPE name is kept.
		self._frappe(monkeypatch, fail={broken: QueryTimeoutError})
		size, reason = maintenance.measure_scan_size()
		assert size == maintenance.SCAN_SIZE_UNKNOWN == maintenance.scrub_scan_size()
		assert size > maintenance.MIGRATE_SCAN_LIMIT
		assert reason == "QueryTimeoutError"

	def test_the_first_failing_part_is_named(self, monkeypatch):
		# On Postgres a failed statement aborts the transaction, so every
		# later part fails too: the first failure is the cause.
		self._frappe(monkeypatch, fail={"count": QueryTimeoutError, "sql": InFailedSqlTransaction})
		assert maintenance.measure_scan_size().reason == "QueryTimeoutError"

	@pytest.mark.parametrize(
		("part", "value", "reason"),
		[
			("count", None, "no value"),
			("count", -1, "negative value"),
			# sql() results: no row, a NULL count, a negative count
			("sql", None, "no value"),
			("sql", (), "no value"),
			("sql", ((None,),), "no value"),
			("sql", ((-1,),), "negative value"),
		],
	)
	def test_a_part_with_no_count_or_a_negative_one_makes_the_size_unknown(self, monkeypatch, part, value, reason):
		# A bad count must never be read as 0 (the old estimate clamped
		# Postgres's -1 for a table never analysed to 0).
		self._frappe(monkeypatch, values={part: value})
		assert maintenance.measure_scan_size() == (maintenance.SCAN_SIZE_UNKNOWN, reason)

	def test_a_new_measurement_forgets_the_last_failure(self, monkeypatch):
		self._frappe(monkeypatch, fail={"count": ConnectionError})
		assert maintenance.measure_scan_size().reason == "ConnectionError"
		self._frappe(monkeypatch)
		assert maintenance.measure_scan_size() == (30, None)


class TestPurgeScope:
	def test_other_apps_ai_fix_frames_are_not_purged(self, fake):
		other = 'File "apps/acme/acme/openai_fix.py", line 3, in call\n    headers = {\'api_key\': \'x\'}\n'
		f = fake([("a", LEAKY), ("o", other)], [("d1", LEAKY), ("d2", other)])
		assert maintenance.purge_ai_error_logs(dry_run=False) == {"error_logs": 1, "deleted_documents": 1}
		assert set(f.tables["Error Log"]) == {"o"}
		assert set(f.tables["Deleted Document"]) == {"d2"}

	@pytest.mark.parametrize("v15_like", [False, True], ids=["v16", "v15"])
	def test_optimus_ai_fix_rows_are_purged_on_v15_and_v16(self, fake, v15_like):
		# The pattern holds no LIKE escape, so v15's backslash doubling
		# cannot turn it into one that matches nothing.
		installed = 'File "env/lib/python3.14/site-packages/optimus/ai_fix.py", line 9, in _http_post\n'
		other = 'File "apps/acme/acme/openai_fix.py", line 3, in call\n'
		f = fake(
			[("a", LEAKY), ("i", installed), ("o", other)],
			[("d1", json.dumps({"doctype": "Error Log", "error": LEAKY})), ("d2", other)],
			v15_like=v15_like,
		)
		assert maintenance.purge_ai_error_logs(dry_run=True) == {"error_logs": 2, "deleted_documents": 1}
		assert maintenance.purge_ai_error_logs(dry_run=False) == {"error_logs": 2, "deleted_documents": 1}
		assert set(f.tables["Error Log"]) == {"o"}
		assert set(f.tables["Deleted Document"]) == {"d2"}

	@pytest.mark.parametrize("v15_like", [False, True], ids=["v16", "v15"])
	def test_rows_from_the_pre_rename_package_are_purged_too(self, fake, v15_like):
		# Releases before the app was renamed to optimus shipped the AI code
		# as frappe_profiler/ai_fix.py, so their rows name that path.
		old = 'File "apps/frappe_profiler/frappe_profiler/ai_fix.py", line 9, in _call_openai_chat\n'
		other = 'File "apps/acme/acme/openai_fix.py", line 3, in call\n'
		f = fake(
			[("a", LEAKY), ("p", old), ("o", other)],
			[("d1", json.dumps({"doctype": "Error Log", "error": old})), ("d2", other)],
			v15_like=v15_like,
		)
		assert maintenance.purge_ai_error_logs(dry_run=True) == {"error_logs": 2, "deleted_documents": 1}
		assert f.deletes == []
		assert maintenance.purge_ai_error_logs(dry_run=False) == {"error_logs": 2, "deleted_documents": 1}
		assert set(f.tables["Error Log"]) == {"o"}
		assert set(f.tables["Deleted Document"]) == {"d2"}

	def test_the_scrub_still_reads_them(self, fake):
		# masking is harmless, so the scrub's candidate filter stays broad
		other = 'File "apps/acme/acme/openai_fix.py", line 3, in call\n    headers = {\'api_key\': \'secret-value-1\'}\n'
		f = fake([("o", other)])
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert (out["candidates"], out["changed"]) == (1, 1)
		assert "secret-value-1" not in f.tables["Error Log"]["o"]["error"]


def test_a_huge_hostile_value_line_masks_fast_with_bounded_memory():
	# A planted multi-MB row: the escaped value-line prefix, then a long run
	# with no \n escape and no double quote. An unbounded repeat keeps one
	# backtrack frame per character (about 150 bytes each) and can OOM-kill
	# bench migrate.
	hostile = "\\n      value = '" + "A" * 4_000_000
	tracemalloc.start()
	try:
		started = time.perf_counter()
		out = maintenance._mask(hostile, KEY)
		elapsed = time.perf_counter() - started
		peak = tracemalloc.get_traced_memory()[1]
	finally:
		tracemalloc.stop()
	assert out.startswith("\\n      value = ********")
	assert elapsed < 2, elapsed
	assert peak < 50_000_000, peak


@pytest.mark.parametrize("extra", [0, 1])
def test_an_escaped_value_line_is_masked_up_to_the_named_bound(extra):
	# The bound is one named constant, interpolated into the pattern: a
	# value of exactly that many units is masked whole, one more keeps a
	# one-unit tail (which the residual check still reads).
	bound = maintenance._ESCAPED_VALUE_MAX_UNITS
	assert f"{{0,{bound}}}" in maintenance._ESCAPED_VALUE_LINE.pattern
	out = maintenance._mask("\\n      value = '" + "A" * (bound + extra), "")
	assert out == "\\n      value = ********" + "A" * extra


class TestUnderSavepoint:
	"""The savepoint helper of the scrub's updates (``_write_row``): True
	when the write and the savepoint's release succeeded;
	otherwise the write is rolled back to the savepoint, the savepoint is
	released, and it returns False."""

	def _db(self, monkeypatch, release_fails=False):
		log = []
		txn = _FakeTxn(log)

		def _release(name):
			if release_fails:
				raise RuntimeError("SAVEPOINT optimus_scrub_row does not exist")
			txn.release_savepoint(name)
		db = SimpleNamespace(savepoint=txn.savepoint, release_savepoint=_release, rollback=txn.rollback)
		monkeypatch.setattr(maintenance, "frappe", SimpleNamespace(db=db))
		return log, txn

	def test_a_write_that_succeeds_returns_true_and_releases_the_savepoint(self, monkeypatch):
		log, txn = self._db(monkeypatch)
		writes = []
		assert maintenance._under_savepoint(lambda: writes.append(1)) is True
		assert writes == [1] and txn.open == []
		assert log == [("savepoint", "optimus_scrub_row"), ("release", "optimus_scrub_row")]

	def test_a_write_that_fails_is_rolled_back_to_the_savepoint_and_released(self, monkeypatch):
		log, txn = self._db(monkeypatch)

		def _write():
			raise RuntimeError("Lock wait timeout exceeded")
		assert maintenance._under_savepoint(_write) is False
		assert log == [
			("savepoint", "optimus_scrub_row"), ("rollback", "optimus_scrub_row"), ("release", "optimus_scrub_row"),
		]
		assert txn.open == []

	def test_a_release_that_fails_rolls_the_write_back(self, monkeypatch):
		log, _ = self._db(monkeypatch, release_fails=True)
		assert maintenance._under_savepoint(lambda: None) is False
		assert log == [("savepoint", "optimus_scrub_row"), ("rollback", "optimus_scrub_row")]


def _v16_error_log_validate(row: dict) -> dict:
	"""Frappe v16's ``ErrorLog.validate``, applied to a row dict."""
	row["method"], row["error"] = str(row.get("method")), str(row.get("error"))
	if len(row["method"]) > 140:
		row["error"] = f"{row['method']}\n{row['error']}"
		row["method"] = row["method"][:140]
	return row


class TestMaskedRecord:
	"""``_masked_record``: an Error Log record as the Error Log hook
	(``optimus.error_log_mask``) stores it, before Frappe's ``validate``,
	length check and INSERT."""

	def test_masks_the_key_in_every_text_field_and_keeps_the_rest(self):
		leaky = {
			"error": LEAKY,
			"method": f"The AI provider returned an error (HTTP 401): bad key {KEY}",
			"metadata": json.dumps({"form_dict": {"doc": f'{{"ai_api_key": "{KEY}"}}'}}),
			"reference_doctype": "Optimus Session", "reference_name": "s-1", "trace_id": "t-1",
		}
		row = maintenance._masked_record(leaky, KEY)
		assert KEY not in json.dumps(row)
		assert "'Bearer ********'" in row["error"] and row["method"].endswith("bad key ********")
		assert json.loads(row["metadata"])  # still valid JSON
		assert (row["reference_doctype"], row["reference_name"], row["trace_id"]) == ("Optimus Session", "s-1", "t-1")
		assert maintenance._masked_record(row, KEY) == row  # idempotent

	@pytest.mark.parametrize("with_key", [False, True])
	def test_a_long_title_goes_in_front_of_the_error_as_v16_does(self, with_key):
		# Frappe v16's ErrorLog.validate moves a title over 140 characters in
		# front of the error and cuts it; v15 has no such validate and fails
		# the insert (CharacterLengthExceededError). The hook stores the same
		# row on both.
		title = ("T" * 150 + (f" key {KEY} " if with_key else " ") + "tail ").ljust(300, "z")
		assert len(title) == 300
		row = maintenance._masked_record({"error": "Traceback ...", "method": title}, KEY)
		full = title.replace(KEY, "********")
		assert row["error"] == f"{full}\nTraceback ..."  # the full title is kept
		assert row["method"] == full[:140] and len(row["method"]) == 140  # v15's length check passes
		assert KEY not in json.dumps(row)
		assert _v16_error_log_validate(dict(row)) == row  # v16's validate has nothing left to do

	def test_a_masked_title_longer_than_the_column_goes_in_front_of_the_error(self):
		# Masking lengthens "u:p" to "********": the full masked title goes
		# in front of the error, and the title is cut to 140.
		title = "POST " + "x" * 90 + " via http://u:p@proxy.example/v1 "
		title += "y" * (140 - len(title))
		assert len(title) == 140
		row = maintenance._masked_record({"error": "x", "method": title}, KEY)
		masked_title = title.replace("u:p@", "********@")
		assert row["error"] == f"{masked_title}\nx"
		assert row["method"] == masked_title[:140] and "u:p@" not in row["method"]

	def test_a_shape_the_long_title_and_the_error_complete_together_is_masked(self):
		# The title ends in "Bearer" and the error starts with the token: only
		# the joined "<title>\n<error>" holds "Bearer <token>". It is masked
		# once joined, so a second pass, or the scrub of the stored row,
		# changes nothing.
		title = "x" * 140 + " Bearer"
		row = maintenance._masked_record({"error": "abcdefghij rest", "method": title}, KEY)
		assert row["error"] == f"{title}\n******** rest"
		assert row["method"] == title[:140]
		assert maintenance._masked_record(row, KEY) == row
		assert maintenance._mask_row({"name": "ERR-1", **row}, ("error", "method"), KEY) == ({}, False)

	def test_only_the_joined_pass_failing_gives_none_never_the_joined_raw_text(self, monkeypatch):
		# Each field was masked; the pass over the joined text failed. The
		# joined text completes "Bearer <token>", so it is never returned.
		real = maintenance._mask_row

		def _mask_row(row, text_fields, api_key, **kw):
			return None if text_fields == ("error",) else real(row, text_fields, api_key, **kw)
		monkeypatch.setattr(maintenance, "_mask_row", _mask_row)
		assert maintenance._masked_record({"error": "abcdefghij rest", "method": "x" * 140 + " Bearer"}, KEY) is None
		assert maintenance._masked_record({"error": "e", "method": "short"}, KEY) == {"error": "e", "method": "short"}

	def test_a_title_that_fits_is_left_as_it_is(self):
		title = "t" * 140
		assert maintenance._masked_record({"error": "e", "method": title}, KEY) == {"error": "e", "method": title}

	def test_a_smart_quote_key_is_masked(self):
		# The bare header values of the urllib3 frames, and the key
		# JSON-escaped in the metadata, only the stored key can find.
		smart = {"error": ANTHROPIC_TB, "metadata": json.dumps({"doc": f"key {ANTHROPIC_KEY}"})}
		assert "\\u2019" in smart["metadata"]
		row = maintenance._masked_record(smart, ANTHROPIC_KEY)
		assert "456789abcdef" not in json.dumps(row)
		assert "      value = ********\n" in row["error"]
		assert json.loads(row["metadata"]) == {"doc": "key ********"}

	def test_a_record_that_cannot_be_masked_or_is_not_a_record_gives_none(self, monkeypatch):
		real_mask = maintenance._mask

		def _mask(text, api_key, **kw):
			if "BOOM" in text:
				raise ValueError("catastrophic backtracking")
			return real_mask(text, api_key, **kw)
		monkeypatch.setattr(maintenance, "_mask", _mask)
		assert maintenance._masked_record({"error": LEAKY + "BOOM"}, KEY) is None
		assert maintenance._masked_record("not a record", KEY) is None

	def test_it_skips_the_residual_check(self, monkeypatch):
		# Its answer is never used there, and it is about a third of the
		# masking's time.
		checks = []
		monkeypatch.setattr(maintenance, "_has_residual_secret", lambda *a: checks.append(a) or False)
		record = {"error": LEAKY, "method": f"bad key {KEY}", "metadata": "{}"}
		masked = maintenance._masked_record(record, KEY)
		assert KEY not in json.dumps(masked) and checks == []
		assert maintenance._mask_row(record, ("error",), KEY) is not None and checks  # the stored-row path checks


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


class TestRecordValueLines:
	"""``_masked_record`` masks the bare header value lines (``value = ...``)
	only in a record from the AI code (a frame in Optimus's ``ai_fix.py``,
	under either package name, in its error, title or metadata) or one
	holding the stored key. Any other snapshot keeps them: scrub_secrets
	alone."""

	@pytest.mark.parametrize("api_key", [KEY, ""], ids=["key_stored", "no_key_stored"])
	def test_an_unrelated_snapshot_keeps_its_value_lines(self, api_key):
		row = maintenance._masked_record({"error": ERP_TB, "method": "Stock Entry failed"}, api_key)
		assert row["error"] == ERP_TB

	def test_another_apps_ai_fix_frame_is_not_optimus(self):
		error = ERP_TB.replace("erpnext/erpnext/stock/doctype/stock_entry/stock_entry.py", "other/other/openai_fix.py")
		assert "ai_fix.py" in error
		assert maintenance._masked_record({"error": error}, KEY)["error"] == error

	@pytest.mark.parametrize("package", ["optimus", "frappe_profiler"])
	def test_an_ai_snapshot_has_its_value_lines_masked(self, package):
		error = ERP_TB + f'  File "apps/{package}/{package}/ai_fix.py", line 1290, in _http_post\n'
		row = maintenance._masked_record({"error": error}, KEY)
		assert "Acme" not in row["error"] and "      value = ********\n" in row["error"]

	@pytest.mark.parametrize("field", ["method", "metadata"])
	def test_an_ai_frame_in_the_title_or_the_metadata_counts_too(self, field):
		# A 500 snapshot's title is the exception text, and a request's
		# metadata can quote a traceback: the frame may sit there alone.
		frame = 'File "apps/optimus/optimus/ai_fix.py", line 1290, in _http_post'
		row = maintenance._masked_record({"error": ERP_TB, field: frame}, KEY)
		assert "Acme" not in row["error"] and "      value = ********\n" in row["error"]

	def test_a_snapshot_holding_the_key_has_its_value_lines_masked(self):
		# No ai_fix.py frame, but the key is in its request metadata.
		record = {"error": ERP_TB, "metadata": json.dumps({"form_dict": {"ai_api_key": KEY}})}
		row = maintenance._masked_record(record, KEY)
		assert "Acme" not in row["error"] and KEY not in row["metadata"]


def _queue_calls(f) -> list:
	"""The Redis calls the scrub made that name Error Log's deferred-insert
	queue or the claim key an earlier release moved it to."""
	return [c for c in f.cache.calls if any("insert_queue_for_" in str(a) or "queue_claim" in str(a) for a in c[1:])]


class TestNoQueue:
	"""The Error Log hook masks every queued record as Frappe inserts it, so
	the scrub never reads or changes Frappe's deferred-insert queue, and a
	dry run does not touch Redis at all."""

	def test_a_dry_run_touches_no_redis(self, fake):
		f = fake([("a", LEAKY)])
		assert maintenance.scrub_error_log_secrets(dry_run=True) == {**_OUT, "candidates": 1, "changed": 1}
		assert f.cache.calls == []

	def test_a_real_run_never_touches_the_queue_and_inserts_nothing(self, fake):
		f = fake([("a", LEAKY)])
		assert maintenance.scrub_error_log_secrets(dry_run=False) == {**_OUT, "candidates": 1, "changed": 1}
		assert _queue_calls(f) == [] and f.inserted == []

	def test_the_result_has_no_queue_counts(self, fake):
		fake([])
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert set(out) == {"candidates", "changed", "deleted_docs_changed", "residual", "failed", "key_unreadable"}

	def test_the_queue_code_is_gone(self):
		for name in (
			"mask_error_log_queue", "_remask_error_log_queue", "_claim_queue", "_drain_claim", "_QUEUE_CLAIM",
			"_ERROR_LOG_QUEUE", "_error_log_queue_length", "_claim_length", "_read_queue",
		):
			assert not hasattr(maintenance, name), name

	def test_the_claim_key_is_no_longer_kept_across_clear_cache(self):
		from optimus import hooks

		assert not hasattr(hooks, "persistent_cache_keys")


class TestHooksCacheRefresh:
	"""Frappe caches every app's hooks under "app_hooks" (Redis, through
	``frappe.client_cache`` on v16 and ``frappe.cache`` on v15). A process
	started before the upgrade that misses that key after migrate's
	``clear_cache`` puts back the old hooks, without the Error Log hook, and
	every process reads them until the key is deleted again. A real scrub
	deletes the key, reloads the hooks in its own (new-code) process and
	drops that process's doc-event copy, before it reads any row."""

	def test_a_real_run_refreshes_the_hooks_before_reading_any_row(self, fake):
		f = fake([("a", LEAKY)])
		maintenance.scrub_error_log_secrets(dry_run=False)
		assert f.client_cache.calls == [("delete_value", "app_hooks")]
		# reloaded after the delete, before the first row was read
		assert f.hook_loads == [([("delete_value", "app_hooks")], 0)]
		assert f.local.doc_events_hooks is None
		assert f.cache.calls == []

	def test_on_frappe_v15_it_deletes_them_from_frappe_cache(self, fake):
		f = fake([("a", LEAKY)])
		f.client_cache = None
		maintenance.scrub_error_log_secrets(dry_run=False)
		assert f.cache.calls == [("delete_value", "app_hooks")] and len(f.hook_loads) == 1

	def test_a_dry_run_leaves_them(self, fake):
		f = fake([("a", LEAKY)])
		maintenance.scrub_error_log_secrets(dry_run=True)
		assert f.client_cache.calls == [] and f.cache.calls == [] and f.hook_loads == []
		assert f.local.doc_events_hooks == {"User": {}}

	@pytest.mark.parametrize("step", ["delete", "reload"])
	def test_a_refresh_that_fails_never_stops_the_scrub(self, fake, step):
		f = fake([("a", LEAKY)])

		def _boom(*a, **k):
			raise ConnectionError("Connection refused")
		if step == "delete":
			f.client_cache.delete_value = _boom
		else:
			f.get_hooks = _boom
		assert maintenance.scrub_error_log_secrets(dry_run=False) == {**_OUT, "candidates": 1, "changed": 1}
		assert maintenance._refresh_hooks_cache() is False

	def test_the_hooks_an_old_process_cached_are_replaced(self, monkeypatch):
		# Frappe v16's get_hooks and get_doc_hooks over a Redis that holds the
		# hooks an old process loaded (no Error Log event). Only the refresh
		# makes the Error Log hook reach get_doc_hooks.
		from optimus import hooks

		redis = {"app_hooks": {"doc_events": {"User": {"validate": ["optimus.install.on_user_role_change"]}}}}

		def _load_app_hooks():  # this process's modules: the new hooks.py
			return {"doc_events": {dt: {ev: [h] for ev, h in evs.items()} for dt, evs in hooks.doc_events.items()}}

		def get_hooks(hook=None, default=None):
			value = redis.get("app_hooks")
			if value is None:
				value = redis["app_hooks"] = _load_app_hooks()
			return value.get(hook, default) if hook else value

		local = SimpleNamespace(doc_events_hooks=None)

		def get_doc_hooks():
			if not local.doc_events_hooks:
				local.doc_events_hooks = get_hooks("doc_events", {})
			return local.doc_events_hooks
		client_cache = SimpleNamespace(delete_value=lambda key: redis.pop(key, None))
		monkeypatch.setattr(
			maintenance, "frappe", SimpleNamespace(client_cache=client_cache, get_hooks=get_hooks, local=local),
		)
		assert "Error Log" not in get_doc_hooks()  # stale, and cached in this process too
		assert maintenance._refresh_hooks_cache() is True
		assert get_doc_hooks()["Error Log"] == {"before_insert": ["optimus.error_log_mask.mask_error_log"]}


class TestKeyUnreadable:
	"""A key is stored but ``_current_key_or_empty`` answers "": the site's
	``encryption_key`` changed (a restore onto another site) or its
	encrypted copy in ``__Auth`` is gone. The scrub then cannot search for
	it or mask it by value, so it says so instead of reporting clean."""

	@pytest.mark.parametrize("dry_run", [True, False])
	def test_a_key_that_is_set_but_cannot_be_read_is_flagged_and_counted(self, fake, dry_run):
		f = fake([("a", LEAKY)], current_key="")
		f.singles[("Optimus Settings", "ai_api_key")] = "*" * len(KEY)  # what Frappe stores in the field
		out = maintenance.scrub_error_log_secrets(dry_run=dry_run)
		assert out["key_unreadable"] is True and out["failed"] == 1
		# the field itself, read plainly: never the decrypted value
		assert f.single_reads == [("Optimus Settings", "ai_api_key")]

	@pytest.mark.parametrize("stored", [None, "", "  "])
	def test_no_key_stored_is_not_unreadable(self, fake, stored):
		f = fake([("a", LEAKY)], current_key="")
		f.singles[("Optimus Settings", "ai_api_key")] = stored
		out = maintenance.scrub_error_log_secrets(dry_run=True)
		assert out["key_unreadable"] is False and out["failed"] == 0

	def test_a_key_that_was_read_is_not_checked_again(self, fake):
		f = fake([("a", LEAKY)])
		f.singles[("Optimus Settings", "ai_api_key")] = "*" * len(KEY)
		out = maintenance.scrub_error_log_secrets(dry_run=True)
		assert out["key_unreadable"] is False and out["failed"] == 0 and f.single_reads == []

	@pytest.mark.parametrize("dry_run", [True, False])
	def test_a_site_optimus_was_uninstalled_from_completes_clean(self, fake, dry_run):
		# The documented console route: uninstalling deletes Optimus
		# Settings (its DocType, its Singles values and its __Auth copy), so
		# the key reads "" and the field's read raises DoesNotExistError.
		# There is no stored key to be unreadable: nothing is flagged or
		# counted, and the rows are still masked by their shapes.
		f = fake([("a", LEAKY)], [("d1", json.dumps({"error": LEAKY}))], current_key="")
		f.installed_singles.clear()
		out = maintenance.scrub_error_log_secrets(dry_run=dry_run)
		assert out == {**_OUT, "candidates": 1, "changed": 1, "deleted_docs_changed": 1}
		assert f.single_reads == [("Optimus Settings", "ai_api_key")]
		assert ("Bearer ********" in f.tables["Error Log"]["a"]["error"]) is not dry_run

	@pytest.mark.parametrize("dry_run", [True, False])
	def test_a_read_that_fails_otherwise_counts_one_failed_and_flags_nothing(self, fake, dry_run):
		f = fake([("a", LEAKY)], current_key="")
		f.single_error = RuntimeError("Lost connection to server during query")
		out = maintenance.scrub_error_log_secrets(dry_run=dry_run)
		assert out == {**_OUT, "candidates": 1, "changed": 1, "failed": 1}

	def test_the_docstring_says_to_enter_the_old_key(self):
		# The scrub searches for the key that leaked: a new key would not find it.
		doc = " ".join(maintenance.scrub_error_log_secrets.__doc__.split())
		assert "enter the OLD key again in Optimus Settings" in doc

	def test_it_never_raises_even_without_frappes_error_class(self, fake, monkeypatch):
		f = fake([("a", LEAKY)], current_key="")
		f.installed_singles.clear()
		monkeypatch.delattr(_FakeFrappe, "DoesNotExistError")
		assert maintenance._key_unreadable("") is None  # an unknown failure, counted by the scrub


class TestKeyHandling:
	"""The frames of the scrub hold the key only under the names Frappe's
	traceback sanitizer and Sentry redact (``api_key``, ``secret``), and the
	module refuses to run inside an RQ job, whose failure log would store
	its frames' locals (the unmasked rows) with context."""

	@staticmethod
	def _holds(value, forms) -> bool:
		if isinstance(value, str):
			return value in forms
		if isinstance(value, (tuple, list)):
			return any(isinstance(v, str) and v in forms for v in value)
		return False

	@pytest.mark.parametrize("key", [KEY, ANTHROPIC_KEY], ids=["ascii", "smart_quote"])
	def test_no_maintenance_frame_holds_the_key_under_another_name(self, fake, monkeypatch, key):
		forms = {key, json.dumps(key)[1:-1]}
		data = json.dumps({"doctype": "Error Log", "method": f"HTTP 401: bad key {key}"})
		f = fake([("a", LEAKY.replace(KEY, key)), ("m", "Traceback ...\n")], [("d1", data)], current_key=key)
		f.tables["Error Log"]["m"]["method"] = f"HTTP 401: bad key {key}"
		out = {}
		offenders, seen = self._profiled(lambda: out.update(maintenance.scrub_error_log_secrets(dry_run=False)), forms)
		assert offenders == set()
		# positive control: the key-handling helpers did run, key in hand
		assert {"_holds_key", "_json_escaped", "_mask", "_key_fragment"} <= seen
		assert out["changed"] >= 2 and out["deleted_docs_changed"] == 1

	@staticmethod
	def _profiled(run, forms):
		"""Run ``run()`` and return the maintenance frames' locals holding
		the key under another name than ``api_key`` / ``secret``, and the
		names of the functions that ran."""
		offenders = set()
		seen = set()

		def _profile(frame, event, arg):
			if frame.f_code.co_filename != maintenance.__file__ or event not in ("call", "return"):
				return
			seen.add(frame.f_code.co_name)
			for name, value in frame.f_locals.items():
				if name not in ("api_key", "secret") and TestKeyHandling._holds(value, forms):
					offenders.add(f"{frame.f_code.co_name}.{name}")
		sys.setprofile(_profile)
		try:
			run()
		finally:
			sys.setprofile(None)
		return offenders, seen

	def test_a_key_written_plainly_into_the_field_is_held_as_secret(self, fake):
		# Frappe keeps asterisks in a Password field; a key written there
		# by hand is read, for the unreadable-key check, as ``secret``.
		f = fake([("a", "Traceback ...\n")], current_key="")
		f.singles[("Optimus Settings", "ai_api_key")] = KEY
		out = {}
		offenders, seen = self._profiled(lambda: out.update(maintenance.scrub_error_log_secrets(dry_run=True)), {KEY})
		assert offenders == set() and "_key_unreadable" in seen and out["key_unreadable"] is True

	@pytest.fixture
	def in_rq_job(self, monkeypatch):
		job = SimpleNamespace(id="job-1")
		monkeypatch.setitem(sys.modules, "rq", SimpleNamespace(get_current_job=lambda: job))

	@pytest.mark.parametrize("dry_run", [True, False])
	@pytest.mark.parametrize("func", ["scrub_error_log_secrets", "purge_ai_error_logs"])
	def test_refuses_to_run_inside_an_rq_job(self, fake, monkeypatch, in_rq_job, func, dry_run):
		f = fake([("a", LEAKY)], [("d1", LEAKY)])
		reads = []
		monkeypatch.setattr("optimus.ai_fix._current_key_or_empty", lambda: reads.append(1) or KEY)
		with pytest.raises(maintenance.InsideBackgroundJobError) as ei:
			getattr(maintenance, func)(dry_run=dry_run)
		message = str(ei.value)
		assert "bench execute" in message and "bench console" in message
		assert KEY not in message
		assert f.statements == [] and f.cache.calls == [] and f.writes == [] and f.deletes == [] and reads == []

	def test_runs_outside_a_job(self, fake, monkeypatch):
		monkeypatch.setitem(sys.modules, "rq", SimpleNamespace(get_current_job=lambda: None))
		fake([("a", LEAKY)])
		assert maintenance.scrub_error_log_secrets(dry_run=True)["changed"] == 1
		assert maintenance.purge_ai_error_logs(dry_run=True)["error_logs"] == 1

	def test_runs_where_rq_is_not_installed(self, fake, monkeypatch):
		monkeypatch.setitem(sys.modules, "rq", None)  # the import raises
		fake([("a", LEAKY)])
		assert maintenance.scrub_error_log_secrets(dry_run=True)["changed"] == 1
		assert maintenance.purge_ai_error_logs(dry_run=True)["error_logs"] == 1

	def test_runs_where_rq_cannot_say_whether_this_is_a_job(self, fake, monkeypatch):
		# get_current_job reads rq's connection stack; if it raises, the
		# guard treats it as "no job" (it fails open) instead of stopping.
		def _get_current_job():
			raise RuntimeError("No connection has been pushed")
		monkeypatch.setitem(sys.modules, "rq", SimpleNamespace(get_current_job=_get_current_job))
		fake([("a", LEAKY)])
		assert maintenance.scrub_error_log_secrets(dry_run=True)["changed"] == 1
		assert maintenance.purge_ai_error_logs(dry_run=True)["error_logs"] == 1

	def test_the_module_docstring_says_never_enqueue_it(self):
		doc = " ".join(maintenance.__doc__.split())
		assert "bench execute" in doc and "bench console" in doc and "never enqueue" in doc


class TestPurgeAiErrorLogs:
	def test_dry_run_counts_only(self, fake):
		f = fake([("a", LEAKY), ("b", CLEAN_AI), ("c", UNRELATED)], [("d1", LEAKY)])
		assert maintenance.purge_ai_error_logs(dry_run=True) == {"error_logs": 2, "deleted_documents": 1}
		assert f.deletes == [] and len(f.tables["Error Log"]) == 3

	def test_deletes_every_ai_row_and_its_copies(self, fake):
		f = fake([("a", LEAKY), ("b", CLEAN_AI), ("c", UNRELATED)], [("d1", LEAKY)])
		assert maintenance.purge_ai_error_logs(dry_run=False) == {"error_logs": 2, "deleted_documents": 1}
		assert set(f.tables["Error Log"]) == {"c"}
		assert f.tables["Deleted Document"] == {}


# ---------------------------------------------------------------------------
# The migrate patch
# ---------------------------------------------------------------------------

_PATCH = "optimus.patches.v0_12.scrub_ai_keys_from_error_log"
_REAL_MEASURE = maintenance.measure_scan_size


def _with_context(exc) -> str:
	"""Every frame's locals of ``exc`` and its chain: what Frappe's
	``get_traceback(with_context=True)`` writes to Patch Log under
	``bench migrate --skip-failing``, without its name-based redaction."""
	parts, seen = [], set()
	while exc is not None and id(exc) not in seen:
		seen.add(id(exc))
		parts.append(repr(exc))
		tb = exc.__traceback__
		while tb is not None:
			parts += [f"{k} = {v!r}" for k, v in tb.tb_frame.f_locals.items()]
			tb = tb.tb_next
		exc = exc.__cause__ if exc.__suppress_context__ else exc.__context__
	return "\n".join(parts)


_COMMAND = "execute optimus.maintenance.scrub_error_log_secrets --kwargs \"{'dry_run': False}\""
_CRUMB_TITLE = "Optimus: Error Log key scrub did not run"


@pytest.fixture(autouse=True)
def patch_logs(monkeypatch):
	"""Record the patch's Error Log breadcrumb and its ``optimus`` log lines.
	``frappe.log_error`` / ``frappe.logger`` are replaced on the module, so
	no row and no log file is written. ``active`` is the exception being
	handled while the row is written (None outside an ``except``)."""
	import frappe

	rec = SimpleNamespace(events=[], errors=[], lines=[])

	def _log_error(title=None, message=None, **kw):
		rec.events.append("log_error")
		rec.errors.append({"title": title, "message": message, "active": sys.exc_info()[0], **kw})

	def _logger(module=None, *a, **k):
		def _level(level):
			def _line(msg, *args, **kwargs):
				rec.events.append(level)
				rec.lines.append((module, level, msg))
			return _line
		return SimpleNamespace(**{level: _level(level) for level in ("debug", "info", "warning", "error")})
	monkeypatch.setattr(frappe, "log_error", _log_error, raising=False)
	monkeypatch.setattr(frappe, "logger", _logger, raising=False)
	return rec


@pytest.fixture
def patch_env(monkeypatch, patch_logs):
	calls = []

	def _scrub(**kw):
		calls.append(kw)
		return {"candidates": 3, "changed": 2, "deleted_docs_changed": 1, "residual": 0, "failed": 0}

	monkeypatch.setattr(maintenance, "measure_scan_size", lambda: maintenance.ScanSize(1000, None))
	monkeypatch.setattr(maintenance, "scrub_error_log_secrets", _scrub)
	_stub_refresh(monkeypatch, patch_logs.events)
	return calls


def _stub_refresh(monkeypatch, events, answer=True):
	"""Replace ``maintenance._refresh_hooks_cache``: each call is logged as
	"refresh" in ``events`` and answers ``answer`` (a callable is called)."""

	def _refresh():
		events.append("refresh")
		return answer() if callable(answer) else answer
	monkeypatch.setattr(maintenance, "_refresh_hooks_cache", _refresh)


def _one_summary_line(patch_logs) -> str:
	"""The one counts-only summary line, logged at ERROR: Frappe's loggers
	drop anything lower unless DEV_SERVER is set, so an info line never
	reached logs/optimus.log on a production site."""
	assert len(patch_logs.lines) == 1, patch_logs.lines
	module, level, line = patch_logs.lines[0]
	assert (module, level) == ("optimus", "error") and "\n" not in line and KEY not in line
	return line


def _scrub_answers(monkeypatch, **counts):
	"""The scrub answers ``counts`` over a clean run's."""
	out = {"candidates": 3, "changed": 0, "deleted_docs_changed": 0, "residual": 0, "failed": 0, **counts}
	monkeypatch.setattr(maintenance, "scrub_error_log_secrets", lambda **kw: dict(out))


_OFF_PEAK_NOTE = "on MariaDB, Error Log is locked while it is scanned, so on a busy site prefer off-peak"
_RUN_IT = f"Run it by hand ({_OFF_PEAK_NOTE}): bench --site <site> {_COMMAND}"
# Every outcome but a failed import prints it last; it repeats no command.
_QUEUE_LINE = (
	"Optimus: Error Log entries still queued in Redis are masked by Optimus when Frappe inserts them; nothing "
	"needs doing for the queue."
)
_NOT_MASKED_LINE = (
	"Optimus: Error Log rows, queued ones included, may be stored unmasked while optimus.maintenance cannot be "
	"imported; run the command above once it can."
)


def test_patch_runs_the_scrub_for_real(patch_env, patch_logs, capsys):
	importlib.import_module(_PATCH).execute()
	assert patch_env == [{"dry_run": False}]
	assert "refresh" not in patch_logs.events  # the scrub refreshed the hooks cache itself
	out = capsys.readouterr().out
	assert "masked AI API keys in 3 stored error row(s)" in out
	assert out.endswith(f"{_QUEUE_LINE}\n")
	assert patch_logs.errors == []  # no breadcrumb when the scrub ran
	line = _one_summary_line(patch_logs)
	assert line.endswith(
		"ran, candidates=3 changed=2 deleted_docs_changed=1 residual=0 failed=0 key_unreadable=0"
	)
	assert "queue" not in line


def test_patch_skips_when_the_size_cannot_be_read(patch_env, patch_logs, monkeypatch, capsys):
	def _count(*a, **k):
		raise QueryTimeoutError(f"Lost connection to server during query: {KEY}")
	db = SimpleNamespace(count=_count, sql=lambda *a, **k: ((5,),))
	monkeypatch.setattr(maintenance, "frappe", SimpleNamespace(db=db, cache=SimpleNamespace(llen=lambda key: 0)))
	monkeypatch.setattr(maintenance, "measure_scan_size", _REAL_MEASURE)
	importlib.import_module(_PATCH).execute()
	assert patch_env == []  # the scrub did not run
	out = capsys.readouterr().out
	assert "skipped the Error Log key scrub" in out and "its size could not be read (QueryTimeoutError)" in out
	assert _COMMAND in out and str(maintenance.SCAN_SIZE_UNKNOWN) not in out
	[crumb] = patch_logs.errors
	# the failing part's TYPE name, never its message
	assert crumb["message"].startswith("skipped: size unknown: QueryTimeoutError. ")
	line = _one_summary_line(patch_logs)
	assert "size could not be read (QueryTimeoutError)" in line
	for text in (out, repr(crumb), line):
		assert "Lost connection" not in text and KEY not in text


def test_patch_runs_the_scrub_at_exactly_the_limit(patch_env, monkeypatch):
	monkeypatch.setattr(maintenance, "measure_scan_size", lambda: maintenance.ScanSize(maintenance.MIGRATE_SCAN_LIMIT, None))
	importlib.import_module(_PATCH).execute()
	assert patch_env == [{"dry_run": False}]


def test_patch_skips_a_table_too_large_for_migrate(patch_env, patch_logs, monkeypatch, capsys):
	size = maintenance.MIGRATE_SCAN_LIMIT + 1
	monkeypatch.setattr(maintenance, "measure_scan_size", lambda: maintenance.ScanSize(size, None))
	importlib.import_module(_PATCH).execute()
	assert patch_env == []
	out = capsys.readouterr().out
	assert (
		f"skipped the Error Log key scrub during migrate ({size} Error Log and Deleted Document rows to read, "
		f"limit {maintenance.MIGRATE_SCAN_LIMIT})"
	) in out
	assert _COMMAND in out
	assert _OFF_PEAK_NOTE in out
	# one breadcrumb, so the skip is visible after the console is gone
	[crumb] = patch_logs.errors
	assert crumb["title"] == _CRUMB_TITLE and crumb["active"] is None
	assert crumb["message"].startswith(f"skipped: {size} rows")
	assert f"bench --site <site> {_COMMAND}" in crumb["message"]
	line = _one_summary_line(patch_logs)
	assert f"skipped, {size} Error Log and Deleted Document rows to read, limit " in line


@pytest.mark.parametrize("outcome", ["too large", "size unknown", "failed"])
def test_a_scrub_that_did_not_run_prints_one_instruction_line(patch_env, patch_logs, monkeypatch, capsys, outcome):
	# One line gives the command; the queue line after it repeats none.
	import frappe

	monkeypatch.setattr(frappe, "db", SimpleNamespace(rollback=lambda *a, **k: patch_logs.events.append("rollback")), raising=False)
	if outcome == "failed":
		def _scrub(**kw):
			raise ValueError("boom")
		monkeypatch.setattr(maintenance, "scrub_error_log_secrets", _scrub)
	else:
		rows = maintenance.MIGRATE_SCAN_LIMIT + 1 if outcome == "too large" else maintenance.SCAN_SIZE_UNKNOWN
		monkeypatch.setattr(maintenance, "measure_scan_size", lambda: maintenance.ScanSize(rows, "OperationalError"))
	importlib.import_module(_PATCH).execute()
	out = capsys.readouterr().out
	assert out.count(_COMMAND) == 1 and out.count("bench --site") == 1
	lines = out.splitlines()
	assert len(lines) == 2 and _RUN_IT in lines[0] and lines[1] == _QUEUE_LINE
	# rolled back, the hooks cache refreshed, then the breadcrumb
	assert patch_logs.events[:3] == ["rollback", "refresh", "log_error"]


@pytest.mark.parametrize(
	"counts", [{}, {"changed": 2}, {"failed": 1}, {"residual": 1}, {"key_unreadable": True, "failed": 1}],
	ids=["clean", "changed", "failed", "residual", "key_unreadable"],
)
def test_every_outcome_says_the_queue_needs_nothing(patch_env, monkeypatch, capsys, counts):
	_scrub_answers(monkeypatch, **counts)
	importlib.import_module(_PATCH).execute()
	out = capsys.readouterr().out
	assert out.endswith(f"{_QUEUE_LINE}\n") and out.count(_QUEUE_LINE) == 1
	assert "masked in Redis" not in out and "deferred-insert queue" not in out


def test_patch_does_not_ask_to_rotate_when_nothing_was_found(patch_env, monkeypatch, capsys):
	monkeypatch.setattr(
		maintenance, "scrub_error_log_secrets",
		lambda **kw: {"candidates": 5, "changed": 0, "deleted_docs_changed": 0, "residual": 0, "failed": 0},
	)
	importlib.import_module(_PATCH).execute()
	out = capsys.readouterr().out
	assert out == f"Optimus: found no AI API keys in stored error rows.\n{_QUEUE_LINE}\n"


@pytest.mark.parametrize(
	"counts",
	[{"failed": 2}, {"residual": 1}, {"key_unreadable": True, "failed": 1}],
	ids=["failed", "residual", "key_unreadable"],
)
def test_patch_prints_only_the_problem_lines_when_nothing_was_masked(patch_env, monkeypatch, capsys, counts):
	_scrub_answers(monkeypatch, **counts)
	importlib.import_module(_PATCH).execute()
	out = capsys.readouterr().out
	assert "Rotate" not in out and "found no AI API keys" not in out
	assert ("could not be processed" in out) is bool(counts.get("failed"))
	assert ("purge_ai_error_logs" in out) is bool(counts.get("residual"))
	assert ("cannot be decrypted" in out) is bool(counts.get("key_unreadable"))


def test_the_run_by_hand_hint_prefers_off_peak(patch_env, monkeypatch, capsys):
	monkeypatch.setattr(maintenance, "measure_scan_size", lambda: maintenance.ScanSize(maintenance.MIGRATE_SCAN_LIMIT + 1, None))
	importlib.import_module(_PATCH).execute()
	out = capsys.readouterr().out
	# only MariaDB's Error Log (MyISAM) is locked while it is scanned
	assert _RUN_IT in out
	assert "Run it now" not in out and "run it off-peak" not in out


def test_the_purge_hint_has_the_off_peak_note_too(patch_env, monkeypatch, capsys):
	_scrub_answers(monkeypatch, candidates=1, residual=1)
	importlib.import_module(_PATCH).execute()
	out = capsys.readouterr().out
	purge = "bench --site <site> execute optimus.maintenance.purge_ai_error_logs --kwargs"
	assert f"then delete them ({_OFF_PEAK_NOTE}): {purge} \"{{'dry_run': False}}\"" in out


_PARTIAL_TITLE = "Optimus: Error Log key scrub did not finish"


@pytest.mark.parametrize(
	"counts",
	[
		{"failed": 1}, {"residual": 2}, {"failed": 1, "residual": 2}, {"key_unreadable": True, "failed": 1},
		{"key_unreadable": True},
	],
	ids=["failed", "residual", "both", "key_unreadable", "key_unreadable_alone"],
)
def test_a_partial_scrub_leaves_a_breadcrumb(patch_env, patch_logs, monkeypatch, counts):
	# Patch Log marks the patch done: without a row, a scrub that could not
	# process every row, left key-shaped values or could not read the key
	# leaves no lasting trace. The flag alone is enough, whether or not the
	# scrub also counted it in failed.
	_scrub_answers(monkeypatch, candidates=5, changed=1, **counts)
	importlib.import_module(_PATCH).execute()
	[crumb] = patch_logs.errors
	assert crumb["title"] == _PARTIAL_TITLE and crumb["active"] is None
	c = {"failed": 0, "residual": 0, **counts}
	assert crumb["message"].startswith(
		f"failed={c['failed']} residual={c['residual']} key_unreadable={int(bool(counts.get('key_unreadable')))}. "
	)
	assert f"{_RUN_IT}" in crumb["message"]
	assert ("purge_ai_error_logs" in crumb["message"]) is bool(c["residual"])
	assert KEY not in repr(crumb) and "queue" not in crumb["message"]
	line = _one_summary_line(patch_logs)
	assert f"failed={c['failed']}" in line and f"residual={c['residual']}" in line


def test_an_unreadable_key_says_how_to_make_it_readable(patch_env, patch_logs, monkeypatch, capsys):
	_scrub_answers(monkeypatch, key_unreadable=True, failed=1)
	importlib.import_module(_PATCH).execute()
	# The scrub searches for the key that leaked: a new key would not find it.
	hint = (
		"The stored AI API key cannot be decrypted: restore the site's encryption_key, or enter the OLD key "
		"again in Optimus Settings, then run the scrub again."
	)
	[crumb] = patch_logs.errors
	assert f". {hint} {_RUN_IT}" in crumb["message"]
	out = capsys.readouterr().out
	assert (
		"Optimus: the AI API key stored in Optimus Settings cannot be decrypted, so the scrub could not search "
		"for it or mask it by value. Restore the site's encryption_key, or enter the OLD key again in Optimus "
		f"Settings, then run the scrub again: bench --site <site> {_COMMAND}\n"
	) in out


def test_a_complete_scrub_leaves_no_breadcrumb(patch_env, patch_logs, monkeypatch):
	monkeypatch.setattr(
		maintenance, "scrub_error_log_secrets",
		lambda **kw: {"candidates": 5, "changed": 5, "deleted_docs_changed": 2, "residual": 0, "failed": 0},
	)
	importlib.import_module(_PATCH).execute()
	assert patch_logs.errors == []


class _PgTransaction:
	"""Postgres after a failed statement: every later statement raises
	until ``rollback()``. ``written`` is what reached the table."""

	def __init__(self):
		self.aborted = False
		self.written = []
		self.rollbacks = 0

	def fail(self, exc):
		self.aborted = True
		raise exc

	def write(self, row):
		if self.aborted:
			raise InFailedSqlTransaction("current transaction is aborted, commands ignored until end of transaction block")
		self.written.append(row)

	def rollback(self, *a, **k):
		self.rollbacks += 1
		self.aborted = False


@pytest.fixture
def pg(monkeypatch, patch_logs):
	"""The patch on Postgres. ``frappe.log_error`` writes through the
	transaction; after ``execute()`` a test writes the Patch Log row as
	``execute_patch``'s ``update_patch_log`` does next."""
	import frappe

	txn = _PgTransaction()

	def _log_error(title=None, message=None, **kw):
		txn.write(("Error Log", title, message))
	monkeypatch.setattr(frappe, "db", SimpleNamespace(rollback=txn.rollback), raising=False)
	monkeypatch.setattr(frappe, "log_error", _log_error, raising=False)
	monkeypatch.setattr(maintenance, "measure_scan_size", _REAL_MEASURE)
	_stub_refresh(monkeypatch, patch_logs.events)
	return txn


def _pg_maintenance(monkeypatch, txn, count=lambda doctype: 10, deleted=5):
	db = SimpleNamespace(count=count, sql=lambda *a, **k: txn.write("select") or ((deleted,),))
	monkeypatch.setattr(maintenance, "frappe", SimpleNamespace(db=db, cache=SimpleNamespace(llen=lambda key: 0)))


@pytest.mark.parametrize("outcome", ["size query fails", "scrub fails", "too large"])
def test_on_postgres_the_patch_log_row_is_still_written(pg, monkeypatch, outcome):
	# A failed statement aborts the Postgres transaction: without a rollback
	# the breadcrumb and then execute_patch's update_patch_log fail, and
	# the migrate stops, on every retry.
	if outcome == "size query fails":
		_pg_maintenance(monkeypatch, pg, count=lambda doctype: pg.fail(QueryTimeoutError("canceling statement")))
	elif outcome == "scrub fails":
		_pg_maintenance(monkeypatch, pg)

		def _scrub(**kw):
			pg.fail(RuntimeError("could not update a chunk"))
		monkeypatch.setattr(maintenance, "scrub_error_log_secrets", _scrub)
	else:
		_pg_maintenance(monkeypatch, pg, deleted=maintenance.MIGRATE_SCAN_LIMIT + 1)
	assert importlib.import_module(_PATCH).execute() is None
	pg.write("Patch Log")  # raises if the transaction is still aborted
	crumbs = [r for r in pg.written if r[0] == "Error Log"]
	assert [c[1] for c in crumbs] == [_CRUMB_TITLE]  # written, after the rollback
	assert pg.written[-1] == "Patch Log"


@pytest.mark.parametrize("answer", ["false", "raises"])
def test_on_postgres_a_refresh_that_fails_is_rolled_back_before_the_breadcrumb(pg, patch_logs, monkeypatch, answer):
	# The refresh reloads the hooks, which reads the installed apps (a
	# SELECT). If that read fails it aborts the transaction, and the refresh
	# cannot say why: the patch rolls back again after a failed refresh.
	_pg_maintenance(monkeypatch, pg, deleted=maintenance.MIGRATE_SCAN_LIMIT + 1)

	def _aborting():
		pg.aborted = True  # the failed read
		if answer == "raises":
			raise RuntimeError("boom")
		return False
	_stub_refresh(monkeypatch, patch_logs.events, _aborting)
	assert importlib.import_module(_PATCH).execute() is None
	assert [r[1] for r in pg.written if r[0] == "Error Log"] == [_CRUMB_TITLE]
	pg.write("Patch Log")  # raises if the transaction is still aborted
	assert pg.rollbacks == 2


def test_an_import_failure_skips_the_refresh(patch_logs, monkeypatch, capsys):
	import frappe

	import optimus

	calls = []
	monkeypatch.delattr(optimus, "maintenance")
	monkeypatch.setitem(sys.modules, "optimus.maintenance", None)  # the import raises
	monkeypatch.setattr(frappe, "db", SimpleNamespace(rollback=lambda *a, **k: calls.append("rollback")), raising=False)
	assert importlib.import_module(_PATCH).execute() is None
	assert calls == ["rollback"]


@pytest.mark.parametrize("outcome", ["skipped", "partial"])
def test_on_postgres_a_failed_breadcrumb_is_rolled_back_too(pg, monkeypatch, outcome):
	# The breadcrumb's own INSERT can fail and abort the transaction.
	import frappe

	_pg_maintenance(monkeypatch, pg, deleted=maintenance.MIGRATE_SCAN_LIMIT + 1 if outcome == "skipped" else 5)
	monkeypatch.setattr(
		maintenance, "scrub_error_log_secrets",
		lambda **kw: {"candidates": 1, "changed": 0, "deleted_docs_changed": 0, "residual": 0, "failed": 1},
	)

	def _log_error(title=None, message=None, **kw):
		pg.fail(RuntimeError("value too long for type character varying(140)"))
	monkeypatch.setattr(frappe, "log_error", _log_error, raising=False)
	assert importlib.import_module(_PATCH).execute() is None
	pg.write("Patch Log")  # raises if the transaction is still aborted
	assert pg.written[-1] == "Patch Log"


def test_the_summary_line_reaches_the_log_in_production(patch_env, monkeypatch, capsys):
	"""Frappe's loggers sit at ERROR unless DEV_SERVER is set (``bench
	start``), so an info summary never reached logs/optimus.log on a
	production site. This runs Frappe's real ``get_logger`` level logic
	(a private copy of ``frappe/utils/logger.py`` with DEV_SERVER unset and
	stream-only handlers, so no log file is written)."""
	import importlib.util
	import logging

	import frappe

	real = pytest.importorskip("frappe.utils.logger", exc_type=ImportError)
	monkeypatch.delenv("DEV_SERVER", raising=False)
	monkeypatch.setattr(frappe, "_dev_server", 0, raising=False)
	monkeypatch.setenv("FRAPPE_STREAM_LOGGING", "1")
	spec = importlib.util.spec_from_file_location("_optimus_test_frappe_logger_patch", real.__file__)
	private = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(private)
	assert private.default_log_level == logging.ERROR
	monkeypatch.setattr(frappe, "loggers", {}, raising=False)
	monkeypatch.setattr(frappe, "log_level", None, raising=False)
	monkeypatch.setattr(frappe, "logger", lambda module=None, **k: private.get_logger(module=module, **k), raising=False)

	named = logging.getLogger("optimus-all")  # what get_logger names it without a site
	saved = (list(named.handlers), named.level, named.propagate)
	try:
		importlib.import_module(_PATCH).execute()
		assert named.level == logging.ERROR
	finally:
		for handler in [h for h in named.handlers if h not in saved[0]]:
			named.removeHandler(handler)
			handler.close()
		named.setLevel(saved[1])
		named.propagate = saved[2]
	assert "optimus scrub_ai_keys_from_error_log: ran, candidates=3" in capsys.readouterr().err


def test_patch_warns_about_residual_rows_with_the_purge_dry_run_first(patch_env, monkeypatch, capsys):
	monkeypatch.setattr(
		maintenance, "scrub_error_log_secrets",
		lambda **kw: {"candidates": 1, "changed": 0, "deleted_docs_changed": 0, "residual": 1, "failed": 0},
	)
	importlib.import_module(_PATCH).execute()
	out = capsys.readouterr().out
	dry = out.find("execute optimus.maintenance.purge_ai_error_logs --kwargs \"{'dry_run': True}\"")
	real = out.find("execute optimus.maintenance.purge_ai_error_logs --kwargs \"{'dry_run': False}\"")
	assert 0 <= dry < real


@pytest.fixture
def failing_scrub(monkeypatch, patch_logs):
	"""A scrub that fails while a frame local and its message hold a key;
	``frappe.db`` replaced wholesale to record the rollbacks."""
	import frappe

	def _scrub(**kw):
		row_text = f"headers = {{'authorization': 'Bearer {KEY}'}}"  # noqa: F841 (a frame local)
		raise ValueError(f"cannot update row holding {KEY}")

	def _rollback(*a, **k):
		patch_logs.events.append("rollback")
	monkeypatch.setattr(maintenance, "measure_scan_size", lambda: maintenance.ScanSize(10, None))
	monkeypatch.setattr(maintenance, "scrub_error_log_secrets", _scrub)
	monkeypatch.setattr(frappe, "db", SimpleNamespace(rollback=_rollback), raising=False)
	_stub_refresh(monkeypatch, patch_logs.events)


def test_a_failed_scrub_never_blocks_migrate(failing_scrub, patch_logs, capsys):
	assert importlib.import_module(_PATCH).execute() is None  # returns normally
	out = capsys.readouterr().out
	assert "failed (ValueError)" in out
	assert _COMMAND in out and _OFF_PEAK_NOTE in out
	assert KEY not in out
	# the failing chunk's writes rolled back, the hooks cache refreshed; the
	# breadcrumb is written after (so it survives the rollback), with no
	# exception being handled, and holds the type and the command
	assert patch_logs.events[:3] == ["rollback", "refresh", "log_error"]
	[crumb] = patch_logs.errors
	assert crumb["title"] == _CRUMB_TITLE and crumb["active"] is None
	assert crumb["message"].startswith("ValueError. ")
	assert f"bench --site <site> {_COMMAND}" in crumb["message"]
	assert KEY not in repr(crumb) and "cannot update" not in repr(crumb)
	line = _one_summary_line(patch_logs)
	assert "failed" in line and "ValueError" in line
	assert out.endswith(f"{_QUEUE_LINE}\n")


def test_an_import_failure_never_blocks_migrate(patch_logs, monkeypatch, capsys):
	# The module import runs inside the patch's try: a broken import is
	# reported by its type with the command, and the migrate continues. The
	# Error Log hook imports the same module, so the console does not say
	# queued entries are masked then.
	import frappe

	import optimus

	rollbacks = []
	monkeypatch.delattr(optimus, "maintenance")
	monkeypatch.setitem(sys.modules, "optimus.maintenance", None)  # the import raises
	monkeypatch.setattr(frappe, "db", SimpleNamespace(rollback=lambda *a, **k: rollbacks.append(1)), raising=False)
	assert importlib.import_module(_PATCH).execute() is None
	out = capsys.readouterr().out
	assert "failed (ModuleNotFoundError)" in out
	assert out.count(_COMMAND) == 1
	assert rollbacks == [1]
	assert out.endswith(f"{_NOT_MASKED_LINE}\n") and _QUEUE_LINE not in out
	[crumb] = patch_logs.errors
	assert crumb["message"].startswith("ModuleNotFoundError. ")


@pytest.mark.parametrize("outcome", ["ran", "skipped", "failed"])
def test_a_failing_breadcrumb_or_log_line_never_blocks_migrate(patch_env, monkeypatch, capsys, outcome):
	import frappe

	def _boom(*a, **k):
		raise RuntimeError("cannot write")
	monkeypatch.setattr(frappe, "log_error", _boom)
	monkeypatch.setattr(frappe, "logger", _boom)
	if outcome == "skipped":
		monkeypatch.setattr(maintenance, "measure_scan_size", lambda: maintenance.ScanSize(maintenance.MIGRATE_SCAN_LIMIT + 1, None))
	elif outcome == "failed":
		def _scrub(**kw):
			raise ValueError("boom")
		monkeypatch.setattr(maintenance, "scrub_error_log_secrets", _scrub)
		monkeypatch.setattr(frappe, "db", SimpleNamespace(rollback=lambda *a, **k: None), raising=False)
	assert importlib.import_module(_PATCH).execute() is None
	assert _COMMAND in capsys.readouterr().out or outcome == "ran"


def test_the_failure_report_holds_no_row_text(failing_scrub, monkeypatch):
	# If even the report failed, the with-context traceback Frappe would store
	# must hold no row text: the patch keeps only the exception type.
	patch_mod = importlib.import_module(_PATCH)

	def _print(*a, **k):
		raise OSError("stdout closed")
	monkeypatch.setattr(patch_mod, "print", _print, raising=False)
	with pytest.raises(OSError) as ei:
		patch_mod.execute()
	assert ei.value.__context__ is None
	assert KEY not in _with_context(ei.value)


def test_patch_reports_rows_that_could_not_be_masked(patch_env, monkeypatch, capsys):
	monkeypatch.setattr(
		maintenance, "scrub_error_log_secrets",
		lambda **kw: {"candidates": 3, "changed": 2, "deleted_docs_changed": 0, "residual": 0, "failed": 1},
	)
	importlib.import_module(_PATCH).execute()
	out = capsys.readouterr().out
	assert f"Optimus: 1 error row(s) could not be processed. {_RUN_IT}\n" in out


def test_patch_is_registered_post_model_sync():
	from pathlib import Path

	text = (Path(maintenance.__file__).parent / "patches.txt").read_text()
	post = text.split("[post_model_sync]", 1)[1]
	assert _PATCH in post
