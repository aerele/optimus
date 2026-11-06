# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Database work done by the index-suggestions analyzer.

The tests run Frappe's REAL ``_optimize_query`` (a pinned verbatim copy in
``fixtures/frappe_optimizer/``, hash-checked against an installed Frappe when one is
found) against a fake database that counts every statement it receives.
"""

import contextlib
import hashlib
import inspect
import json
import os
import re
import sys
import types
from collections import Counter
from pathlib import Path

import pytest

from optimus.analyzers import index_suggestions, optimizer_db_cache
from optimus.dbdialect.mariadb import MariaDBDialect

FIXTURES = Path(__file__).parent / "fixtures" / "frappe_optimizer"
PINNED_SHA256 = {
	"db_optimizer.py.txt": "fee47ae4db94af6124629c888838b31cc448bcebb2791f24535b266cfd1d2447",
	"recorder_optimizer.py.txt": "9a8628b3779e39439eb67338f4a25ae76836cd26bc05feee4f6b44b16664e438",
}
PINNED_FRAPPE_VERSION = "16.54.0"
COLUMNS = ("name", "status", "customer", "territory", "owner", "read")
TIMEOUT = (1969, "Query execution was interrupted (max_statement_time exceeded)")


class JobTimeout(Exception):
	"""Stands in for rq's job timeout (unit CI has no rq)."""


class FakeDb:
	"""Counts statements and answers the ones Frappe's optimizer sends."""

	def __init__(self, tables=("tabSales Invoice",)):
		self.tables = set(tables)
		self.log = Counter()
		self.failures = {}  # statement kind -> exception to raise
		self.limit = 0.0
		self.limit_seen = []  # was the time cap active at each of Frappe's optimizer reads
		self.on_read = None

	@property
	def total(self):
		return sum(self.log.values())

	def table_exists(self, doctype, cached=True):
		self.log["table_exists"] += 1
		return f"tab{doctype}" in self.tables

	def sql(self, query, values=None, as_dict=False, **kw):
		text = " ".join(str(query).split()).lower()
		kind = self._kind(text)
		self.log[kind] += 1
		if kind == "limit_read":
			return [[self.limit]]
		if kind == "limit_set":
			self.limit = values[0]
			return None
		if kind in ("describe", "row_count", "cardinality"):  # SHOW INDEX is also read after the loop
			self.limit_seen.append(self.limit > 0)
		if self.on_read:
			self.on_read(kind)
		if kind in self.failures:
			raise self.failures[kind]
		return self._rows(kind)

	@staticmethod
	def _kind(text):
		for marker, kind in (
			("select @@session.max_statement_time", "limit_read"),
			("set session max_statement_time", "limit_set"),
			("describe", "describe"),
			("show index", "show_index"),
			("table_rows", "row_count"),
			("count(distinct", "cardinality"),
		):
			if marker in text:
				return kind
		return "other"

	@staticmethod
	def _rows(kind):
		if kind == "describe":
			return [{"Field": c, "Type": "varchar(140)", "Null": "YES", "Default": None} for c in COLUMNS]
		if kind == "show_index":
			return [
				{
					"Non_unique": 0,
					"Cardinality": 100000,
					"Key_name": "PRIMARY",
					"Seq_in_index": 1,
					"Null": "",
					"Column_name": "name",
					"Index_type": "BTREE",
				}
			]
		if kind == "row_count":
			return [[100000]]
		if kind == "cardinality":
			return [[5000]]
		return []


class DbHandle:
	"""What ``frappe.db`` is on a bench: a view of ``frappe.local.db``."""

	def __init__(self, local):
		object.__setattr__(self, "_local", local)

	def __getattr__(self, name):
		return getattr(self._local.db, name)


