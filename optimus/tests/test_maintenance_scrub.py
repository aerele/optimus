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
scrub must never call). ``cache`` is ``_FakeRedis``, Frappe's RedisWrapper
over an in-memory Redis.
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


class _FakeFrappe:
	"""``has_metadata=False`` models Frappe v15, whose Error Log has no
	``metadata`` column: a statement naming it fails. ``v15_like=True``
	models v15's ``db_query``, which doubles every backslash of a LIKE value
	before it reaches SQL. ``method`` is Data (varchar(140)) and a longer
	write fails as strict mode does. ``cache`` holds Error Log's
	deferred-insert queue, and ``get_doc(record).insert()`` adds a row
	(``inserted`` keeps each record as it reached the "database"; the scrub
	never inserts, Frappe's own flush does). ``singles`` holds the Singles
	values ``db.get_single_value`` reads (``single_reads`` logs each read)."""

	def __init__(self, error_logs, deleted_docs=(), has_metadata=True, v15_like=False):
		self.v15_like = v15_like
		self.cache = _FakeRedis()
		self.inserted = []
		self.singles = {}
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

	def _get_single_value(self, doctype, fieldname, cache=True):
		self.single_reads.append((doctype, fieldname))
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


# The scrub's result when nothing was found: every count 0.
_OUT = {
	"candidates": 0, "changed": 0, "deleted_docs_changed": 0, "residual": 0, "failed": 0, "queued": 0,
	"queue_masked": 0, "queue_unmasked": 0,
}


@pytest.fixture
def fake(monkeypatch):
	def _make(error_logs, deleted_docs=(), current_key=KEY, has_metadata=True, v15_like=False):
		f = _FakeFrappe(error_logs, deleted_docs, has_metadata=has_metadata, v15_like=v15_like)
		f.remasks = []
		monkeypatch.setattr(maintenance, "frappe", f)
		monkeypatch.setattr(maintenance, "safe_commit", f.commit)
		monkeypatch.setattr(
			maintenance, "_remask_error_log_queue",
			lambda *a: f.remasks.append(f.reads) or maintenance._QueueMasked(0, 0, 0, False),
		)
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
		assert f.writes == [] and f.commits == [] and f.remasks == []

	def test_scrubs_error_log_and_deleted_document_copies(self, fake):
		f = fake([("a", LEAKY), ("b", CLEAN_AI), ("c", UNRELATED)], [("d1", json.dumps({"error": LEAKY}))])
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert out == {**_OUT, "candidates": 2, "changed": 1, "deleted_docs_changed": 1}
		assert f.remasks == [0]  # the queue masked in Redis before the first read
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
		assert KEY not in f.tables["Error Log"]["a"]["error"] and f.remasks == [0]

	@pytest.mark.parametrize("value", [*TRUE, None], ids=repr)
	def test_a_true_value_or_none_writes_nothing(self, fake, value):
		f = fake([("a", LEAKY)], [("d1", LEAKY)])
		out = maintenance.scrub_error_log_secrets(dry_run=value)
		assert (out["changed"], out["deleted_docs_changed"]) == (1, 1)
		assert f.writes == [] and f.commits == [] and f.remasks == []

	def test_the_default_is_a_dry_run(self, fake):
		f = fake([("a", LEAKY), ("c", UNRELATED)])
		assert maintenance.scrub_error_log_secrets()["changed"] == 1
		assert maintenance.purge_ai_error_logs() == {"error_logs": 1, "deleted_documents": 0}
		assert f.writes == [] and f.deletes == [] and f.remasks == []

	@pytest.mark.parametrize("value", BAD, ids=repr)
	@pytest.mark.parametrize("func", ["scrub_error_log_secrets", "purge_ai_error_logs"])
	def test_anything_else_raises_before_reading_a_row(self, fake, func, value):
		f = fake([("a", LEAKY)], [("d1", LEAKY)])
		with pytest.raises(ValueError) as ei:
			getattr(maintenance, func)(dry_run=value)
		message = str(ei.value)
		for allowed in ("True", "False", "1", "0", '"true"', '"false"', '"yes"', '"no"'):
			assert allowed in message, allowed
		assert f.statements == [] and f.remasks == [] and f.writes == [] and f.deletes == []

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
	"""count(Error Log) + a bounded exact count of Deleted Document + the
	Error Log queue length. The Deleted Document count reads at most
	MIGRATE_SCAN_LIMIT + 1 rows: exact below the limit, above it once the
	table is larger, and portable (no estimate: v15's is not scoped to the
	site's schema, Postgres answers -1 for a table never analysed, InnoDB's
	is about a fifth low)."""

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

	def test_counts_error_logs_every_deleted_document_and_the_queue(self, monkeypatch):
		calls = self._frappe(monkeypatch)
		assert maintenance.scrub_scan_size() == 33
		assert maintenance.measure_scan_size() == (33, None)
		# every Deleted Document: deleted_doctype is not indexed, so the
		# passes read the whole table; the LIMIT is a bound parameter
		assert calls[:3] == [
			("count", ("Error Log",), {}),
			("sql", (self.BOUNDED, (maintenance.MIGRATE_SCAN_LIMIT + 1,)), {}),
			("llen", ("insert_queue_for_Error Log",), {}),
		]

	def test_a_deleted_document_table_past_the_limit_is_counted_only_up_to_it(self, monkeypatch):
		self._frappe(monkeypatch, deleted_documents=5_000_000)
		size = maintenance.scrub_scan_size()
		assert size == 10 + maintenance.MIGRATE_SCAN_LIMIT + 1 + 3
		assert size > maintenance.MIGRATE_SCAN_LIMIT  # the migrate skips it

	def test_a_deleted_document_table_at_the_limit_is_exact(self, monkeypatch):
		self._frappe(monkeypatch, deleted_documents=maintenance.MIGRATE_SCAN_LIMIT - 13)
		assert maintenance.scrub_scan_size() == maintenance.MIGRATE_SCAN_LIMIT

	@pytest.mark.parametrize("broken", ["count", "sql", "llen"])
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
			("llen", None, "no value"),
			("llen", -1, "negative value"),
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
		self._frappe(monkeypatch, fail={"llen": ConnectionError})
		assert maintenance.measure_scan_size().reason == "ConnectionError"
		self._frappe(monkeypatch)
		assert maintenance.measure_scan_size() == (33, None)


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


class ResponseError(Exception):
	"""redis-py's ``ResponseError``: Redis refused the command."""


class OutOfMemoryError(ResponseError):
	"""redis-py's ``OutOfMemoryError``: past ``maxmemory`` with the
	``noeviction`` policy, Redis refuses every command that adds data (an
	RPUSH) and still runs the others (a read, a pop, a rename)."""


