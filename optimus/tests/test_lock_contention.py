# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for DB lock-contention capture + analysis.

Two layers:
  * ``lock_contention.analyze`` the pure analyzer that turns captured
    ``rec["lock_events"]`` into ``Lock Contention`` findings.
  * ``capture.wrap_db_sql_for_lock_capture`` the request-scoped SQL wrapper
    that records deadlocks / lock-wait timeouts and re-raises them unchanged.
"""

import json
import types

import pytest

from optimus import capture
from optimus.analyzers import lock_contention
from optimus.analyzers.base import AnalyzeContext

_USER_STACK = [
	{"filename": "apps/ugly_code/ugly_code/jobs.py", "lineno": 316, "function": "bg_chained_audit"},
]


def _event(kind="deadlock", query="UPDATE `tabUser` SET x = ? WHERE name = ?", stack=None):
	return {
		"kind": kind,
		"normalized_query": query,
		"error": "Deadlock found when trying to get lock; try restarting transaction",
		"caller_stack": stack if stack is not None else _USER_STACK,
		"time": 1.0,
	}


def _ctx():
	return AnalyzeContext(session_uuid="t", docname="t")


# ----- analyzer -----------------------------------------------------------


def test_deadlock_event_emits_high_lock_contention_finding():
	result = lock_contention.analyze([{"calls": [], "lock_events": [_event()]}], _ctx())
	assert len(result.findings) == 1
	f = result.findings[0]
	assert f["finding_type"] == "Lock Contention"
	assert f["severity"] == "High"
	assert f["action_ref"] == "0"
	assert f["affected_count"] == 1
	assert f["estimated_impact_ms"] == 0.0
	detail = json.loads(f["technical_detail_json"])
	assert detail["callsite"]["function"] == "bg_chained_audit"
	assert detail["callsite"]["lineno"] == 316
	assert detail["lock_kinds"] == ["deadlock"]
	assert "jobs.py:316" in f["title"]
	assert f["title"].startswith("Deadlock")


def test_no_lock_events_no_findings():
	assert lock_contention.analyze([{"lock_events": []}], _ctx()).findings == []
	assert lock_contention.analyze([{"calls": []}], _ctx()).findings == []
	assert lock_contention.analyze([], _ctx()).findings == []


def test_repeated_events_at_one_callsite_bucket_with_count():
	rec = {"lock_events": [_event(), _event(), _event()]}
	f = lock_contention.analyze([rec], _ctx()).findings[0]
	assert f["affected_count"] == 3
	assert "3" in f["title"]


def test_mixed_kinds_label_deadlock_when_any_deadlock():
	rec = {"lock_events": [_event(kind="lock_wait_timeout"), _event(kind="deadlock")]}
	f = lock_contention.analyze([rec], _ctx()).findings[0]
	assert f["title"].startswith("Deadlock")
	assert json.loads(f["technical_detail_json"])["lock_kinds"] == ["deadlock", "lock_wait_timeout"]


def test_timeout_only_labels_lock_wait_timeout():
	f = lock_contention.analyze([{"lock_events": [_event(kind="lock_wait_timeout")]}], _ctx()).findings[0]
	assert f["title"].startswith("Lock-wait timeout")
	assert f["severity"] == "High"


def test_distinct_callsites_produce_distinct_findings():
	other = [{"filename": "apps/ugly_code/ugly_code/common.py", "lineno": 40, "function": "recheck"}]
	rec = {"lock_events": [_event(), _event(stack=other)]}
	findings = lock_contention.analyze([rec], _ctx()).findings
	assert len(findings) == 2


def test_event_with_no_resolvable_callsite_is_skipped():
	# Empty stack → no user frame → dropped. The renderer suppresses frameless
	# findings anyway; such a deadlock is usually benign framework contention
	# that Frappe handles itself.
	assert lock_contention.analyze([{"lock_events": [_event(stack=[])]}], _ctx()).findings == []


def test_framework_only_callsite_is_skipped():
	# An all-framework stack (frappe/*) is benign contention Frappe catches via
	# savepoint/suppress; it must not become a user-facing finding.
	fw = [{"filename": "frappe/sessions.py", "lineno": 481, "function": "update"}]
	assert lock_contention.analyze([{"lock_events": [_event(stack=fw)]}], _ctx()).findings == []


def test_malformed_events_are_skipped():
	assert lock_contention.analyze([{"lock_events": ["x", None, 42]}], _ctx()).findings == []


def test_title_stays_within_140_chars():
	long_stack = [{
		"filename": "apps/really_long_app/really_long_app/doctype/thing/thing_controller.py",
		"lineno": 99999,
		"function": "validate",
	}]
	rec = {"lock_events": [_event(stack=long_stack) for _ in range(99)]}
	f = lock_contention.analyze([rec], _ctx()).findings[0]
	assert len(f["title"]) <= 140


def test_action_ref_tracks_recording_index():
	recs = [{"lock_events": []}, {"lock_events": [_event()]}]
	f = lock_contention.analyze(recs, _ctx()).findings[0]
	assert f["action_ref"] == "1"


# ----- capture wrap -------------------------------------------------------


class _FakeLocal:
	pass


_BENCH = "/home/frappe/frappe-bench/apps"
_RAW_STACK = [  # innermost first, absolute paths, like _capture_caller_stack
	{"filename": f"{_BENCH}/frappe/frappe/database/database.py", "lineno": 230, "function": "sql"},
	{"filename": f"{_BENCH}/ugly_code/ugly_code/stock.py", "lineno": 42, "function": "reserve"},
	{"filename": f"{_BENCH}/ugly_code/ugly_code/api.py", "lineno": 10, "function": "endpoint"},
	{"filename": "/usr/lib/python3.12/threading.py", "lineno": 1010, "function": "run"},
]


@pytest.fixture
def bench_stack(monkeypatch):
	# Accept *a/**k: the lock path calls _capture_caller_stack(max_depth=None).
	monkeypatch.setattr(capture, "_capture_caller_stack", lambda *a, **k: list(_RAW_STACK))


class _FakeDB:
	"""Mimics a Frappe request DB. ``_sql`` is the underlying executor the lock
	wrap targets; ``sql`` is a thin shim that delegates to the CURRENT ``_sql``
	(like ``frappe.recorder.record_sql``), so wrapping ``_sql`` is seen through
	``sql`` too and survives the recorder swapping ``sql`` out and back."""

	def __init__(self, sql_fn):
		self._sql = sql_fn

		def _sql_entry(*a, **k):
			return self._sql(*a, **k)

		self.sql = _sql_entry


def test_wrap_records_deadlock_and_reraises(bench_stack):
	import frappe

	hits = {"n": 0}

	def boom(*a, **k):
		hits["n"] += 1
		raise frappe.QueryDeadlockError("Deadlock found when trying to get lock")

	local = _FakeLocal()
	local.db = _FakeDB(boom)
	capture.wrap_db_sql_for_lock_capture(local)

	with pytest.raises(frappe.QueryDeadlockError):
		local.db.sql("UPDATE `tabX` SET a = 1 WHERE name = 'SECRET-VALUE'")

	assert hits["n"] == 1
	events = local.optimus_lock_events
	assert len(events) == 1
	assert events[0]["kind"] == "deadlock"
	# The query is normalized: the raw literal is stripped to a placeholder. The
	# normalized form is also populated (conftest stubs normalize_query).
	nq = events[0]["normalized_query"]
	assert "SECRET-VALUE" not in nq
	assert nq and "?" in nq and "UPDATE" in nq
	assert isinstance(events[0]["caller_stack"], list)


def test_wrap_passes_through_success_without_recording():
	local = _FakeLocal()
	local.db = _FakeDB(lambda *a, **k: "OK")
	capture.wrap_db_sql_for_lock_capture(local)
	assert local.db.sql("SELECT 1") == "OK"
	assert not hasattr(local, "optimus_lock_events")


def test_wrap_reraises_non_lock_exception_without_recording():
	local = _FakeLocal()

	def boom(*a, **k):
		raise ValueError("unrelated")

	local.db = _FakeDB(boom)
	capture.wrap_db_sql_for_lock_capture(local)
	with pytest.raises(ValueError):
		local.db.sql("SELECT 1")
	assert not hasattr(local, "optimus_lock_events")


def test_wrap_is_idempotent():
	local = _FakeLocal()
	local.db = _FakeDB(lambda *a, **k: "OK")
	capture.wrap_db_sql_for_lock_capture(local)
	wrapped_once = local.db._sql
	capture.wrap_db_sql_for_lock_capture(local)
	assert local.db._sql is wrapped_once


def test_wrap_records_timeout_kind(bench_stack):
	import frappe

	def boom(*a, **k):
		raise frappe.QueryTimeoutError("Lock wait timeout exceeded")

	local = _FakeLocal()
	local.db = _FakeDB(boom)
	capture.wrap_db_sql_for_lock_capture(local)
	with pytest.raises(frappe.QueryTimeoutError):
		local.db.sql("SELECT 1 FOR UPDATE")
	assert local.optimus_lock_events[0]["kind"] == "lock_wait_timeout"


def test_wrap_no_db_is_noop():
	local = _FakeLocal()  # no .db attribute
	capture.wrap_db_sql_for_lock_capture(local)  # must not raise
	assert not hasattr(local, "optimus_lock_events")


def test_caller_stack_unbounded_keeps_deep_user_frame():
	"""Issue #4: the lock path walks the stack UNBOUNDED (max_depth=None) so a
	deep ORM / hook chain can't push the user frame past the default 20-frame cap
	and get the real contention dropped or misattributed."""

	def deep(n):
		if n > 0:
			return deep(n - 1)
		return (
			capture._capture_caller_stack(),              # default cap (20)
			capture._capture_caller_stack(max_depth=None),  # unbounded
		)

	capped, full = deep(40)
	assert len(capped) <= 20
	assert len(full) > 20  # the deep frames the cap would have dropped are kept


def test_wrap_no_underlying_sql_is_noop():
	# A DB object without ``_sql`` (exotic wrapper) degrades to no capture.
	local = _FakeLocal()
	local.db = types.SimpleNamespace()  # no _sql
	capture.wrap_db_sql_for_lock_capture(local)  # must not raise
	assert not hasattr(local, "optimus_lock_events")


def test_wrap_survives_recorder_sql_unpatch_and_repatch(bench_stack):
	"""Issue #10: the recorder swaps ``db.sql`` between ``record_sql`` and
	``db._sql`` (do_not_record / record_queries / teardown). Because the wrap
	lives on ``_sql``, lock capture keeps firing across such a swap instead of
	silently stopping for the rest of the request."""
	import frappe

	def boom(*a, **k):
		raise frappe.QueryDeadlockError("Deadlock found")

	local = _FakeLocal()
	local.db = _FakeDB(boom)
	capture.wrap_db_sql_for_lock_capture(local)

	# 1) normal recorder-patched path (sql shim -> wrapped _sql)
	with pytest.raises(frappe.QueryDeadlockError):
		local.db.sql("UPDATE `tabX` SET a = 1")
	assert len(local.optimus_lock_events) == 1

	# 2) recorder UNPATCH: db.sql = db._sql (our wrapped _sql). Still captures.
	local.db.sql = local.db._sql
	with pytest.raises(frappe.QueryDeadlockError):
		local.db.sql("UPDATE `tabX` SET a = 2")
	assert len(local.optimus_lock_events) == 2

	# 3) recorder RE-PATCH with a record_sql-like shim delegating to _sql.
	def record_sql_like(*a, **k):
		return local.db._sql(*a, **k)

	local.db.sql = record_sql_like
	with pytest.raises(frappe.QueryDeadlockError):
		local.db.sql("UPDATE `tabX` SET a = 3")
	assert len(local.optimus_lock_events) == 3


def test_wrap_skips_nowait_probe(bench_stack):
	"""Issue #2: a ``NOWAIT`` lock probe's failure is the caller's designed
	fast-fail path (Frappe uses them, e.g. delete_doc), not contention so it must
	not be recorded as a finding."""
	import frappe

	def boom(*a, **k):
		raise frappe.QueryTimeoutError("could not obtain lock")

	local = _FakeLocal()
	local.db = _FakeDB(boom)
	capture.wrap_db_sql_for_lock_capture(local)
	with pytest.raises(frappe.QueryTimeoutError):
		local.db.sql("SELECT name FROM `tabX` WHERE name = 'y' FOR UPDATE NOWAIT")
	assert getattr(local, "optimus_lock_events", []) == []


def test_wrap_does_not_skip_data_literal_nowait(bench_stack):
	"""The NOWAIT skip keys off the SQL KEYWORD (checked after normalization, where
	literals are stripped), so a genuine deadlock on a query that merely compares a
	column to the row value 'nowait' is still recorded not false-skipped."""
	import frappe

	def boom(*a, **k):
		raise frappe.QueryDeadlockError("Deadlock found")

	local = _FakeLocal()
	local.db = _FakeDB(boom)
	capture.wrap_db_sql_for_lock_capture(local)
	with pytest.raises(frappe.QueryDeadlockError):
		local.db.sql("UPDATE `tabTask` SET x = 1 WHERE status = 'nowait'")
	assert len(getattr(local, "optimus_lock_events", [])) == 1


def test_wrap_records_duration_ms(bench_stack):
	"""Issue #5: the wall-time the failed query burned is captured on the event."""
	import frappe

	def boom(*a, **k):
		raise frappe.QueryDeadlockError("Deadlock found")

	local = _FakeLocal()
	local.db = _FakeDB(boom)
	capture.wrap_db_sql_for_lock_capture(local)
	with pytest.raises(frappe.QueryDeadlockError):
		local.db.sql("UPDATE `tabX` SET a = 1")
	ev = local.optimus_lock_events[0]
	assert "duration_ms" in ev
	assert ev["duration_ms"] is not None and ev["duration_ms"] >= 0


