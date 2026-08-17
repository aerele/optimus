# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Unit tests for optimus.analyzers.n_plus_one.

The key tests here validate the callsite attribution fix: grouping must
key on the deepest NON-FRAPPE frame, not just the deepest `/apps/` frame,
so N+1 loops routed through frappe helpers still get attributed to
business logic.
"""

import json

from optimus.analyzers import n_plus_one


def test_n_plus_one_detected_from_n_plus_one_fixture(n_plus_one_recording, empty_context):
	"""The fixture has 10 `SELECT tax_rate` queries from the same loop.

	Callsite walking should skip the frappe.database frames and attribute
	them all to sales_invoice.py:212. A single finding should emerge.
	"""
	result = n_plus_one.analyze([n_plus_one_recording], empty_context)

	# Exactly one finding for the 10-query loop (12-query recording but
	# 2 queries are unique non-loop queries).
	assert len(result.findings) == 1
	f = result.findings[0]
	assert f["finding_type"] == "N+1 Query"
	assert f["affected_count"] == 10
	# Severity should be Medium (10 is below the High threshold of 50)
	# OR we hit the total_time_ms > 200 threshold which would make it High.
	# The fixture total is ~93ms so Medium is correct.
	assert f["severity"] in ("Medium", "Low")  # could be Low if threshold bumped


def test_n_plus_one_callsite_attributes_to_business_code(n_plus_one_recording, empty_context):
	"""The N+1 finding must point at the custom-app business frame
	(acme_sales/.../custom_invoice.py:212), NOT frappe/database/database.py.

	This is the fix for review issue #1. The stack has frappe framework
	frames AFTER the business-logic frame, so without the fix we'd blame
	database.py:742 for the N+1 instead of the business-code frame.

	v0.5.2: fixture renamed from erpnext/… to acme_sales/… because
	erpnext is now classified as framework — this test is checking
	the 'blame user code, not framework helpers' behavior, which
	requires the blame frame to be in a non-framework app.
	"""
	result = n_plus_one.analyze([n_plus_one_recording], empty_context)
	assert len(result.findings) == 1

	detail = json.loads(result.findings[0]["technical_detail_json"])
	callsite = detail["callsite"]
	assert "custom_invoice.py" in callsite["filename"]
	assert callsite["lineno"] == 212
	# Must NOT be the frappe database frame
	assert "frappe/database" not in callsite["filename"]


def test_clean_recording_has_no_n_plus_one(clean_recording, empty_context):
	"""A normal list+count query pair should NOT trigger N+1."""
	result = n_plus_one.analyze([clean_recording], empty_context)
	assert result.findings == []


def test_threshold_respected(empty_context):
	"""Groups below the threshold should not become findings."""
	# Build a recording with only 5 identical queries (below default 10)
	recording = {
		"uuid": "thr",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 20,
		"calls": [
			{
				"query": "SELECT 1 FROM t WHERE x=1",
				"normalized_query": "SELECT ? FROM t WHERE x=?",
				"duration": 2,
				"stack": [
					{"filename": "apps/myapp/module.py", "lineno": 100, "function": "f"},
				],
			}
		] * 5,
	}
	result = n_plus_one.analyze([recording], empty_context)
	assert result.findings == []  # 5 < 10 threshold


def test_one_query_per_request_across_many_requests_is_not_n_plus_one(empty_context):
	"""Regression: the same query run ONCE per request across 50 separate
	requests is NOT an N+1 (no loop) and must not be flagged. The old code
	counted occurrences across all recordings, so 50 requests × 1 query =
	"Same query ran 50× — usually a Python loop", a confident false positive.
	N+1 must require the loop to be WITHIN a single action."""
	recordings = [
		{
			"uuid": f"req-{i}",
			"path": "/",
			"cmd": None,
			"method": "GET",
			"event_type": "HTTP Request",
			"duration": 20,
			"calls": [
				{
					"query": "SELECT name FROM tabUser WHERE x=1",
					"normalized_query": "SELECT NAME FROM tabUser WHERE X=?",
					"duration": 5,
					"stack": [
						{"filename": "apps/myapp/module.py", "lineno": 100, "function": "f"},
					],
				}
			],
		}
		for i in range(50)
	]
	result = n_plus_one.analyze(recordings, empty_context)
	assert result.findings == []  # one-per-request × 50 ≠ a loop


def test_loop_within_single_request_still_detected(empty_context):
	"""Sanity counterpart: 15 of the same query in ONE request IS an N+1."""
	recording = {
		"uuid": "loop",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 100,
		"calls": [
			{
				"query": "SELECT name FROM tabUser WHERE x=1",
				"normalized_query": "SELECT NAME FROM tabUser WHERE X=?",
				"duration": 5,
				"stack": [
					{"filename": "apps/myapp/module.py", "lineno": 100, "function": "f"},
				],
			}
		] * 15,
	}
	result = n_plus_one.analyze([recording], empty_context)
	assert any(f["finding_type"] in ("N+1 Query", "Framework N+1") for f in result.findings)


def test_fallback_to_deepest_frame_when_only_frappe_frames(empty_context):
	"""If the only /apps/ frame is in frappe itself, we still emit a
	finding rather than silently dropping it — but as the
	'Framework N+1' type (Low severity, framework-aware description),
	not the actionable 'N+1 Query' type.
	"""
	recording = {
		"uuid": "fb",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 100,
		"calls": [
			{
				"query": "SELECT 1",
				"normalized_query": "SELECT ?",
				"duration": 5,
				"stack": [
					{"filename": "frappe/model/document.py", "lineno": 200, "function": "save"},
				],
			}
		] * 15,  # 15 copies, well above threshold
	}
	result = n_plus_one.analyze([recording], empty_context)
	assert len(result.findings) == 1
	finding = result.findings[0]
	# v0.5.1: framework callsite → Framework N+1, not N+1 Query.
	assert finding["finding_type"] == "Framework N+1", (
		f"Pure-frappe/* stack must emit 'Framework N+1'; got: {finding['finding_type']}"
	)
	assert finding["severity"] == "Low", (
		"Framework N+1 is always Low severity — user can rarely fix it"
	)
	detail = json.loads(finding["technical_detail_json"])
	# It fell back to the frappe frame — still include full path.
	assert "frappe/model/document.py" in detail["callsite"]["filename"]
	# And the detail must carry the is_framework flag.
	assert detail.get("is_framework") is True


def test_severity_scales_with_count_and_time(empty_context):
	"""50+ occurrences OR >200ms total → High severity."""
	high_recording = {
		"uuid": "hi",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 500,
		"calls": [
			{
				"query": f"SELECT name FROM t WHERE id = {i}",
				"normalized_query": "SELECT NAME FROM t WHERE ID = ?",
				"duration": 5.0,
				"stack": [
					{"filename": "apps/myapp/module.py", "lineno": 50, "function": "loop"},
				],
			}
			for i in range(60)  # 60 × 5ms = 300ms total
		],
	}
	result = n_plus_one.analyze([high_recording], empty_context)
	assert len(result.findings) == 1
	assert result.findings[0]["severity"] == "High"
	assert result.findings[0]["affected_count"] == 60


def test_loop_across_many_requests_reports_per_request_count_not_total(empty_context):
	"""Audit #1: a 12× loop across 10 requests is "12× in a row", not "120×".
	Time is kept low (120ms) to isolate the count inflation from severity-by-time."""
	recordings = [
		{
			"uuid": f"req-{r}",
			"path": "/",
			"cmd": None,
			"method": "GET",
			"event_type": "HTTP Request",
			"duration": 50,
			"calls": [
				{
					"query": "SELECT name FROM tabItem WHERE id = 1",
					"normalized_query": "SELECT NAME FROM tabItem WHERE ID = ?",
					"duration": 1.0,
					"stack": [
						{"filename": "apps/myapp/module.py", "lineno": 100, "function": "loop"},
					],
				}
			] * 12,  # 12 in a row, WITHIN one request
		}
		for r in range(10)  # ...the same loop across 10 requests → 120 total
	]
	result = n_plus_one.analyze(recordings, empty_context)
	assert len(result.findings) == 1
	f = result.findings[0]

	# Title reports the per-request loop size, not the cross-request total.
	assert "12×" in f["title"]
	assert "120" not in f["title"]

	# Count no longer drives severity to High: 12 < 50 occurrences AND
	# 120ms < 200ms total → Low.
	assert f["severity"] == "Low"

	# Description states "12 times in a row" and names the spread, never
	# the false "120 times in a row".
	desc = f["customer_description"]
	assert "12 times in a row" in desc
	assert "120 times in a row" not in desc
	assert "10 separate requests" in desc

	# Detail keeps the total as scope but exposes the honest loop/run split.
	detail = json.loads(f["technical_detail_json"])
	assert detail["loop_count"] == 12
	assert detail["run_count"] == 10
	assert detail["occurrences"] == 120  # session-wide total kept in the detail (scope)
	# Headline field is LOOP-scoped (matches the "12×" title), not the 120 total —
	# it feeds the hero, the impact box, the report sort, and the AI-fix prompt.
	assert f["affected_count"] == 12


def test_loop_across_requests_stays_high_when_cumulative_time_is_large(empty_context):
	"""Companion guard: the loop stays High when cumulative time warrants it
	(60 × 5ms = 300ms > 200ms) — severity-by-time is honest, by-count was not."""
	recordings = [
		{
			"uuid": f"req-{r}",
			"path": "/",
			"cmd": None,
			"method": "GET",
			"event_type": "HTTP Request",
			"duration": 100,
			"calls": [
				{
					"query": "SELECT name FROM tabItem WHERE id = 1",
					"normalized_query": "SELECT NAME FROM tabItem WHERE ID = ?",
					"duration": 5.0,
					"stack": [
						{"filename": "apps/myapp/module.py", "lineno": 100, "function": "loop"},
					],
				}
			] * 12,
		}
		for r in range(5)  # 12 × 5 requests = 60 queries, 300ms total
	]
	result = n_plus_one.analyze(recordings, empty_context)
	assert len(result.findings) == 1
	f = result.findings[0]
	assert "12×" in f["title"]  # still per-request, not 60×
	assert f["severity"] == "High"  # justified by 300ms cumulative, not count
	detail = json.loads(f["technical_detail_json"])
	assert detail["loop_count"] == 12
	assert detail["run_count"] == 5
	# Projected post-fix ≈ per-request batched query (2 × 5ms avg) × 5 runs
	# = 50ms, NOT collapsed to a single 10ms query for the whole session.
	assert detail["projected_total_ms"] == 50.0


def test_run_count_ignores_requests_where_query_did_not_loop(empty_context):
	"""run_count counts only requests where the query REPEATED. Loops 10× in one
	request, runs once in 20 others → run_count == 1; projection ≤ total_time."""
	looping = {
		"uuid": "loop-req",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 100,
		"calls": [
			{
				"query": "SELECT name FROM tabItem WHERE id = 1",
				"normalized_query": "SELECT NAME FROM tabItem WHERE ID = ?",
				"duration": 5.0,
				"stack": [
					{"filename": "apps/myapp/module.py", "lineno": 100, "function": "loop"},
				],
			}
		] * 10,  # a real 10× loop in this one request
	}
	singles = [
		{
			"uuid": f"single-{r}",
			"path": "/",
			"cmd": None,
			"method": "GET",
			"event_type": "HTTP Request",
			"duration": 20,
			"calls": [
				{
					"query": "SELECT name FROM tabItem WHERE id = 1",
					"normalized_query": "SELECT NAME FROM tabItem WHERE ID = ?",
					"duration": 5.0,
					"stack": [
						{"filename": "apps/myapp/module.py", "lineno": 100, "function": "loop"},
					],
				}
			],  # the SAME query, but run once — no loop here
		}
		for r in range(20)
	]
	result = n_plus_one.analyze([looping, *singles], empty_context)
	assert len(result.findings) == 1
	f = result.findings[0]
	assert "10×" in f["title"]
	detail = json.loads(f["technical_detail_json"])
	assert detail["loop_count"] == 10
	assert detail["run_count"] == 1  # only the one request that looped
	assert detail["occurrences"] == 30  # 10 + 20 total appearances (scope)
	# Projection must never read as worse than doing nothing.
	assert detail["projected_total_ms"] <= detail["total_time_ms"]


# ---------------------------------------------------------------------------
# v0.5.1 regression guards: don't surface the profiler's own instrumentation
# queries as N+1 findings. A production session flagged:
#
#   "Same query ran 22× at optimus/optimus/infra_capture.py:176"
#
# That's the SHOW GLOBAL STATUS snapshot our before_request hook runs on
# every recording — real SQL, but profiler overhead, not application work
# the user can optimize. Same goes for top_queries.


def test_framework_n_plus_one_query_builder_utils(empty_context):
	"""Exact production payload: Frappe's query builder utility
	issues the same normalized query 138 times while building
	SELECTs for different inputs. Pre-v0.5.1 this emitted as
	'N+1 Query' with an actionable fix hint, misleading the user
	into thinking they should refactor their code — but the
	blamed file is frappe/query_builder/utils.py which the user
	doesn't own. v0.5.1 routes it to 'Framework N+1' instead."""
	recording = {
		"uuid": "framework-qb",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 500,
		"calls": [
			{
				"query": "SELECT DEFAULT_STORAGE_ENGINE FROM information_schema.GLOBAL_VARIABLES",
				"normalized_query": "SELECT ? FROM information_schema.GLOBAL_VARIABLES",
				"duration": 71 / 138,  # ~0.51ms each, matches 71ms cumulative
				"stack": [
					{
						"filename": "frappe/query_builder/utils.py",
						"lineno": 87,
						"function": "get_db_type",
					},
				],
			}
		] * 138,  # matches the production count
	}
	result = n_plus_one.analyze([recording], empty_context)
	# Exactly one finding, and it must be Framework N+1.
	assert len(result.findings) == 1
	f = result.findings[0]
	assert f["finding_type"] == "Framework N+1", (
		f"Pure-frappe callsite (query_builder/utils.py) must emit "
		f"'Framework N+1'; got: {f['finding_type']}"
	)
	assert f["severity"] == "Low"
	# Title signals it's framework — no scary "Same query ran Nx"
	# that implies the user should fix it.
	assert "Framework query repeated" in f["title"]
	assert "138" in f["title"]
	assert "utils.py:87" in f["title"]
	# Description acknowledges limited user action.
	desc = f["customer_description"]
	assert "Frappe's own code" in desc
	assert "not as an action item" in desc or "rarely something you can change" in desc