class _FakeRedis:
	"""Frappe's ``RedisWrapper`` over an in-memory Redis.

	``make_key`` prefixes the site's db name and encodes, as
	``RedisWrapper.make_key`` does. The wrapper's ``llen`` / ``lpop`` /
	``rpush`` / ``get_keys`` / ``delete_value`` make the key and send the raw
	command, as RedisWrapper does. ``execute_command`` is the raw client: it
	takes made keys only (an unmade one fails the test). Lists hold bytes, a
	list that runs empty is deleted, and RENAME / RENAMENX of a missing key
	raise ``ResponseError("no such key")``, all as in Redis.

	``commands`` logs every raw command. ``fail`` maps a command (or "*",
	every command) to a function of its arguments that returns the exception
	to raise instead of running it, or None. ``lost`` holds commands that
	run and then raise, as when the connection drops before the reply.
	``before(command, args)`` runs before each raw command: another process
	acting in between."""

	DB = "_optimus_test_site"

	def __init__(self, queues=None):
		self.store = {}
		self.commands = []
		self.fail = {}
		self.lost = set()
		self.before = None
		for name, entries in (queues or {}).items():
			if entries:
				self.store[self.make_key(name)] = [e.encode() if isinstance(e, str) else e for e in entries]

	def make_key(self, key, user=None, shared=False):
		assert user is None and not shared
		return f"{self.DB}|{key}".encode()

	def _made(self, key):
		assert isinstance(key, bytes) and key.startswith(f"{self.DB}|".encode()), key
		return key

	def execute_command(self, command, *args, **options):
		self.commands.append((command, *args))
		if self.before is not None:
			self.before(command, args)
		check = self.fail.get(command) or self.fail.get("*")
		error = check(args) if check else None
		if error is not None:
			raise error
		result = self._run(command, args)
		if command in self.lost:
			raise ConnectionError("Connection closed by server.")
		return result

	def _run(self, command, args):
		if command == "EXISTS":
			return sum(1 for key in args if self._made(key) in self.store)
		if command == "LLEN":
			return len(self.store.get(self._made(args[0]), []))
		if command == "LPOP":
			queue = self.store.get(self._made(args[0]))
			if not queue:
				return None
			item = queue.pop(0)
			if not queue:
				del self.store[args[0]]
			return item
		if command == "RPUSH":
			queue = self.store.setdefault(self._made(args[0]), [])
			queue += [v.encode() if isinstance(v, str) else v for v in args[1:]]
			return len(queue)
		if command in ("RENAME", "RENAMENX"):
			src, dst = self._made(args[0]), self._made(args[1])
			if src not in self.store:
				raise ResponseError("no such key")
			if command == "RENAMENX" and dst in self.store:
				return False
			self.store[dst] = self.store.pop(src)
			return True
		if command == "KEYS":
			pattern = args[0].decode()
			return [key for key in self.store if fnmatch.fnmatchcase(key.decode(), pattern)]
		if command == "UNLINK":
			return sum(1 for key in args if self.store.pop(self._made(key), None) is not None)
		raise AssertionError(f"unexpected Redis command {command}")

	# RedisWrapper's own methods: each makes the key, then sends the command.
	def llen(self, key):
		return self.execute_command("LLEN", self.make_key(key))

	def lpop(self, key):
		return self.execute_command("LPOP", self.make_key(key))

	def rpush(self, key, value):
		return self.execute_command("RPUSH", self.make_key(key), value)

	def get_keys(self, key):
		return self.execute_command("KEYS", self.make_key(key + "*"))

	def delete_value(self, keys, make_keys=True):
		if keys:
			self.execute_command("UNLINK", *(self.make_key(k) if make_keys else k for k in keys))

	def entries(self, name) -> list[str]:
		"""The entries of the list ``name`` (an unmade key), as text."""
		return [e.decode() for e in self.store.get(self.make_key(name), [])]


Q = "insert_queue_for_Error Log"
CLAIM = "optimus_error_log_queue_claim"


def _frappe_save_to_db(cache, insert, cap=10_000):
	"""Frappe's ``frappe.deferred_insert.save_to_db`` (v16; v15 caps at 500),
	which bench migrate runs right after the patches and the scheduler every
	15 minutes: it finds the queues by the ``insert_queue_for_`` prefix, reads
	the doctype from the rest of the key, and inserts every record it pops as
	it is (``insert`` gets the record with its doctype set)."""
	for key in cache.get_keys("insert_queue_for_"):
		count = 0
		queue_key = key.decode().split("|")[1]
		doctype = key.decode().split("insert_queue_for_")[1]
		while cache.llen(queue_key) > 0 and count <= cap:
			records = json.loads(cache.lpop(queue_key).decode("utf-8"))
			for record in [records] if isinstance(records, dict) else records:
				count += 1
				record.update({"doctype": doctype})
				insert(record)


# Frappe's own persistent_cache_keys (frappe/hooks.py, v16; v15 has all but
# "concurrency:*").
_FRAPPE_PERSISTENT_CACHE_KEYS = (
	"changelog-*", "insert_queue_for_*", "recorder-*", "global_search_queue", "monitor-transactions",
	"rate-limit-counter-*", "rl:*", "concurrency:*",
)


def _frappe_clear_cache(cache):
	"""What ``frappe.clear_cache()`` (no doctype, no user) does to Redis, as
	bench migrate's setUp runs it: delete every key of the site except those
	matching an app's ``persistent_cache_keys`` (``frappe/cache_manager.py``
	``clear_cache`` on v16, ``frappe/__init__.py`` on v15). Optimus's entries
	are read from its real hooks module."""
	from optimus import hooks

	doomed = set(cache.get_keys(""))
	for key in (*_FRAPPE_PERSISTENT_CACHE_KEYS, *getattr(hooks, "persistent_cache_keys", ())):
		doomed.difference_update(cache.get_keys(key))
	cache.delete_value(list(doomed), make_keys=False)


class _NoDatabase:
	"""``frappe.db`` for the re-mask: any use of it is logged in ``used``
	and fails."""

	def __init__(self):
		self.used = []

	def __getattr__(self, name):
		self.used.append(name)
		raise AssertionError(f"frappe.db.{name} used")


@pytest.fixture
def redis(monkeypatch):
	"""``maintenance.frappe`` with a ``_FakeRedis`` cache and no database:
	``r.db.used`` logs any database use, ``r.touched`` any insert or commit."""

	def _make(queues=None):
		r = _FakeRedis(queues)
		r.db = _NoDatabase()
		r.touched = []
		frappe = SimpleNamespace(cache=r, db=r.db, get_doc=lambda *a, **k: r.touched.append("get_doc"))
		monkeypatch.setattr(maintenance, "frappe", frappe)
		monkeypatch.setattr(maintenance, "safe_commit", lambda: r.touched.append("commit"))
		return r
	return _make