def _load_frappe_optimizer(local, monkeypatch):
	"""Frappe's real db_optimizer and recorder segment, executed against ``local``."""
	handle = DbHandle(local)
	utils = sys.modules.get("frappe.utils") or types.ModuleType("frappe.utils")
	monkeypatch.setattr(utils, "flt", lambda v, *a: float(v or 0), raising=False)
	monkeypatch.setitem(sys.modules, "frappe.utils", utils)
	shim = types.SimpleNamespace(db=handle, local=local)
	opt = types.ModuleType("db_optimizer")
	opt.__dict__["frappe"] = shim
	exec((FIXTURES / "db_optimizer.py.txt").read_text(), opt.__dict__)
	from collections import defaultdict

	rec = types.ModuleType("recorder")
	rec.__dict__.update(
		frappe=shim,
		DBOptimizer=opt.DBOptimizer,
		DBTable=opt.DBTable,
		defaultdict=defaultdict,
		redis_cache=lambda f: f,
		cstr=lambda v: "" if v is None else str(v),
		cint=lambda v: int(v or 0),
		get_table_name=lambda d, wrap_in_backticks=False: f"`tab{d}`" if wrap_in_backticks else f"tab{d}",
	)
	exec((FIXTURES / "recorder_optimizer.py.txt").read_text(), rec.__dict__)
	return rec._optimize_query, handle


@pytest.fixture
def env(monkeypatch):
	"""A fake db wired in as ``frappe.local.db`` with Frappe's real optimizer behind it."""
	import frappe

	db = FakeDb()
	local = types.SimpleNamespace(db=db)
	optimize, handle = _load_frappe_optimizer(local, monkeypatch)
	monkeypatch.setattr(frappe, "local", local, raising=False)
	monkeypatch.setattr(frappe, "db", handle, raising=False)
	for name in ("frappe.core", "frappe.core.doctype", "frappe.core.doctype.recorder"):
		monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
	module = types.ModuleType("frappe.core.doctype.recorder.recorder")
	module._optimize_query = optimize
	monkeypatch.setitem(sys.modules, module.__name__, module)
	monkeypatch.setattr(index_suggestions, "get_dialect", lambda: MariaDBDialect())
	monkeypatch.setattr(index_suggestions, "job_timeout_types", lambda: (JobTimeout,))
	return types.SimpleNamespace(db=db, local=local, frappe=frappe, optimize=optimize)


def _call(i, table="Sales Invoice", duration=50.0, where="customer = ? AND status = ?"):
	normalized = f"SELECT name, f{i} FROM `tab{table}` WHERE {where}"
	return {
		"query": normalized.replace("?", "'x'"),
		"normalized_query": normalized,
		"duration": duration,
		"stack": [],
	}


def _session(calls):
	return [{"uuid": "r", "calls": calls}]


def _shapes(n, **kw):
	return _session([_call(i, **kw) for i in range(n)])


def _outcome(result):
	return result.findings, result.warnings


def test_pinned_copies_are_unchanged():
	assert PINNED_SHA256 == {
		name: hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() for name in PINNED_SHA256
	}


def test_pinned_optimizer_copy_matches_installed_frappe():
	"""The vendored copy is what the tests run. When an installed Frappe differs, say which
	files and how to re-pin; a different Frappe version only skips (see the NOTICE file)."""
	root = _installed_recorder_dir()
	if root is None:
		pytest.skip("no Frappe checkout found next to this app")
	source = (root / "recorder.py").read_text()
	end = 'return table_name.removeprefix("tab")'
	segment = source[source.index("def _optimize_query") : source.index(end) + len(end)] + "\n"
	installed = {
		"recorder_optimizer.py.txt": segment,
		"db_optimizer.py.txt": (root / "db_optimizer.py").read_text(),
	}
	differing = [n for n, text in installed.items() if text != (FIXTURES / n).read_text()]
	if not differing:
		return
	version = _installed_version(root)
	message = (
		f"Installed Frappe {version} differs from the pinned {PINNED_FRAPPE_VERSION} copies: "
		f"{', '.join(differing)}. Re-check that Optimus still works with the new optimizer, then "
		"re-pin the copies and their sha256 as described in fixtures/frappe_optimizer/NOTICE.md."
	)
	if version != PINNED_FRAPPE_VERSION:
		pytest.skip(message)
	pytest.fail(message)


def _installed_version(recorder_dir):
	text = (recorder_dir.parents[2] / "__init__.py").read_text()
	match = re.search(r'__version__ = "([^"]+)"', text)
	return match.group(1) if match else "unknown"


def _installed_recorder_dir():
	override = os.environ.get("FRAPPE_RECORDER_DIR")
	if override:
		return Path(override)
	for parent in Path(__file__).resolve().parents:
		candidate = parent / "frappe" / "frappe" / "core" / "doctype" / "recorder"
		if candidate.is_dir():
			return candidate
		candidate = parent / "apps" / "frappe" / "frappe" / "core" / "doctype" / "recorder"
		if candidate.is_dir():
			return candidate
	return None