def test_framework_finding_gates_on_cumulative_not_loop_time(empty_context):
	"""#1: framework loop is 10ms (< 20ms floor) but cumulative 25ms — framework
	findings gate on total_time, so this survives (the loop_time gate dropped it)."""
	fw_call = lambda dur: {  # noqa: E731
		"query": "SELECT ? FROM information_schema.GLOBAL_VARIABLES",
		"normalized_query": "SELECT ? FROM information_schema.GLOBAL_VARIABLES",
		"duration": dur,
		"stack": [{"filename": "frappe/query_builder/utils.py", "lineno": 87, "function": "get_db_type"}],
	}
	looping = {
		"uuid": "loop-req", "path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 100,
		"calls": [fw_call(1.0)] * 10,  # loop_time = 10ms < 20ms floor
	}
	singles = [
		{
			"uuid": f"single-{r}", "path": "/", "cmd": None, "method": "GET",
			"event_type": "HTTP Request", "duration": 20, "calls": [fw_call(1.0)],
		}
		for r in range(15)  # +15ms → cumulative 25ms ≥ 20ms floor
	]
	result = n_plus_one.analyze([looping, *singles], empty_context)
	assert len(result.findings) == 1, "framework N+1 must survive the cumulative gate"
	f = result.findings[0]
	assert f["finding_type"] == "Framework N+1"
	detail = json.loads(f["technical_detail_json"])
	assert detail["occurrences"] == 25
	assert detail["loop_count"] == 10  # the one looping request's size
	assert detail["run_count"] == 1  # only one request actually looped
	assert round(detail["total_time_ms"]) == 25


