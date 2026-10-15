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
# Absolute bench paths, and the bucket's most frequent callsite
# ---------------------------------------------------------------------------

_BENCH = "/home/frappe/frappe-bench/apps"


def _single_finding(sidecar):
	recording = {"uuid": "rec-1", "calls": [], "pyi_session": None, "sidecar": sidecar}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	rc = [f for f in redundant_calls.analyze([recording], ctx).findings if f["finding_type"] == "Redundant Call"]
	assert len(rc) == 1, ctx.warnings
	return rc[0]


def test_absolute_bench_paths_keep_the_user_loop():
	"""On /home/frappe/frappe-bench every absolute path held 'frappe/', so every
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
	"""Two early calls from a non-loop line in action 0 no longer decide where the
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
	"""The first occurrence went through ERPNext; the repeating loop is the user's."""
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


# ---------------------------------------------------------------------------
# Anchored occurrences and path cuts
# ---------------------------------------------------------------------------


def _stack_at(path, lineno, function="fn"):
	return [{"filename": path, "lineno": lineno, "function": function}, *_USER_CALLER_STACK[1:]]


def _rec(uuid, entries):
	return {"uuid": uuid, "calls": [], "pyi_session": None, "sidecar": entries}


def _calls(n, stack, ident="x"):
	return [_sidecar_entry("get_doc", ["User", ident], ("User", "h"), caller_stack=stack) for _ in range(n)]


def _analyze_one(recordings):
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	rc = [f for f in redundant_calls.analyze(recordings, ctx).findings if f["finding_type"] == "Redundant Call"]
	assert len(rc) == 1, ctx.warnings
	return rc[0], json.loads(rc[0]["technical_detail_json"])["callsite"]


def test_action_ref_comes_from_the_anchored_occurrences_only():
	a = _stack_at("apps/myapp/myapp/a.py", 10)
	b = _stack_at("apps/myapp/myapp/b.py", 20)
	recs = [_rec("r0", _calls(8, b)), _rec("r1", _calls(5, a)), _rec("r2", _calls(5, a))]
	rc, cs = _analyze_one(recs)
	assert cs["filename"] == "apps/myapp/myapp/a.py"
	assert rc["action_ref"] == "1"


def test_a_tie_between_callsites_goes_to_the_first_seen_in_either_order():
	a = _stack_at("apps/myapp/myapp/a.py", 10)
	b = _stack_at("apps/myapp/myapp/b.py", 20)
	rc, cs = _analyze_one([_rec("r0", _calls(8, a)), _rec("r1", _calls(8, b))])
	assert cs["filename"] == "apps/myapp/myapp/a.py" and rc["action_ref"] == "0"
	rc, cs = _analyze_one([_rec("r0", _calls(8, b)), _rec("r1", _calls(8, a))])
	assert cs["filename"] == "apps/myapp/myapp/b.py" and rc["action_ref"] == "0"


def test_the_path_is_cut_at_the_last_apps_segment():
	stack = [{"filename": "/home/apps/frappe-bench/apps/myapp/myapp/x.py", "lineno": 1, "function": "f"}]
	assert redundant_calls._apps_relative_stack(stack)[0]["filename"] == "myapp/myapp/x.py"


def test_windows_paths_are_cut_too():
	stack = [{"filename": "C:\\bench\\apps\\myapp\\myapp\\x.py", "lineno": 1, "function": "f"}]
	assert redundant_calls._apps_relative_stack(stack)[0]["filename"] == "myapp/myapp/x.py"