# ----- render -------------------------------------------------------------


def test_lock_contention_finding_renders_in_report():
	"""An analyzer-produced Lock Contention finding renders through the real
	report template (title, callsite, query and fix hint all visible)."""
	import types

	from optimus import renderer

	finding_dict = lock_contention.analyze([{"lock_events": [_event()]}], _ctx()).findings[0]
	finding = types.SimpleNamespace(llm_fix_json=None, **finding_dict)
	action = types.SimpleNamespace(
		action_label="GET bg_chained_audit", event_type="HTTP Request",
		http_method="GET", path="/x", recording_uuid="r0", duration_ms=100,
		queries_count=0, query_time_ms=0, slowest_query_ms=0,
		call_tree_json=json.dumps({
			"function": "<root>", "filename": "", "lineno": 0,
			"self_ms": 0, "cumulative_ms": 100, "children": [],
		}),
	)
	doc = types.SimpleNamespace(
		name="PS-lc", session_uuid="lc", title="lock test", user="a@b.com",
		status="Ready", started_at="2026-05-13T00:00:00",
		stopped_at="2026-05-13T00:00:01", notes=None, top_severity="High",
		summary_html=None, total_duration_ms=100, total_query_time_ms=0,
		total_queries=0, total_requests=1, top_queries_json="[]",
		table_breakdown_json="[]", hot_frames_json="[]",
		session_time_breakdown_json=None, total_python_ms=None, total_sql_ms=None,
		analyzer_warnings=None, v5_aggregate_json="{}",
		actions=[action], findings=[finding], phase_2_runs=[],
	)
	html = renderer.render_raw(doc, recordings=[])
	assert "Deadlock on a query" in html   # the finding title
	assert "jobs.py:316" in html           # the callsite
	assert "consistent order" in html      # the fix hint
	# The contended query must actually render (via example_queries): the card's
	# "Query (normalized)" block was removed, so a finding that only set
	# normalized_query would show no SQL at all.
	assert "Affected queries" in html
	assert "UPDATE `tabUser` SET x = ?" in html  # the failed query, normalized
	# Issue #6: Lock Contention is actionable (joins "Fix these first") but has
	# impact 0, so its action-plan step must NOT render a dangling "est. saving"
	# label with no number. This is the only finding, so the label must be absent.
	assert "est. saving" not in html