def test_user_code_n_plus_one_still_emits_as_actionable(empty_context):
	"""Positive case: a genuine user-code N+1 (apps/myapp/...) must
	still emit as 'N+1 Query' with its actionable fix hint. The
	framework routing must NOT over-apply."""
	recording = {
		"uuid": "user-loop",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 500,
		"calls": [
			{
				"query": "SELECT name FROM tabItem WHERE id = ?",
				"normalized_query": "SELECT NAME FROM tabItem WHERE ID = ?",
				"duration": 3.0,
				"stack": [
					{
						"filename": "apps/myapp/controllers/import.py",
						"lineno": 42,
						"function": "import_batch",
					},
				],
			}
		] * 15,
	}
	result = n_plus_one.analyze([recording], empty_context)
	missing = [f for f in result.findings if f["finding_type"] == "N+1 Query"]
	framework = [f for f in result.findings if f["finding_type"] == "Framework N+1"]
	assert len(missing) == 1, (
		"User-code N+1 must still emit as the actionable 'N+1 Query' "
		f"type; got findings={[f['finding_type'] for f in result.findings]}"
	)
	assert len(framework) == 0
	# Fix hint still directs to frappe.get_all / JOIN refactor.
	detail = json.loads(missing[0]["technical_detail_json"])
	assert "frappe.get_all" in detail["fix_hint"]