def test_two_hundred_shapes_on_one_table_read_the_table_once(env, empty_context):
	result = index_suggestions.analyze(_shapes(200), empty_context)

	log = env.db.log
	assert log["describe"] == 1
	assert log["show_index"] <= 2  # Frappe's read, plus the analyzer's own index lookup
	assert log["row_count"] == 1
	assert log["cardinality"] <= 2
	assert env.db.total <= 12  # develop: about 1800
	assert any(f["finding_type"] == "Missing Index" for f in result.findings)


def test_a_timed_out_distinct_count_runs_once(env, empty_context):
	env.db.failures["cardinality"] = Exception(*TIMEOUT)

	result = index_suggestions.analyze(_shapes(200), empty_context)

	assert env.db.log["cardinality"] == 1
	assert any("Skipped index suggestions for 200 SELECT" in w for w in result.warnings)
	assert not any("Could not analyze" in w for w in result.warnings)


def test_a_failing_describe_is_read_once_per_table(env, empty_context, monkeypatch):
	env.db.failures["describe"] = Exception(1146, "Table does not exist")
	logged = []
	monkeypatch.setattr(env.frappe, "log_error", lambda **kw: logged.append(kw), raising=False)

	result = index_suggestions.analyze(_shapes(200), empty_context)

	assert env.db.log["describe"] == 1
	assert any("Could not analyze 200 of 200" in w for w in result.warnings)
	assert len(logged) <= index_suggestions.MAX_LOGGED_FAILURES


def test_a_reserved_column_name_is_skipped_quietly_and_read_once(env, empty_context, monkeypatch):
	env.db.failures["cardinality"] = Exception(1064, "You have an error in your SQL syntax near 'read)'")
	logged = []
	monkeypatch.setattr(env.frappe, "log_error", lambda **kw: logged.append(kw), raising=False)
	calls = [_call(i, where="`read` = ?") for i in range(20)]

	result = index_suggestions.analyze(_session(calls), empty_context)

	assert logged == []
	assert env.db.log["cardinality"] == 1
	assert any("20 query shape(s) use a column name MariaDB rejects unquoted" in w for w in result.warnings)
	assert not any("Could not analyze" in w for w in result.warnings)
	assert not any("sql_metadata" in w for w in result.warnings)


@pytest.mark.parametrize("kind", ["describe", "show_index", "row_count"])
def test_a_syntax_error_from_another_statement_is_still_a_real_failure(env, empty_context, monkeypatch, kind):
	env.db.failures[kind] = Exception(1064, "You have an error in your SQL syntax")
	logged = []
	monkeypatch.setattr(env.frappe, "log_error", lambda **kw: logged.append(kw), raising=False)

	result = index_suggestions.analyze(_shapes(20), empty_context)

	assert len(logged) == index_suggestions.MAX_LOGGED_FAILURES
	assert any("Could not analyze 20 of 20" in w for w in result.warnings)
	assert not any("MariaDB rejects unquoted" in w for w in result.warnings)


def test_a_syntax_error_from_an_uncached_path_is_a_real_failure(env, empty_context, monkeypatch):
	"""Without the cache nothing says which statement failed, so a 1064 is never assumed benign."""
	monkeypatch.setattr(index_suggestions, "cached_db", _no_cache)
	env.db.failures["cardinality"] = Exception(1064, "You have an error in your SQL syntax")

	result = index_suggestions.analyze(_shapes(5, where="`read` = ?"), empty_context)

	assert any("Could not analyze 5 of 5" in w for w in result.warnings)


def test_a_missing_table_is_looked_up_once(env, empty_context):
	env.db.tables.clear()

	result = index_suggestions.analyze(_shapes(50), empty_context)

	assert env.db.log["table_exists"] == 1
	assert result.findings == []


def test_the_time_cap_is_set_once_and_active_for_every_read(env, empty_context):
	index_suggestions.analyze(_shapes(30), empty_context)

	assert env.db.log["limit_set"] == 2  # on, then restored
	assert env.db.limit_seen
	assert all(env.db.limit_seen)
	assert env.db.limit == 0.0