def test_lock_contention_tldr_hero_has_no_zero_ms_badge():
	"""A top-ranked deadlock (impact 0) must not headline the TL;DR hero as
	'0ms · ...'; the lock_contention branch leads with the title alone."""
	import types

	from optimus.renderer import _internal

	finding = lock_contention.analyze([{"lock_events": [_event()]}], _ctx()).findings[0]
	session_doc = types.SimpleNamespace(total_duration_ms=100, total_requests=1)
	tldr = _internal._compose_tldr([finding], session_doc)
	headline = str(tldr["headline_markup"])
	assert "Deadlock on a query" in headline
	assert "0ms" not in headline and "0.0ms" not in headline
	assert "&middot;" not in headline and "·" not in headline


def test_wrap_respects_events_cap(bench_stack):
	import frappe

	def boom(*a, **k):
		raise frappe.QueryDeadlockError("Deadlock found")

	local = _FakeLocal()
	local.db = _FakeDB(boom)
	capture.wrap_db_sql_for_lock_capture(local)
	for _ in range(capture.LOCK_EVENTS_CAP_PER_RECORDING + 25):
		with pytest.raises(frappe.QueryDeadlockError):
			local.db.sql("SELECT 1")
	assert len(local.optimus_lock_events) == capture.LOCK_EVENTS_CAP_PER_RECORDING


