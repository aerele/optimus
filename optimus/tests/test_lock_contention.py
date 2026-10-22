# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for DB lock-contention capture + analysis.

Two layers:
  * ``lock_contention.analyze`` the pure analyzer that turns captured
    ``rec["lock_events"]`` into ``Lock Contention`` findings.
  * ``capture.install_lock_capture`` the class-level ``Database.sql`` patch
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


# Outermost first, app-relative, like frappe.recorder.get_current_stack_frames.
_FRAMES = [
	{"filename": "ugly_code/ugly_code/api.py", "lineno": 10, "function": "endpoint"},
	{"filename": "ugly_code/ugly_code/stock.py", "lineno": 42, "function": "reserve"},
	{"filename": "frappe/frappe/database/database.py", "lineno": 230, "function": "sql"},
]


def _frames(*extra):
	return [dict(f) for f in _FRAMES] + list(extra)


@pytest.fixture
def lock_env(monkeypatch):
	"""A fake ``Database`` class patched by ``install_lock_capture``, plus the
	request-local namespace of an active Optimus session."""
	local = _FakeLocal()
	local.optimus_session_id = "SESSION"

	class FakeDatabase:
		db_type = "mariadb"
		outcome = staticmethod(lambda query: "OK")

		def sql(self, query, *args, **kwargs):
			return type(self).outcome(query)

	original = FakeDatabase.sql
	monkeypatch.setattr(capture, "_database_class", lambda: FakeDatabase)
	monkeypatch.setattr(capture, "_lock_caller_frames", lambda: _frames())
	capture.install_lock_capture(local)

	def raises(exc):
		def outcome(query):
			raise exc

		FakeDatabase.outcome = staticmethod(outcome)

	return types.SimpleNamespace(
		local=local, database=FakeDatabase, db=FakeDatabase(), raises=raises, original=original
	)


def _lock_error(name="QueryDeadlockError", message="Deadlock found", pgcode=None):
	import frappe

	exc = getattr(frappe, name)(message)
	if pgcode:
		cause = Exception("db error")
		cause.pgcode = pgcode
		exc.__cause__ = cause
	return exc


def test_wrap_records_deadlock_and_reraises(lock_env):
	import frappe

	lock_env.raises(_lock_error())
	with pytest.raises(frappe.QueryDeadlockError):
		lock_env.db.sql("UPDATE `tabX` SET a = 1 WHERE name = 'SECRET-VALUE'")

	events = lock_env.local.optimus_lock_events
	assert len(events) == 1
	assert events[0]["kind"] == "deadlock"
	nq = events[0]["normalized_query"]
	assert "SECRET-VALUE" not in nq
	assert nq and "?" in nq and "UPDATE" in nq
	assert events[0]["caller_stack"][0]["function"] == "reserve"  # the user callsite


def test_wrap_passes_through_success_without_recording(lock_env):
	assert lock_env.db.sql("SELECT 1") == "OK"
	assert not hasattr(lock_env.local, "optimus_lock_events")


def test_wrap_reraises_non_lock_exception_without_recording(lock_env):
	lock_env.raises(ValueError("unrelated"))
	with pytest.raises(ValueError):
		lock_env.db.sql("SELECT 1")
	assert not hasattr(lock_env.local, "optimus_lock_events")


def test_wrap_ignores_requests_without_an_active_session(lock_env):
	"""The class-level patch is gated on an Optimus session: any other request
	passes straight through and records nothing."""
	import frappe

	del lock_env.local.optimus_session_id
	lock_env.raises(_lock_error())
	with pytest.raises(frappe.QueryDeadlockError):
		lock_env.db.sql("UPDATE `tabX` SET a = 1")
	assert not hasattr(lock_env.local, "optimus_lock_events")


def test_install_is_idempotent_and_uninstall_restores(lock_env):
	wrapped_once = lock_env.database.sql
	capture.install_lock_capture(lock_env.local)
	assert lock_env.database.sql is wrapped_once
	capture.uninstall_lock_capture()
	assert lock_env.database.sql is lock_env.original


def test_install_without_database_class_is_noop(monkeypatch):
	monkeypatch.setattr(capture, "_database_class", lambda: None)
	capture.install_lock_capture(_FakeLocal())  # must not raise
	capture.uninstall_lock_capture()