def test_findings_and_warnings_match_without_the_cache(env, empty_context, monkeypatch):
	calls = [_call(i, duration=10.0 + i) for i in range(6)]
	calls += [_call(10, table="Customer", duration=80.0, where="territory = ?")]
	calls += [_call(11, table="Customer", duration=0.5, where="owner = ?")]
	env.db.tables.add("tabCustomer")
	recordings = _session(calls)

	cached = index_suggestions.analyze(recordings, empty_context)
	cached_reads = env.db.total
	monkeypatch.setattr(index_suggestions, "cached_db", _no_cache)
	env.db.log.clear()
	plain = index_suggestions.analyze(recordings, empty_context)

	assert cached.findings
	assert _outcome(cached) == _outcome(plain)
	assert cached_reads < env.db.total


@contextlib.contextmanager
def _no_cache(frappe_module, passthrough_errors=()):
	yield False


def test_example_queries_keep_their_first_seen_order_when_the_slowest_is_last(env, empty_context):
	durations = [10.0, 40.0, 25.0, 90.0]
	calls = [_call(i, duration=d) for i, d in enumerate(durations)]

	result = index_suggestions.analyze(_session(calls), empty_context)

	detail = json.loads(result.findings[0]["technical_detail_json"])
	assert detail["example_queries"] == [calls[i]["normalized_query"] for i in range(3)]


def test_findings_with_equal_impact_keep_their_first_seen_order(env, empty_context):
	env.db.tables.update({"tabCustomer", "tabItem"})
	calls = [
		_call(1, table="Customer", duration=30.0, where="territory = ?"),
		_call(2, table="Item", duration=30.0, where="territory = ?"),
		_call(3, table="Sales Invoice", duration=30.0, where="territory = ?"),
	]

	result = index_suggestions.analyze(_session(calls), empty_context)

	assert [f["title"].split("(")[0] for f in result.findings] == [
		"Add index on tabCustomer",
		"Add index on tabItem",
		"Add index on tabSales Invoice",
	]


def _ticking(env, step):
	clock = {"now": 1000.0}
	env.db.on_read = lambda kind: clock.__setitem__("now", clock["now"] + (step if kind == "describe" else 0))
	return clock


def test_the_budget_stops_the_loop_and_says_how_many_were_not_checked(env, empty_context, monkeypatch):
	clock = _ticking(env, 1.0)
	monkeypatch.setattr(index_suggestions, "_monotonic", lambda: clock["now"])
	monkeypatch.setattr(index_suggestions, "INDEX_SUGGESTIONS_BUDGET_SECONDS", 2.5)
	empty_context.deadline = None
	# distinct tables, so every shape reads its table's stats
	env.db.tables.update({f"tabT{i}" for i in range(10)})
	calls = [_call(i, table=f"T{i}", duration=float(100 - i)) for i in range(10)]

	result = index_suggestions.analyze(_session(calls), empty_context)

	note = [w for w in result.warnings if w.startswith("Index suggestions stopped early")]
	assert len(note) == 1
	checked = int(re.search(r"(\d+) of 10 SELECT query shapes were checked", note[0]).group(1))
	assert 0 < checked < 10
	assert f"The other {10 - checked} were not checked" in note[0]
	assert env.db.log["describe"] == checked


def test_the_budget_checks_the_slowest_shapes_first(env, empty_context, monkeypatch):
	clock = _ticking(env, 1.0)
	monkeypatch.setattr(index_suggestions, "_monotonic", lambda: clock["now"])
	monkeypatch.setattr(index_suggestions, "INDEX_SUGGESTIONS_BUDGET_SECONDS", 0.5)
	env.db.tables.update({"tabSlow", "tabFast"})
	calls = [
		_call(1, table="Fast", duration=5.0, where="territory = ?"),
		_call(2, table="Slow", duration=500.0, where="territory = ?"),
	]

	result = index_suggestions.analyze(_session(calls), empty_context)

	titles = [f["title"] for f in result.findings]
	assert titles == ["Add index on tabSlow(territory)"]
	assert any("1 of 2 SELECT query shapes were checked" in w for w in result.warnings)


def test_a_context_deadline_in_the_past_does_no_database_work(env, empty_context):
	empty_context.deadline = index_suggestions._monotonic() - 1

	result = index_suggestions.analyze(_shapes(5), empty_context)

	assert env.db.total == 0
	assert result.findings == []
	assert any("0 of 5 SELECT query shapes were checked" in w for w in result.warnings)


def test_the_overall_deadline_shortens_the_loop_budget(empty_context):
	empty_context.deadline = index_suggestions._monotonic() + 3

	assert index_suggestions._loop_deadline(empty_context) == pytest.approx(empty_context.deadline)