# ----- review fixes -------------------------------------------------------


def test_bench_under_frappe_home_still_attributes_user_frame(bench_stack):
	"""Absolute /home/frappe/... paths must not read as framework frames."""
	import frappe

	def boom(*a, **k):
		raise frappe.QueryDeadlockError("Deadlock found")

	local = _FakeLocal()
	local.db = _FakeDB(boom)
	capture.wrap_db_sql_for_lock_capture(local)
	with pytest.raises(frappe.QueryDeadlockError):
		local.db.sql("UPDATE `tabBin` SET qty = 1")
	stored = local.optimus_lock_events[0]["caller_stack"]
	assert [f["filename"] for f in stored] == [
		"ugly_code/ugly_code/api.py", "ugly_code/ugly_code/stock.py", "frappe/frappe/database/database.py",
	]
	f = lock_contention.analyze([{"lock_events": local.optimus_lock_events}], _ctx()).findings[0]
	# Deepest user frame wins over the outer entry point.
	assert json.loads(f["technical_detail_json"])["callsite"]["function"] == "reserve"


def test_framework_only_events_do_not_use_the_cap(monkeypatch):
	fw = [{"filename": "/home/x/apps/frappe/frappe/sessions.py", "lineno": 1, "function": "update"}]
	monkeypatch.setattr(capture, "_capture_caller_stack", lambda: fw)
	local = _FakeLocal()
	for _ in range(capture.LOCK_EVENTS_CAP_PER_RECORDING + 5):
		capture._record_lock_event(local, "deadlock", Exception("d"), "SELECT 1", fw)
	assert getattr(local, "optimus_lock_events", []) == []
	assert not getattr(local, "optimus_lock_events_truncated", False)