def test_absolute_frames_outside_apps_are_user_code_unless_library_or_stdlib():
	keep = {"filename": "/srv/dev/myapp/myapp/x.py", "lineno": 1, "function": "f"}
	std = {"filename": "/usr/lib/python3.14/json/__init__.py", "lineno": 2, "function": "g"}
	site = {"filename": "/opt/venv/lib/python3.14/site-packages/requests/api.py", "lineno": 3, "function": "h"}
	dist = {"filename": "/usr/lib/python3/dist-packages/x.py", "lineno": 4, "function": "i"}
	assert redundant_calls._apps_relative_stack([std, site, dist, keep]) == [keep]
	bare_site = {"filename": "/opt/venv/site-packages/x.py", "lineno": 5, "function": "j"}
	bare_dist = {"filename": "/opt/dist-packages/x.py", "lineno": 6, "function": "k"}
	assert redundant_calls._apps_relative_stack([bare_site, bare_dist, keep]) == [keep]


def test_an_editable_installed_app_outside_the_bench_keeps_its_finding():
	stack = [
		{"filename": "/srv/dev/myapp/myapp/x.py", "lineno": 7, "function": "loop"},
		{"filename": f"{_BENCH}/frappe/frappe/app.py", "lineno": 120, "function": "application"},
	]
	_rc, cs = _analyze_one([_rec("r0", _calls(8, stack))])
	assert cs["filename"] == "/srv/dev/myapp/myapp/x.py"


def test_a_more_frequent_framework_callsite_does_not_suppress_a_user_loop():
	erp = _stack_at("apps/erpnext/erpnext/stock/utils.py", 30, "get_bin")
	user = _stack_at("apps/myapp/myapp/rows.py", 24, "check_rows")
	rc, cs = _analyze_one([_rec("r0", _calls(9, erp)), _rec("r1", _calls(8, user))])
	assert cs["filename"] == "apps/myapp/myapp/rows.py"
	assert rc["action_ref"] == "1"


# ---------------------------------------------------------------------------
# Optimus's own settings reads, anchored counts, bench-shaped cut
# ---------------------------------------------------------------------------

_HOME = "/home/frappe/frappe-bench"


def _bench(app_path, lineno, function):
	return {"filename": f"{_HOME}/apps/{app_path}", "lineno": lineno, "function": function}


# The tail every real frame chain below shares: Frappe's doc-event runner and the save.
_DOC_EVENT_TAIL = [
	_bench("frappe/frappe/model/document.py", 1563, "runner"),
	_bench("frappe/frappe/model/document.py", 1581, "composer"),
	_bench("frappe/frappe/model/document.py", 1184, "run_method"),
	_bench("frappe/frappe/model/document.py", 518, "save"),
	{"filename": "/usr/lib/python3.14/socketserver.py", "lineno": 697, "function": "process_request_thread"},
]

# The real shape of Optimus's per-query settings read (recordings snapshot on
# optimus.local): every recorded SQL call reads Optimus Settings from the cache
# inside the recorder hook, so the sidecar logs a cache_get whose innermost frames
# are Optimus's and whose first frame outside frappe/ and optimus/ is the user's
# query line (ugly_code common.py:25 here).
_OPTIMUS_SETTINGS_READ = [
	_bench("optimus/optimus/settings.py", 750, "get_config"),
	_bench("optimus/optimus/__init__.py", 200, "_read_extras"),
	_bench("optimus/optimus/__init__.py", 228, "_profiler_register"),
	_bench("frappe/frappe/recorder.py", 84, "record_sql"),
	_bench("ugly_code/ugly_code/python/common.py", 25, "_check_user_exists"),
	_bench("ugly_code/ugly_code/python/common.py", 15, "_run_validations"),
	_bench("ugly_code/ugly_code/python/common.py", 9, "looped_validate"),
	*_DOC_EVENT_TAIL,
]