def _masked(record, api_key=KEY):
	"""The one-record entry the re-mask pushes for ``record``."""
	return [maintenance._masked_record(record, api_key)]


class TestRemaskErrorLogQueue:
	"""The scrub no longer inserts queued Error Log records. It claims the
	queue at once (RENAMENX to a key Frappe never reads), masks each entry,
	pushes the masked records back onto the queue, and leaves inserting them
	to Frappe's own ``save_to_db``."""

	def test_claims_masks_and_pushes_so_frappes_flush_inserts_each_masked_row_once(self, redis):
		leaky = {
			"error": LEAKY,
			"method": f"The AI provider returned an error (HTTP 401): bad key {KEY}",
			"metadata": json.dumps({"form_dict": {"doc": f'{{"ai_api_key": "{KEY}"}}'}}),
			"reference_doctype": "Optimus Session", "reference_name": "s-1", "trace_id": "t-1",
		}
		pair = [{"error": "x"}, {"error": f"y api_key={KEY}"}]
		r = redis({Q: [json.dumps(leaky), json.dumps(pair)], "insert_queue_for_Route History": [json.dumps({"route": "app"})]})
		assert maintenance._remask_error_log_queue(KEY) == (2, 0, 0, False)
		# the whole queue claimed at once, then only ever pushed onto: never popped
		assert ("RENAMENX", r.make_key(Q), r.make_key(CLAIM)) in r.commands
		assert not [c for c in r.commands if c[0] == "LPOP" and c[1] == r.make_key(Q)]
		assert r.make_key(CLAIM) not in r.store
		assert r.entries(Q) == [json.dumps(_masked(leaky)), json.dumps([maintenance._masked_record(x, KEY) for x in pair])]
		assert r.entries("insert_queue_for_Route History") == [json.dumps({"route": "app"})]  # not Error Log's
		# a second run changes nothing, so a re-run never inserts twice
		assert maintenance._remask_error_log_queue(KEY) == (2, 0, 0, False)
		rows = []
		_frappe_save_to_db(r, rows.append)
		logs = [{k: v for k, v in row.items() if k != "doctype"} for row in rows if row["doctype"] == "Error Log"]
		assert logs == [maintenance._masked_record(x, KEY) for x in (leaky, *pair)]  # each once
		assert not [row for row in rows if KEY in json.dumps(row)]
		first = logs[0]
		assert "'Bearer ********'" in first["error"] and first["method"].endswith("bad key ********")
		assert json.loads(first["metadata"])  # still valid JSON
		assert (first["reference_doctype"], first["reference_name"], first["trace_id"]) == ("Optimus Session", "s-1", "t-1")
		assert r.touched == [] and r.db.used == []  # Frappe inserted them, not the scrub

	def test_a_claim_an_interrupted_run_left_is_drained_before_the_queue_is_claimed(self, redis):
		left = [{"error": f"L{i} api_key={KEY}"} for i in range(2)]
		new = [{"error": f"R{i} api_key={KEY}"} for i in range(2)]
		r = redis({CLAIM: [json.dumps(x) for x in left], Q: [json.dumps(x) for x in new]})
		# The leftover's masked entries join the queue, so the claim takes
		# them again with it and they are pushed twice (masking is
		# idempotent): 2 + 4 pushes.
		assert maintenance._remask_error_log_queue(KEY) == (6, 0, 0, False)
		assert r.entries(Q) == [json.dumps(_masked(x)) for x in new + left]
		assert r.make_key(CLAIM) not in r.store
		# the leftover first, so the rename found no claim and moved the queue
		assert [c[0] for c in r.commands] == [
			"EXISTS", "LLEN", *["LPOP", "RPUSH"] * 2, "RENAMENX", "LLEN", *["LPOP", "RPUSH"] * 4,
		]

	def test_a_claim_that_appears_just_before_the_rename_is_never_renamed_over(self, redis):
		# Another run claimed the queue after this run's check, and a new
		# snapshot was queued since. Renaming over that claim would drop the
		# entries it holds.
		other = [{"error": f"C{i} api_key={KEY}"} for i in range(2)]
		newer = json.dumps({"error": "a new snapshot"})
		r = redis({Q: [newer]})

		def _other_run(command, args):
			if command in ("RENAME", "RENAMENX") and r.make_key(CLAIM) not in r.store:
				r.store[r.make_key(CLAIM)] = [json.dumps(x).encode() for x in other]
		r.before = _other_run
		assert maintenance._remask_error_log_queue(KEY) == (2, 0, 0, False)
		assert r.entries(Q) == [newer, *[json.dumps(_masked(x)) for x in other]]

	def test_a_refused_push_stops_it_and_holds_the_rest_back_in_the_claim(self, redis):
		# Redis past maxmemory with noeviction: pops still work, pushes fail.
		# Popping on would lose every entry after the first refused push.
		entries = [{"error": f"E{i} api_key={KEY}"} for i in range(5)]
		r = redis({Q: [json.dumps(x) for x in entries]})
		pushes = []

		def _rpush(args):
			pushes.append(args)
			return OutOfMemoryError("OOM command not allowed when used memory > 'maxmemory'.") if len(pushes) > 2 else None
		r.fail["RPUSH"] = _rpush
		assert maintenance._remask_error_log_queue(KEY) == (2, 2, 1, True)
		assert [c[0] for c in r.commands].count("LPOP") == 3  # none after the refused push
		held = [json.dumps(x) for x in entries[3:]]
		assert r.entries(CLAIM) == held  # E2, popped and refused, is the one entry lost
		rows = []
		_frappe_save_to_db(r, rows.append)
		assert [row["error"] for row in rows] == ["E0 api_key=********", "E1 api_key=********"]
		assert r.entries(CLAIM) == held  # Frappe never inserts the claim
		# the next run, with Redis taking writes again, masks what was held
		# back (pushed twice: from the leftover claim, then with the queue)
		del r.fail["RPUSH"]
		assert maintenance._remask_error_log_queue(KEY) == (4, 0, 0, False)
		assert r.entries(Q) == [json.dumps(_masked(x)) for x in entries[3:]]
		assert r.make_key(CLAIM) not in r.store

	def test_a_failed_pop_stops_it_and_the_rest_stays_in_the_claim(self, redis):
		r = redis({Q: [json.dumps({"error": e}) for e in "abc"]})
		pops = []

		def _lpop(args):
			pops.append(args)
			return ConnectionError("Connection reset by peer") if len(pops) > 1 else None
		r.fail["LPOP"] = _lpop
		assert maintenance._remask_error_log_queue(KEY) == (1, 2, 1, True)
		assert r.entries(CLAIM) == [json.dumps({"error": e}) for e in "bc"]
		assert r.entries(Q) == [json.dumps([{"error": "a"}])]

	def test_a_consumer_popping_the_queue_meanwhile_never_gets_a_raw_entry(self, redis):
		# Frappe's scheduler runs save_to_db every 15 minutes, and bench
		# migrate does not pause it: it can pop the queue while this runs.
		entries = [{"error": f"E{i} api_key={KEY}", "method": f"bad key {KEY}"} for i in range(6)]
		r = redis({Q: [json.dumps(x) for x in entries]})
		got = []

		def _save_to_db(command, args):
			# between any two commands, once the run takes entries
			queue = r.store.get(r.make_key(Q))
			if queue and any(c[0] == "LPOP" for c in r.commands):
				got.append(queue.pop(0).decode())
				if not queue:
					del r.store[r.make_key(Q)]
		r.before = _save_to_db
		maintenance._remask_error_log_queue(KEY)
		assert got  # the consumer did take entries
		taken = got + r.entries(Q)
		assert sorted(taken) == sorted(json.dumps(_masked(x)) for x in entries)
		assert not [e for e in taken if KEY in e]

	def test_an_entry_that_is_not_json_or_not_a_record_is_counted_and_dropped(self, redis):
		# save_to_db would fail on it; a record that is not a dict cannot be masked.
		queue = [json.dumps({"error": f"x api_key={KEY}"}), "{not json", json.dumps([{"error": "z"}, "not a record"]), "5"]
		r = redis({Q: queue})
		assert maintenance._remask_error_log_queue(KEY) == (2, 0, 3, False)
		assert r.entries(Q) == [json.dumps([{"error": "x api_key=********"}]), json.dumps([{"error": "z"}])]

	def test_a_record_that_cannot_be_masked_is_dropped_never_pushed_back_raw(self, redis, monkeypatch):
		real_mask = maintenance._mask

		def _mask(text, api_key, **kw):
			if "BOOM" in text:
				raise ValueError("catastrophic backtracking")
			return real_mask(text, api_key, **kw)
		monkeypatch.setattr(maintenance, "_mask", _mask)
		r = redis({Q: [json.dumps({"error": LEAKY + "BOOM"}), json.dumps([{"error": "z"}, {"error": LEAKY + "BOOM"}])]})
		assert maintenance._remask_error_log_queue(KEY) == (1, 0, 2, False)
		assert r.entries(Q) == [json.dumps([{"error": "z"}])]

	def test_it_never_touches_the_database(self, redis):
		# A record too large for max_allowed_packet kills a MariaDB
		# connection on INSERT: the re-mask has no insert, read or commit.
		r = redis({CLAIM: [json.dumps({"error": LEAKY})], Q: [json.dumps({"error": LEAKY, "method": "t" * 300})]})
		assert maintenance._remask_error_log_queue(KEY) == (3, 0, 0, False)
		assert r.db.used == [] and r.touched == []
		assert len(r.entries(Q)) == 2 and not [e for e in r.entries(Q) if KEY in e]

	def test_an_empty_queue_is_not_a_failure(self, redis):
		# RENAMENX of a missing key fails ("no such key"): nothing to claim.
		r = redis({})
		assert maintenance._remask_error_log_queue(KEY) == (0, 0, 0, False)
		assert "RENAMENX" in [c[0] for c in r.commands]

	def test_a_rename_refused_while_the_queue_is_there_counts_once(self, redis):
		r = redis({Q: [json.dumps({"error": "x"})]})
		r.fail["RENAMENX"] = lambda args: ResponseError("READONLY You can't write against a read only replica.")
		assert maintenance._remask_error_log_queue(KEY) == (0, 0, 1, True)
		assert r.entries(Q) == [json.dumps({"error": "x"})]

	def test_a_rename_whose_answer_was_lost_is_still_drained(self, redis):
		# The rename ran, then the connection dropped: the claim holds the
		# queue, and it is drained anyway.
		r = redis({Q: [json.dumps({"error": f"x api_key={KEY}"})]})
		r.lost = {"RENAMENX"}
		assert maintenance._remask_error_log_queue(KEY) == (1, 0, 0, False)
		assert r.entries(Q) == [json.dumps([{"error": "x api_key=********"}])]
		assert r.make_key(CLAIM) not in r.store

	def test_redis_down_counts_once(self, redis):
		r = redis({Q: [json.dumps({"error": "x"})]})
		r.fail["*"] = lambda args: ConnectionError("Error 61 connecting to 127.0.0.1:13000. Connection refused.")
		assert maintenance._remask_error_log_queue(KEY) == (0, 0, 1, True)

	def test_redis_going_down_after_the_first_command_counts_once(self, redis):
		# The rename, the check of the queue and the drain all fail.
		r = redis({Q: [json.dumps({"error": "x"})]})
		r.fail["*"] = lambda args: ConnectionError("Connection reset by peer") if len(r.commands) > 1 else None
		assert maintenance._remask_error_log_queue(KEY) == (0, 0, 1, True)
		assert [c[0] for c in r.commands] == ["EXISTS", "RENAMENX", "EXISTS", "LLEN"]

	def test_a_refused_push_while_draining_a_leftover_stops_the_run(self, redis):
		# The queue is not claimed then: the claim key still holds entries.
		left = [{"error": f"L{i}"} for i in range(3)]
		r = redis({CLAIM: [json.dumps(x) for x in left], Q: [json.dumps({"error": "new"})]})
		r.fail["RPUSH"] = lambda args: OutOfMemoryError("OOM command not allowed when used memory > 'maxmemory'.")
		assert maintenance._remask_error_log_queue(KEY) == (0, 2, 1, True)
		assert r.entries(CLAIM) == [json.dumps(x) for x in left[1:]]
		assert r.entries(Q) == [json.dumps({"error": "new"})]
		assert "RENAMENX" not in [c[0] for c in r.commands]

	def test_the_drain_is_bounded_by_the_claims_length_when_it_starts(self, redis):
		r = redis({Q: [json.dumps({"error": str(i)}) for i in range(3)]})

		def _refill(command, args):
			if command == "LPOP":
				r.store.setdefault(r.make_key(CLAIM), []).append(json.dumps({"error": "late"}).encode())
		r.before = _refill
		maintenance._remask_error_log_queue(KEY)
		assert [c[0] for c in r.commands].count("LPOP") == 3

	def test_entries_queued_after_the_claim_are_left_as_they_are(self, redis):
		# Only the entries the claim took are masked, and the queue is only
		# ever added to: a new snapshot keeps its place.
		new = json.dumps({"error": "a new snapshot"})
		r = redis({Q: [json.dumps({"error": str(i)}) for i in range(2)]})

		def _producer(command, args):
			if command == "LPOP":
				r.store.setdefault(r.make_key(Q), []).append(new.encode())
		r.before = _producer
		assert maintenance._remask_error_log_queue(KEY) == (2, 0, 0, False)
		assert r.entries(Q) == [new, json.dumps([{"error": "0"}]), new, json.dumps([{"error": "1"}])]

	def test_the_claim_key_is_outside_frappes_queue_prefix(self, redis):
		# save_to_db finds its queues by the prefix and reads the doctype
		# from the rest of the key: the claim must never look like a queue.
		r = redis({CLAIM: [json.dumps({"error": "held"})], Q: [json.dumps({"error": "x"})]})
		assert r.get_keys("insert_queue_for_") == [r.make_key(Q)]
		assert not maintenance._QUEUE_CLAIM.startswith("insert_queue_for_")

	@pytest.mark.parametrize("with_key", [False, True])
	def test_a_long_queued_title_goes_in_front_of_the_error_as_v16_does(self, redis, with_key):
		# Frappe v16's ErrorLog.validate moves a title over 140 characters in
		# front of the error and cuts it; v15 has no such validate and fails
		# the insert (CharacterLengthExceededError), which save_to_db logs
		# and drops. Doing it in Redis stores the same row on both.
		title = ("T" * 150 + (f" key {KEY} " if with_key else " ") + "tail ").ljust(300, "z")
		assert len(title) == 300
		r = redis({Q: [json.dumps({"error": "Traceback ...", "method": title})]})
		maintenance._remask_error_log_queue(KEY)
		[[row]] = [json.loads(e) for e in r.entries(Q)]
		full = title.replace(KEY, "********")
		assert row["error"] == f"{full}\nTraceback ..."  # the full title is kept
		assert row["method"] == full[:140] and len(row["method"]) == 140  # v15's length check passes
		assert KEY not in json.dumps(row)
		assert _v16_error_log_validate(dict(row)) == row  # v16's validate has nothing left to do

	def test_a_masked_title_longer_than_the_column_goes_in_front_of_the_error(self, redis):
		# Masking lengthens "u:p" to "********": the full masked title goes
		# in front of the error, and the title is cut to 140.
		title = "POST " + "x" * 90 + " via http://u:p@proxy.example/v1 "
		title += "y" * (140 - len(title))
		assert len(title) == 140
		r = redis({Q: [json.dumps([{"error": "x", "method": title}])]})
		maintenance._remask_error_log_queue(KEY)
		[[row]] = [json.loads(e) for e in r.entries(Q)]
		masked_title = title.replace("u:p@", "********@")
		assert row["error"] == f"{masked_title}\nx"
		assert row["method"] == masked_title[:140] and "u:p@" not in row["method"]

	def test_a_queued_title_that_fits_is_left_as_it_is(self, redis):
		title = "t" * 140
		r = redis({Q: [json.dumps({"error": "e", "method": title})]})
		maintenance._remask_error_log_queue(KEY)
		assert r.entries(Q) == [json.dumps([{"error": "e", "method": title}])]

	def test_a_queued_smart_quote_key_is_masked(self, redis):
		# The bare header values of the urllib3 frames, and the key
		# JSON-escaped in the metadata, only the key read first can find.
		smart = {"error": ANTHROPIC_TB, "metadata": json.dumps({"doc": f"key {ANTHROPIC_KEY}"})}
		assert "\\u2019" in smart["metadata"]
		r = redis({Q: [json.dumps(smart)]})
		assert maintenance._remask_error_log_queue(ANTHROPIC_KEY).failed == 0
		[[row]] = [json.loads(e) for e in r.entries(Q)]
		assert "456789abcdef" not in json.dumps(row)
		assert "      value = ********\n" in row["error"]
		assert json.loads(row["metadata"]) == {"doc": "key ********"}



