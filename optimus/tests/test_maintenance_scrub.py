# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""optimus.maintenance: scrub / purge of the AI Error Log rows written before
the key-leak fix, and the v0_12 patch that runs the scrub on migrate.

``maintenance.frappe`` is replaced wholesale by an in-memory fake that
implements just the ORM calls the module makes (``get_all`` with LIKE / = / >
filters, ``or_filters``, ``order_by="name asc"``, ``limit_page_length``;
``db.set_value`` with a field dict; ``db.count``; ``db.delete``; savepoints;
``cache.lpop``; ``get_doc(...).insert``).
"""

import importlib
import json
import re
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
	rx = "^" + ".*".join(re.escape(p) for p in pattern.split("%")) + "$"
	return re.search(rx, value or "", re.S) is not None


def _match(row, flt):
	field, op, val = flt
	if op == "like":
		return _like(row.get(field), val)
	if op == "=":
		return row.get(field) == val
	if op == ">":
		return (row.get(field) or "") > val
	raise AssertionError(op)


class _FakeFrappe:
	def __init__(self, error_logs, deleted_docs=()):
		self.tables = {
			"Error Log": {n: {"name": n, "error": e, "method": "t", "metadata": "{}"} for n, e in error_logs},
			"Deleted Document": {
				n: {"name": n, "deleted_doctype": "Error Log", "data": d} for n, d in deleted_docs
			},
		}
		self.reads = 0
		self.writes = []
		self.deletes = []
		self.fail_writes = set()
		self.savepoints = []
		self.db = SimpleNamespace(
			set_value=self._set_value, delete=self._delete, count=self._count,
			savepoint=self.savepoints.append,
			rollback=lambda save_point=None: self.savepoints.append(("rollback", save_point)),
		)

	def get_all(self, doctype, filters=None, or_filters=None, fields=None, order_by=None, limit_page_length=0):
		assert order_by == "name asc"
		self.reads += 1
		rows = [
			r for r in self.tables[doctype].values()
			if all(_match(r, f) for f in filters or [])
			and (not or_filters or any(_match(r, f) for f in or_filters))
		]
		rows.sort(key=lambda r: r["name"])
		if limit_page_length:
			rows = rows[:limit_page_length]
		return [{k: r.get(k) for k in fields} for r in rows]

	def _set_value(self, doctype, name, values, update_modified=True):
		assert update_modified is False and isinstance(values, dict)
		if name in self.fail_writes:
			raise RuntimeError("Lock wait timeout exceeded")
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
	def _make(error_logs, deleted_docs=(), current_key=KEY):
		f = _FakeFrappe(error_logs, deleted_docs)
		f.commits, f.flushes = [], []
		monkeypatch.setattr(maintenance, "frappe", f)
		monkeypatch.setattr(maintenance, "safe_commit", lambda: f.commits.append(1))
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

	def test_a_failing_row_does_not_stop_the_others(self, fake):
		f = fake([("a", LEAKY), ("b", LEAKY), ("c", LEAKY)])
		f.fail_writes = {"b"}
		out = maintenance.scrub_error_log_secrets(dry_run=False)
		assert (out["changed"], out["failed"]) == (2, 1)
		assert KEY not in f.tables["Error Log"]["a"]["error"] + f.tables["Error Log"]["c"]["error"]
		assert f.tables["Error Log"]["b"]["error"] == LEAKY  # rolled back to its savepoint only
		assert ("rollback", "optimus_scrub_row") in f.savepoints

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


class _FakeCache:
	def __init__(self, queues, broken=False):
		self.queues = queues
		self.broken = broken

	def lpop(self, key):
		if self.broken:
			raise ConnectionError("redis down")
		queue = self.queues.get(key) or []
		return queue.pop(0).encode() if queue else None


class TestFlushDeferredErrorLogs:
	def _frappe(self, monkeypatch, cache):
		inserted, commits = [], []

		def _get_doc(record):
			return SimpleNamespace(insert=lambda ignore_permissions=False: inserted.append(record))
		monkeypatch.setattr(maintenance, "frappe", SimpleNamespace(cache=cache, get_doc=_get_doc))
		monkeypatch.setattr(maintenance, "safe_commit", lambda: commits.append(1))
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

	def test_a_broken_queue_is_not_fatal(self, monkeypatch):
		self._frappe(monkeypatch, _FakeCache({}, broken=True))
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


@pytest.fixture
def patch_env(monkeypatch):
	calls = []

	def _scrub(**kw):
		calls.append(kw)
		return {"candidates": 3, "changed": 2, "deleted_docs_changed": 1, "residual": 0, "failed": 0}

	monkeypatch.setattr(maintenance, "scrub_scan_size", lambda: 1000)
	monkeypatch.setattr(maintenance, "scrub_error_log_secrets", _scrub)
	return calls


def test_patch_runs_the_scrub_for_real(patch_env, capsys):
	importlib.import_module(_PATCH).execute()
	assert patch_env == [{"dry_run": False}]
	assert "masked AI API keys in 3 stored error row(s)" in capsys.readouterr().out


def test_patch_skips_a_table_too_large_for_migrate(patch_env, monkeypatch, capsys):
	monkeypatch.setattr(maintenance, "scrub_scan_size", lambda: maintenance.MIGRATE_SCAN_LIMIT + 1)
	importlib.import_module(_PATCH).execute()
	assert patch_env == []
	out = capsys.readouterr().out
	assert "skipped the Error Log key scrub" in out
	assert "execute optimus.maintenance.scrub_error_log_secrets" in out


def test_patch_warns_about_residual_rows(patch_env, monkeypatch, capsys):
	monkeypatch.setattr(
		maintenance, "scrub_error_log_secrets",
		lambda **kw: {"candidates": 1, "changed": 0, "deleted_docs_changed": 0, "residual": 1, "failed": 0},
	)
	importlib.import_module(_PATCH).execute()
	assert "purge_ai_error_logs" in capsys.readouterr().out


@pytest.fixture
def failing_scrub(monkeypatch):
	"""A scrub that fails while a frame local and its message hold a key;
	``frappe.db`` replaced wholesale to record the rollback."""
	import frappe

	def _scrub(**kw):
		row_text = f"headers = {{'authorization': 'Bearer {KEY}'}}"  # noqa: F841 (a frame local)
		raise ValueError(f"cannot update row holding {KEY}")

	rollbacks = []
	monkeypatch.setattr(maintenance, "scrub_scan_size", lambda: 10)
	monkeypatch.setattr(maintenance, "scrub_error_log_secrets", _scrub)
	monkeypatch.setattr(frappe, "db", SimpleNamespace(rollback=lambda *a, **k: rollbacks.append(1)), raising=False)
	return rollbacks


def test_a_failed_scrub_never_blocks_migrate(failing_scrub, capsys):
	assert importlib.import_module(_PATCH).execute() is None  # returns normally
	out = capsys.readouterr().out
	assert "failed (ValueError)" in out
	assert "execute optimus.maintenance.scrub_error_log_secrets --kwargs \"{'dry_run': False}\"" in out
	assert KEY not in out
	assert failing_scrub == [1]  # the failing chunk's writes rolled back


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