def test_title_fits_in_140_chars_for_deeply_nested_module_paths(empty_context):
	"""Optimus Finding.title is VARCHAR(140). Apps with deeply-nested
	module paths (jewellery_erpnext has /doctype/<name>/<name>.py with
	three 'jewellery_erpnext' segments in the path) produce N+1 titles
	that overflow the limit and crash the analyze pipeline with
	CharacterLengthExceededError.

	v0.5.1 shortens the filename in the TITLE to the last two path
	segments; the full path is still in customer_description and
	technical_detail_json for navigation.
	"""
	long_filename = (
		"jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
		"doctype/parent_manufacturing_order/parent_manufacturing_order.py"
	)
	recording = {
		"uuid": "overlong-title",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 800,
		"calls": [
			{
				"query": "SELECT * FROM tabChild WHERE parent = ?",
				"normalized_query": "SELECT * FROM tabChild WHERE parent = ?",
				"duration": 5.0,
				"stack": [{
					"filename": long_filename,
					"lineno": 503,
					"function": "process_batch",
				}],
			}
		] * 65,  # 65 occurrences — matches production error count
	}
	result = n_plus_one.analyze([recording], empty_context)
	assert len(result.findings) == 1
	f = result.findings[0]

	# Must fit in the 140-char Optimus Finding.title limit.
	assert len(f["title"]) <= 140, (
		f"Title must fit in 140 chars; got {len(f['title'])}: {f['title']!r}"
	)

	# Title should still reference the filename (shortened) so the
	# developer can navigate to it.
	assert "parent_manufacturing_order" in f["title"]
	assert "503" in f["title"]

	# Full path still present in the customer_description and detail
	# for disambiguation.
	assert long_filename in f["customer_description"]
	detail = json.loads(f["technical_detail_json"])
	assert detail["callsite"]["filename"] == long_filename
	assert detail["callsite"]["lineno"] == 503