class TestClaimKeySurvivesClearCache:
	"""bench migrate's setUp runs ``frappe.clear_cache()``, which deletes
	every Redis key of the site except the ``persistent_cache_keys`` of the
	installed apps: without its hook entry, the claim an interrupted run
	left (entries it could not mask) would be dropped."""

	def test_the_hook_names_the_claim_key(self):
		from optimus import hooks

		assert maintenance._QUEUE_CLAIM in hooks.persistent_cache_keys

	def test_clear_cache_between_two_runs_keeps_the_claim(self, redis):
		entries = [{"error": f"E{i} api_key={KEY}"} for i in range(3)]
		r = redis({Q: [json.dumps(x) for x in entries], "bootinfo": ["cached"]})
		r.fail["RPUSH"] = lambda args: OutOfMemoryError("OOM command not allowed when used memory > 'maxmemory'.")
		assert maintenance._remask_error_log_queue(KEY).unmasked == 2
		_frappe_clear_cache(r)
		assert r.make_key("bootinfo") not in r.store  # the cache was cleared
		assert r.entries(CLAIM) == [json.dumps(x) for x in entries[1:]]  # the claim was kept
		del r.fail["RPUSH"]
		assert maintenance._remask_error_log_queue(KEY) == (4, 0, 0, False)
		assert r.entries(Q) == [json.dumps(_masked(x)) for x in entries[1:]]


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