def test_capture_survives_every_recorder_state(lock_env):
	"""Issue 2/10: Frappe's recorder patches and unpatches the DB INSTANCE (it sets
	``db._sql`` and swaps ``db.sql``, and none of that happens with Record SQL
	off). The class-level patch records in every state."""
	import frappe

	db = lock_env.db
	lock_env.raises(_lock_error())

	def attempt():
		with pytest.raises(frappe.QueryDeadlockError):
			db.sql("UPDATE `tabX` SET a = 1")

	attempt()  # 1) recorder never patched (Record SQL off)

	db._sql, db.sql = db.sql, lambda *a, **k: db._sql(*a, **k)  # 2) recorder patched
	attempt()

	db.sql = db._sql  # 3) recorder unpatched (record_sql swapped back out)
	attempt()

	del db._sql  # 4) a recorder that drops _sql on unpatch, then patches again
	db._sql, db.sql = db.sql, lambda *a, **k: db._sql(*a, **k)
	attempt()
	assert len(lock_env.local.optimus_lock_events) == 4


def test_wrap_records_timeout_kind(lock_env):
	import frappe

	lock_env.raises(_lock_error("QueryTimeoutError", "Lock wait timeout exceeded"))
	with pytest.raises(frappe.QueryTimeoutError):
		lock_env.db.sql("SELECT 1 FOR UPDATE")
	assert lock_env.local.optimus_lock_events[0]["kind"] == "lock_wait_timeout"


def test_postgres_statement_timeout_is_not_a_lock_wait(lock_env):
	"""Issue 3: Frappe maps a cancelled slow query (SQLSTATE 57014) to the same
	QueryTimeoutError as a lock timeout (55P03). Only the latter is contention."""
	import frappe

	lock_env.raises(_lock_error("QueryTimeoutError", "canceling statement", pgcode="57014"))
	with pytest.raises(frappe.QueryTimeoutError):
		lock_env.db.sql("SELECT pg_sleep(100)")
	assert not hasattr(lock_env.local, "optimus_lock_events")

	lock_env.raises(_lock_error("QueryTimeoutError", "lock timeout", pgcode="55P03"))
	with pytest.raises(frappe.QueryTimeoutError):
		lock_env.db.sql("UPDATE tabx SET a = 1")
	assert lock_env.local.optimus_lock_events[0]["kind"] == "lock_wait_timeout"


def test_statement_timeout_is_found_in_exception_args():
	"""``QueryTimeoutError(e)`` carries the DB error as args[0] when it was not
	raised ``from`` it."""
	import frappe

	cause = Exception("db error")
	cause.pgcode = "57014"
	assert capture._is_statement_timeout(frappe.QueryTimeoutError(cause))
	assert not capture._is_statement_timeout(frappe.QueryTimeoutError("no cause"))


def test_wrap_skips_nowait_probe(lock_env):
	"""A ``NOWAIT`` probe's failure is the caller's designed fast-fail path."""
	import frappe

	lock_env.raises(_lock_error("QueryTimeoutError", "could not obtain lock"))
	with pytest.raises(frappe.QueryTimeoutError):
		lock_env.db.sql("SELECT name FROM `tabX` WHERE name = 'y' FOR UPDATE NOWAIT")
	assert not hasattr(lock_env.local, "optimus_lock_events")


def test_nowait_probe_is_skipped_even_when_sql_cannot_be_parsed(lock_env, monkeypatch):
	"""Issue 7: the probe check must not depend on sqlparse. When parsing fails the
	normalized text is "", which used to hide the keyword and record the probe."""
	import frappe
	import sqlparse

	def broken(*a, **k):
		raise ValueError("cannot parse")

	monkeypatch.setattr(sqlparse, "parse", broken)
	lock_env.raises(_lock_error("QueryTimeoutError", "could not obtain lock"))
	with pytest.raises(frappe.QueryTimeoutError):
		lock_env.db.sql("SELECT name FROM `tabX` WHERE name = 'y' FOR UPDATE NOWAIT")
	assert not hasattr(lock_env.local, "optimus_lock_events")

	# A genuine deadlock is still recorded, with no query text since it did not parse.
	lock_env.raises(_lock_error())
	with pytest.raises(frappe.QueryDeadlockError):
		lock_env.db.sql("UPDATE `tabX` SET a = 'v'")
	assert lock_env.local.optimus_lock_events[0]["normalized_query"] == ""


@pytest.mark.parametrize(
	("query", "is_probe"),
	[
		("SELECT 1 FROM t WHERE a = 1 FOR UPDATE NOWAIT", True),
		("select 1 from t for update nowait", True),
		("SELECT 1 FROM t FOR UPDATE", False),
		("UPDATE t SET x = 1 WHERE status = 'nowait'", False),  # data value
		("UPDATE t SET x = 1 WHERE status = 'it''s nowait'", False),  # escaped quote
		('SELECT "nowait" FROM t', False),  # quoted identifier
		("SELECT `nowait` FROM t", False),
		("SELECT 1 /* nowait */ FROM t", False),  # comment
		("SELECT nowait_x FROM t", False),  # not the whole word
		(None, False),
	],
)
def test_is_nowait_probe(query, is_probe):
	assert capture._is_nowait_probe(query) is is_probe