def test_cap_sets_truncated_flag(bench_stack):
	local = _FakeLocal()
	for _ in range(capture.LOCK_EVENTS_CAP_PER_RECORDING + 1):
		capture._record_lock_event(local, "deadlock", Exception("d"), "SELECT 1", list(_RAW_STACK))
	assert local.optimus_lock_events_truncated is True


def test_truncation_marker_becomes_warning():
	result = lock_contention.analyze([{"lock_events": [_event(), {"_truncated": True}]}], _ctx())
	assert len(result.findings) == 1
	assert any("event cap" in w for w in result.warnings)


def test_same_callsite_across_recordings_is_one_finding():
	recs = [{"lock_events": [_event()]}, {"lock_events": [_event(), _event()]}, {"lock_events": [_event()]}]
	findings = lock_contention.analyze(recs, _ctx()).findings
	assert len(findings) == 1
	assert findings[0]["affected_count"] == 4
	assert findings[0]["action_ref"] == "1"  # recording with the most events
	assert json.loads(findings[0]["technical_detail_json"])["affected_actions"] == [0, 1, 2]

def test_unknown_kind_is_skipped_not_labelled_deadlock():
	rec = {"lock_events": [_event(kind="lock_wait_timeout"), _event(kind=None), _event(kind="weird")]}
	f = lock_contention.analyze([rec], _ctx()).findings[0]
	assert f["affected_count"] == 1
	assert f["title"].startswith("Lock-wait timeout")


def test_failed_query_is_in_example_queries():
	f = lock_contention.analyze([{"lock_events": [_event()]}], _ctx()).findings[0]
	assert json.loads(f["technical_detail_json"])["example_queries"] == ["UPDATE `tabUser` SET x = ? WHERE name = ?"]


def test_lock_contention_is_ai_eligible():
	from optimus import ai_fix

	assert "Lock Contention" in ai_fix.AI_ELIGIBLE_FINDING_TYPES
	assert "Lock Contention" in ai_fix._FINDING_TYPE_HINTS


# ----- query normalization (privacy) --------------------------------------


def test_normalize_sql_safe_strips_literals():
	out = capture._normalize_sql_safe("UPDATE `tabX` SET a = 1 WHERE name = 'SECRET'")
	assert "SECRET" not in out
	assert "?" in out and "UPDATE" in out