class TestQueuedValueLines:
	"""The re-mask masks the bare header value lines (``value = ...``) only
	in a queued record from the AI code (a frame in Optimus's ``ai_fix.py``,
	under either package name, in its error, title or metadata) or one
	holding the stored key. Any other snapshot keeps them: scrub_secrets
	alone."""

	def _remasked(self, redis, record, api_key=KEY):
		r = redis({Q: [json.dumps(record)]})
		assert maintenance._remask_error_log_queue(api_key) == (1, 0, 0, False)
		[[row]] = [json.loads(e) for e in r.entries(Q)]
		return row

	@pytest.mark.parametrize("api_key", [KEY, ""], ids=["key_stored", "no_key_stored"])
	def test_an_unrelated_snapshot_keeps_its_value_lines(self, redis, api_key):
		row = self._remasked(redis, {"error": ERP_TB, "method": "Stock Entry failed"}, api_key)
		assert row["error"] == ERP_TB

	def test_another_apps_ai_fix_frame_is_not_optimus(self, redis):
		error = ERP_TB.replace("erpnext/erpnext/stock/doctype/stock_entry/stock_entry.py", "other/other/openai_fix.py")
		assert "ai_fix.py" in error
		assert self._remasked(redis, {"error": error})["error"] == error

	@pytest.mark.parametrize("package", ["optimus", "frappe_profiler"])
	def test_an_ai_snapshot_has_its_value_lines_masked(self, redis, package):
		error = ERP_TB + f'  File "apps/{package}/{package}/ai_fix.py", line 1290, in _http_post\n'
		row = self._remasked(redis, {"error": error})
		assert "Acme" not in row["error"] and "      value = ********\n" in row["error"]

	def test_a_snapshot_holding_the_key_has_its_value_lines_masked(self, redis):
		# No ai_fix.py frame, but the key is in its request metadata.
		record = {"error": ERP_TB, "metadata": json.dumps({"form_dict": {"ai_api_key": KEY}})}
		row = self._remasked(redis, record)
		assert "Acme" not in row["error"] and KEY not in row["metadata"]