def test_without_a_context_deadline_the_loop_budget_applies(empty_context):
	now = index_suggestions._monotonic()

	deadline = index_suggestions._loop_deadline(empty_context)

	assert deadline - now == pytest.approx(index_suggestions.INDEX_SUGGESTIONS_BUDGET_SECONDS, abs=1)


def test_a_job_timeout_propagates_and_the_real_db_is_put_back(env, empty_context):
	env.db.failures["describe"] = JobTimeout("job exceeded its time limit")

	with pytest.raises(JobTimeout):
		index_suggestions.analyze(_shapes(5), empty_context)

	assert env.local.db is env.db
	assert env.db.limit == 0.0


def test_the_real_db_is_put_back_after_success(env, empty_context):
	index_suggestions.analyze(_shapes(3), empty_context)

	assert env.local.db is env.db


def test_the_real_db_is_put_back_after_an_unexpected_error(env, empty_context, monkeypatch):
	def explode(*args, **kwargs):
		raise KeyboardInterrupt

	monkeypatch.setattr(index_suggestions, "_check_shape", explode)

	with pytest.raises(KeyboardInterrupt):
		index_suggestions.analyze(_shapes(3), empty_context)

	assert env.local.db is env.db


def test_without_a_db_to_wrap_the_per_shape_reads_come_back_with_one_log_line(
	env, empty_context, monkeypatch
):
	del env.local.db
	lines = []
	monkeypatch.setattr(index_suggestions, "log_error_line", lines.append)
	seen = []
	env.frappe.local = types.SimpleNamespace()
	module = sys.modules["frappe.core.doctype.recorder.recorder"]
	monkeypatch.setattr(module, "_optimize_query", lambda q: seen.append(q))

	index_suggestions.analyze(_shapes(4), empty_context)

	assert len(seen) == 4
	assert len(lines) == 1


class _Unrebuildable(Exception):
	def __init__(self, a, b):
		super().__init__(a)


def _cache(db, passthrough=()):
	return optimizer_db_cache.CachingDb(db, passthrough)


def test_a_remembered_failure_is_raised_as_a_fresh_exception():
	db = FakeDb()
	db.failures["cardinality"] = Exception(*TIMEOUT)
	cache = _cache(db)
	raised = []
	for _ in range(3):
		with pytest.raises(Exception) as caught:
			cache.sql("select count(distinct status) from `tabSales Invoice`")
		raised.append(caught.value)

	assert db.log["cardinality"] == 1
	assert len({id(e) for e in raised}) == 3
	assert all(e.args == TIMEOUT and e.__traceback__ is not None for e in raised)
	assert all(e.__cause__ is None for e in raised)


def test_a_failure_that_cannot_be_rebuilt_is_not_remembered():
	db = FakeDb()
	db.failures["describe"] = _Unrebuildable(1064, "b")
	cache = _cache(db)
	for _ in range(2):
		with pytest.raises(_Unrebuildable):
			cache.sql("describe `tabItem`")

	assert db.log["describe"] == 2


def test_a_job_timeout_is_never_remembered():
	db = FakeDb()
	db.failures["describe"] = JobTimeout("stop")
	cache = _cache(db, (JobTimeout,))
	for _ in range(2):
		with pytest.raises(JobTimeout):
			cache.sql("describe `tabItem`")

	assert db.log["describe"] == 2


def test_keys_are_exact_and_case_sensitive():
	db = FakeDb()
	cache = _cache(db)
	for table in ("tabitem", "tabItem", "tabitem", "tabItem"):
		cache.sql(f"describe `{table}`")
		cache.sql(f"select count(distinct Status) from `{table}`")
		cache.sql(f"select count(distinct status) from `{table}`")

	assert db.log["describe"] == 2
	assert db.log["cardinality"] == 4


def test_other_statements_and_calls_with_values_pass_through():
	db = FakeDb()
	cache = _cache(db)
	for _ in range(2):
		cache.sql("select name from `tabItem`")
		cache.sql("describe `tabItem`", ("x",))

	assert db.log["other"] == 2
	assert db.log["describe"] == 2


def test_attribute_writes_go_to_the_real_db():
	db = FakeDb()
	cache = _cache(db)
	cache.marker = 1

	assert db.marker == 1