def _real_genuine_sidecar():
	"""The three genuine loops of the same snapshot, with their real stacks."""
	user_doc = [
		_bench("ugly_code/ugly_code/python/common.py", 24, "_check_user_exists"),
		_bench("ugly_code/ugly_code/python/common.py", 15, "_run_validations"),
		_bench("ugly_code/ugly_code/python/common.py", 9, "looped_validate"),
		*_DOC_EVENT_TAIL,
	]

	def perm(line):
		return [
			_bench("frappe/frappe/__init__.py", 609, "has_permission"),
			_bench("ugly_code/ugly_code/python/common.py", line, "_verify_permissions"),
			_bench("ugly_code/ugly_code/python/common.py", 198, "_run_post_checks"),
			_bench("ugly_code/ugly_code/python/common.py", 11, "looped_validate"),
			*_DOC_EVENT_TAIL,
		]

	side = [_sidecar_entry("get_doc", ["User", "Administrator"], ["User", "e7d3e769f3f5"], user_doc) for _ in range(230)]
	for line, ptype in ((205, "read"), (206, "write")):
		side += [
			_sidecar_entry(
				"has_permission", ["User", "Administrator", ptype], ["User", "e7d3e769f3f5", ptype], perm(line)
			)
			for _ in range(120)
		]
	return side


def _findings(recordings):
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	rc = [f for f in redundant_calls.analyze(recordings, ctx).findings if f["finding_type"] == "Redundant Call"]
	return rc, ctx


def _callsite_of(finding):
	cs = json.loads(finding["technical_detail_json"])["callsite"]
	return cs["filename"], cs["lineno"]


def test_optimus_own_settings_reads_are_not_a_redundant_call():
	"""The snapshot gave 4 findings; the cache lookup of optimus_settings_cached
	(585 times, blamed on common.py:25) was Optimus's own read. Exactly it goes."""
	own = [
		_sidecar_entry("cache_get", "optimus_settings_cached", "605996ade876", _OPTIMUS_SETTINGS_READ)
		for _ in range(585)
	]
	genuine = _real_genuine_sidecar()
	rc, _ctx = _findings([_rec("r0", own + genuine)])
	assert sorted(_callsite_of(f) for f in rc) == [
		("ugly_code/ugly_code/python/common.py", 24),
		("ugly_code/ugly_code/python/common.py", 205),
		("ugly_code/ugly_code/python/common.py", 206),
	]
	assert not any("optimus_settings_cached" in f["technical_detail_json"] for f in rc)


def _own_read(*frames):
	return _sidecar_entry("cache_get", "k", "h", [*frames, *_OPTIMUS_SETTINGS_READ[3:]])


def test_an_optimus_read_behind_frappe_or_library_frames_is_still_optimus_own():
	"""The rule is the innermost frame outside frappe/ (library and stdlib frames are
	dropped first), not the innermost frame."""
	frappe_cache = _bench("frappe/frappe/utils/redis_wrapper.py", 90, "get_value")
	stdlib = {"filename": "/usr/lib/python3.14/functools.py", "lineno": 1, "function": "wrapper"}
	settings = _bench("optimus/optimus/settings.py", 750, "get_config")
	rc, _ctx = _findings([_rec("r0", [_own_read(frappe_cache, stdlib, settings) for _ in range(60)])])
	assert rc == []


def test_optimus_frames_in_other_path_shapes_are_optimus_own():
	for path in ("apps/optimus/optimus/settings.py", "optimus/settings.py", f"{_HOME}/apps/optimus/.wt/x/optimus/settings.py"):
		frame = {"filename": path, "lineno": 750, "function": "get_config"}
		rc, _ctx = _findings([_rec("r0", [_own_read(frame) for _ in range(60)])])
		assert rc == [], path


def test_a_user_loop_reached_through_an_optimus_frame_further_out_is_kept():
	"""Only the INNERMOST frame outside frappe/ decides: a user line inside, with an
	Optimus frame further out, is the user's call."""
	stack = [
		_bench("frappe/frappe/utils/redis_wrapper.py", 90, "get_value"),
		_bench("myapp/myapp/rows.py", 24, "check_rows"),
		_bench("optimus/optimus/hooks_callbacks.py", 280, "before_request"),
		*_DOC_EVENT_TAIL,
	]
	rc, _ctx = _findings([_rec("r0", [_sidecar_entry("cache_get", "k", "h", stack) for _ in range(60)])])
	assert [_callsite_of(f) for f in rc] == [("myapp/myapp/rows.py", 24)]