_REAL_REMASK = maintenance._remask_error_log_queue


class TestQueuedRows:
	"""The scrub reads the key first, masks Error Log's deferred-insert queue
	in Redis with it (a real run only), and reports the queue."""

	def _real_remask(self, fake, monkeypatch, queue, claim=(), **kw):
		f = fake([("a", LEAKY)], **kw)
		f.cache = _FakeRedis({Q: list(queue), CLAIM: list(claim)})
		monkeypatch.setattr(maintenance, "_remask_error_log_queue", _REAL_REMASK)
		return f

	def test_the_key_is_read_before_the_queue_is_touched(self, fake, monkeypatch):
		f = self._real_remask(fake, monkeypatch, [json.dumps({"error": f"late row: api_key={KEY}"})])
		events = []
		f.cache.before = lambda command, args: events.append(command)
		monkeypatch.setattr("optimus.ai_fix._current_key_or_empty", lambda: events.append("key") or KEY)
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert events[0] == "key" and events.count("key") == 1 and "LPOP" in events
		assert f.cache.entries(Q) == [json.dumps([{"error": "late row: api_key=********"}])]
		assert (out["failed"], out["queued"], out["queue_masked"], out["queue_unmasked"]) == (0, 1, 1, 0)

	def test_the_queue_is_masked_before_any_row_is_read(self, fake, monkeypatch):
		# A failing statement later in the scan cannot stop it.
		f = self._real_remask(fake, monkeypatch, [json.dumps({"error": "x"})])
		reads_at_pop = []
		f.cache.before = lambda command, args: command == "LPOP" and reads_at_pop.append(f.reads)
		maintenance.scrub_error_log_secrets(dry_run=False)
		assert reads_at_pop == [0]

	def test_a_real_run_masks_the_queue_and_inserts_nothing(self, fake, monkeypatch):
		queue = [json.dumps({"error": f"late row {i}: api_key={KEY}"}) for i in range(3)]
		f = self._real_remask(fake, monkeypatch, queue)
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert (out["queue_masked"], out["queue_unmasked"], out["queued"], out["failed"]) == (3, 0, 3, 0)
		assert f.inserted == []  # Frappe's own flush inserts them
		assert not [e for e in f.cache.entries(Q) if KEY in e]

	def test_a_stop_reports_the_entries_held_back(self, fake, monkeypatch):
		queue = [json.dumps({"error": f"E{i}"}) for i in range(4)]
		f = self._real_remask(fake, monkeypatch, queue)
		f.cache.fail["RPUSH"] = lambda args: OutOfMemoryError("OOM command not allowed when used memory > 'maxmemory'.")
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert (out["queue_masked"], out["queue_unmasked"], out["queued"], out["failed"]) == (0, 3, 0, 1)

	def test_a_dry_run_reports_the_queue_and_the_claim_and_never_pops(self, fake, monkeypatch):
		f = self._real_remask(fake, monkeypatch, [json.dumps({"error": LEAKY})] * 4, claim=[json.dumps({"error": LEAKY})] * 2)
		out = maintenance.scrub_error_log_secrets(dry_run=True)
		assert (out["queued"], out["queue_unmasked"], out["queue_masked"], out["failed"]) == (4, 2, 0, 0)
		assert {c[0] for c in f.cache.commands} == {"LLEN"}
		assert len(f.cache.entries(Q)) == 4 and f.inserted == []

	@pytest.mark.parametrize("dry_run", [True, False])
	def test_a_queue_that_cannot_be_read_counts_as_failed(self, fake, dry_run):
		f = fake([("a", CLEAN_AI)])
		f.cache.fail["*"] = lambda args: ConnectionError("Error 61 connecting to 127.0.0.1:13000. Connection refused.")
		out = maintenance.scrub_error_log_secrets(dry_run=dry_run)
		assert (out["queued"], out["queue_unmasked"], out["failed"]) == (0, 0, 1)

	def test_one_redis_outage_counts_once(self, fake, monkeypatch):
		# The re-mask cannot reach Redis and neither can the final count:
		# one outage, one failure.
		f = self._real_remask(fake, monkeypatch, [json.dumps({"error": "x"})])
		f.cache.fail["*"] = lambda args: ConnectionError("Error 61 connecting to 127.0.0.1:13000. Connection refused.")
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert (out["queued"], out["queue_masked"], out["failed"]) == (0, 0, 1)

	def test_a_queue_failure_after_a_clean_remask_still_counts(self, fake, monkeypatch):
		f = self._real_remask(fake, monkeypatch, [json.dumps({"error": "x"})])
		lens = []

		def _llen(args):
			lens.append(args)
			return ConnectionError("redis went away") if len(lens) > 1 else None
		f.cache.fail["LLEN"] = _llen
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert len(lens) == 2 and (out["queue_masked"], out["queued"], out["failed"]) == (1, 0, 1)