def test_short_filename_helper_unit():
	"""Direct unit test of the base.short_filename helper."""
	from optimus.analyzers.base import short_filename

	# Typical case: keep last 2 segments
	assert short_filename("frappe/model/document.py") == "model/document.py"
	assert short_filename("a/b/c/d/e.py") == "d/e.py"

	# Already short enough
	assert short_filename("erpnext.py") == "erpnext.py"
	assert short_filename("model/document.py") == "model/document.py"

	# Absolute path — drop leading slash, keep last 2 segments
	assert (
		short_filename(
			"/Users/navin/office/frappe_bench/apps/frappe/frappe/handler.py"
		)
		== "frappe/handler.py"
	)

	# The exact production payload
	long_fn = (
		"jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
		"doctype/parent_manufacturing_order/parent_manufacturing_order.py"
	)
	assert (
		short_filename(long_fn)
		== "parent_manufacturing_order/parent_manufacturing_order.py"
	)

	# Windows-style path separator
	assert short_filename("a\\b\\c.py") == "b/c.py"

	# Edge cases
	assert short_filename("") == ""
	assert short_filename("/") == ""
	assert short_filename("x.py") == "x.py"

	# Custom keep_segments
	assert short_filename("a/b/c/d.py", keep_segments=1) == "d.py"
	assert short_filename("a/b/c/d.py", keep_segments=3) == "b/c/d.py"


def test_profiler_infra_capture_query_is_not_flagged(empty_context):
	"""Exact production payload: 22 SHOW GLOBAL STATUS calls from
	infra_capture.py:176. Must NOT produce an N+1 finding."""
	recording = {
		"uuid": "infra-noise",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 200,
		"calls": [
			{
				"query": (
					"SHOW GLOBAL STATUS WHERE Variable_name IN "
					"('Threads_connected', 'Threads_running', 'Slow_queries')"
				),
				"normalized_query": (
					"SHOW GLOBAL STATUS WHERE Variable_name IN "
					"('Threads_connected', 'Threads_running', 'Slow_queries')"
				),
				"duration": 1.5,
				"stack": [
					{"filename": "frappe/app.py", "lineno": 202, "function": "init_request"},
					{
						"filename": "optimus/hooks_callbacks.py",
						"lineno": 108,
						"function": "before_request",
					},
					{
						"filename": "optimus/infra_capture.py",
						"lineno": 176,
						"function": "_read_db",
					},
					{
						"filename": "frappe/database/mariadb/database.py",
						"lineno": 742,
						"function": "sql",
					},
				],
			}
		] * 22,  # 22 identical calls, well above threshold
	}
	result = n_plus_one.analyze([recording], empty_context)
	assert result.findings == [], (
		"Profiler's own instrumentation queries must be filtered. "
		f"Got findings: {[f['title'] for f in result.findings]}"
	)


def test_user_code_routed_through_profiler_wrap_still_attributed(empty_context):
	"""A legitimate user-code N+1 where the stack happens to include a
	optimus/capture.py wrap frame (because the wrap intercepts
	frappe.get_doc) must STILL be attributed to the user code, not
	filtered as profiler noise.

	The rule is 'is the deepest non-frappe frame inside optimus/?'
	— here the deepest is user code, so the finding fires."""
	recording = {
		"uuid": "user-through-wrap",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 500,
		"calls": [
			{
				"query": "SELECT name FROM t WHERE id = ?",
				"normalized_query": "SELECT NAME FROM t WHERE ID = ?",
				"duration": 5.0,
				"stack": [
					{"filename": "frappe/app.py", "lineno": 120, "function": "dispatch"},
					{"filename": "apps/myapp/controller.py", "lineno": 42, "function": "bulk_update"},
					# Simulated capture-wrap frame in the middle
					{"filename": "optimus/capture.py", "lineno": 88, "function": "wrapped_get_doc"},
					{"filename": "frappe/model/document.py", "lineno": 200, "function": "get_doc"},
					{"filename": "frappe/database/mariadb/database.py", "lineno": 742, "function": "sql"},
				],
			}
		] * 15,
	}
	result = n_plus_one.analyze([recording], empty_context)
	assert len(result.findings) == 1, (
		"User-code N+1 with a profiler wrap frame in the middle of the "
		"stack must still produce a finding — the deepest non-frappe "
		"frame is the user's controller.py, so the callsite rule matches."
	)
	detail = json.loads(result.findings[0]["technical_detail_json"])
	assert "apps/myapp/controller.py" in detail["callsite"]["filename"]
	assert detail["callsite"]["lineno"] == 42