def test_wrap_records_duration_ms(lock_env):
	import frappe

	lock_env.raises(_lock_error())
	with pytest.raises(frappe.QueryDeadlockError):
		lock_env.db.sql("UPDATE `tabX` SET a = 1")
	ev = lock_env.local.optimus_lock_events[0]
	assert ev["duration_ms"] is not None and ev["duration_ms"] >= 0


def test_event_error_keeps_only_the_first_line(lock_env):
	"""Postgres appends DETAIL lines that quote the other sessions' SQL, literals
	included. Only the first line is stored."""
	import frappe

	lock_env.raises(_lock_error(message="deadlock detected\nDETAIL: Process 1: UPDATE t SET x = 'SECRET'"))
	with pytest.raises(frappe.QueryDeadlockError):
		lock_env.db.sql("UPDATE t SET x = 1")
	assert lock_env.local.optimus_lock_events[0]["error"] == "deadlock detected"


def test_wrap_respects_events_cap(lock_env):
	import frappe

	lock_env.raises(_lock_error())
	for _ in range(capture.LOCK_EVENTS_CAP_PER_RECORDING + 25):
		with pytest.raises(frappe.QueryDeadlockError):
			lock_env.db.sql("SELECT 1")
	assert len(lock_env.local.optimus_lock_events) == capture.LOCK_EVENTS_CAP_PER_RECORDING
	assert lock_env.local.optimus_lock_events_truncated is True


def test_recording_failure_never_replaces_the_db_error(lock_env, monkeypatch):
	import frappe

	def explode(*a, **k):
		raise RuntimeError("recording is broken")

	monkeypatch.setattr(capture, "_lock_caller_frames", explode)
	lock_env.raises(_lock_error())
	with pytest.raises(frappe.QueryDeadlockError):
		lock_env.db.sql("UPDATE `tabX` SET a = 1")


def test_recording_does_not_reenter_itself(lock_env, monkeypatch):
	"""Recording reads settings, which can query the DB. A lock error inside that
	nested query must not recurse into recording again."""
	import frappe

	calls = {"n": 0}

	def nested(*a, **k):
		calls["n"] += 1
		with pytest.raises(frappe.QueryDeadlockError):
			lock_env.db.sql("SELECT nested")
		return _frames()

	monkeypatch.setattr(capture, "_lock_caller_frames", nested)
	lock_env.raises(_lock_error())
	with pytest.raises(frappe.QueryDeadlockError):
		lock_env.db.sql("UPDATE `tabX` SET a = 1")
	assert calls["n"] == 1


def test_framework_app_contention_is_not_recorded_and_skips_the_cap(lock_env, monkeypatch):
	"""Issue 1: ERPNext / HRMS code is framework. Its deadlocks are dropped at
	capture without using up the cap that real findings need."""
	import frappe

	erpnext = [
		{"filename": "erpnext/erpnext/stock/stock_ledger.py", "lineno": 90, "function": "repost"},
		{"filename": "frappe/frappe/database/database.py", "lineno": 230, "function": "sql"},
	]
	monkeypatch.setattr(capture, "_lock_caller_frames", lambda: [dict(f) for f in erpnext])
	lock_env.raises(_lock_error())
	for _ in range(capture.LOCK_EVENTS_CAP_PER_RECORDING + 5):
		with pytest.raises(frappe.QueryDeadlockError):
			lock_env.db.sql("UPDATE `tabBin` SET qty = 1")
	assert getattr(lock_env.local, "optimus_lock_events", []) == []
	assert not getattr(lock_env.local, "optimus_lock_events_truncated", False)


def test_user_code_calling_into_erpnext_is_attributed_to_the_user_frame(lock_env, monkeypatch):
	import frappe

	stack = [
		{"filename": "ugly_code/ugly_code/api.py", "lineno": 10, "function": "endpoint"},
		{"filename": "erpnext/erpnext/stock/stock_ledger.py", "lineno": 90, "function": "repost"},
		{"filename": "frappe/frappe/database/database.py", "lineno": 230, "function": "sql"},
	]
	monkeypatch.setattr(capture, "_lock_caller_frames", lambda: [dict(f) for f in stack])
	lock_env.raises(_lock_error())
	with pytest.raises(frappe.QueryDeadlockError):
		lock_env.db.sql("UPDATE `tabBin` SET qty = 1")
	assert lock_env.local.optimus_lock_events[0]["caller_stack"][0]["function"] == "endpoint"


