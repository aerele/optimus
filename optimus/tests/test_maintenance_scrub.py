# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""optimus.maintenance: scrub / purge of the AI Error Log rows written before
the key-leak fix, and the v0_12 patch that runs the scrub on migrate.

``maintenance.frappe`` is replaced wholesale by an in-memory fake that
implements just the ORM calls the module makes (``get_all`` with LIKE / = / >
/ <= filters, ``or_filters``, ``order_by="name asc"``, ``limit_start``,
``limit_page_length``; ``db.set_value`` with a field dict; ``db.count``;
``db.sql`` (the bounded count); ``db.has_column``; ``db.delete``;
savepoints; ``cache.llen`` / ``cache.lpop`` / ``cache.rpush``;
``get_doc(...).insert``).
"""

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
	(``inserted`` keeps each record as it reached the "database")."""

	def __init__(self, error_logs, deleted_docs=(), has_metadata=True, v15_like=False):
		self.v15_like = v15_like
		self.cache = _FakeCache({})
		self.inserted = []
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
		self.db_down = False  # select 1 fails, as in an outage
		self.db = SimpleNamespace(
			set_value=self._set_value, delete=self._delete, count=self._count, has_column=self._has_column,
			savepoint=self.txn.savepoint, release_savepoint=self.txn.release_savepoint,
			rollback=self.txn.rollback, sql=self._sql,
		)

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
		assert query == "select 1"
		if self.db_down:
			raise RuntimeError("Lost connection to server during query")
		return ((1,),)

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