def test_an_already_cached_db_is_not_wrapped_twice(env):
	with optimizer_db_cache.cached_db(env.frappe) as outer:
		wrapped = env.local.db
		with optimizer_db_cache.cached_db(env.frappe) as inner:
			assert env.local.db is wrapped
	assert (outer, inner) == (True, False)
	assert env.local.db is env.db


@pytest.mark.parametrize("code", [2006, 2013, 1205, 1213, 9999])
def test_a_transient_error_is_not_remembered(code):
	db = FakeDb()
	db.failures["describe"] = Exception(code, "transient")
	cache = _cache(db)
	for _ in range(2):
		with pytest.raises(Exception) as caught:
			cache.sql("describe `tabItem`")
		assert caught.value.args[0] == code

	assert db.log["describe"] == 2


@pytest.mark.parametrize("code", [1064, 1969, 1146, 1054])
def test_a_repeating_error_is_remembered(code):
	db = FakeDb()
	db.failures["describe"] = Exception(code, "repeats")
	cache = _cache(db)
	for _ in range(3):
		with pytest.raises(Exception):
			cache.sql("describe `tabItem`")

	assert db.log["describe"] == 1


def test_an_error_without_an_integer_code_is_not_remembered():
	db = FakeDb()
	db.failures["describe"] = Exception(["not", "hashable"])
	cache = _cache(db)
	for _ in range(2):
		with pytest.raises(Exception):
			cache.sql("describe `tabItem`")

	assert db.log["describe"] == 2


def test_the_failed_statement_is_named_on_the_error():
	db = FakeDb()
	db.failures["cardinality"] = Exception(1064, "syntax")
	cache = _cache(db)
	with pytest.raises(Exception) as caught:
		cache.sql("select count(distinct `read`) from `tabItem`")

	assert optimizer_db_cache.failed_statement(caught.value) == "cardinality"
	assert optimizer_db_cache.failed_statement(Exception(1064)) is None


def test_the_db_is_not_put_back_over_a_replacement(env):
	other = FakeDb()
	with optimizer_db_cache.cached_db(env.frappe):
		env.local.db = other

	assert env.local.db is other


class _TwoShapes:
	def __init__(self):
		self.calls = 0

	def sql(self, query, values=None, as_dict=False, **kw):
		self.calls += 1
		return [{"as": "dict"}] if as_dict else [("as", "tuple")]


def test_results_with_and_without_as_dict_do_not_cross():
	db = _TwoShapes()
	cache = _cache(db)
	statement = "describe `tabItem`"

	assert cache.sql(statement) == [("as", "tuple")]
	assert cache.sql(statement, as_dict=True) == [{"as": "dict"}]
	assert cache.sql(statement) == [("as", "tuple")]
	assert cache.sql(statement, as_dict=True) == [{"as": "dict"}]
	assert db.calls == 2


def test_the_budget_is_two_minutes():
	assert index_suggestions.INDEX_SUGGESTIONS_BUDGET_SECONDS == 120


def test_analyze_run_gives_the_context_the_overall_deadline():
	from optimus import analyze

	source = inspect.getsource(analyze.run)

	assert re.search(
		r"AnalyzeContext\([^)]*deadline=analyze_start \+ ANALYZE_TOTAL_BUDGET_SECONDS", source, re.S
	)


def test_a_clock_exactly_at_the_deadline_does_no_work(env, empty_context, monkeypatch):
	monkeypatch.setattr(index_suggestions, "_monotonic", lambda: 500.0)
	empty_context.deadline = 500.0

	result = index_suggestions.analyze(_shapes(3), empty_context)

	assert env.db.total == 0
	assert any("0 of 3 SELECT query shapes were checked" in w for w in result.warnings)


def test_a_clock_exactly_at_the_deadline_stops_the_loop(env, empty_context, monkeypatch):
	clock = _ticking(env, 1.0)
	monkeypatch.setattr(index_suggestions, "_monotonic", lambda: clock["now"])
	monkeypatch.setattr(index_suggestions, "INDEX_SUGGESTIONS_BUDGET_SECONDS", 1.0)
	env.db.tables.update({f"tabT{i}" for i in range(4)})
	calls = [_call(i, table=f"T{i}", duration=float(10 - i)) for i in range(4)]

	result = index_suggestions.analyze(_session(calls), empty_context)

	assert env.db.log["describe"] == 1  # the first shape moved the clock onto the deadline
	assert any("1 of 4 SELECT query shapes were checked" in w for w in result.warnings)