@pytest.mark.parametrize(
	("filename", "function"),
	[
		("frappe/frappe/utils/change_log.py", "update_last_known_versions"),
		("frappe/frappe/utils/dashboard.py", "generate_and_cache_results"),
		("frappe/frappe/sessions.py", "update_user_metadata"),
	],
)
def test_lock_errors_frappe_handles_itself_are_not_recorded(lock_env, monkeypatch, filename, function):
	"""Issue 6: a deadlock Frappe catches (``suppress`` / ``savepoint``) never
	reaches the caller, so user code above it in the stack is not a finding."""
	import frappe

	handler = {"filename": filename, "lineno": 5, "function": function}
	monkeypatch.setattr(capture, "_lock_caller_frames", lambda: _frames(handler))
	lock_env.raises(_lock_error())
	with pytest.raises(frappe.QueryDeadlockError):
		lock_env.db.sql("UPDATE `tabUser` SET x = 1")
	assert not hasattr(lock_env.local, "optimus_lock_events")


def test_stored_stack_is_trimmed_to_the_callsite_and_a_short_tail(lock_env, monkeypatch):
	import frappe

	deep = [{"filename": "frappe/frappe/model/document.py", "lineno": i, "function": f"f{i}"} for i in range(1, 80)]
	monkeypatch.setattr(capture, "_lock_caller_frames", lambda: _frames()[:2] + deep)
	lock_env.raises(_lock_error())
	with pytest.raises(frappe.QueryDeadlockError):
		lock_env.db.sql("UPDATE `tabX` SET a = 1")
	stored = lock_env.local.optimus_lock_events[0]["caller_stack"]
	assert len(stored) == 1 + capture._LOCK_STACK_TAIL_FRAMES
	assert stored[0]["function"] == "reserve"
	assert stored[-1]["function"] == "f79"  # innermost kept


def test_stored_stack_still_resolves_to_the_same_callsite():
	"""The analyzer re-derives the callsite from the trimmed stack."""
	stack = [
		{"filename": "ugly_code/ugly_code/stock.py", "lineno": 42, "function": "reserve"},
		{"filename": "frappe/frappe/model/document.py", "lineno": 3, "function": "save"},
	]
	f = lock_contention.analyze([{"lock_events": [_event(stack=stack)]}], _ctx()).findings[0]
	assert json.loads(f["technical_detail_json"])["callsite"]["function"] == "reserve"


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


# ----- review fixes -------------------------------------------------------


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


# ----- analyzer: framework apps, normalization, render, summary -----------


def _stack(*frames):
	return [{"filename": f, "lineno": 7, "function": fn} for f, fn in frames]


def test_erpnext_only_contention_is_not_a_finding():
	"""Issue 1: a deadlock inside ERPNext / HRMS code is framework contention."""
	for path in ("erpnext/erpnext/stock/stock_ledger.py", "hrms/hrms/payroll/x.py"):
		stack = _stack((path, "repost"), ("frappe/frappe/database/database.py", "sql"))
		assert lock_contention.analyze([{"lock_events": [_event(stack=stack)]}], _ctx()).findings == []


def test_user_frame_calling_into_erpnext_is_the_callsite():
	stack = _stack(
		("ugly_code/ugly_code/api.py", "endpoint"),
		("erpnext/erpnext/stock/stock_ledger.py", "repost"),
		("frappe/frappe/database/database.py", "sql"),
	)
	f = lock_contention.analyze([{"lock_events": [_event(stack=stack)]}], _ctx()).findings[0]
	assert json.loads(f["technical_detail_json"])["callsite"]["function"] == "endpoint"


def test_tracked_apps_inclusion_mode_is_honoured(monkeypatch):
	"""With Tracked Apps set, only those apps' frames are the developer's code."""
	from optimus import settings

	monkeypatch.setattr(
		settings, "get_config", lambda: types.SimpleNamespace(tracked_apps=("ugly_code",))
	)
	other = _stack(("other_app/other_app/jobs.py", "run"))
	assert lock_contention.analyze([{"lock_events": [_event(stack=other)]}], _ctx()).findings == []
	assert len(lock_contention.analyze([{"lock_events": [_event()]}], _ctx()).findings) == 1


def test_postgres_quoted_identifiers_survive_normalization():
	"""Issue (first review): ``"tabX"`` is an identifier, not data."""
	query = 'UPDATE "tabBin" SET "qty" = 5 WHERE "name" = \'SECRET\''
	out = capture._normalize_sql_safe(query, keep_quoted_identifiers=True)
	assert '"tabBin"' in out and '"qty"' in out
	assert "SECRET" not in out and "5" not in out


