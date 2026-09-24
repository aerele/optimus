# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""optimus.maintenance: scrub / purge of the AI Error Log rows written before
the key-leak fix, and the v0_12 patch that runs the scrub on migrate.

``maintenance.frappe`` is replaced wholesale by an in-memory fake that
implements just the ORM calls the module makes (``get_all`` with LIKE / = / >
/ <= filters, ``or_filters``, ``order_by="name asc"``, ``limit_start``,
``limit_page_length``; ``db.set_value`` with a field dict; ``db.count``;
``db.estimate_count``; ``db.has_column``; ``db.delete``; savepoints;
``cache.llen`` / ``cache.lpop``; ``get_doc(...).insert``).
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
	``metadata`` column: a statement naming it fails. ``method`` is Data
	(varchar(140)) and a longer write fails as strict mode does."""

	def __init__(self, error_logs, deleted_docs=(), has_metadata=True):
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
			set_value=self._set_value, delete=self._delete, count=self._count,
			estimate_count=lambda doctype: len(self.tables[doctype]), has_column=self._has_column,
			savepoint=self.txn.savepoint, release_savepoint=self.txn.release_savepoint,
			rollback=self.txn.rollback,
		)

	def commit(self):
		self.commits.append(1)
		self.txn.commit()

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
	def _make(error_logs, deleted_docs=(), current_key=KEY, has_metadata=True):
		f = _FakeFrappe(error_logs, deleted_docs, has_metadata=has_metadata)
		f.flushes = []
		monkeypatch.setattr(maintenance, "frappe", f)
		monkeypatch.setattr(maintenance, "safe_commit", f.commit)
		monkeypatch.setattr(maintenance, "_flush_deferred_error_logs", lambda: f.flushes.append(f.reads) or 0)
		monkeypatch.setattr("optimus.ai_fix._current_key_or_empty", lambda: current_key)
		return f
	return _make


class TestScrubErrorLogSecrets:
	def test_dry_run_counts_without_writing(self, fake):
		f = fake([("a", LEAKY), ("b", CLEAN_AI), ("c", UNRELATED)], [("d1", json.dumps({"error": LEAKY}))])
		out = maintenance.scrub_error_log_secrets(dry_run=True)
		# b has an ai_fix.py frame and the api_key marker, so it is a
		# candidate, but nothing in it changes.
		assert out == {"candidates": 2, "changed": 1, "deleted_docs_changed": 1, "residual": 0, "failed": 0}
		assert f.writes == [] and f.commits == [] and f.flushes == []

	def test_scrubs_error_log_and_deleted_document_copies(self, fake):
		f = fake([("a", LEAKY), ("b", CLEAN_AI), ("c", UNRELATED)], [("d1", json.dumps({"error": LEAKY}))])
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert out == {"candidates": 2, "changed": 1, "deleted_docs_changed": 1, "residual": 0, "failed": 0}
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

		def _mask(text, api_key):
			if "BOOM" in text:
				raise ValueError("catastrophic backtracking")
			return real_mask(text, api_key)
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
		assert out == {"candidates": 3, "changed": 2, "deleted_docs_changed": 0, "residual": 0, "failed": 0}
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
		assert [v for v in values if _mid8(key) in _unlike(v)]  # the fragment is what is sent

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
	"""``bench execute --kwargs`` or a hand-written call can pass a string."""

	@pytest.mark.parametrize("value", ["False", "false", "0"])
	def test_a_false_string_writes(self, fake, value):
		f = fake([("a", LEAKY)], [("d1", LEAKY)])
		out = maintenance.scrub_error_log_secrets(dry_run=value)
		assert (out["changed"], out["deleted_docs_changed"]) == (1, 1)
		assert KEY not in f.tables["Error Log"]["a"]["error"] and f.flushes == [0]

	@pytest.mark.parametrize("value", ["True", "true", "1"])
	def test_a_true_string_writes_nothing(self, fake, value):
		f = fake([("a", LEAKY)], [("d1", LEAKY)])
		out = maintenance.scrub_error_log_secrets(dry_run=value)
		assert (out["changed"], out["deleted_docs_changed"]) == (1, 1)
		assert f.writes == [] and f.commits == [] and f.flushes == []

	def test_purge_coerces_it_too(self, fake):
		f = fake([("a", LEAKY), ("c", UNRELATED)])
		assert maintenance.purge_ai_error_logs(dry_run="True") == {"error_logs": 1, "deleted_documents": 0}
		assert f.deletes == []
		assert maintenance.purge_ai_error_logs(dry_run="False") == {"error_logs": 1, "deleted_documents": 0}
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

	def test_purge_is_windowed_too(self, fake):
		f = self._fake(fake)
		assert maintenance.purge_ai_error_logs(dry_run=False) == {"error_logs": len(self.HITS), "deleted_documents": 0}
		assert not set(self.HITS) & set(f.tables["Error Log"])
		assert len(f.tables["Error Log"]) == len(self.NAMES) - len(self.HITS)
		self._assert_windows(f)


class TestScrubScanSize:
	def _frappe(self, monkeypatch, fail=(), estimate=20):
		calls = []

		def part(name, value):
			def call(*a, **k):
				calls.append((name, a, k))
				if name in fail:
					raise RuntimeError(f"{name} failed")
				return value
			return call
		db = SimpleNamespace(count=part("count", 10), estimate_count=part("estimate_count", estimate))
		cache = SimpleNamespace(llen=part("llen", 3))
		monkeypatch.setattr(maintenance, "frappe", SimpleNamespace(db=db, cache=cache))
		return calls

	def test_counts_error_logs_every_deleted_document_and_the_queue(self, monkeypatch):
		calls = self._frappe(monkeypatch)
		assert maintenance.scrub_scan_size() == 33
		# every Deleted Document: deleted_doctype is not indexed, so the
		# passes read the whole table; the estimate is O(1)
		assert sorted(calls) == [
			("count", ("Error Log",), {}),
			("estimate_count", ("Deleted Document",), {}),
			("llen", ("insert_queue_for_Error Log",), {}),
		]

	@pytest.mark.parametrize(("broken", "expected"), [("count", 23), ("estimate_count", 13), ("llen", 30)])
	def test_each_part_is_guarded(self, monkeypatch, broken, expected):
		self._frappe(monkeypatch, fail={broken})
		assert maintenance.scrub_scan_size() == expected

	def test_a_negative_estimate_counts_as_zero(self, monkeypatch):
		# Postgres reports reltuples = -1 for a table never analysed
		self._frappe(monkeypatch, estimate=-1)
		assert maintenance.scrub_scan_size() == 13


class TestPurgeScope:
	def test_other_apps_ai_fix_frames_are_not_purged(self, fake):
		other = 'File "apps/acme/acme/openai_fix.py", line 3, in call\n    headers = {\'api_key\': \'x\'}\n'
		lookalike = 'File "apps/optimus/optimus/ai-fix.py", line 3\n'  # "_" is not a wildcard
		f = fake([("a", LEAKY), ("o", other), ("l", lookalike)], [("d1", LEAKY), ("d2", other)])
		assert maintenance.purge_ai_error_logs(dry_run=False) == {"error_logs": 1, "deleted_documents": 1}
		assert set(f.tables["Error Log"]) == {"o", "l"}
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


class _FakeCache:
	"""``broken`` fails every call (Redis down); ``broken_pop`` fails only
	``lpop``; ``refill`` is an entry a busy producer pushes back after every
	pop. More than 1000 pops raise, so a runaway loop fails fast."""

	def __init__(self, queues, broken=False, broken_pop=False, refill=None):
		self.queues = queues
		self.broken = broken
		self.broken_pop = broken_pop
		self.refill = refill
		self.pops = 0

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
		item = queue.pop(0).encode() if queue else None
		if self.refill is not None:
			queue.append(self.refill)
		return item


class TestFlushDeferredErrorLogs:
	def _frappe(self, monkeypatch, cache, fail_on=None):
		"""A failed insert aborts the transaction, as a failed statement does
		on Postgres: every later statement except a ROLLBACK fails until a
		rollback to a savepoint that was set (``_FakeTxn``)."""
		inserted, commits = [], []
		state = {"aborted": False}
		self.db_log = []
		txn = self.txn = _FakeTxn(self.db_log)

		def _live():
			if state["aborted"]:
				raise RuntimeError("current transaction is aborted")

		def _insert(record):
			_live()
			self.db_log.append(("insert", record.get("error")))
			if record.get("error") == fail_on:
				state["aborted"] = True
				raise RuntimeError("Duplicate entry")
			inserted.append(record)

		def _savepoint(name):
			_live()
			txn.savepoint(name)

		def _release(name):
			_live()
			txn.release_savepoint(name)

		def _rollback(save_point=None):
			txn.rollback(save_point)  # raises for a savepoint that was never set
			state["aborted"] = False

		self.commit_points = []  # rows inserted so far, at each commit

		def _commit():
			commits.append(1)
			self.commit_points.append(len(inserted))
			txn.commit()
			state["aborted"] = False

		def _get_doc(record):
			return SimpleNamespace(insert=lambda ignore_permissions=False: _insert(record))
		db = SimpleNamespace(savepoint=_savepoint, release_savepoint=_release, rollback=_rollback)
		monkeypatch.setattr(maintenance, "frappe", SimpleNamespace(cache=cache, get_doc=_get_doc, db=db))
		monkeypatch.setattr(maintenance, "safe_commit", _commit)
		return inserted, commits

	def test_takes_only_the_error_log_queue(self, monkeypatch):
		cache = _FakeCache({
			"insert_queue_for_Error Log": [json.dumps({"error": LEAKY}), json.dumps([{"error": "x"}, {"error": "y"}])],
			"insert_queue_for_Route History": [json.dumps({"route": "app"})],
		})
		inserted, commits = self._frappe(monkeypatch, cache)
		assert maintenance._flush_deferred_error_logs() == 0
		assert [r["error"] for r in inserted] == [LEAKY, "x", "y"]
		assert all(r["doctype"] == "Error Log" for r in inserted)
		assert cache.queues["insert_queue_for_Error Log"] == []
		assert len(cache.queues["insert_queue_for_Route History"]) == 1  # left to the scheduler
		assert commits == [1]
		assert self.txn.max_depth == 1  # each insert's savepoint released, never nested

	def test_a_broken_queue_is_not_fatal(self, monkeypatch):
		self._frappe(monkeypatch, _FakeCache({}, broken=True))
		assert maintenance._flush_deferred_error_logs() == 1

	def test_a_failing_insert_rolls_back_to_its_savepoint_only(self, monkeypatch):
		cache = _FakeCache({
			"insert_queue_for_Error Log": [json.dumps({"error": e}) for e in ("x", "BAD", "z")],
		})
		inserted, commits = self._frappe(monkeypatch, cache, fail_on="BAD")
		assert maintenance._flush_deferred_error_logs() == 1
		assert [r["error"] for r in inserted] == ["x", "z"]  # the third still lands
		i = self.db_log.index(("insert", "BAD"))
		assert self.db_log[i - 1] == ("savepoint", "optimus_scrub_row")  # set before the failing insert
		assert self.db_log[i + 1] == ("rollback", "optimus_scrub_row")
		assert self.txn.max_depth == 1  # released after the rollback too
		assert commits == [1]

	def test_a_malformed_record_does_not_stop_the_valid_ones_behind_it(self, monkeypatch):
		# A record that cannot be parsed is counted and skipped; the records
		# behind it are still inserted, instead of staying queued and landing
		# unmasked after the scrub.
		queue = [json.dumps({"error": "x"}), "{not json", json.dumps({"error": "z"}), json.dumps([{"error": "w"}])]
		cache = _FakeCache({"insert_queue_for_Error Log": queue})
		inserted, commits = self._frappe(monkeypatch, cache)
		assert maintenance._flush_deferred_error_logs() == 1
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
		assert maintenance._flush_deferred_error_logs() == 1
		assert [r["error"] for r in inserted] == ["x"]
		assert commits == [1]
		assert len(cache.queues["insert_queue_for_Error Log"]) == 1  # left for the next run

	def test_an_lpop_failure_stops_the_loop(self, monkeypatch):
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps({"error": "x"})] * 3}, broken_pop=True)
		inserted, commits = self._frappe(monkeypatch, cache)
		assert maintenance._flush_deferred_error_logs() == 1
		assert inserted == [] and commits == [1]

	def test_a_producer_that_refills_the_queue_cannot_keep_it_running(self, monkeypatch):
		# Only the entries queued when the flush starts are taken.
		refill = json.dumps({"error": "new"})
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps({"error": e}) for e in "abc"]}, refill=refill)
		inserted, _ = self._frappe(monkeypatch, cache)
		assert maintenance._flush_deferred_error_logs() == 0
		assert cache.pops == 3
		assert [r["error"] for r in inserted] == ["a", "b", "c"]
		assert len(cache.queues["insert_queue_for_Error Log"]) == 3

	def test_a_queue_drained_meanwhile_ends_the_flush_without_a_failure(self, monkeypatch):
		# The scheduler's save_to_db can empty the queue after the length was
		# read: an empty pop ends the flush, it is not a bad record.
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps({"error": "x"})]})
		cache.llen = lambda key: 3
		inserted, commits = self._frappe(monkeypatch, cache)
		assert maintenance._flush_deferred_error_logs() == 0
		assert [r["error"] for r in inserted] == ["x"] and cache.pops == 2
		assert commits == [1]

	def test_pops_at_most_the_cap(self, monkeypatch):
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps({"error": str(i)}) for i in range(8)]})
		inserted, _ = self._frappe(monkeypatch, cache)
		monkeypatch.setattr(maintenance, "_FLUSH_MAX_POPS", 5)
		assert maintenance._flush_deferred_error_logs() == 0
		assert cache.pops == 5 and len(inserted) == 5
		assert len(cache.queues["insert_queue_for_Error Log"]) == 3

	def test_the_default_cap_is_ten_thousand_pops(self):
		assert maintenance._FLUSH_MAX_POPS == 10_000

	def test_commits_every_hundred_inserts(self, monkeypatch):
		queue = [json.dumps({"error": str(i)}) for i in range(150)]
		queue.append(json.dumps([{"error": f"l{i}"} for i in range(100)]))  # one entry, 100 records
		cache = _FakeCache({"insert_queue_for_Error Log": queue})
		inserted, _ = self._frappe(monkeypatch, cache)
		assert maintenance._flush_deferred_error_logs() == 0
		assert len(inserted) == 250
		assert self.commit_points == [100, 200, 250]

	def test_a_failing_commit_is_not_fatal(self, monkeypatch):
		cache = _FakeCache({"insert_queue_for_Error Log": [json.dumps({"error": "x"})]})
		self._frappe(monkeypatch, cache)

		def _commit():
			raise RuntimeError("Lost connection to server during query")
		monkeypatch.setattr(maintenance, "safe_commit", _commit)
		assert maintenance._flush_deferred_error_logs() == 1


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
		def _line(msg, *args, **kwargs):
			rec.events.append("info")
			rec.lines.append((module, msg))
		return SimpleNamespace(info=_line)
	monkeypatch.setattr(frappe, "log_error", _log_error, raising=False)
	monkeypatch.setattr(frappe, "logger", _logger, raising=False)
	return rec


@pytest.fixture
def patch_env(monkeypatch):
	calls = []

	def _scrub(**kw):
		calls.append(kw)
		return {"candidates": 3, "changed": 2, "deleted_docs_changed": 1, "residual": 0, "failed": 0}

	monkeypatch.setattr(maintenance, "scrub_scan_size", lambda: 1000)
	monkeypatch.setattr(maintenance, "scrub_error_log_secrets", _scrub)
	return calls


def _one_summary_line(patch_logs) -> str:
	assert len(patch_logs.lines) == 1, patch_logs.lines
	module, line = patch_logs.lines[0]
	assert module == "optimus" and "\n" not in line and KEY not in line
	return line


def test_patch_runs_the_scrub_for_real(patch_env, patch_logs, capsys):
	importlib.import_module(_PATCH).execute()
	assert patch_env == [{"dry_run": False}]
	assert "masked AI API keys in 3 stored error row(s)" in capsys.readouterr().out
	assert patch_logs.errors == []  # no breadcrumb when the scrub ran
	line = _one_summary_line(patch_logs)
	for count in ("candidates=3", "changed=2", "deleted_docs_changed=1", "residual=0", "failed=0"):
		assert count in line


def test_patch_runs_the_scrub_at_exactly_the_limit(patch_env, monkeypatch):
	monkeypatch.setattr(maintenance, "scrub_scan_size", lambda: maintenance.MIGRATE_SCAN_LIMIT)
	importlib.import_module(_PATCH).execute()
	assert patch_env == [{"dry_run": False}]


def test_patch_skips_a_table_too_large_for_migrate(patch_env, patch_logs, monkeypatch, capsys):
	size = maintenance.MIGRATE_SCAN_LIMIT + 1
	monkeypatch.setattr(maintenance, "scrub_scan_size", lambda: size)
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


@pytest.mark.parametrize(("failed", "residual"), [(2, 0), (0, 1)])
def test_patch_prints_only_the_problem_lines_when_nothing_was_masked(patch_env, monkeypatch, capsys, failed, residual):
	monkeypatch.setattr(
		maintenance, "scrub_error_log_secrets",
		lambda **kw: {"candidates": 3, "changed": 0, "deleted_docs_changed": 0, "residual": residual, "failed": failed},
	)
	importlib.import_module(_PATCH).execute()
	out = capsys.readouterr().out
	assert "Rotate" not in out and "found no AI API keys" not in out
	assert ("could not be masked" in out) is bool(failed)
	assert ("purge_ai_error_logs" in out) is bool(residual)


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
	monkeypatch.setattr(maintenance, "scrub_scan_size", lambda: 10)
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
		monkeypatch.setattr(maintenance, "scrub_scan_size", lambda: maintenance.MIGRATE_SCAN_LIMIT + 1)
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