def test_optimus_own_is_decided_by_the_app_root_not_a_substring():
	"""The skip matches the app root (the prototype's /apps/optimus/ or optimus/ start),
	so an app merely named like Optimus is not skipped here."""
	assert redundant_calls._is_optimus_own([_bench("myoptimus/myoptimus/rows.py", 24, "f")]) is False
	assert redundant_calls._is_optimus_own([_bench("myapp/myapp/optimus/x.py", 24, "f")]) is False
	assert redundant_calls._is_optimus_own([_bench("frappe/frappe/x.py", 1, "f")]) is False
	assert redundant_calls._is_optimus_own([]) is False
	# A frame without a filename is skipped, as walk_callsite skips it.
	nameless = {"filename": "", "lineno": 1, "function": "?"}
	assert redundant_calls._is_optimus_own([nameless, _bench("optimus/optimus/settings.py", 750, "f")]) is True
	# Only frappe/ frames are passed over: an ERPNext frame inside an Optimus one decides.
	erp_inside = [_bench("erpnext/erpnext/x.py", 1, "f"), _bench("optimus/optimus/settings.py", 750, "f")]
	assert redundant_calls._is_optimus_own(erp_inside) is False


_ERP_VIA = [
	{"filename": "apps/erpnext/erpnext/accounts/party.py", "lineno": 610, "function": "get_party_account"},
	{"filename": "apps/erpnext/erpnext/controllers/accounts_controller.py", "lineno": 300, "function": "validate"},
	*_USER_CALLER_STACK[1:],
]
_USER_HOOK = [{"filename": "apps/myapp/myapp/hooks_impl.py", "lineno": 12, "function": "si_validate"}, *_USER_CALLER_STACK[1:]]


def _company(stack):
	return _sidecar_entry("get_doc", ["Company", "Acme"], ("Company", "h"), caller_stack=stack)


def test_one_user_call_in_an_erpnext_loop_is_no_finding_in_either_order():
	"""One user call anchored a "31 times" High
	finding counted over all 31 occurrences. The repetition is ERPNext's, so the bucket
	is suppressed as framework code, as it was before the non-framework vote."""
	erp = [_company(_ERP_VIA) for _ in range(30)]
	for side in (erp + [_company(_USER_HOOK)], [_company(_USER_HOOK)] + erp):
		rc, ctx = _findings([_rec("r0", side)])
		assert rc == []
		assert any("Frappe framework code" in w for w in ctx.warnings), ctx.warnings


def test_a_user_loop_beside_a_bigger_erpnext_loop_is_counted_alone():
	"""8 user-loop calls plus 9 ERPNext calls in one action is a finding counted 8,
	not 17: title, description, count, affected_count and severity all use the anchored
	callsite's occurrences."""
	user = _stack_at("apps/myapp/myapp/rows.py", 24, "check_rows")
	erp = _stack_at("apps/erpnext/erpnext/stock/utils.py", 30, "get_bin")
	for n_erp in (9, 30):
		side = [_company(erp) for _ in range(n_erp)] + [_company(user) for _ in range(8)]
		rc, ctx = _findings([_rec("r0", side)])
		assert len(rc) == 1, ctx.warnings
		f = rc[0]
		detail = json.loads(f["technical_detail_json"])
		assert f["title"].endswith("(8 times)")
		assert "**8 times**" in f["customer_description"]
		assert (detail["count"], f["affected_count"], detail["distinct_actions"]) == (8, 8, 1)
		assert f["severity"] == "Medium"  # 8 < 5 x 5; 38 occurrences would read High