def test_mariadb_double_quoted_values_are_still_stripped():
	"""On MariaDB a double-quoted token can be a string value, so it is stripped."""
	out = capture._normalize_sql_safe('SELECT * FROM `tabX` WHERE name = "SECRET"')
	assert "SECRET" not in out
	assert "`tabX`" in out


def test_summary_does_not_claim_zero_ms_for_a_lock_finding():
	"""Issue 5: no "~0ms - high priority" and no "time went into" for a finding that
	has no measured impact."""
	from optimus import analyze

	finding = lock_contention.analyze([{"lock_events": [_event()]}], _ctx()).findings[0]
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	ctx.actions = [{"action_label": "GET x", "duration_ms": 50000, "recording_uuid": "r0"}]
	ctx.findings = [finding]
	summary = analyze._build_summary_html(ctx, total_queries=1, recordings=[])
	assert "~0" not in summary
	assert "went into" not in summary
	assert "it also hit" in summary and "high priority" in summary


def test_summary_keeps_time_wording_for_timed_findings():
	from optimus import analyze

	timed = {
		"finding_type": "Slow Query", "severity": "High", "title": "Slow query on tabX",
		"estimated_impact_ms": 1200.0, "action_ref": "0",
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	ctx.actions = [{"action_label": "GET x", "duration_ms": 2000, "recording_uuid": "r0"}]
	ctx.findings = [timed]
	summary = analyze._build_summary_html(ctx, total_queries=1, recordings=[])
	assert "most of its time went into" in summary and "~1200ms" in summary


def test_lock_finding_shows_no_zero_ms_badge_in_report():
	"""Issue 4: a 50s lock wait must not render as '0ms consolidated'."""
	import re
	import types as _types

	from optimus import renderer

	ev = _event(kind="lock_wait_timeout")
	ev["duration_ms"] = 50000.0
	finding_dict = lock_contention.analyze([{"lock_events": [ev, ev, ev]}], _ctx()).findings[0]
	html = _render_single(finding_dict)
	assert not re.search(r'class="big">\s*0(\.0+)?\s*ms', html)
	# 3 x 50s of waiting, shown instead of the missing impact.
	assert re.search(r'class="big"><span[^>]*>150\.00s</span></span><small class="scope-tag">waited', html)
	assert "3× hits" in html


def test_lock_finding_without_wait_shows_kind_not_a_number():
	html = _render_single(lock_contention.analyze([{"lock_events": [_event()]}], _ctx()).findings[0])
	assert "reliability issue" in html


def test_reliability_finding_outranks_timed_one_in_a_root_cause_group():
	"""The 0ms deadlock must not lose the primary card to a same-severity timed
	finding in the same function."""
	from optimus.renderer import finding_enrichment as fe

	callsite = {"filename": "app/app/stock.py", "function": "reserve", "lineno": 3}
	lock = {
		"finding_type": "Lock Contention", "severity": "High", "estimated_impact_ms": 0.0,
		"technical_detail": {"callsite": callsite},
	}
	slow = {
		"finding_type": "Slow Query", "severity": "High", "estimated_impact_ms": 900.0,
		"technical_detail": {"callsite": callsite},
	}
	grouped = fe._group_findings_by_root_cause([slow, lock])
	assert len(grouped) == 1
	assert grouped[0]["finding_type"] == "Lock Contention"
	assert grouped[0]["sub_findings"][0]["finding_type"] == "Slow Query"


def test_ai_hint_does_not_claim_every_timeout_rolls_back():
	from optimus import ai_fix

	hint = ai_fix._FINDING_TYPE_HINTS["Lock Contention"]
	assert "its transaction was rolled back" not in hint
	assert "transaction may still be open" in hint


def _render_single(finding_dict):
	import types as _types

	from optimus import renderer

	finding = _types.SimpleNamespace(llm_fix_json=None, **finding_dict)
	action = _types.SimpleNamespace(
		action_label="GET bg_chained_audit", event_type="HTTP Request",
		http_method="GET", path="/x", recording_uuid="r0", duration_ms=100,
		queries_count=0, query_time_ms=0, slowest_query_ms=0,
		call_tree_json=json.dumps({
			"function": "<root>", "filename": "", "lineno": 0,
			"self_ms": 0, "cumulative_ms": 100, "children": [],
		}),
	)
	doc = _types.SimpleNamespace(
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
	return renderer.render_raw(doc, recordings=[])
