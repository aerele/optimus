# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the redundant_calls analyzer, which requires a caller_stack on each
sidecar entry and filters findings whose callsite is framework code (users
can't act on framework loops). The fixture builder defaults to a user-code
stack so most tests exercise the core aggregation logic."""

import json
from pathlib import Path

from optimus.analyzers import redundant_calls
from optimus.analyzers.base import AnalyzeContext

# Canonical user-code caller stack used by the default fixture. Passing
# this through walk_callsite yields ``apps/myapp/controllers/bulk.py:42``
# as the blame frame, so findings built from this fixture are kept.
# Canonical user-code caller stack used by the default fixture, innermost frame
# first (the order capture._capture_caller_stack builds). redundant_calls
# reverses it before walk_callsite, which yields
# apps/myapp/controllers/bulk.py:42 as the blame frame, so findings built from
# this fixture are kept.
_USER_CALLER_STACK = [
	{"filename": "apps/myapp/controllers/bulk.py", "lineno": 42, "function": "do_import"},
	{"filename": "frappe/handler.py", "lineno": 46, "function": "handle"},
	{"filename": "frappe/app.py", "lineno": 120, "function": "application"},
]

# Framework-only stack (innermost first): walk_callsite returns only framework
# frames for it, so findings built from it get filtered out.
_FRAMEWORK_CALLER_STACK = [
	{"filename": "frappe/cache_manager.py", "lineno": 30, "function": "get_doctype_map"},
	{"filename": "frappe/model/document.py", "lineno": 500, "function": "save"},
	{"filename": "frappe/app.py", "lineno": 120, "function": "application"},
]


def _sidecar_entry(fn_name, raw, safe, caller_stack=None):
	"""Build a sidecar entry; ``caller_stack`` defaults to the user-code stack."""
	return {
		"fn_name": fn_name,
		"identifier_raw": raw,
		"identifier_safe": safe,
		"caller_stack": caller_stack if caller_stack is not None else _USER_CALLER_STACK,
	}


def test_emits_finding_when_get_doc_threshold_exceeded():
	# 8 identical get_doc calls (threshold = 5)
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [
			_sidecar_entry("get_doc", ("Item", "ITEM-X"), ("Item", "abc123hash"))
			for _ in range(8)
		],
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	ctx.actions = [{"action_label": "test", "duration_ms": 100}]
	result = redundant_calls.analyze([recording], ctx)

	rc = [f for f in result.findings if f["finding_type"] == "Redundant Call"]
	assert len(rc) == 1
	assert rc[0]["affected_count"] == 8
	# Title in the safe form (no plaintext name)
	assert "Item" in rc[0]["title"]
	# technical_detail carries both forms AND the callsite (v0.5.2)
	td = json.loads(rc[0]["technical_detail_json"])
	assert "identifier_raw" in td
	assert "identifier_safe" in td
	assert "callsite" in td
	assert td["callsite"]["filename"] == "apps/myapp/controllers/bulk.py"
	assert td["callsite"]["lineno"] == 42
	# Description surfaces the callsite so users can navigate
	assert "apps/myapp/controllers/bulk.py:42" in rc[0]["customer_description"]


def test_no_finding_below_threshold():
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [
			_sidecar_entry("get_doc", ("Item", "X"), ("Item", "hash1")),
			_sidecar_entry("get_doc", ("Item", "X"), ("Item", "hash1")),
		],
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = redundant_calls.analyze([recording], ctx)
	assert result.findings == []


def test_high_severity_at_5x_threshold():
	# 25 calls = 5x threshold of 5 → High
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [
			_sidecar_entry("get_doc", ("Item", "X"), ("Item", "hash1"))
			for _ in range(25)
		],
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = redundant_calls.analyze([recording], ctx)
	rc = [f for f in result.findings if f["finding_type"] == "Redundant Call"]
	assert rc[0]["severity"] == "High"


def test_cache_get_threshold_separate_from_doc_threshold():
	# 8 cache_get calls well under cache threshold of 50
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [
			_sidecar_entry("cache_get", "user_lang:x", "hashx")
			for _ in range(8)
		],
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = redundant_calls.analyze([recording], ctx)
	assert result.findings == []  # below cache threshold


def test_cache_threshold_suppresses_low_count_noise():
	"""A 30x cache loop (under the 50 threshold) must be suppressed entirely:
	small 0ms loops are noise, not actionable findings."""
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [
			_sidecar_entry("cache_get", "role_permissions", "hash1")
			for _ in range(30)
		],
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = redundant_calls.analyze([recording], ctx)
	assert result.findings == [], (
		"30× cache loop must be suppressed post-threshold-bump. "
		"Loops this small at 0ms impact are noise. "
		f"Got: {[f['title'] for f in result.findings]}"
	)


def test_cache_high_count_still_fires():
	"""A 60x loop (above the 50 threshold) still emits a finding."""
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [
			_sidecar_entry("cache_get", "role_permissions", "hash1")
			for _ in range(60)
		],
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = redundant_calls.analyze([recording], ctx)
	rc = [f for f in result.findings if f["finding_type"] == "Redundant Call"]
	assert len(rc) == 1
	assert rc[0]["affected_count"] == 60


def test_truncation_marker_emits_warning():
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [
			_sidecar_entry("get_doc", ("Item", "X"), ("Item", "h1"))
			for _ in range(3)
		] + [{"_truncated": True}],
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	redundant_calls.analyze([recording], ctx)
	assert any("truncated" in w.lower() for w in ctx.warnings)


def test_identifier_safe_is_used_as_bucket_key():
	"""Two different raw values with different safe values must not merge."""
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": (
			[_sidecar_entry("get_doc", ("Item", "A"), ("Item", "hashA")) for _ in range(6)]
			+ [_sidecar_entry("get_doc", ("Item", "B"), ("Item", "hashB")) for _ in range(6)]
		),
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = redundant_calls.analyze([recording], ctx)
	# Two separate findings because the safe hashes are different
	rc = [f for f in result.findings if f["finding_type"] == "Redundant Call"]
	assert len(rc) == 2


# ---------------------------------------------------------------------------
# v0.5.2: callsite-based filtering
# ---------------------------------------------------------------------------


def test_framework_callsite_filters_finding():
	"""A loop whose callsite is framework code (frappe/*) must be suppressed, with
	a warning explaining why."""
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [
			_sidecar_entry(
				"cache_get",
				"role_permissions:Administrator",
				"hash-fw",
				caller_stack=_FRAMEWORK_CALLER_STACK,
			)
			for _ in range(60)  # above cache threshold (50, bumped from 10 in v0.5.2 round 4)
		],
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = redundant_calls.analyze([recording], ctx)

	# No finding the callsite was framework-only.
	rc = [f for f in result.findings if f["finding_type"] == "Redundant Call"]
	assert rc == [], (
		f"Framework-only callsite must not produce a Redundant Call "
		f"finding (user can't act on it). Got: "
		f"{[f['title'] for f in rc]}"
	)
	# And the suppression is surfaced as a warning so the user
	# understands WHY they see no Redundant Call entries despite
	# having hot cache loops.
	assert any(
		"Frappe framework code" in w and "Suppressed" in w
		for w in ctx.warnings
	), f"Expected framework-filter warning; got: {ctx.warnings}"


def test_user_callsite_finding_is_kept():
	"""A genuine user-code loop still produces a finding, with the callsite
	visible in the detail."""
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [
			_sidecar_entry(
				"get_doc",
				("Customer", f"C-{i}"),
				("Customer", "userloop_hash"),
			)
			for i in range(10)
		],
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = redundant_calls.analyze([recording], ctx)
	rc = [f for f in result.findings if f["finding_type"] == "Redundant Call"]
	assert len(rc) == 1
	td = json.loads(rc[0]["technical_detail_json"])
	assert td["callsite"]["filename"] == "apps/myapp/controllers/bulk.py"


def test_erpnext_callsite_filters_finding():
	"""Official Frappe-maintained apps (erpnext, hrms, etc.) count as framework for
	the Redundant Call filter, so a loop inside erpnext code is suppressed."""
	erpnext_stack = [
		{"filename": "frappe/app.py", "lineno": 120, "function": "application"},
		{"filename": "frappe/desk/form/save.py", "lineno": 40, "function": "savedocs"},
		{
			"filename": (
				"apps/erpnext/erpnext/accounts/doctype/"
				"sales_invoice/sales_invoice.py"
			),
			"lineno": 300,
			"function": "validate",
		},
	]
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [
			_sidecar_entry(
				"cache_get",
				"role_permissions:SalesManager",
				"hash-erpnext",
				caller_stack=erpnext_stack,
			)
			for _ in range(106)  # matches production report count
		],
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = redundant_calls.analyze([recording], ctx)
	rc = [f for f in result.findings if f["finding_type"] == "Redundant Call"]
	assert rc == [], (
		"ERPNext-internal cache loop must be suppressed from the "
		"actionable Redundant Call list users can't patch ERPNext. "
		f"Got: {[f['title'] for f in rc]}"
	)
	# Suppression warning surfaces the reason.
	assert any(
		"Frappe framework code" in w or "third-party library" in w
		for w in ctx.warnings
	), f"Expected framework-filter warning; got: {ctx.warnings}"


def test_third_party_lib_callsite_filters_finding():
	"""Third-party infrastructure callsites (werkzeug / site-packages / gunicorn /
	rq) must be filtered: users can't modify them."""
	werkzeug_stack = [
		{"filename": "frappe/app.py", "lineno": 120, "function": "application"},
		{
			"filename": "env/lib/python3.14/site-packages/werkzeug/serving.py",
			"lineno": 370,
			"function": "run_wsgi",
		},
	]
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [
			_sidecar_entry(
				"cache_get",
				f"session-key-{i}",
				"hash-werkzeug",
				caller_stack=werkzeug_stack,
			)
			for i in range(15)  # above cache threshold
		],
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = redundant_calls.analyze([recording], ctx)
	rc = [f for f in result.findings if f["finding_type"] == "Redundant Call"]
	assert rc == [], (
		"werkzeug/site-packages callsite must be suppressed. "
		f"Got: {[f['title'] for f in rc]}"
	)


def test_cross_request_spread_does_not_count_as_redundant():
	"""A cache lookup called once per request across many requests isn't a loop
	(per-action max = 1), so the per-action threshold check must suppress it."""
	# 60 recordings, each with exactly ONE cache_get for the same key.
	# Total = 60 (above threshold of 50, bumped in v0.5.2 round 4),
	# but per-action max = 1 not a loop.
	recordings = []
	for i in range(60):
		recordings.append({
			"uuid": f"rec-{i}",
			"calls": [],
			"sidecar": [
				_sidecar_entry("cache_get", "user_lang:x", "hash-spread")
			],
			"pyi_session": None,
		})
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = redundant_calls.analyze(recordings, ctx)
	rc = [f for f in result.findings if f["finding_type"] == "Redundant Call"]
	assert rc == [], (
		"Cross-request spread (1 call × 20 requests) must NOT be "
		"flagged as a redundant loop. "
		f"Got: {[f['title'] for f in rc]}"
	)
	# Warning should explain the suppression.
	assert any(
		"summing across multiple requests" in w
		for w in ctx.warnings
	), f"Expected cross-request-spread warning; got: {ctx.warnings}"


def test_single_request_loop_still_fires():
	"""60 calls from a single request still fire (a real loop): the per-action
	threshold must not over-filter."""
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [
			_sidecar_entry("cache_get", "user_lang:x", "hash-loop")
			for _ in range(60)  # 60 in ONE action, above 50 threshold
		],
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = redundant_calls.analyze([recording], ctx)
	rc = [f for f in result.findings if f["finding_type"] == "Redundant Call"]
	assert len(rc) == 1, (
		"A 60-call loop in a single request must still fire. "
		f"Got: {[f['title'] for f in rc]}"
	)


def test_missing_caller_stack_is_dropped_with_warning():
	"""Sidecar entries without a caller_stack are dropped (with a warning) rather
	than emitted as findings with no navigable callsite."""
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [
			# NOTE: explicitly no caller_stack (use dict literal, not
			# the _sidecar_entry helper that defaults it).
			{
				"fn_name": "get_doc",
				"identifier_raw": ("Item", "X"),
				"identifier_safe": ("Item", "hash"),
			}
			for _ in range(10)
		],
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = redundant_calls.analyze([recording], ctx)
	rc = [f for f in result.findings if f["finding_type"] == "Redundant Call"]
	assert rc == []
	# Warning explains why the candidate was dropped so the user
	# knows to re-run the session on the upgraded profiler.
	assert any("no captured caller stack" in w for w in ctx.warnings), (
		f"Expected no-caller-stack warning; got: {ctx.warnings}"
	)




# ---------------------------------------------------------------------------
# L5 (corpus anchors): the real loop line, not the outer doc-event-hook frame
# ---------------------------------------------------------------------------
# These chains mirror ugly_code/ugly_code/python/common.py (looped_validate:9
# calls _run_validations -> _check_user_exists, whose loop line 24 fetches the
# User doc; looped_validate:11 calls _run_post_checks -> _verify_permissions,
# whose loop lines 205 and 206 check read and write permission). Before the fix
# the corpus findings 3q1efl686s, 3q1qg8btng and 3q1fsdhl5p were anchored to
# looped_validate:9 / :11. Each stack is innermost-first, the order
# capture._capture_caller_stack builds it in.

_COMMON_PY = "apps/ugly_code/ugly_code/python/common.py"


def _hook_chain(loop_line, loop_fn, mid_line, mid_fn, hook_line):
	return [
		{"filename": _COMMON_PY, "lineno": loop_line, "function": loop_fn},
		{"filename": _COMMON_PY, "lineno": mid_line, "function": mid_fn},
		{"filename": _COMMON_PY, "lineno": hook_line, "function": "looped_validate"},
		{"filename": "apps/frappe/frappe/model/document.py", "lineno": 500, "function": "run_method"},
		{"filename": "apps/frappe/frappe/app.py", "lineno": 120, "function": "application"},
	]


def _anchor(fn_name, raw, safe, stack, count):
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"pyi_session": None,
		"sidecar": [_sidecar_entry(fn_name, raw, safe, caller_stack=stack) for _ in range(count)],
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	rc = [f for f in redundant_calls.analyze([recording], ctx).findings if f["finding_type"] == "Redundant Call"]
	assert len(rc) == 1
	detail = json.loads(rc[0]["technical_detail_json"])
	assert detail["callsite_walk"] == "outermost_first"  # D-STAMP: built by the fixed walk
	return detail["callsite"], rc[0]


def test_corpus_anchor_get_doc_3q1efl686s():
	callsite, finding = _anchor(
		"get_doc", ("User", "Administrator"), ("User", "hashadmin"),
		_hook_chain(24, "_check_user_exists", 15, "_run_validations", 9), 150,
	)
	assert (callsite["lineno"], callsite["function"]) == (24, "_check_user_exists")
	assert f"{_COMMON_PY}:24" in finding["customer_description"]


def test_corpus_anchor_has_permission_read_3q1qg8btng():
	callsite, _ = _anchor(
		"has_permission", ("User", "Administrator", "read"), ("User", "hashadmin", "read"),
		_hook_chain(205, "_verify_permissions", 198, "_run_post_checks", 11), 120,
	)
	assert (callsite["lineno"], callsite["function"]) == (205, "_verify_permissions")


def test_corpus_anchor_has_permission_write_3q1fsdhl5p():
	callsite, _ = _anchor(
		"has_permission", ("User", "Administrator", "write"), ("User", "hashadmin", "write"),
		_hook_chain(206, "_verify_permissions", 198, "_run_post_checks", 11), 120,
	)
	assert (callsite["lineno"], callsite["function"]) == (206, "_verify_permissions")


def test_corpus_anchor_cache_get_inside_erpnext_is_suppressed_3q1gf7r9lq():
	"""Corpus id 3q1gf7r9lq: the repeated Company cache lookup runs inside ERPNext's
	validate chain (the innermost frame outside frappe/ is ERPNext code), so after the fix
	the finding is suppressed as framework code, as this analyzer always intended; the
	pre-fix walk blamed the outer ugly_code override line instead. The ERPNext frames
	here are illustrative (the corpus stores only the blamed frame)."""
	stack = [
		{"filename": "apps/erpnext/erpnext/setup/doctype/company/company.py", "lineno": 900, "function": "get_default_currency"},
		{"filename": "apps/erpnext/erpnext/accounts/doctype/sales_invoice/sales_invoice.py", "lineno": 300, "function": "validate"},
		{"filename": "apps/ugly_code/ugly_code/customizations/sales_invoice_override.py", "lineno": 35, "function": "validate"},
		{"filename": "apps/frappe/frappe/model/document.py", "lineno": 500, "function": "run_method"},
		{"filename": "apps/frappe/frappe/app.py", "lineno": 120, "function": "application"},
	]
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"pyi_session": None,
		"sidecar": [
			_sidecar_entry("cache_get", "document_cache::Company::Aerele (Demo)", "93bf3d83c65a", caller_stack=stack)
			for _ in range(200)
		],
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = redundant_calls.analyze([recording], ctx)
	assert [f for f in result.findings if f["finding_type"] == "Redundant Call"] == []
	assert any("Frappe framework code" in w for w in ctx.warnings)


# ---------------------------------------------------------------------------
# P4 / P14: absolute bench paths, and the bucket's most frequent callsite
# ---------------------------------------------------------------------------

_BENCH = "/home/frappe/frappe-bench/apps"


def _single_finding(sidecar):
	recording = {"uuid": "rec-1", "calls": [], "pyi_session": None, "sidecar": sidecar}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	rc = [f for f in redundant_calls.analyze([recording], ctx).findings if f["finding_type"] == "Redundant Call"]
	assert len(rc) == 1, ctx.warnings
	return rc[0]


def test_absolute_bench_paths_keep_the_user_loop():
	"""P4: on /home/frappe/frappe-bench every absolute path held 'frappe/', so every
	frame was skipped and the finding was dropped as framework code."""
	stack = [  # innermost first, absolute co_filename paths, as capture records them
		{"filename": "/usr/lib/python3.14/contextlib.py", "lineno": 81, "function": "inner"},
		{"filename": f"{_BENCH}/frappe/frappe/model/document.py", "lineno": 900, "function": "get_doc"},
		{"filename": f"{_BENCH}/myapp/myapp/controllers/bulk.py", "lineno": 42, "function": "do_import"},
		{"filename": f"{_BENCH}/frappe/frappe/app.py", "lineno": 120, "function": "application"},
	]
	rc = _single_finding([
		_sidecar_entry("get_doc", ("Item", "X"), ("Item", "h"), caller_stack=stack) for _ in range(8)
	])
	assert json.loads(rc["technical_detail_json"])["callsite"] == {
		"filename": "myapp/myapp/controllers/bulk.py", "lineno": 42, "function": "do_import",
	}


def test_relative_and_server_script_frames_pass_through():
	stack = [{"filename": "<serverscript>: my_script", "lineno": 3, "function": "<module>"}, *_USER_CALLER_STACK]
	assert redundant_calls._apps_relative_stack(stack) == stack


def test_the_bucket_is_anchored_on_its_most_frequent_callsite():
	"""P14: two early calls from a non-loop line in action 0 no longer decide where the
	finding points; the eight from the loop line in action 1 do."""
	once = [{"filename": "apps/myapp/myapp/setup.py", "lineno": 5, "function": "prepare"}, *_USER_CALLER_STACK[1:]]
	loop = [{"filename": "apps/myapp/myapp/rows.py", "lineno": 24, "function": "check_rows"}, *_USER_CALLER_STACK[1:]]
	recordings = [
		{"uuid": "r0", "calls": [], "pyi_session": None, "sidecar": [
			_sidecar_entry("get_doc", ["User", "first"], ("User", "h"), caller_stack=once) for _ in range(2)
		]},
		{"uuid": "r1", "calls": [], "pyi_session": None, "sidecar": [
			_sidecar_entry("get_doc", ["User", "loop"], ("User", "h"), caller_stack=loop) for _ in range(8)
		]},
	]
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	rc = [f for f in redundant_calls.analyze(recordings, ctx).findings if f["finding_type"] == "Redundant Call"]
	assert len(rc) == 1
	detail = json.loads(rc[0]["technical_detail_json"])
	assert (detail["callsite"]["filename"], detail["callsite"]["lineno"]) == ("apps/myapp/myapp/rows.py", 24)
	assert rc[0]["action_ref"] == "1"
	assert detail["identifier_raw"] == ["User", "loop"]


def test_a_first_call_through_erpnext_no_longer_drops_the_finding():
	"""P14: the first occurrence went through ERPNext; the repeating loop is the user's."""
	via_erpnext = [{"filename": "apps/erpnext/erpnext/stock/utils.py", "lineno": 30, "function": "get_bin"}, *_USER_CALLER_STACK[1:]]
	sidecar = [_sidecar_entry("get_doc", ("Item", "X"), ("Item", "h"), caller_stack=via_erpnext)]
	sidecar += [_sidecar_entry("get_doc", ("Item", "X"), ("Item", "h")) for _ in range(7)]
	rc = _single_finding(sidecar)
	assert json.loads(rc["technical_detail_json"])["callsite"]["filename"] == "apps/myapp/controllers/bulk.py"


def test_the_stamp_constants_live_in_analyzers_base():
	from optimus import ai_grounding
	from optimus.analyzers import base

	assert ai_grounding.CALLSITE_WALK_KEY is base.CALLSITE_WALK_KEY
	assert ai_grounding.CALLSITE_WALK_FIXED is base.CALLSITE_WALK_FIXED == "outermost_first"
	src = Path(redundant_calls.__file__).read_text(encoding="utf-8")
	assert '"outermost_first"' not in src and "CALLSITE_WALK_FIXED" in src