def test_the_anchored_callsite_must_reach_the_threshold_within_one_action():
	"""The bucket reaches the threshold in action 0 (2 ERPNext + 3 user calls), but
	the anchored user line runs only 3 times per action: a per-request call, not a loop."""
	user = _stack_at("apps/myapp/myapp/rows.py", 24, "check_rows")
	erp = _stack_at("apps/erpnext/erpnext/stock/utils.py", 30, "get_bin")
	r0 = [_company(erp) for _ in range(2)] + [_company(user) for _ in range(3)]
	r1 = [_company(user) for _ in range(3)]
	rc, ctx = _findings([_rec("r0", r0), _rec("r1", r1)])
	assert rc == []
	assert any("summing across multiple requests" in w for w in ctx.warnings), ctx.warnings


def test_a_per_request_user_call_beside_an_erpnext_loop_is_suppressed_as_framework():
	"""The user line runs 3 times in each of two actions (no loop); the bucket's
	loop is ERPNext's 10 calls, so it is suppressed as framework code."""
	user = _stack_at("apps/myapp/myapp/rows.py", 24, "check_rows")
	erp = _stack_at("apps/erpnext/erpnext/stock/utils.py", 30, "get_bin")
	r0 = [_company(erp) for _ in range(10)] + [_company(user) for _ in range(3)]
	r1 = [_company(user) for _ in range(3)]
	rc, ctx = _findings([_rec("r0", r0), _rec("r1", r1)])
	assert rc == []
	assert any("Frappe framework code" in w for w in ctx.warnings), ctx.warnings
	assert not any("summing across multiple requests" in w for w in ctx.warnings), ctx.warnings


_HOOK = _stack_at("apps/myapp/myapp/hooks_impl.py", 12, "si_validate")
_LOOP = _stack_at("apps/myapp/myapp/rows.py", 24, "check_rows")
_PERM = {"fn_name": "has_permission", "identifier_raw": ["Sales Invoice", "SI-1", "read"], "identifier_safe": ("Sales Invoice", "h", "read")}


def _entry(stack, fn=None):
	return dict(_PERM, caller_stack=stack) if fn == "has_permission" else _company(stack)


def test_a_user_loop_is_found_beside_a_more_frequent_per_request_user_line():
	"""The anchor is the most frequent callsite that loops on its own (the
	threshold within one action), not the most frequent overall. The per-request hook
	line outnumbers the loop in every case, so it took the bucket and failed the recheck,
	and the loop was lost."""
	cases = [  # (fn, per-request hook calls per action, actions, loop calls, loop action)
		("get_doc", 2, 6, 8, 3),
		("get_doc", 4, 3, 6, 1),
		("has_permission", 9, 3, 20, 0),
		("get_doc", 2, 6, 5, 2),  # a loop of exactly the threshold
	]
	for fn, per, actions, n_loop, loop_at in cases:
		recs = [[_entry(_HOOK, fn) for _ in range(per)] for _ in range(actions)]
		recs[loop_at] += [_entry(_LOOP, fn) for _ in range(n_loop)]
		rc, ctx = _findings([_rec(f"r{i}", side) for i, side in enumerate(recs)])
		assert len(rc) == 1, (fn, ctx.warnings)
		detail = json.loads(rc[0]["technical_detail_json"])
		assert _callsite_of(rc[0]) == ("apps/myapp/myapp/rows.py", 24), fn
		assert rc[0]["title"].endswith(f"({n_loop} times)"), fn
		assert (detail["count"], detail["distinct_actions"]) == (n_loop, 1), fn  # the bucket spans every action
		assert rc[0]["action_ref"] == str(loop_at), fn