def test_is_profiler_own_query_matches_bench_relative_paths():
	"""v0.5.1 bug: is_profiler_own_query used startswith() and missed
	the bench-layout path format ``apps/optimus/optimus
	/capture.py`` that pyinstrument produces on some sites. Fixed by
	switching to substring match. Regression test uses the exact
	shapes that were leaking through.
	"""
	from optimus.analyzers.base import is_profiler_own_query

	# Bench-relative paths: apps/<app>/<module>/...
	bench_stack = [
		{"filename": "apps/frappe/frappe/app.py", "lineno": 120, "function": "dispatch"},
		{
			"filename": "apps/optimus/optimus/infra_capture.py",
			"lineno": 176,
			"function": "_read_db",
		},
		{
			"filename": "apps/frappe/frappe/database/mariadb/database.py",
			"lineno": 742,
			"function": "sql",
		},
	]
	assert is_profiler_own_query(bench_stack) is True, (
		"Bench-relative apps/optimus/... path must be detected "
		"as profiler-own. startswith() would have missed this shape."
	)

	# Absolute path format
	abs_stack = [
		{
			"filename": "/Users/navin/office/frappe_bench/apps/frappe/frappe/app.py",
			"lineno": 120, "function": "dispatch",
		},
		{
			"filename": "/Users/navin/office/frappe_bench/apps/optimus/optimus/capture.py",
			"lineno": 88, "function": "wrap",
		},
	]
	assert is_profiler_own_query(abs_stack) is True

	# User frame present in bench-relative stack → NOT profiler own
	user_bench_stack = [
		{"filename": "apps/frappe/frappe/app.py", "lineno": 120, "function": "dispatch"},
		{
			"filename": "apps/myapp/controllers/import.py",
			"lineno": 42, "function": "do_import",
		},
		{
			"filename": "apps/optimus/optimus/capture.py",
			"lineno": 88, "function": "wrap",
		},
		{
			"filename": "apps/frappe/frappe/database/mariadb/database.py",
			"lineno": 742, "function": "sql",
		},
	]
	assert is_profiler_own_query(user_bench_stack) is False


def test_walk_callsite_bench_path_profiler_stack_returns_none():
	"""End-to-end guard: a stack of apps/frappe/... + apps/optimus/...
	frames must route through walk_callsite's fallback and return None
	(not return a profiler frame as the blame callsite). Pre-v0.5.1
	this was the leak that caused 'Framework N+1 at
	apps/optimus/optimus/infra_capture.py:176' to
	still appear in production reports even after the previous
	fixes."""
	from optimus.analyzers.base import walk_callsite

	stack = [
		{
			"filename": "apps/frappe/frappe/hooks.py",
			"lineno": 10, "function": "run_request_hooks",
		},
		{
			"filename": "apps/optimus/optimus/hooks_callbacks.py",
			"lineno": 164, "function": "before_request",
		},
		{
			"filename": "apps/optimus/optimus/infra_capture.py",
			"lineno": 176, "function": "_read_db",
		},
	]
	assert walk_callsite(stack) is None, (
		"walk_callsite must return None for bench-relative profiler "
		"stacks so n_plus_one drops the query — otherwise a "
		"Framework N+1 finding gets emitted blaming the profiler's "
		"own code."
	)


def test_n_plus_one_bench_relative_profiler_stack_produces_no_finding(empty_context):
	"""Regression guard: the exact call shape from the user's
	production report — ``apps/optimus/optimus/
	infra_capture.py`` stacks — must produce zero findings (neither
	normal N+1 Query nor Framework N+1)."""
	recording = {
		"uuid": "bench-profiler-leak",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 200,
		"calls": [
			{
				"query": "SHOW GLOBAL STATUS WHERE Variable_name IN (...)",
				"normalized_query": "SHOW GLOBAL STATUS WHERE Variable_name IN (...)",
				"duration": 1.5,
				"stack": [
					{
						"filename": "apps/frappe/frappe/app.py",
						"lineno": 202, "function": "init_request",
					},
					{
						"filename": "apps/optimus/optimus/hooks_callbacks.py",
						"lineno": 164, "function": "before_request",
					},
					{
						"filename": "apps/optimus/optimus/infra_capture.py",
						"lineno": 176, "function": "_read_db",
					},
					{
						"filename": "apps/frappe/frappe/database/mariadb/database.py",
						"lineno": 742, "function": "sql",
					},
				],
			}
		] * 22,  # 22 occurrences — matches the reported count
	}
	result = n_plus_one.analyze([recording], empty_context)
	# No findings of EITHER type — the profiler's own queries are
	# filtered entirely before the framework-vs-user routing.
	assert result.findings == [], (
		"Profiler's own bench-relative stack must produce zero findings; "
		f"got: {[(f['finding_type'], f['title']) for f in result.findings]}"
	)