def test_normalize_sql_safe_never_returns_raw_literals():
	# Even on a statement that doesn't cleanly normalize, the literal must never
	# survive (frappe.recorder.normalize_query would return the raw text here).
	out = capture._normalize_sql_safe("GARBAGE 'rowsecret' ;;; not valid")
	assert "rowsecret" not in out


def test_normalize_sql_safe_strips_comment_literals():
	# Comments can carry row data; frappe's normalize_query leaves them intact
	# (they're not Token.Literal), so we strip them to honor "no row data".
	out = capture._normalize_sql_safe("SELECT 1 /* user secretpii@x.com */ FROM `tabX`")
	assert "secretpii" not in out
	assert "SELECT" in out


def test_normalize_sql_safe_empty_and_none():
	assert capture._normalize_sql_safe("") == ""
	assert capture._normalize_sql_safe(None) == ""


def test_normalize_sql_safe_coerces_non_str_query():
	# db.sql accepts non-str query objects (query builders) str()-coerce, then
	# normalize, so the finding shows the shape instead of a blank query.
	class _QB:
		def __str__(self):
			return "SELECT * FROM `tabX` WHERE id = 42"

	out = capture._normalize_sql_safe(_QB())
	assert "42" not in out
	assert "?" in out and "SELECT" in out


# ----- analyzer: query list, descriptions, wait duration ------------------


def test_finding_lists_all_distinct_queries_at_callsite():
	"""Issue #7: multiple DISTINCT statements deadlocking at one callsite all
	appear, not just the first otherwise the developer and the AI fix prompt are
	pointed at the wrong table."""
	q1 = "UPDATE `tabA` SET x = ? WHERE name = ?"
	q2 = "UPDATE `tabB` SET y = ? WHERE name = ?"
	rec = {"lock_events": [_event(query=q1), _event(query=q2), _event(query=q1)]}
	f = lock_contention.analyze([rec], _ctx()).findings[0]
	detail = json.loads(f["technical_detail_json"])
	# Stored under example_queries the key the report card + AI prompt render;
	# sample_queries renders nowhere.
	assert sorted(detail["example_queries"]) == sorted([q1, q2])  # both, deduped
	assert "sample_queries" not in detail
	assert detail["normalized_query"] in (q1, q2)


def test_deadlock_description_does_not_overclaim_mechanism():
	"""Issue #3: MariaDB reports a snapshot conflict (err 1020) as a deadlock too,
	so the description must not assert 'rows in opposite orders' as the only cause."""
	f = lock_contention.analyze([{"lock_events": [_event(kind="deadlock")]}], _ctx()).findings[0]
	assert "opposite orders" not in f["customer_description"]


def test_timeout_description_is_db_agnostic():
	"""Issue #3: the lock-wait description names innodb_lock_wait_timeout as a
	MariaDB/MySQL EXAMPLE, not a fact about every backend (the same
	QueryTimeoutError also covers a Postgres statement timeout)."""
	f = lock_contention.analyze(
		[{"lock_events": [_event(kind="lock_wait_timeout")]}], _ctx()
	).findings[0]
	assert "MariaDB/MySQL" in f["customer_description"]


def test_finding_surfaces_wait_duration():
	"""Issue #5: when events carry a duration, the finding exposes the wait time
	(so the cost of a lock-wait timeout that burned ~50s is not invisible)."""
	ev = _event(kind="lock_wait_timeout")
	ev["duration_ms"] = 50000.0
	f = lock_contention.analyze([{"lock_events": [ev]}], _ctx()).findings[0]
	detail = json.loads(f["technical_detail_json"])
	assert detail["max_wait_ms"] == 50000.0
	assert detail["total_wait_ms"] == 50000.0
	assert "waited" in f["customer_description"].lower()


def test_finding_wait_duration_absent_when_events_lack_it():
	"""Events without a duration (older recordings) leave the wait fields None and
	add no wait sentence no crash, no fabricated number."""
	f = lock_contention.analyze([{"lock_events": [_event()]}], _ctx()).findings[0]
	detail = json.loads(f["technical_detail_json"])
	assert detail["max_wait_ms"] is None and detail["total_wait_ms"] is None
	assert "waited" not in f["customer_description"].lower()