@pytest.fixture
def fake(monkeypatch):
	def _make(error_logs, deleted_docs=(), current_key=KEY, has_metadata=True, v15_like=False):
		f = _FakeFrappe(error_logs, deleted_docs, has_metadata=has_metadata, v15_like=v15_like)
		f.flushes = []
		monkeypatch.setattr(maintenance, "frappe", f)
		monkeypatch.setattr(maintenance, "safe_commit", f.commit)
		monkeypatch.setattr(
			maintenance, "_flush_deferred_error_logs",
			lambda *a: f.flushes.append(f.reads) or maintenance._Flushed(0, False),
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
		assert out == {"candidates": 2, "changed": 1, "deleted_docs_changed": 1, "residual": 0, "failed": 0, "queued": 0}
		assert f.writes == [] and f.commits == [] and f.flushes == []

	def test_scrubs_error_log_and_deleted_document_copies(self, fake):
		f = fake([("a", LEAKY), ("b", CLEAN_AI), ("c", UNRELATED)], [("d1", json.dumps({"error": LEAKY}))])
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert out == {"candidates": 2, "changed": 1, "deleted_docs_changed": 1, "residual": 0, "failed": 0, "queued": 0}
		assert f.flushes == [0]  # deferred rows inserted before the first read
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
		assert out == {"candidates": 3, "changed": 2, "deleted_docs_changed": 0, "residual": 0, "failed": 0, "queued": 0}
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
		assert KEY not in f.tables["Error Log"]["a"]["error"] and f.flushes == [0]

	@pytest.mark.parametrize("value", [*TRUE, None], ids=repr)
	def test_a_true_value_or_none_writes_nothing(self, fake, value):
		f = fake([("a", LEAKY)], [("d1", LEAKY)])
		out = maintenance.scrub_error_log_secrets(dry_run=value)
		assert (out["changed"], out["deleted_docs_changed"]) == (1, 1)
		assert f.writes == [] and f.commits == [] and f.flushes == []

	def test_the_default_is_a_dry_run(self, fake):
		f = fake([("a", LEAKY), ("c", UNRELATED)])
		assert maintenance.scrub_error_log_secrets()["changed"] == 1
		assert maintenance.purge_ai_error_logs() == {"error_logs": 1, "deleted_documents": 0}
		assert f.writes == [] and f.deletes == [] and f.flushes == []

	@pytest.mark.parametrize("value", BAD, ids=repr)
	@pytest.mark.parametrize("func", ["scrub_error_log_secrets", "purge_ai_error_logs"])
	def test_anything_else_raises_before_reading_a_row(self, fake, func, value):
		f = fake([("a", LEAKY)], [("d1", LEAKY)])
		with pytest.raises(ValueError) as ei:
			getattr(maintenance, func)(dry_run=value)
		message = str(ei.value)
		for allowed in ("True", "False", "1", "0", '"true"', '"false"', '"yes"', '"no"'):
			assert allowed in message, allowed
		assert f.statements == [] and f.flushes == [] and f.writes == [] and f.deletes == []

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


class _FakeCache:
	"""``broken`` fails every call (Redis down); ``broken_pop`` fails only
	``lpop``; ``refill`` is an entry a busy producer pushes back after every
	pop. More than 1000 pops raise, so a runaway loop fails fast. ``lpop``
	returns bytes, as Redis does; ``pushes`` records every ``rpush``."""

	def __init__(self, queues, broken=False, broken_pop=False, refill=None):
		self.queues = queues
		self.broken = broken
		self.broken_pop = broken_pop
		self.refill = refill
		self.pops = 0
		self.pushes = []

	def llen(self, key):
		if self.broken:
			raise ConnectionError("redis down")
		return len(self.queues.get(key) or [])

	def lpop(self, key):
		if self.broken or self.broken_pop:
			raise ConnectionError("redis down")
		self.pops += 1
		if self.pops > 1000:
			raise RuntimeError("runaway flush")
		queue = self.queues.setdefault(key, [])
		item = queue.pop(0) if queue else None
		if self.refill is not None:
			queue.append(self.refill)
		return item.encode() if isinstance(item, str) else item

	def rpush(self, key, value):
		if self.broken:
			raise ConnectionError("redis down")
		self.pushes.append((key, value))
		self.queues.setdefault(key, []).append(value)


class TestUnderSavepoint:
	"""The one savepoint helper of the flush's inserts and the scrub's
	updates: True when the write and the savepoint's release succeeded;
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


def _replay_frappe_flush(cache, insert):
	"""Frappe's ``save_to_db`` for the Error Log queue, which bench migrate
	runs right after the patches: every entry left is inserted as it is."""
	queue = cache.queues.get("insert_queue_for_Error Log") or []
	while queue:
		records = json.loads(queue.pop(0))
		for record in records if isinstance(records, list) else [records]:
			insert(record)


class TestFlushDeferredErrorLogs:
	def _frappe(
		self, monkeypatch, cache, fail_on=(), stored_then_fail=lambda error: False, transactional=False, outage=False,
	):
		"""A failed insert aborts the transaction, as a failed statement does
		on Postgres: every later statement except a ROLLBACK fails until a
		rollback to a savepoint that was set (``_FakeTxn``).

		``stored_then_fail(error)`` picks records whose INSERT runs and a
		hook after it then raises, as a failing ``after_insert`` does. The
		row stays in ``self.table`` after the rollback to the savepoint, as
		in a MyISAM table, unless ``transactional`` (the rollback removes
		it, as on Postgres). ``frappe.db.exists`` reads ``self.table``.
		``fail_on`` is a set of ``error`` values or a predicate over it.
		``outage``: the database is down, so ``select 1`` fails too
		(``self.probes`` counts the calls)."""
		fails = fail_on if callable(fail_on) else (lambda error: error in fail_on)
		self.probes = 0
		inserted, commits = [], []
		state = {"aborted": False}
		self.db_log = []
		self.table = {}  # name -> record, every row the "database" holds
		since_savepoint = []
		txn = self.txn = _FakeTxn(self.db_log)

		def _live():
			if state["aborted"]:
				raise RuntimeError("current transaction is aborted")

		def _store(doc, record):
			doc.name = f"e{len(self.table) + 1:04d}"
			self.table[doc.name] = record
			since_savepoint.append(doc.name)

		def _insert(doc, record):
			_live()
			self.db_log.append(("insert", record.get("error")))
			if fails(record.get("error")):
				state["aborted"] = True
				raise RuntimeError("Lost connection to server during query" if outage else "Duplicate entry")
			_store(doc, record)
			if stored_then_fail(record.get("error")):
				raise RuntimeError("after_insert hook failed")
			inserted.append(record)

		def _savepoint(name):
			_live()
			since_savepoint.clear()
			txn.savepoint(name)

		def _release(name):
			_live()
			txn.release_savepoint(name)

		def _rollback(save_point=None):
			txn.rollback(save_point)  # raises for a savepoint that was never set
			state["aborted"] = False
			if transactional:
				for name in since_savepoint:
					self.table.pop(name, None)
			since_savepoint.clear()

		def _exists(doctype, name):
			assert doctype == "Error Log"
			_live()
			return name if name in self.table else None

		def _sql(query, *a, **k):
			assert query == "select 1"
			self.probes += 1
			_live()
			if outage:
				raise RuntimeError("Lost connection to server during query")
			return ((1,),)

		self.commit_points = []  # rows inserted so far, at each commit

		def _commit():
			commits.append(1)
			self.commit_points.append(len(inserted))
			txn.commit()
			state["aborted"] = False

		def _get_doc(record):
			doc = SimpleNamespace(name=None)
			doc.insert = lambda ignore_permissions=False: _insert(doc, record)
			return doc
		db = SimpleNamespace(
			savepoint=_savepoint, release_savepoint=_release, rollback=_rollback, exists=_exists, sql=_sql,
		)
		monkeypatch.setattr(maintenance, "frappe", SimpleNamespace(cache=cache, get_doc=_get_doc, db=db))
		monkeypatch.setattr(maintenance, "safe_commit", _commit)
		return inserted, commits

	def test_takes_only_the_error_log_queue(self, monkeypatch):
		cache = _FakeCache({
			"insert_queue_for_Error Log": [json.dumps({"error": LEAKY}), json.dumps([{"error": "x"}, {"error": "y"}])],
			"insert_queue_for_Route History": [json.dumps({"route": "app"})],
		})
		inserted, commits = self._frappe(monkeypatch, cache)
		assert maintenance._flush_deferred_error_logs(KEY).failed == 0
		# the leaky record is masked before it is inserted, never verbatim
		assert [r["error"] for r in inserted] == [maintenance._mask(LEAKY, KEY), "x", "y"]
		assert KEY not in inserted[0]["error"] and "'Bearer ********'" in inserted[0]["error"]
		assert all(r["doctype"] == "Error Log" for r in inserted)
		assert cache.queues["insert_queue_for_Error Log"] == []
		assert len(cache.queues["insert_queue_for_Route History"]) == 1  # left to the scheduler
		assert commits == [1]
		assert self.txn.max_depth == 1  # each insert's savepoint released, never nested

	def test_a_queued_leaky_record_is_inserted_masked(self, monkeypatch):
		# A pre-fix snapshot queued in Redis holds the key in its traceback,
		# its title and its request metadata. It must never reach the
		# database (the INSERT, the binlog, the query logs) verbatim.
		leaky = {
			"error": LEAKY,
			"method": f"The AI provider returned an error (HTTP 401): bad key {KEY}",
			"metadata": json.dumps({"form_dict": {"doc": f'{{"ai_api_key": "{KEY}"}}'}}),
			"reference_doctype": "Optimus Session", "reference_name": "s-1", "trace_id": "t-1",
		}
		title = "POST " + "x" * 90 + " via http://u:p@proxy.example/v1 "
		long_title = {"error": "x", "method": title + "y" * (140 - len(title))}
		assert len(long_title["method"]) == 140
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps(leaky), json.dumps([long_title])]})
		inserted, _ = self._frappe(monkeypatch, cache)
		assert maintenance._flush_deferred_error_logs(KEY).failed == 0
		assert len(inserted) == 2
		assert not [r for r in inserted if KEY in json.dumps(r)]
		first = inserted[0]
		assert "'Bearer ********'" in first["error"] and first["method"].endswith("bad key ********")
		assert json.loads(first["metadata"])  # still valid JSON
		assert (first["reference_doctype"], first["reference_name"], first["trace_id"]) == ("Optimus Session", "s-1", "t-1")
		# masking lengthens "u:p" to "********": the title is cut to 140
		assert len(inserted[1]["method"]) == 140 and "u:p@" not in inserted[1]["method"]

	def test_a_queued_smart_quote_key_is_inserted_masked(self, monkeypatch):
		# The bare header values of the urllib3 frames, and the key
		# JSON-escaped in the metadata, only the key read before the flush
		# can find.
		smart = {"error": ANTHROPIC_TB, "metadata": json.dumps({"doc": f"key {ANTHROPIC_KEY}"})}
		assert "\\u2019" in smart["metadata"]
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps(smart)]})
		inserted, _ = self._frappe(monkeypatch, cache)
		assert maintenance._flush_deferred_error_logs(ANTHROPIC_KEY).failed == 0
		[row] = inserted
		assert "456789abcdef" not in json.dumps(row)
		assert "      value = ********\n" in row["error"]
		assert json.loads(row["metadata"]) == {"doc": "key ********"}

	def test_a_record_that_cannot_be_masked_is_never_inserted(self, monkeypatch):
		real_mask = maintenance._mask

		def _mask(text, api_key, **kw):
			if "BOOM" in text:
				raise ValueError("catastrophic backtracking")
			return real_mask(text, api_key, **kw)
		monkeypatch.setattr(maintenance, "_mask", _mask)
		queue = [json.dumps({"error": LEAKY + "BOOM"}), json.dumps({"error": "z"}), json.dumps(["not a record"])]
		cache = _FakeCache({"insert_queue_for_Error Log": queue})
		inserted, _ = self._frappe(monkeypatch, cache)
		assert maintenance._flush_deferred_error_logs(KEY).failed == 2
		assert [r["error"] for r in inserted] == ["z"]
		assert cache.pushes == []  # dropped, not pushed back for Frappe to insert verbatim

	def test_three_failed_inserts_in_a_row_push_every_one_back_masked(self, monkeypatch):
		# The database is down: stop instead of popping (and losing) up to
		# 10000 entries, and push back EVERY record of the run of failures,
		# masked, as one entry at the end of the queue. bench migrate's own
		# flush inserts it right after the patches, as it is.
		leaky = [{"error": f"L{i} api_key={KEY}", "method": f"bad key {KEY}"} for i in (1, 2, 3)]
		entries = [json.dumps(r) for r in leaky] + [json.dumps({"error": "d"})]
		cache = _FakeCache({"insert_queue_for_Error Log": list(entries)})
		inserted, commits = self._frappe(monkeypatch, cache, fail_on=lambda e: e.startswith("L"), outage=True)
		assert maintenance._flush_deferred_error_logs(KEY) == (3, False)
		assert inserted == [] and commits == [1]
		(queue, pushed), _ = cache.pushes  # the run, then "d" masked in Redis
		assert json.loads(pushed) == [maintenance._masked_record(r, KEY) for r in leaky]  # none lost
		assert KEY not in pushed and "api_key=********" in pushed
		assert cache.queues[queue] == [pushed, json.dumps([{"error": "d"}])]

	def test_a_stop_pushes_back_the_untried_rest_of_the_entry_masked(self, monkeypatch):
		entry = [{"error": "ok"}] + [{"error": f"L{i} api_key={KEY}"} for i in (1, 2, 3)] + [{"error": f"T api_key={KEY}"}]
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps(entry)]})
		inserted, _ = self._frappe(monkeypatch, cache, fail_on=lambda e: e.startswith("L"), outage=True)
		assert maintenance._flush_deferred_error_logs(KEY).failed == 3
		assert [r["error"] for r in inserted] == ["ok"]  # never pushed back: no duplicate
		[(_, pushed)] = cache.pushes
		assert json.loads(pushed) == [maintenance._masked_record(r, KEY) for r in entry[1:]]
		assert KEY not in pushed

	def test_a_failed_periodic_commit_stops_and_pushes_back_the_rest_of_the_entry_masked(self, monkeypatch):
		entry = [{"error": f"R{i} api_key={KEY}"} for i in range(102)]
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps(entry), json.dumps({"error": "next"})]})
		inserted, _ = self._frappe(monkeypatch, cache)

		def _commit():
			raise RuntimeError("Lost connection to server during query")
		monkeypatch.setattr(maintenance, "safe_commit", _commit)
		# the periodic commit after row 100 and the final one
		assert maintenance._flush_deferred_error_logs(KEY).failed == 2
		assert len(inserted) == 100
		[(_, pushed)] = [p for p in cache.pushes if "R100" in p[1]]
		assert json.loads(pushed) == [maintenance._masked_record(r, KEY) for r in entry[100:]]
		assert KEY not in pushed

	def test_failures_that_are_not_in_a_row_do_not_stop_it(self, monkeypatch):
		errors = ("B1", "ok1", "B2", "ok2", "B3", "ok3", "B4")
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps({"error": e}) for e in errors]})
		inserted, _ = self._frappe(monkeypatch, cache, fail_on={"B1", "B2", "B3", "B4"})
		assert maintenance._flush_deferred_error_logs(KEY).failed == 4
		assert [r["error"] for r in inserted] == ["ok1", "ok2", "ok3"]
		assert cache.pops == 7 and cache.pushes == []
		assert self.probes == 1  # B4 ends the queue: checked, answered, dropped

	def test_three_bad_records_in_a_row_do_not_stop_it_while_the_database_answers(self, monkeypatch):
		# A burst of records that fail validation is not an outage: select 1
		# answers, so the bad records are counted and dropped, and the leaky
		# snapshots behind them are still inserted masked, instead of being
		# left for bench migrate's own flush to insert as they are.
		queue = [json.dumps({"error": f"BAD{i}"}) for i in range(3)]
		queue += [json.dumps({"error": f"row {i} api_key={KEY}"}) for i in range(100)]
		cache = _FakeCache({"insert_queue_for_Error Log": queue})
		inserted, _ = self._frappe(monkeypatch, cache, fail_on=lambda e: e.startswith("BAD"))
		assert maintenance._flush_deferred_error_logs(KEY).failed == 3
		assert len(inserted) == 100 and not [r for r in inserted if KEY in r["error"]]
		assert cache.pushes == [] and cache.queues["insert_queue_for_Error Log"] == []
		assert self.probes == 1  # asked once, on the third failure in a row

	def test_a_run_of_failures_is_checked_again_after_each_third(self, monkeypatch):
		# Six bad records: two probes, both answered, nothing pushed back.
		queue = [json.dumps({"error": f"BAD{i}"}) for i in range(6)] + [json.dumps({"error": "ok"})]
		cache = _FakeCache({"insert_queue_for_Error Log": queue})
		inserted, _ = self._frappe(monkeypatch, cache, fail_on=lambda e: e.startswith("BAD"))
		assert maintenance._flush_deferred_error_logs(KEY).failed == 6
		assert [r["error"] for r in inserted] == ["ok"] and cache.pushes == [] and self.probes == 2

	def test_a_probe_that_fails_to_run_counts_as_an_outage(self, monkeypatch):
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps({"error": f"B{i}"}) for i in range(4)]})
		self._frappe(monkeypatch, cache, fail_on=lambda e: True)

		def _sql(query, *a, **k):
			raise TimeoutError("statement timeout")
		maintenance.frappe.db.sql = _sql
		assert maintenance._flush_deferred_error_logs(KEY).failed == 3
		(_, pushed), _ = cache.pushes  # the run, then B3 masked in Redis
		assert [r["error"] for r in json.loads(pushed)] == ["B0", "B1", "B2"]

	@pytest.mark.parametrize("outage", [True, False])
	def test_failures_at_the_end_of_the_queue_are_kept_only_in_an_outage(self, monkeypatch, outage):
		# The queue ends during a run of fewer than three failures: in an
		# outage they are pushed back, masked; otherwise dropped as bad.
		queue = [json.dumps({"error": "ok"}), json.dumps({"error": f"L1 api_key={KEY}"}), json.dumps({"error": "L2"})]
		cache = _FakeCache({"insert_queue_for_Error Log": queue})
		self._frappe(monkeypatch, cache, fail_on=lambda e: e.startswith("L"), outage=outage)
		assert maintenance._flush_deferred_error_logs(KEY).failed == 2
		assert self.probes == 1
		if outage:
			[(_, pushed)] = cache.pushes
			assert [r["error"] for r in json.loads(pushed)] == ["L1 api_key=********", "L2"]
		else:
			assert cache.pushes == []

	def test_a_stop_inside_an_entry_pushes_back_only_its_records_not_inserted(self, monkeypatch):
		# Pushing the whole entry back would insert "ok" twice.
		entry = [{"error": e} for e in ("ok", "B1", "B2", "B3", "z")]
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps(entry), json.dumps({"error": "next"})]})
		inserted, _ = self._frappe(monkeypatch, cache, fail_on={"B1", "B2", "B3"}, outage=True)
		assert maintenance._flush_deferred_error_logs(KEY).failed == 3
		assert [r["error"] for r in inserted] == ["ok"]
		(queue, pushed), _ = cache.pushes  # the rest, then "next" masked in Redis
		assert json.loads(pushed) == [{"error": e} for e in ("B1", "B2", "B3", "z")]
		assert cache.queues[queue] == [pushed, json.dumps([{"error": "next"}])]

	def test_failures_count_across_entries_and_all_of_them_are_pushed_back(self, monkeypatch):
		# The run of failures spans two entries: the first entry's record is
		# pushed back too, not lost.
		entries = [json.dumps({"error": "B1"}), json.dumps([{"error": "B2"}, {"error": "B3"}, {"error": "w"}])]
		cache = _FakeCache({"insert_queue_for_Error Log": list(entries)})
		inserted, _ = self._frappe(monkeypatch, cache, fail_on={"B1", "B2", "B3"}, outage=True)
		assert maintenance._flush_deferred_error_logs(KEY).failed == 3
		assert inserted == []
		assert cache.pushes == [("insert_queue_for_Error Log", json.dumps([{"error": e} for e in ("B1", "B2", "B3", "w")]))]

	def test_a_failed_push_back_is_counted(self, monkeypatch):
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps({"error": e}) for e in ("B1", "B2", "B3")]})
		self._frappe(monkeypatch, cache, fail_on={"B1", "B2", "B3"}, outage=True)

		def _rpush(key, value):
			raise ConnectionError("redis went away")
		cache.rpush = _rpush
		assert maintenance._flush_deferred_error_logs(KEY).failed == 4

	def test_a_broken_queue_is_not_fatal(self, monkeypatch):
		self._frappe(monkeypatch, _FakeCache({}, broken=True))
		assert maintenance._flush_deferred_error_logs(KEY) == (1, True)

	def test_a_queue_that_was_read_reports_no_queue_failure(self, monkeypatch):
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps({"error": "x"})]})
		self._frappe(monkeypatch, cache, fail_on={"x"})
		assert maintenance._flush_deferred_error_logs(KEY) == (1, False)

	def test_a_failing_insert_rolls_back_to_its_savepoint_only(self, monkeypatch):
		cache = _FakeCache({
			"insert_queue_for_Error Log": [json.dumps({"error": e}) for e in ("x", "BAD", "z")],
		})
		inserted, commits = self._frappe(monkeypatch, cache, fail_on={"BAD"})
		assert maintenance._flush_deferred_error_logs(KEY).failed == 1
		assert [r["error"] for r in inserted] == ["x", "z"]  # the third still lands
		i = self.db_log.index(("insert", "BAD"))
		assert self.db_log[i - 1] == ("savepoint", "optimus_scrub_row")  # set before the failing insert
		assert self.db_log[i + 1] == ("rollback", "optimus_scrub_row")
		assert self.txn.max_depth == 1  # released after the rollback too
		assert commits == [1]

	def test_a_row_stored_before_its_insert_failed_counts_as_inserted(self, monkeypatch):
		# Error Log is MyISAM on MariaDB: the rollback to the savepoint does
		# not undo an INSERT, so a hook that fails after it leaves the row
		# stored. It counts as inserted: not failed, never pushed back (bench
		# migrate's own flush would insert it a second time, unmasked), and it
		# resets the run of failed inserts, so the flush does not stop.
		errors = ("B1", "B2", f"H1 api_key={KEY}", "B3", f"H2 api_key={KEY}", f"H3 api_key={KEY}", "B4", "ok")
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps({"error": e}) for e in errors]})
		self._frappe(monkeypatch, cache, fail_on={"B1", "B2", "B3", "B4"}, stored_then_fail=lambda e: e.startswith("H"))
		assert maintenance._flush_deferred_error_logs(KEY) == (4, False)
		assert cache.pushes == [] and cache.queues["insert_queue_for_Error Log"] == []
		rows = sorted(r["error"] for r in self.table.values())
		assert rows == sorted([maintenance._mask(f"H{i} api_key={KEY}", KEY) for i in (1, 2, 3)] + ["ok"])
		_replay_frappe_flush(cache, lambda record: self.table.setdefault(f"f{len(self.table)}", record))
		assert len(self.table) == 4  # no duplicate
		assert not [r for r in self.table.values() if KEY in json.dumps(r)]

	def test_a_row_the_rollback_removed_counts_as_failed(self, monkeypatch):
		# On a transactional engine (Postgres) the rollback to the savepoint
		# removes the row the failing hook followed: not inserted.
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps({"error": "H1"}), json.dumps({"error": "ok"})]})
		self._frappe(monkeypatch, cache, stored_then_fail=lambda e: e == "H1", transactional=True)
		assert maintenance._flush_deferred_error_logs(KEY) == (1, False)
		assert [r["error"] for r in self.table.values()] == ["ok"]

	def test_a_stored_row_that_cannot_be_checked_counts_as_failed(self, monkeypatch):
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps({"error": "H1"})]})
		self._frappe(monkeypatch, cache, stored_then_fail=lambda e: True)

		def _exists(doctype, name):
			raise RuntimeError("Lost connection to server during query")
		maintenance.frappe.db.exists = _exists
		assert maintenance._flush_deferred_error_logs(KEY) == (1, False)

	def test_a_malformed_record_does_not_stop_the_valid_ones_behind_it(self, monkeypatch):
		# A record that cannot be parsed is counted and skipped; the records
		# behind it are still inserted, instead of staying queued and landing
		# unmasked after the scrub.
		queue = [json.dumps({"error": "x"}), "{not json", json.dumps({"error": "z"}), json.dumps([{"error": "w"}])]
		cache = _FakeCache({"insert_queue_for_Error Log": queue})
		inserted, commits = self._frappe(monkeypatch, cache)
		assert maintenance._flush_deferred_error_logs(KEY).failed == 1
		assert [r["error"] for r in inserted] == ["x", "z", "w"]
		assert commits == [1]
		assert cache.queues["insert_queue_for_Error Log"] == []

	def test_rows_inserted_before_a_failed_pop_are_committed(self, monkeypatch):
		# An lpop failure stops the loop; the rows already popped and inserted
		# are committed, or a later rollback (the patch's, after a failed
		# scrub) would drop them after Redis has let them go.
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps({"error": "x"}), json.dumps({"error": "y"})]})
		inserted, commits = self._frappe(monkeypatch, cache)
		real_lpop = cache.lpop

		def _lpop(key):
			if cache.pops:
				raise ConnectionError("redis went away")
			return real_lpop(key)
		cache.lpop = _lpop
		assert maintenance._flush_deferred_error_logs(KEY).failed == 1
		assert [r["error"] for r in inserted] == ["x"]
		assert commits == [1]
		assert len(cache.queues["insert_queue_for_Error Log"]) == 1  # left for the next run

	def test_an_lpop_failure_stops_the_loop(self, monkeypatch):
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps({"error": "x"})] * 3}, broken_pop=True)
		inserted, commits = self._frappe(monkeypatch, cache)
		assert maintenance._flush_deferred_error_logs(KEY).failed == 1
		assert inserted == [] and commits == [1]

	def test_a_producer_that_refills_the_queue_cannot_keep_it_running(self, monkeypatch):
		# Only the entries queued when the flush starts are taken.
		refill = json.dumps({"error": "new"})
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps({"error": e}) for e in "abc"]}, refill=refill)
		inserted, _ = self._frappe(monkeypatch, cache)
		assert maintenance._flush_deferred_error_logs(KEY).failed == 0
		assert cache.pops == 3
		assert [r["error"] for r in inserted] == ["a", "b", "c"]
		assert len(cache.queues["insert_queue_for_Error Log"]) == 3

	def test_a_queue_drained_meanwhile_ends_the_flush_without_a_failure(self, monkeypatch):
		# The scheduler's save_to_db can empty the queue after the length was
		# read: an empty pop ends the flush, it is not a bad record.
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps({"error": "x"})]})
		cache.llen = lambda key: 3
		inserted, commits = self._frappe(monkeypatch, cache)
		assert maintenance._flush_deferred_error_logs(KEY).failed == 0
		assert [r["error"] for r in inserted] == ["x"] and cache.pops == 2
		assert commits == [1]

	def test_inserts_at_most_the_cap(self, monkeypatch):
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps({"error": str(i)}) for i in range(8)]})
		inserted, _ = self._frappe(monkeypatch, cache)
		monkeypatch.setattr(maintenance, "_FLUSH_MAX_POPS", 5)
		assert maintenance._flush_deferred_error_logs(KEY).failed == 0
		assert len(inserted) == 5
		# the 3 left are popped once more, to be masked in Redis (see below)
		assert cache.pops == 8 and len(cache.queues["insert_queue_for_Error Log"]) == 3

	def test_the_entries_the_cap_left_are_masked_in_place(self, monkeypatch):
		# bench migrate inserts the queue as it is right after the patches:
		# the entries past the cap are masked in Redis, with no database
		# access, so they never reach the table with the key.
		leaky = [{"error": f"row {i} api_key={KEY}", "method": f"bad key {KEY}"} for i in range(8)]
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps(r) for r in leaky]})
		inserted, _ = self._frappe(monkeypatch, cache)
		monkeypatch.setattr(maintenance, "_FLUSH_MAX_POPS", 5)
		assert maintenance._flush_deferred_error_logs(KEY) == (0, False)
		assert len(inserted) == 5 and self.db_log.count(("savepoint", "optimus_scrub_row")) == 5
		left = cache.queues["insert_queue_for_Error Log"]
		assert [json.loads(e) for e in left] == [[maintenance._masked_record(r, KEY)] for r in leaky[5:]]
		assert not [e for e in left if KEY in e]

	def test_the_entries_a_stop_left_are_masked_in_place(self, monkeypatch):
		failing = [{"error": f"L{i} api_key={KEY}"} for i in range(3)]
		rest = [{"error": f"row {i} api_key={KEY}"} for i in range(3)]
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps(r) for r in failing + rest]})
		inserted, _ = self._frappe(monkeypatch, cache, fail_on=lambda e: e.startswith("L"), outage=True)
		assert maintenance._flush_deferred_error_logs(KEY).failed == 3
		assert inserted == []
		left = cache.queues["insert_queue_for_Error Log"]
		# the pushed-back run first, then the rest, each masked
		assert [json.loads(e) for e in left] == [
			[maintenance._masked_record(r, KEY) for r in failing],
			*[[maintenance._masked_record(r, KEY)] for r in rest],
		]
		assert not [e for e in left if KEY in e]

	def test_only_the_entries_queued_when_it_started_are_masked(self, monkeypatch):
		# Entries queued meanwhile are new snapshots, from the fixed code once
		# the processes restarted: the masking pops only what the snapshot
		# length leaves, so a busy producer cannot keep it running either.
		refill = json.dumps({"error": "a new snapshot"})
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps({"error": str(i)}) for i in range(4)]}, refill=refill)
		inserted, _ = self._frappe(monkeypatch, cache)
		monkeypatch.setattr(maintenance, "_FLUSH_MAX_POPS", 2)
		assert maintenance._flush_deferred_error_logs(KEY) == (0, False)
		assert len(inserted) == 2 and cache.pops == 4
		queue = cache.queues["insert_queue_for_Error Log"]
		assert queue.count(refill) == 4 and len(queue) == 6

	def test_a_bad_entry_left_in_the_queue_is_counted_and_dropped(self, monkeypatch):
		# As in the insert loop: a non-JSON entry (Frappe's own flush would
		# fail on it) and a record that is not a dict are dropped.
		queue = [json.dumps({"error": "x"}), "{not json", json.dumps([{"error": "z"}, "not a record"])]
		cache = _FakeCache({"insert_queue_for_Error Log": queue})
		self._frappe(monkeypatch, cache)
		monkeypatch.setattr(maintenance, "_FLUSH_MAX_POPS", 1)
		assert maintenance._flush_deferred_error_logs(KEY) == (2, False)
		assert cache.queues["insert_queue_for_Error Log"] == [json.dumps([{"error": "z"}])]

	def test_a_failed_push_while_masking_stops_it(self, monkeypatch):
		# Popping on after a failed push would lose every entry left.
		queue = [json.dumps({"error": e}) for e in "abc"]
		cache = _FakeCache({"insert_queue_for_Error Log": list(queue)})
		self._frappe(monkeypatch, cache)
		monkeypatch.setattr(maintenance, "_FLUSH_MAX_POPS", 1)

		def _rpush(key, value):
			raise ConnectionError("redis went away")
		cache.rpush = _rpush
		assert maintenance._flush_deferred_error_logs(KEY) == (1, True)
		assert cache.pops == 2 and cache.queues["insert_queue_for_Error Log"] == [queue[2]]

	def test_the_default_cap_is_ten_thousand_pops(self):
		assert maintenance._FLUSH_MAX_POPS == 10_000

	def test_commits_every_hundred_inserts(self, monkeypatch):
		queue = [json.dumps({"error": str(i)}) for i in range(150)]
		queue.append(json.dumps([{"error": f"l{i}"} for i in range(100)]))  # one entry, 100 records
		cache = _FakeCache({"insert_queue_for_Error Log": queue})
		inserted, _ = self._frappe(monkeypatch, cache)
		assert maintenance._flush_deferred_error_logs(KEY).failed == 0
		assert len(inserted) == 250
		assert self.commit_points == [100, 200, 250]

	def test_a_failing_commit_is_not_fatal(self, monkeypatch):
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps({"error": "x"})]})
		self._frappe(monkeypatch, cache)

		def _commit():
			raise RuntimeError("Lost connection to server during query")
		monkeypatch.setattr(maintenance, "safe_commit", _commit)
		assert maintenance._flush_deferred_error_logs(KEY).failed == 1


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
	"""The flush masks the bare header value lines (``value = ...``) only in
	a queued record from the AI code (a frame in Optimus's ``ai_fix.py``, under
	either package name) or one holding the stored key. Any other snapshot
	keeps them: scrub_secrets alone."""

	def _frappe(self, monkeypatch, queue):
		cache = _FakeCache({"insert_queue_for_Error Log": queue})
		inserted = []

		def _get_doc(rec):
			return SimpleNamespace(name=None, insert=lambda ignore_permissions=False: inserted.append(rec))
		db = SimpleNamespace(savepoint=lambda n: None, release_savepoint=lambda n: None, rollback=lambda **k: None)
		monkeypatch.setattr(maintenance, "frappe", SimpleNamespace(cache=cache, get_doc=_get_doc, db=db))
		monkeypatch.setattr(maintenance, "safe_commit", lambda: None)
		return cache, inserted

	def _flush(self, monkeypatch, record, api_key=KEY):
		_, inserted = self._frappe(monkeypatch, [json.dumps(record)])
		assert maintenance._flush_deferred_error_logs(api_key) == (0, False)
		[row] = inserted
		return row

	@pytest.mark.parametrize("api_key", [KEY, ""], ids=["key_stored", "no_key_stored"])
	def test_an_unrelated_snapshot_keeps_its_value_lines(self, monkeypatch, api_key):
		row = self._flush(monkeypatch, {"error": ERP_TB, "method": "Stock Entry failed"}, api_key)
		assert row["error"] == ERP_TB

	def test_another_apps_ai_fix_frame_is_not_optimus(self, monkeypatch):
		error = ERP_TB.replace("erpnext/erpnext/stock/doctype/stock_entry/stock_entry.py", "other/other/openai_fix.py")
		assert "ai_fix.py" in error
		assert self._flush(monkeypatch, {"error": error})["error"] == error

	@pytest.mark.parametrize("package", ["optimus", "frappe_profiler"])
	def test_an_ai_snapshot_has_its_value_lines_masked(self, monkeypatch, package):
		error = ERP_TB + f'  File "apps/{package}/{package}/ai_fix.py", line 1290, in _http_post\n'
		row = self._flush(monkeypatch, {"error": error})
		assert "Acme" not in row["error"] and "      value = ********\n" in row["error"]

	def test_a_snapshot_holding_the_key_has_its_value_lines_masked(self, monkeypatch):
		# No ai_fix.py frame, but the key is in its request metadata.
		record = {"error": ERP_TB, "metadata": json.dumps({"form_dict": {"ai_api_key": KEY}})}
		row = self._flush(monkeypatch, record)
		assert "Acme" not in row["error"] and KEY not in row["metadata"]

	def test_the_masking_of_what_is_left_in_redis_follows_the_same_rule(self, monkeypatch):
		ai = ERP_TB + '  File "apps/optimus/optimus/ai_fix.py", line 1290, in _http_post\n'
		cache, _ = self._frappe(monkeypatch, [json.dumps({"error": e}) for e in ("x", ERP_TB, ai)])
		monkeypatch.setattr(maintenance, "_FLUSH_MAX_POPS", 1)
		assert maintenance._flush_deferred_error_logs(KEY) == (0, False)
		erp, masked = (json.loads(e)[0]["error"] for e in cache.queues["insert_queue_for_Error Log"])
		assert erp == ERP_TB and "Acme" not in masked


_REAL_FLUSH = maintenance._flush_deferred_error_logs


class TestQueuedRows:
	"""The scrub reads the key first, flushes Error Log's deferred-insert
	queue with it (a real run only), and reports what is still queued."""

	def _real_flush(self, fake, monkeypatch, queue, **kw):
		f = fake([("a", LEAKY)], **kw)
		f.cache = _FakeCache({"insert_queue_for_Error Log": list(queue)})
		monkeypatch.setattr(maintenance, "_flush_deferred_error_logs", _REAL_FLUSH)
		return f

	def test_the_key_is_read_before_the_first_pop(self, fake, monkeypatch):
		f = self._real_flush(fake, monkeypatch, [json.dumps({"error": f"late row: api_key={KEY}"})])
		events = []
		real_lpop = f.cache.lpop

		def _lpop(key):
			events.append("lpop")
			return real_lpop(key)
		f.cache.lpop = _lpop
		monkeypatch.setattr("optimus.ai_fix._current_key_or_empty", lambda: events.append("key") or KEY)
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert events[:2] == ["key", "lpop"] and events.count("key") == 1
		assert f.inserted and KEY not in json.dumps(f.inserted)  # masked before the INSERT
		assert all(KEY not in r["error"] for r in f.tables["Error Log"].values())
		assert (out["failed"], out["queued"]) == (0, 0)

	def test_queued_counts_what_the_cap_left_masked(self, fake, monkeypatch):
		queue = [json.dumps({"error": f"late row {i}: api_key={KEY}"}) for i in range(8)]
		f = self._real_flush(fake, monkeypatch, queue)
		monkeypatch.setattr(maintenance, "_FLUSH_MAX_POPS", 5)
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert (out["queued"], out["failed"]) == (3, 0)
		assert len(f.inserted) == 5
		left = f.cache.queues["insert_queue_for_Error Log"]
		assert len(left) == 3 and not [e for e in left if KEY in e]

	def test_queued_counts_an_entry_pushed_back_after_failed_inserts(self, fake, monkeypatch):
		queue = [json.dumps({"error": e}) for e in ("B1", "B2", "B3", "d")]
		f = self._real_flush(fake, monkeypatch, queue)
		f.db_down = True

		def _get_doc(record):
			def _insert(ignore_permissions=False):
				raise RuntimeError("Lost connection to server during query")
			return SimpleNamespace(insert=_insert)
		f.get_doc = _get_doc
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert (out["queued"], out["failed"]) == (2, 3)

	def test_a_dry_run_reads_the_queue_length_and_never_pops(self, fake, monkeypatch):
		f = self._real_flush(fake, monkeypatch, [json.dumps({"error": LEAKY})] * 4)
		out = maintenance.scrub_error_log_secrets(dry_run=True)
		assert out["queued"] == 4 and f.cache.pops == 0 and f.inserted == []

	@pytest.mark.parametrize("dry_run", [True, False])
	def test_a_queue_that_cannot_be_read_counts_as_failed(self, fake, dry_run):
		f = fake([("a", CLEAN_AI)])
		f.cache = _FakeCache({}, broken=True)
		out = maintenance.scrub_error_log_secrets(dry_run=dry_run)
		assert (out["queued"], out["failed"]) == (0, 1)

	def test_one_redis_outage_counts_once(self, fake, monkeypatch):
		# The flush cannot read the queue and neither can the final count:
		# one outage, one failure.
		f = self._real_flush(fake, monkeypatch, [])
		f.cache = _FakeCache({}, broken=True)
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert (out["queued"], out["failed"]) == (0, 1)

	def test_a_queue_failure_after_a_clean_flush_still_counts(self, fake, monkeypatch):
		f = self._real_flush(fake, monkeypatch, [json.dumps({"error": "x"})])
		real_llen = f.cache.llen
		reads = []

		def _llen(key):
			reads.append(key)
			if len(reads) > 1:
				raise ConnectionError("redis went away")
			return real_llen(key)
		f.cache.llen = _llen
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert len(reads) == 2 and (out["queued"], out["failed"]) == (0, 1)


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
		f.cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps({"error": f"queued api_key={key}"})]})
		monkeypatch.setattr(maintenance, "_flush_deferred_error_logs", _REAL_FLUSH)
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
			out = maintenance.scrub_error_log_secrets(dry_run=False)
		finally:
			sys.setprofile(None)
		assert offenders == set()
		# positive control: the key-handling helpers did run, key in hand
		assert {"_holds_key", "_json_escaped", "_mask", "_flush_deferred_error_logs", "_key_fragment"} <= seen
		assert out["changed"] >= 2 and out["deleted_docs_changed"] == 1

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
		assert f.statements == [] and f.flushes == [] and f.writes == [] and f.deletes == [] and reads == []

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
	assert ("could not be masked" in out) is bool(failed)
	assert ("purge_ai_error_logs" in out) is bool(residual)
	assert ("4 Error Log entry(ies) are still waiting in the deferred-insert queue" in out) is bool(queued)


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


@pytest.mark.parametrize(("failed", "residual", "queued"), [(1, 0, 0), (0, 2, 0), (0, 0, 3), (1, 2, 3)])
def test_a_partial_scrub_leaves_a_breadcrumb(patch_env, patch_logs, monkeypatch, failed, residual, queued):
	# Patch Log marks the patch done: without a row, a scrub that left rows
	# unmasked, key-shaped values or queued entries leaves no lasting trace.
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
	assert "1 error row(s) could not be masked" in capsys.readouterr().out


def test_patch_is_registered_post_model_sync():
	from pathlib import Path

	text = (Path(maintenance.__file__).parent / "patches.txt").read_text()
	post = text.split("[post_model_sync]", 1)[1]
	assert _PATCH in post