class TestMaskErrorLogQueue:
	"""The queue half of the scrub alone, for the migrate patch when the
	scrub was skipped or failed."""

	def test_reads_the_key_masks_the_queue_and_reports_it(self, redis, monkeypatch):
		r = redis({Q: [json.dumps({"error": f"x api_key={KEY}"})], CLAIM: [json.dumps({"error": "held"})]})
		monkeypatch.setattr("optimus.ai_fix._current_key_or_empty", lambda: KEY)
		out = maintenance.mask_error_log_queue()
		# the leftover pushed, then claimed again with the queue: 1 + 2
		assert out == {"queue_masked": 3, "queue_unmasked": 0, "queued": 2, "failed": 0}
		assert not [e for e in r.entries(Q) if KEY in e]
		assert r.db.used == [] and r.touched == []  # past the key read, Redis only

	def test_a_redis_outage_counts_once(self, redis, monkeypatch):
		r = redis({Q: [json.dumps({"error": "x"})]})
		r.fail["*"] = lambda args: ConnectionError("Error 61 connecting to 127.0.0.1:13000. Connection refused.")
		monkeypatch.setattr("optimus.ai_fix._current_key_or_empty", lambda: KEY)
		assert maintenance.mask_error_log_queue() == {"queue_masked": 0, "queue_unmasked": 0, "queued": 0, "failed": 1}

	def test_refuses_to_run_inside_an_rq_job(self, redis, monkeypatch):
		r = redis({Q: [json.dumps({"error": f"x api_key={KEY}"})]})
		monkeypatch.setitem(sys.modules, "rq", SimpleNamespace(get_current_job=lambda: SimpleNamespace(id="job-1")))
		with pytest.raises(maintenance.InsideBackgroundJobError):
			maintenance.mask_error_log_queue()
		assert r.commands == []


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
		f.cache = _FakeRedis({
			Q: [json.dumps({"error": f"queued api_key={key}"})], CLAIM: [json.dumps({"error": f"held api_key={key}"})],
		})
		monkeypatch.setattr(maintenance, "_remask_error_log_queue", _REAL_REMASK)
		out = {}
		offenders, seen = self._profiled(lambda: out.update(maintenance.scrub_error_log_secrets(dry_run=False)), forms)
		assert offenders == set()
		# positive control: the key-handling helpers did run, key in hand
		assert {
			"_holds_key", "_json_escaped", "_mask", "_remask_error_log_queue", "_drain_claim", "_masked_record",
			"_key_fragment",
		} <= seen
		assert out["changed"] >= 2 and out["deleted_docs_changed"] == 1
		assert f.cache.entries(Q) and not [e for e in f.cache.entries(Q) if key in e]

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
		assert f.statements == [] and f.remasks == [] and f.writes == [] and f.deletes == [] and reads == []

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
def patch_env(monkeypatch):
	calls = []

	def _scrub(**kw):
		calls.append(kw)
		return {"candidates": 3, "changed": 2, "deleted_docs_changed": 1, "residual": 0, "failed": 0, "queued": 0}

	monkeypatch.setattr(maintenance, "measure_scan_size", lambda: maintenance.ScanSize(1000, None))
	monkeypatch.setattr(maintenance, "scrub_error_log_secrets", _scrub)
	return calls


def _one_summary_line(patch_logs) -> str:
	"""The one counts-only summary line, logged at ERROR: Frappe's loggers
	drop anything lower unless DEV_SERVER is set, so an info line never
	reached logs/optimus.log on a production site."""
	assert len(patch_logs.lines) == 1, patch_logs.lines
	module, level, line = patch_logs.lines[0]
	assert (module, level) == ("optimus", "error") and "\n" not in line and KEY not in line
	return line


def test_patch_runs_the_scrub_for_real(patch_env, patch_logs, capsys):
	importlib.import_module(_PATCH).execute()
	assert patch_env == [{"dry_run": False}]
	assert "masked AI API keys in 3 stored error row(s)" in capsys.readouterr().out
	assert patch_logs.errors == []  # no breadcrumb when the scrub ran
	line = _one_summary_line(patch_logs)
	for count in ("candidates=3", "changed=2", "deleted_docs_changed=1", "residual=0", "failed=0", "queued=0"):
		assert count in line


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
	assert "skipped the Error Log key scrub" in out
	assert _COMMAND in out
	assert "locked while it is scanned" in out and "off-peak" in out
	# one breadcrumb, so the skip is visible after the console is gone
	[crumb] = patch_logs.errors
	assert crumb["title"] == _CRUMB_TITLE and crumb["active"] is None
	assert crumb["message"].startswith(f"skipped: {size} rows")
	assert f"bench --site <site> {_COMMAND}" in crumb["message"]
	line = _one_summary_line(patch_logs)
	assert "skipped" in line and str(size) in line


def test_patch_does_not_ask_to_rotate_when_nothing_was_found(patch_env, monkeypatch, capsys):
	monkeypatch.setattr(
		maintenance, "scrub_error_log_secrets",
		lambda **kw: {"candidates": 5, "changed": 0, "deleted_docs_changed": 0, "residual": 0, "failed": 0},
	)
	importlib.import_module(_PATCH).execute()
	out = capsys.readouterr().out
	assert out == "Optimus: found no AI API keys in stored error rows.\n"


@pytest.mark.parametrize(("failed", "residual", "queued"), [(2, 0, 0), (0, 1, 0), (0, 0, 4)])
def test_patch_prints_only_the_problem_lines_when_nothing_was_masked(
	patch_env, monkeypatch, capsys, failed, residual, queued,
):
	monkeypatch.setattr(
		maintenance, "scrub_error_log_secrets",
		lambda **kw: {
			"candidates": 3, "changed": 0, "deleted_docs_changed": 0, "residual": residual, "failed": failed,
			"queued": queued,
		},
	)
	importlib.import_module(_PATCH).execute()
	out = capsys.readouterr().out
	assert "Rotate" not in out and "found no AI API keys" not in out
	assert ("could not be processed" in out) is bool(failed)
	assert ("purge_ai_error_logs" in out) is bool(residual)
	assert ("Error Log entries still in the deferred-insert queue: 4." in out) is bool(queued)


def test_the_queued_line_says_migrate_inserts_them_and_to_run_the_scrub_again(patch_env, monkeypatch, capsys):
	monkeypatch.setattr(
		maintenance, "scrub_error_log_secrets",
		lambda **kw: {"candidates": 3, "changed": 0, "deleted_docs_changed": 0, "residual": 0, "failed": 0, "queued": 1},
	)
	importlib.import_module(_PATCH).execute()
	# "unless the flush stopped early": a queue that failed mid-flush can
	# leave entries it never masked, and failed counts that.
	assert capsys.readouterr().out == (
		"Optimus: Error Log entries still in the deferred-insert queue: 1. The scrub masked the ones waiting "
		"when it started, unless the flush stopped early (see the failed count); bench migrate inserts them "
		"all right after the patches. Run the scrub again after the restart to mask them in the table: "
		f"bench --site <site> {_COMMAND}\n"
	)


def test_the_run_by_hand_hint_prefers_off_peak(patch_env, monkeypatch, capsys):
	monkeypatch.setattr(maintenance, "measure_scan_size", lambda: maintenance.ScanSize(maintenance.MIGRATE_SCAN_LIMIT + 1, None))
	importlib.import_module(_PATCH).execute()
	out = capsys.readouterr().out
	assert (
		"Run it by hand (Error Log is locked while it is scanned, so on a busy site prefer off-peak): "
		f"bench --site <site> {_COMMAND}"
	) in out
	assert "Run it now" not in out and "run it off-peak" not in out


def test_the_purge_hint_has_the_off_peak_note_too(patch_env, monkeypatch, capsys):
	monkeypatch.setattr(
		maintenance, "scrub_error_log_secrets",
		lambda **kw: {"candidates": 1, "changed": 0, "deleted_docs_changed": 0, "residual": 1, "failed": 0, "queued": 0},
	)
	importlib.import_module(_PATCH).execute()
	out = capsys.readouterr().out
	purge = "bench --site <site> execute optimus.maintenance.purge_ai_error_logs --kwargs"
	assert (
		f"then delete them (Error Log is locked while it is scanned, so on a busy site prefer off-peak): "
		f"{purge} \"{{'dry_run': False}}\""
	) in out