def test_the_user_loop_wins_over_a_per_request_user_line_and_a_bigger_erpnext_loop():
	"""Among user callsites the loop is chosen before the fallback, so a
	bigger ERPNext loop does not outvote it."""
	erp = _stack_at("apps/erpnext/erpnext/stock/utils.py", 30, "get_bin")
	recs = [[_company(_HOOK) for _ in range(2)] for _ in range(6)]
	recs[0] += [_company(erp) for _ in range(10)]
	recs[3] += [_company(_LOOP) for _ in range(8)]
	rc, ctx = _findings([_rec(f"r{i}", side) for i, side in enumerate(recs)])
	assert [_callsite_of(f) for f in rc] == [("apps/myapp/myapp/rows.py", 24)], ctx.warnings
	assert rc[0]["action_ref"] == "3"


def test_a_framework_loop_beside_a_more_frequent_per_request_user_line_is_framework():
	"""The user line runs twice in each of 6 actions (12, no loop); ERPNext
	loops 10 times in action 0. The fallback anchor is the looping callsite, so the
	bucket is suppressed as framework code, not reported as a per-request call."""
	erp = _stack_at("apps/erpnext/erpnext/stock/utils.py", 30, "get_bin")
	recs = [[_company(_HOOK) for _ in range(2)] for _ in range(6)]
	recs[0] += [_company(erp) for _ in range(10)]
	rc, ctx = _findings([_rec(f"r{i}", side) for i, side in enumerate(recs)])
	assert rc == []
	assert any("Frappe framework code" in w for w in ctx.warnings), ctx.warnings
	assert not any("summing across multiple requests" in w for w in ctx.warnings), ctx.warnings


def test_a_user_loop_of_exactly_the_threshold_beside_more_erpnext_calls_is_a_finding():
	erp = _stack_at("apps/erpnext/erpnext/stock/utils.py", 30, "get_bin")
	rc, ctx = _findings([_rec("r0", [_company(erp) for _ in range(9)] + [_company(_LOOP) for _ in range(5)])])
	assert len(rc) == 1, ctx.warnings
	assert _callsite_of(rc[0]) == ("apps/myapp/myapp/rows.py", 24)
	assert json.loads(rc[0]["technical_detail_json"])["count"] == 5
	assert rc[0]["title"].endswith("(5 times)")


def test_no_loop_at_any_callsite_is_not_blamed_on_framework_code():
	"""ERPNext once per request (20 requests) plus a user line 4 times in
	action 0 reach the threshold in action 0 only together. No line loops, so no
	framework loop is claimed: the most frequent callsite runs once per request, and the
	cross-request warning says so."""
	erp = _stack_at("apps/erpnext/erpnext/stock/utils.py", 30, "get_bin")
	for requests in (20, 5):  # 5: the ERPNext line's calls exactly reach the threshold
		recs = [[_company(erp)] for _ in range(requests)]
		recs[0] += [_company(_HOOK) for _ in range(4)]
		rc, ctx = _findings([_rec(f"r{i}", side) for i, side in enumerate(recs)])
		assert rc == []
		assert not any("Frappe framework code" in w for w in ctx.warnings), ctx.warnings
		assert any("summing across multiple requests" in w for w in ctx.warnings), (requests, ctx.warnings)


def test_two_user_lines_that_reach_the_threshold_only_together_are_no_finding():
	"""3 calls from each of two user lines in one action. Neither line is a loop of
	5; the drop is silent, since nothing was summed across requests."""
	a = _stack_at("apps/myapp/myapp/a.py", 10)
	b = _stack_at("apps/myapp/myapp/b.py", 20)
	rc, ctx = _findings([_rec("r0", _calls(3, a) + _calls(3, b))])
	assert rc == []
	assert not any("summing across multiple requests" in w for w in ctx.warnings), ctx.warnings
	assert not any("Frappe framework code" in w for w in ctx.warnings), ctx.warnings


def test_a_bucket_below_the_threshold_is_dropped_without_a_warning():
	"""The bucket checks run before the anchored ones: 4 calls in one action are
	under the threshold of 5, not a cross-request spread."""
	rc, ctx = _findings([_rec("r0", _calls(4, _USER_CALLER_STACK))])
	assert rc == [] and ctx.warnings == []