def test_is_profiler_own_query_unit():
	"""Direct unit test of the helper — clearer than going through
	the full n_plus_one pipeline for each branch."""
	from optimus.analyzers.base import is_profiler_own_query

	# Stack is ONLY optimus + frappe → is profiler
	stack = [
		{"filename": "frappe/app.py", "lineno": 120, "function": "dispatch"},
		{"filename": "optimus/infra_capture.py", "lineno": 176, "function": "_read_db"},
		{"filename": "frappe/database/mariadb/database.py", "lineno": 742, "function": "sql"},
	]
	assert is_profiler_own_query(stack) is True

	# Stack has a user frame → NOT profiler
	stack = [
		{"filename": "frappe/app.py", "lineno": 120, "function": "dispatch"},
		{"filename": "apps/myapp/controller.py", "lineno": 42, "function": "do_thing"},
		{"filename": "frappe/database/mariadb/database.py", "lineno": 742, "function": "sql"},
	]
	assert is_profiler_own_query(stack) is False

	# Pure frappe stack (migration/fixture) → NOT profiler
	stack = [
		{"filename": "frappe/migrate.py", "lineno": 50, "function": "run"},
		{"filename": "frappe/model/document.py", "lineno": 200, "function": "save"},
		{"filename": "frappe/database/mariadb/database.py", "lineno": 742, "function": "sql"},
	]
	assert is_profiler_own_query(stack) is False

	# Empty / None stack → False (don't drop; let caller's normal path handle)
	assert is_profiler_own_query(None) is False
	assert is_profiler_own_query([]) is False

	# Mixed profiler + user frame → user wins (NOT profiler)
	stack = [
		{"filename": "apps/myapp/bulk.py", "lineno": 10, "function": "bulk"},
		{"filename": "optimus/capture.py", "lineno": 88, "function": "wrap"},
		{"filename": "frappe/database/mariadb/database.py", "lineno": 742, "function": "sql"},
	]
	assert is_profiler_own_query(stack) is False


def test_walk_callsite_returns_none_for_profiler_only_stack():
	"""walk_callsite's fallback used to return the innermost frame
	for 100%-framework stacks. Now it checks is_profiler_own_query and
	returns None when the stack is profiler instrumentation, so
	analyzers drop the query via their `if not callsite: continue`
	guard."""
	from optimus.analyzers.base import walk_callsite

	stack = [
		{"filename": "frappe/app.py", "lineno": 202, "function": "init_request"},
		{"filename": "optimus/hooks_callbacks.py", "lineno": 108, "function": "before_request"},
		{"filename": "optimus/infra_capture.py", "lineno": 176, "function": "_read_db"},
	]
	assert walk_callsite(stack) is None


def test_walk_callsite_still_falls_back_for_pure_frappe_stack():
	"""Legacy behavior preserved: a 100% frappe/ stack (no
	optimus) still falls back to the innermost frame, so
	legitimate framework queries aren't silently dropped."""
	from optimus.analyzers.base import walk_callsite

	stack = [
		{"filename": "frappe/migrate.py", "lineno": 50, "function": "run"},
		{"filename": "frappe/model/document.py", "lineno": 200, "function": "save"},
	]
	frame = walk_callsite(stack)
	assert frame is not None
	assert "frappe/model/document.py" in frame["filename"]


def test_top_queries_filters_profiler_instrumentation(empty_context):
	"""top_queries must skip the SHOW GLOBAL STATUS infra_capture query
	entirely, so it doesn't clutter the slow-queries leaderboard."""
	from optimus.analyzers import top_queries

	recording = {
		"uuid": "mixed",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 500,
		"calls": [
			# Profiler instrumentation — should be dropped
			{
				"query": "SHOW GLOBAL STATUS WHERE Variable_name IN (...)",
				"normalized_query": "SHOW GLOBAL STATUS WHERE Variable_name IN (...)",
				"duration": 350.0,  # Would otherwise rank as top slow query
				"stack": [
					{"filename": "optimus/infra_capture.py", "lineno": 176, "function": "_read_db"},
					{"filename": "frappe/database/mariadb/database.py", "lineno": 742, "function": "sql"},
				],
			},
			# Real application query — should appear in leaderboard
			{
				"query": "SELECT * FROM tabSales Invoice WHERE customer = ?",
				"normalized_query": "SELECT * FROM tabSales Invoice WHERE customer = ?",
				"duration": 250.0,
				"stack": [
					{"filename": "apps/myapp/handler.py", "lineno": 99, "function": "list_invoices"},
					{"filename": "frappe/database/mariadb/database.py", "lineno": 742, "function": "sql"},
				],
			},
		],
	}
	result = top_queries.analyze([recording], empty_context)
	queries = result.aggregate.get("top_queries", [])

	# Only the real query should appear in the leaderboard.
	assert len(queries) == 1
	assert "tabSales Invoice" in queries[0]["normalized_query"]
	# And only the real query should produce a Slow Query finding.
	slow = [f for f in result.findings if f["finding_type"] == "Slow Query"]
	assert len(slow) == 1
	assert "tabSales Invoice" in slow[0]["technical_detail_json"]


# ---------------------------------------------------------------------------
# v0.12.28 regression guards: cross-request leaks that survived 0.12.27.
# ---------------------------------------------------------------------------