_PARTIAL_TITLE = "Optimus: Error Log key scrub did not finish"


@pytest.mark.parametrize(("failed", "residual", "queued"), [(1, 0, 0), (0, 2, 0), (1, 0, 3), (1, 2, 3)])
def test_a_partial_scrub_leaves_a_breadcrumb(patch_env, patch_logs, monkeypatch, failed, residual, queued):
	# Patch Log marks the patch done: without a row, a scrub that could not
	# process every row or left key-shaped values leaves no lasting trace.
	monkeypatch.setattr(
		maintenance, "scrub_error_log_secrets",
		lambda **kw: {
			"candidates": 5, "changed": 1, "deleted_docs_changed": 0, "residual": residual, "failed": failed,
			"queued": queued,
		},
	)
	importlib.import_module(_PATCH).execute()
	[crumb] = patch_logs.errors
	assert crumb["title"] == _PARTIAL_TITLE and crumb["active"] is None
	assert crumb["message"].startswith(f"failed={failed} residual={residual} queued={queued}. Run it by hand (")
	assert f"bench --site <site> {_COMMAND}" in crumb["message"]
	assert ("purge_ai_error_logs" in crumb["message"]) is bool(residual)
	assert KEY not in repr(crumb)
	line = _one_summary_line(patch_logs)
	assert f"failed={failed}" in line and f"residual={residual}" in line and f"queued={queued}" in line


def test_entries_still_queued_alone_leave_no_breadcrumb(patch_env, patch_logs, monkeypatch, capsys):
	# queued also counts Frappe's own new error snapshots (a server error,
	# any error in developer mode), so on a busy site it is rarely 0: it is
	# printed and logged, but it is not a scrub that "did not finish".
	monkeypatch.setattr(
		maintenance, "scrub_error_log_secrets",
		lambda **kw: {"candidates": 5, "changed": 1, "deleted_docs_changed": 0, "residual": 0, "failed": 0, "queued": 3},
	)
	importlib.import_module(_PATCH).execute()
	assert patch_logs.errors == []
	assert "Error Log entries still in the deferred-insert queue: 3." in capsys.readouterr().out
	assert "queued=3" in _one_summary_line(patch_logs)


def test_a_complete_scrub_leaves_no_breadcrumb(patch_env, patch_logs, monkeypatch):
	monkeypatch.setattr(
		maintenance, "scrub_error_log_secrets",
		lambda **kw: {"candidates": 5, "changed": 5, "deleted_docs_changed": 2, "residual": 0, "failed": 0, "queued": 0},
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


@pytest.mark.parametrize("outcome", ["skipped", "partial"])
def test_on_postgres_a_failed_breadcrumb_is_rolled_back_too(pg, monkeypatch, outcome):
	# The breadcrumb's own INSERT can fail and abort the transaction.
	import frappe

	_pg_maintenance(monkeypatch, pg, deleted=maintenance.MIGRATE_SCAN_LIMIT + 1 if outcome == "skipped" else 5)
	monkeypatch.setattr(
		maintenance, "scrub_error_log_secrets",
		lambda **kw: {"candidates": 1, "changed": 0, "deleted_docs_changed": 0, "residual": 0, "failed": 1, "queued": 0},
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

	real = pytest.importorskip("frappe.utils.logger")
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
	``frappe.db`` replaced wholesale to record the rollback."""
	import frappe

	def _scrub(**kw):
		row_text = f"headers = {{'authorization': 'Bearer {KEY}'}}"  # noqa: F841 (a frame local)
		raise ValueError(f"cannot update row holding {KEY}")

	rollbacks = []

	def _rollback(*a, **k):
		rollbacks.append(1)
		patch_logs.events.append("rollback")
	monkeypatch.setattr(maintenance, "measure_scan_size", lambda: maintenance.ScanSize(10, None))
	monkeypatch.setattr(maintenance, "scrub_error_log_secrets", _scrub)
	monkeypatch.setattr(frappe, "db", SimpleNamespace(rollback=_rollback), raising=False)
	return rollbacks


def test_a_failed_scrub_never_blocks_migrate(failing_scrub, patch_logs, capsys):
	assert importlib.import_module(_PATCH).execute() is None  # returns normally
	out = capsys.readouterr().out
	assert "failed (ValueError)" in out
	assert _COMMAND in out and "locked while it is scanned" in out
	assert KEY not in out
	assert failing_scrub == [1]  # the failing chunk's writes rolled back
	# the breadcrumb is written after the rollback (so it survives it), with
	# no exception being handled, and holds the type and the command only
	assert patch_logs.events.index("rollback") < patch_logs.events.index("log_error")
	[crumb] = patch_logs.errors
	assert crumb["title"] == _CRUMB_TITLE and crumb["active"] is None
	assert crumb["message"].startswith("ValueError") and f"bench --site <site> {_COMMAND}" in crumb["message"]
	assert KEY not in repr(crumb) and "cannot update" not in repr(crumb)
	line = _one_summary_line(patch_logs)
	assert "failed" in line and "ValueError" in line


def test_an_import_failure_never_blocks_migrate(patch_logs, monkeypatch, capsys):
	# The module import runs inside the patch's try: a broken import is
	# reported by its type with the command, and the migrate continues.
	import frappe

	import optimus

	rollbacks = []
	monkeypatch.delattr(optimus, "maintenance")
	monkeypatch.setitem(sys.modules, "optimus.maintenance", None)  # the import raises
	monkeypatch.setattr(frappe, "db", SimpleNamespace(rollback=lambda *a, **k: rollbacks.append(1)), raising=False)
	assert importlib.import_module(_PATCH).execute() is None
	out = capsys.readouterr().out
	assert "failed (ModuleNotFoundError)" in out
	assert _COMMAND in out
	assert rollbacks == [1]
	[crumb] = patch_logs.errors
	assert crumb["message"].startswith("ModuleNotFoundError")


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
	# failed also counts queued entries that could not be inserted or masked
	assert (
		"Optimus: 1 error row(s) or queued entries could not be processed. Run it by hand ("
		"Error Log is locked while it is scanned, so on a busy site prefer off-peak): "
		f"bench --site <site> {_COMMAND}\n"
	) in out
	assert "could not be masked" not in out


def test_patch_is_registered_post_model_sync():
	from pathlib import Path

	text = (Path(maintenance.__file__).parent / "patches.txt").read_text()
	post = text.split("[post_model_sync]", 1)[1]
	assert _PATCH in post