def test_two_per_request_lines_are_reported_as_cross_request_spread():
	"""The bucket's spread check: two user lines once each in 3 requests (6 calls, 2 per
	action) are per-request calls, though neither line alone reaches the threshold."""
	a = _stack_at("apps/myapp/myapp/a.py", 10)
	b = _stack_at("apps/myapp/myapp/b.py", 20)
	rc, ctx = _findings([_rec(f"r{i}", _calls(1, a) + _calls(1, b)) for i in range(3)])
	assert rc == []
	assert any("summing across multiple requests" in w for w in ctx.warnings), ctx.warnings


def test_a_per_request_framework_call_is_reported_as_cross_request_spread():
	"""The bucket's spread check runs before the walk: one werkzeug-side lookup per
	request is a per-request call (the v0.5.2 production case), whatever its callsite."""
	werkzeug = [
		{"filename": "/opt/venv/lib/python3.14/site-packages/werkzeug/serving.py", "lineno": 370, "function": "run_wsgi"},
		*_USER_CALLER_STACK[1:],
	]
	recs = [_rec(f"r{i}", [_sidecar_entry("cache_get", "k", "h", werkzeug)]) for i in range(60)]
	rc, ctx = _findings(recs)
	assert rc == []
	assert any("summing across multiple requests" in w for w in ctx.warnings), ctx.warnings
	assert not any("Frappe framework code" in w for w in ctx.warnings), ctx.warnings


def test_the_cut_keeps_an_inner_apps_package_of_the_app():
	"""The last /apps/ was an `apps` package inside the app; the bench's apps dir is
	the /apps/ followed by <app>/<app>/."""
	stack = [{"filename": "/home/f/bench/apps/myapp/myapp/apps/x.py", "lineno": 1, "function": "f"}]
	assert redundant_calls._apps_relative_stack(stack)[0]["filename"] == "myapp/myapp/apps/x.py"
	win = [{"filename": "C:\\bench\\apps\\myapp\\myapp\\apps\\x.py", "lineno": 1, "function": "f"}]
	assert redundant_calls._apps_relative_stack(win)[0]["filename"] == "myapp/myapp/apps/x.py"


def test_the_last_bench_shaped_apps_dir_wins():
	"""Of two /apps/<a>/<a>/ candidates the last is kept, as the plain rule keeps the
	last /apps/; an app named apps still resolves."""
	cases = {
		"/home/apps/x/x/bench/apps/myapp/myapp/y.py": "myapp/myapp/y.py",
		"/srv/bench/apps/apps/apps/y.py": "apps/apps/y.py",
		"/h/apps/apps/myapp/myapp/y.py": "myapp/myapp/y.py",  # a bench dir named apps
		"/srv/bench/apps/myapp/myapp/apps/sub/subpkg/x.py": "myapp/myapp/apps/sub/subpkg/x.py",
		"/srv/bench/apps/myapp/myapp/apps/foo/bar/bar/x.py": "myapp/myapp/apps/foo/bar/bar/x.py",
		"/home/apps/bench/apps/myapp/x.py": "myapp/x.py",
		"/srv/bench/apps/x.py": "x.py",
	}
	for path, cut in cases.items():
		stack = [{"filename": path, "lineno": 1, "function": "f"}]
		assert redundant_calls._apps_relative_stack(stack)[0]["filename"] == cut, path


def test_an_inner_apps_package_keeps_its_finding():
	"""The cut to x.py made the loop's app root `x.py`; it is myapp's code."""
	stack = [
		{"filename": f"{_HOME}/apps/myapp/myapp/apps/x.py", "lineno": 7, "function": "loop"},
		*_DOC_EVENT_TAIL,
	]
	_rc, cs = _analyze_one([_rec("r0", _calls(8, stack))])
	assert cs["filename"] == "myapp/myapp/apps/x.py"
