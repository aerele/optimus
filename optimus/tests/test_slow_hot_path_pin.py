# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""_pin_slow_hot_path_to_related_hot_line: a Slow Hot Path whose time is in DB
calls is pinned to the exact hot line inside its deepest user frame, taken from
the related N+1 / redundant / hot-line finding's persisted callsite (so the pin
survives re-render and needs no file re-read)."""

from optimus.renderer import finding_enrichment as fe

FILE = "ugly_code/python/common.py"


def _shp(*, chain, function="looped_validate", lineno=1, action_ref="0", impact=20000):
	return {
		"finding_type": "Slow Hot Path",
		"action_ref": action_ref,
		"estimated_impact_ms": impact,
		"technical_detail": {
			"callsite": {"filename": FILE, "lineno": lineno, "function": function},
			"drilldown_chain": chain,
		},
	}


def _hot(*, function, lineno, action_ref="0", ftype="N+1 Query", impact=65,
         filename=FILE, snippet="present"):
	cs = {"filename": filename, "lineno": lineno, "function": function, "_abs": "/abs/" + filename}
	if snippet == "present":
		cs["source_snippet"] = [{"lineno": lineno, "content": "        roles = db.sql(...)"}]
	elif snippet is not None:
		cs["source_snippet"] = snippet
	return {
		"finding_type": ftype,
		"action_ref": action_ref,
		"estimated_impact_ms": impact,
		"technical_detail": {"callsite": cs},
	}


def _cs(f):
	return f["technical_detail"]["callsite"]


def test_chain_leaf_pinned_to_related_query_line():
	shp = _shp(chain=[
		{"function": "_run_validations", "lineno": 14, "filename": FILE},
		{"function": "_check_user_exists", "lineno": 20, "filename": FILE},
	])
	n1 = _hot(function="_check_user_exists", lineno=25)
	fe._pin_slow_hot_path_to_related_hot_line([shp, n1], file_cache=None)
	cs = _cs(shp)
	assert cs["lineno"] == 25
	assert cs.get("self_time_hot_line_pinned") is True
	# Header shows the function the hot line lives in; banner keeps the origin.
	assert cs["function"] == "_check_user_exists"
	assert cs["original_wrapper"]["function"] == "looped_validate"
	assert cs["original_wrapper"]["lineno"] == 1
	# Phase-2 crosslink aims at the leaf (raw name).
	assert cs["phase2_lookup_function"] == "_check_user_exists"
	# Snippet is reused from the source finding (not re-read).
	assert cs["source_snippet"] == n1["technical_detail"]["callsite"]["source_snippet"]


def test_doctype_suffixed_function_still_matches():
	# _attach_action_context suffixes hook findings' function with " (DocType)"
	# BEFORE the pin, while the chain leaf stays raw. The pin must still match.
	shp = _shp(function="looped_validate (Sales Invoice)", chain=[
		{"function": "_check_user_exists", "lineno": 20, "filename": FILE},  # raw
	])
	n1 = _hot(function="_check_user_exists (Sales Invoice)", lineno=25)  # suffixed
	fe._pin_slow_hot_path_to_related_hot_line([shp, n1], file_cache=None)
	cs = _cs(shp)
	assert cs["lineno"] == 25
	assert cs.get("self_time_hot_line_pinned") is True
	# Phase-2 lookup strips the suffix (raw name for the profiler index).
	assert cs["phase2_lookup_function"] == "_check_user_exists"


def test_empty_chain_pinned_to_own_query_line():
	shp = _shp(chain=[], function="bg_recheck_users", lineno=4, action_ref="1")
	n1 = _hot(function="bg_recheck_users", lineno=6, action_ref="1")
	fe._pin_slow_hot_path_to_related_hot_line([shp, n1], file_cache=None)
	assert _cs(shp)["lineno"] == 6
	assert _cs(shp).get("self_time_hot_line_pinned") is True


def test_highest_impact_line_wins():
	shp = _shp(chain=[{"function": "_check_user_exists", "lineno": 20, "filename": FILE}])
	small = _hot(function="_check_user_exists", lineno=25, impact=10)
	big = _hot(function="_check_user_exists", lineno=26, impact=500, ftype="Redundant Call")
	fe._pin_slow_hot_path_to_related_hot_line([shp, small, big], file_cache=None)
	assert _cs(shp)["lineno"] == 26


def test_compute_leaf_without_related_finding_untouched():
	shp = _shp(chain=[{"function": "_square_then_mod", "lineno": 20, "filename": FILE}])
	other = _hot(function="_check_user_exists", lineno=25)  # different function
	fe._pin_slow_hot_path_to_related_hot_line([shp, other], file_cache=None)
	cs = _cs(shp)
	assert "self_time_hot_line_pinned" not in cs
	assert cs["lineno"] == 1  # unchanged


def test_wrong_function_hot_line_does_not_pin():
	# A present hot-line finding for a DIFFERENT function must not pin the card
	# (guards against dropping `function` from the lookup key).
	shp = _shp(chain=[{"function": "_check_user_exists", "lineno": 20, "filename": FILE}])
	n1 = _hot(function="some_other_fn", lineno=25)
	fe._pin_slow_hot_path_to_related_hot_line([shp, n1], file_cache=None)
	assert "self_time_hot_line_pinned" not in _cs(shp)


def test_empty_action_ref_not_matched():
	shp = _shp(chain=[{"function": "_check_user_exists", "lineno": 20, "filename": FILE}], action_ref="")
	n1 = _hot(function="_check_user_exists", lineno=25, action_ref="")
	fe._pin_slow_hot_path_to_related_hot_line([shp, n1], file_cache=None)
	assert "self_time_hot_line_pinned" not in _cs(shp)


def test_non_numeric_lineno_does_not_crash():
	shp = _shp(chain=[{"function": "_check_user_exists", "lineno": 20, "filename": FILE}])
	bad = _hot(function="_check_user_exists", lineno="n/a")
	good = _hot(function="_check_user_exists", lineno=25)
	fe._pin_slow_hot_path_to_related_hot_line([shp, bad, good], file_cache=None)
	assert _cs(shp)["lineno"] == 25  # bad one skipped, good one pins


def test_sql_red_flag_types_are_excluded():
	# Missing Index etc. have recorder-derived (non-persisted) callsites, so they
	# must not be a pin source or the pin would regress on re-render.
	shp = _shp(chain=[{"function": "_check_user_exists", "lineno": 20, "filename": FILE}])
	redflag = _hot(function="_check_user_exists", lineno=25, ftype="Missing Index")
	fe._pin_slow_hot_path_to_related_hot_line([shp, redflag], file_cache=None)
	assert "self_time_hot_line_pinned" not in _cs(shp)


def test_hot_finding_without_snippet_not_used():
	# Require a persisted snippet so the pin is a pure copy, never a file re-read.
	shp = _shp(chain=[{"function": "_check_user_exists", "lineno": 20, "filename": FILE}])
	n1 = _hot(function="_check_user_exists", lineno=25, snippet=None)
	fe._pin_slow_hot_path_to_related_hot_line([shp, n1], file_cache=None)
	assert "self_time_hot_line_pinned" not in _cs(shp)


def test_pin_clears_stale_server_script_link():
	# Origin is a Server Script wrapper (desk link), but the hot line is in a
	# regular file with no _abs. The pinned card must not keep the wrapper's stale
	# desk _abs / _link_kind, or clicking the hot line opens the wrong editor.
	shp = _shp(chain=[{"function": "_check_user_exists", "lineno": 20, "filename": FILE}])
	cs = _cs(shp)
	cs["_abs"] = "/app/server-script/my_script"
	cs["_link_kind"] = "desk"
	n1 = _hot(function="_check_user_exists", lineno=25)
	n1["technical_detail"]["callsite"].pop("_abs", None)  # regular-code hot line
	fe._pin_slow_hot_path_to_related_hot_line([shp, n1], file_cache=None)
	assert cs.get("self_time_hot_line_pinned") is True
	assert cs["lineno"] == 25
	assert "_abs" not in cs         # stale wrapper _abs cleared
	assert "_link_kind" not in cs   # stale desk link kind cleared


def test_pin_takes_hot_source_link():
	# When the hot source has its own _abs, it replaces the origin's.
	shp = _shp(chain=[{"function": "_check_user_exists", "lineno": 20, "filename": FILE}])
	_cs(shp)["_abs"] = "/app/server-script/old"
	n1 = _hot(function="_check_user_exists", lineno=25)  # _hot sets _abs = /abs/<FILE>
	fe._pin_slow_hot_path_to_related_hot_line([shp, n1], file_cache=None)
	assert _cs(shp)["_abs"] == "/abs/" + FILE


def test_pin_copies_snippet_list():
	# The pinned snippet is a distinct list so future mutation can't bleed back.
	shp = _shp(chain=[{"function": "_check_user_exists", "lineno": 20, "filename": FILE}])
	n1 = _hot(function="_check_user_exists", lineno=25)
	fe._pin_slow_hot_path_to_related_hot_line([shp, n1], file_cache=None)
	src = n1["technical_detail"]["callsite"]["source_snippet"]
	assert _cs(shp)["source_snippet"] == src
	assert _cs(shp)["source_snippet"] is not src


def test_other_finding_types_untouched():
	n1 = _hot(function="_check_user_exists", lineno=25)
	before = dict(_cs(n1))
	fe._pin_slow_hot_path_to_related_hot_line([n1], file_cache=None)
	assert _cs(n1) == before