def test_action_ref_points_to_the_looping_request_not_first_appearance(empty_context):
	"""#1: action_ref must name the dominant (looping) request, not the first the
	query appeared in. Runs once in req 0, loops 15× in req 1 → ref == "1"."""
	recordings = [
		{  # request 0 — the query appears exactly once (NOT a loop)
			"uuid": "req-0",
			"path": "/",
			"cmd": None,
			"method": "GET",
			"event_type": "HTTP Request",
			"duration": 50,
			"calls": [
				{
					"query": "SELECT rate FROM tabItem Price WHERE item = 1",
					"normalized_query": "SELECT rate FROM tabItem Price WHERE item = ?",
					"duration": 2.0,
					"stack": [
						{"filename": "apps/myapp/pricing.py", "lineno": 42, "function": "price"},
					],
				}
			],
		},
		{  # request 1 — the real 15× loop
			"uuid": "req-1",
			"path": "/",
			"cmd": None,
			"method": "GET",
			"event_type": "HTTP Request",
			"duration": 80,
			"calls": [
				{
					"query": "SELECT rate FROM tabItem Price WHERE item = 1",
					"normalized_query": "SELECT rate FROM tabItem Price WHERE item = ?",
					"duration": 2.0,
					"stack": [
						{"filename": "apps/myapp/pricing.py", "lineno": 42, "function": "price"},
					],
				}
			] * 15,
		},
	]
	result = n_plus_one.analyze(recordings, empty_context)
	assert len(result.findings) == 1
	f = result.findings[0]
	# Points at request 1 (index 1), where the loop ran — NOT request 0.
	assert f["action_ref"] == "1"
	assert "15×" in f["title"]
	detail = json.loads(f["technical_detail_json"])
	assert detail["loop_count"] == 15
	assert detail["run_count"] == 1
	assert detail["occurrences"] == 16  # 1 + 15 total appearances (scope)


def _mixed_loop_and_singles(loop_hits, loop_ms, single_count, single_ms):
	"""One request that loops the query loop_hits× at loop_ms, plus single_count
	other requests that each run the SAME query once at single_ms."""
	call = lambda dur: {  # noqa: E731
		"query": "SELECT name FROM tabItem WHERE id = 1",
		"normalized_query": "SELECT NAME FROM tabItem WHERE ID = ?",
		"duration": dur,
		"stack": [{"filename": "apps/myapp/module.py", "lineno": 100, "function": "loop"}],
	}
	looping = {
		"uuid": "loop-req", "path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 100,
		"calls": [call(loop_ms)] * loop_hits,
	}
	singles = [
		{
			"uuid": f"single-{r}", "path": "/", "cmd": None, "method": "GET",
			"event_type": "HTTP Request", "duration": 20, "calls": [call(single_ms)],
		}
		for r in range(single_count)
	]
	return [looping, *singles]


def test_small_loop_low_severity_and_loop_scoped_cost(empty_context):
	"""#2/#1: severity AND the description cost use the loop's own time (30ms),
	not the 280ms cumulative — so Low, and the description quotes 30ms not 280ms."""
	recordings = _mixed_loop_and_singles(loop_hits=10, loop_ms=3.0, single_count=50, single_ms=5.0)
	result = n_plus_one.analyze(recordings, empty_context)
	assert len(result.findings) == 1
	f = result.findings[0]
	# Loop is 30ms → Low, despite the 280ms cumulative that pre-fix read High.
	assert f["severity"] == "Low"
	# Description quotes the LOOP's own cost (30ms), never the 280ms total.
	assert "30ms" in f["customer_description"]
	assert "280ms" not in f["customer_description"]
	detail = json.loads(f["technical_detail_json"])
	assert detail["loop_count"] == 10
	assert detail["run_count"] == 1
	assert detail["occurrences"] == 60  # 10 + 50 total appearances (scope)
	# Cumulative time is still reported honestly for scope/impact.
	assert detail["total_time_ms"] == 280.0
	# Projection never exceeds the current total.
	assert detail["projected_total_ms"] <= detail["total_time_ms"]
	# Headline economics are LOOP-scoped, so every downstream surface (hero, impact
	# box, report sort, AI-fix prompt) tells the "10× / 30ms" story — NOT the 60 /
	# 280 session totals, which stay in the detail above under a "session-wide" label.
	assert f["affected_count"] == 10  # loop_count, not the 60 total
	assert f["estimated_impact_ms"] == 30.0  # loop_time, not the 280ms total


def test_trivial_loop_suppressed_when_only_noise_lifts_it_over_gate(empty_context):
	"""#2 (gate): a trivial loop (loop_time 5ms < 20ms floor) is suppressed even
	when the same query, run once across 100 requests, lifts the session total."""
	recordings = _mixed_loop_and_singles(loop_hits=10, loop_ms=0.5, single_count=100, single_ms=5.0)
	result = n_plus_one.analyze(recordings, empty_context)
	assert result.findings == []  # loop_time 5ms < 20ms floor → suppressed
