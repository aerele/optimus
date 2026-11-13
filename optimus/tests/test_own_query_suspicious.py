# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""A fail-safe own-query hide: an Optimus frame with a developer's code outside it that is not a known
Optimus entry point may be a wrapper forwarding the user's query, so the query is kept and flagged."""

import pytest

from optimus.analyzers.base import (
	own_query_verdict,
	strip_own_calls,
	strip_profiler_own_calls,
)


def F(filename, function="f", lineno=10):
	return {"filename": filename, "lineno": lineno, "function": function}


USER = F("myapp/myapp/api.py", "attach_pdf", 12)
APP = F("frappe/frappe/app.py", "application", 1)
RUN_METHOD = F("frappe/frappe/model/document.py", "run_method", 1100)
GET_DOC = F("frappe/frappe/__init__.py", "get_doc", 90)
SQL = F("frappe/frappe/database/database.py", "sql", 200)
UNREGISTERED = F("optimus/optimus/new_feature.py", "forward_get_doc", 30)
AI_FIX = F("optimus/optimus/ai_fix.py", "run_suggestion", 400)
PROTECT_FILE = F("optimus/optimus/managed.py", "protect_file", 234)
OPTIMUS_HELPER = F("optimus/optimus/settings.py", "get_config", 40)
ERROR_LOG_HOOK = F("optimus/optimus/error_log_mask.py", "mask_error_log", 50)
BEFORE_INSERT_WRAP = F("optimus/optimus/managed.py", "before_insert", 157)
WRAPPED = F("optimus/optimus/capture.py", "wrapped", 216)

INSTALLED = frozenset({"frappe", "erpnext", "optimus", "myapp"})
LISTS = {"tracked_apps": (), "installed_apps": INSTALLED}


def _verdict(stack, **lists):
	return own_query_verdict(stack, **(lists or LISTS))


class TestUnregisteredWrapperIsKept:
	def test_a_user_query_through_an_unregistered_optimus_wrapper_is_suspicious(self):
		frame, suspicious = _verdict([USER, RUN_METHOD, UNREGISTERED, GET_DOC, SQL])
		assert frame is UNREGISTERED and suspicious

	def test_strip_keeps_the_suspicious_call_and_counts_it(self):
		call = {"query": "select user", "stack": [USER, UNREGISTERED, GET_DOC, SQL]}
		rec = {"calls": [call]}
		result = strip_own_calls(rec, **LISTS)
		assert rec["calls"] == [call]
		assert (result.dropped, result.suspicious) == (0, 1)
		assert result.suspicious_calls == [call]

	def test_the_developer_frame_may_be_any_distance_outside(self):
		deep = [F(f"frappe/frappe/m{i}.py", "f") for i in range(20)]
		assert _verdict([USER, *deep, OPTIMUS_HELPER, UNREGISTERED, SQL])[1]

	def test_other_optimus_frames_outside_the_deciding_frame_do_not_hide_the_developer_frame(self):
		assert _verdict([USER, UNREGISTERED, OPTIMUS_HELPER, SQL])[1]

	def test_a_tracked_app_names_the_developer_code_in_inclusion_mode(self):
		assert _verdict([USER, UNREGISTERED, SQL], tracked_apps=("myapp",), installed_apps=None)[1]

	def test_a_hook_entry_beyond_the_developer_frame_does_not_clear_it(self):
		assert _verdict([PROTECT_FILE, USER, UNREGISTERED, SQL])[1]


class TestGenuineOwnIssuersStayHidden:
	@pytest.mark.parametrize("stack", [
		[APP, AI_FIX, SQL],
		[APP, AI_FIX, OPTIMUS_HELPER, GET_DOC, SQL],
		[F("/env/lib/python3.14/site-packages/rq/worker.py", "work"),
			F("frappe/frappe/utils/background_jobs.py", "execute_job"),
			F("optimus/optimus/ai_jobs.py", "run_job", 90), SQL],
		[F("erpnext/erpnext/x.py", "y"), UNREGISTERED, SQL],
	], ids=["ai_fix direct", "ai_fix helper", "rq job", "stock app outside"])
	def test_only_framework_frames_outside_is_hidden(self, stack):
		frame, suspicious = _verdict(stack)
		assert frame is not None and not suspicious

	@pytest.mark.parametrize("stack", [
		[USER, RUN_METHOD, PROTECT_FILE, F("frappe/frappe/__init__.py", "get_all", 2000), SQL],
		[USER, RUN_METHOD, PROTECT_FILE, OPTIMUS_HELPER, GET_DOC, SQL],
		[USER, F("frappe/frappe/permissions.py", "has_permission", 60),
			F("optimus/optimus/permissions.py", "file_has_permission", 87), SQL],
		[USER, F("frappe/frappe/recorder.py", "record_sql", 70),
			F("optimus/optimus/__init__.py", "_profiler_register", 360), OPTIMUS_HELPER, GET_DOC, SQL],
	], ids=["protect_file", "protect_file helper", "file permission", "recorder register"])
	def test_a_registered_hook_entry_point_inside_user_code_is_hidden(self, stack):
		frame, suspicious = _verdict(stack)
		assert frame is not None and not suspicious

	def test_the_error_log_hook_inside_user_code_is_hidden(self):
		assert _verdict([USER, ERROR_LOG_HOOK, SQL]) == (ERROR_LOG_HOOK, False)

	def test_a_helper_below_the_error_log_hook_is_hidden(self):
		helper = F("optimus/optimus/ai_fix.py", "_current_key_or_empty", 12)
		assert _verdict([USER, ERROR_LOG_HOOK, helper, SQL]) == (helper, False)

	@pytest.mark.parametrize("stack", [
		[USER, RUN_METHOD, BEFORE_INSERT_WRAP, F("frappe/frappe/__init__.py", "get_installed_apps", 1500), SQL],
		[USER, WRAPPED, OPTIMUS_HELPER, F("frappe/frappe/model/utils/__init__.py", "wrapper", 226), SQL],
	], ids=["before_insert wrapper own work", "capture wrapper own work"])
	def test_a_pass_through_wrappers_own_work_is_hidden(self, stack):
		frame, suspicious = _verdict(stack)
		assert frame is not None and not suspicious

	def test_a_stack_that_is_not_own_has_no_verdict(self):
		assert _verdict([USER, RUN_METHOD, WRAPPED, GET_DOC, SQL]) == (None, False)
		assert _verdict(None) == (None, False)


class TestOffBenchDegradesToTheOldRule:
	def test_without_app_lists_nothing_is_newly_kept(self):
		stack = [USER, UNREGISTERED, GET_DOC, SQL]
		assert own_query_verdict(stack) == (UNREGISTERED, False)
		rec = {"calls": [{"query": "q", "stack": stack}]}
		assert strip_profiler_own_calls(rec) == 1 and rec["calls"] == []

	def test_empty_lists_behave_like_no_lists(self):
		stack = [USER, UNREGISTERED, SQL]
		assert own_query_verdict(stack, tracked_apps=(), installed_apps=None) == (UNREGISTERED, False)
		assert own_query_verdict(stack, tracked_apps=(), installed_apps=frozenset()) == (UNREGISTERED, False)


class TestStripResult:
	def test_dropped_and_suspicious_are_counted_apart_across_one_recording(self):
		own = {"query": "own", "stack": [APP, AI_FIX, SQL]}
		hook = {"query": "hook", "stack": [USER, PROTECT_FILE, SQL]}
		wrapped = {"query": "wrapped", "stack": [USER, UNREGISTERED, SQL]}
		users = {"query": "user", "stack": [USER, RUN_METHOD, WRAPPED, GET_DOC, SQL]}
		rec = {"calls": [own, hook, wrapped, users, "junk", None]}
		result = strip_own_calls(rec, **LISTS)
		assert (result.dropped, result.suspicious) == (2, 1)
		assert result.dropped_calls == [own, hook]
		assert rec["calls"] == [wrapped, users, "junk", None]

	def test_strip_profiler_own_calls_returns_only_the_dropped_count(self):
		rec = {"calls": [{"query": "w", "stack": [USER, UNREGISTERED, SQL]}, {"query": "o", "stack": [APP, AI_FIX, SQL]}]}
		assert strip_profiler_own_calls(rec, **LISTS) == 1
		assert [c["query"] for c in rec["calls"]] == ["w"]

	def test_a_recording_without_calls_is_a_no_op(self):
		result = strip_own_calls({}, **LISTS)
		assert (result.dropped, result.suspicious) == (0, 0)


class TestKeptCallStaysTheUsersEverywhere:
	"""The strip is not the only reader of a call: the analyzers and the per-action table run the
	own-query rule again, so a kept call must read as the user's there too."""

	def _kept(self):
		from optimus.analyzers.base import KeptStack

		call = {
			"query": "select user", "normalized_query": "select user", "duration": 50,
			"stack": [USER, UNREGISTERED, GET_DOC, SQL],
		}
		rec = {"uuid": "r1", "calls": [call]}
		result = strip_own_calls(rec, **LISTS)
		assert result.suspicious == 1 and isinstance(call["stack"], KeptStack)
		return rec, call

	def test_the_rule_and_the_callsite_walk_see_the_user(self):
		from optimus.analyzers.base import is_profiler_own_query, walk_callsite

		_rec, call = self._kept()
		assert not is_profiler_own_query(call["stack"])
		assert walk_callsite(call["stack"]) == USER

	def test_a_second_strip_keeps_it_and_does_not_count_it_again(self):
		rec, call = self._kept()
		result = strip_own_calls(rec, **LISTS)
		assert (result.dropped, result.suspicious) == (0, 0) and rec["calls"] == [call]

	def test_top_queries_lists_the_kept_query_with_its_user_callsite(self, empty_context):
		from optimus.analyzers import top_queries

		rec, _call = self._kept()
		top = top_queries.analyze([rec], empty_context).aggregate["top_queries"]
		assert [q["normalized_query"] for q in top] == ["select user"]
		assert top[0]["callsite"] == "myapp/myapp/api.py:12"

	def test_the_per_action_table_keeps_it(self):
		from optimus.renderer.queries_per_action import _is_own_query

		_rec, call = self._kept()
		assert not _is_own_query(call["stack"])

	def test_the_stack_keeps_its_frames_and_json_form(self):
		import json

		_rec, call = self._kept()
		assert list(call["stack"]) == [USER, UNREGISTERED, GET_DOC, SQL]
		assert json.loads(json.dumps(call["stack"])) == [USER, UNREGISTERED, GET_DOC, SQL]


class TestRedundantCallsSidecarIsFailSafeToo:
	def _sidecar(self, caller_stack):
		return {"fn_name": "get_doc", "identifier_safe": ("Note", "x"), "identifier_raw": ("Note", "x"),
			"caller_stack": caller_stack}

	def test_a_suspicious_sidecar_stack_is_not_own(self):
		from optimus.analyzers.redundant_calls import _is_optimus_own

		inner_first = [GET_DOC, UNREGISTERED, USER]
		assert _is_optimus_own(inner_first)  # no lists: the old rule
		assert not _is_optimus_own(inner_first, (), INSTALLED)

	def test_a_genuine_own_sidecar_stack_is_still_own(self):
		from optimus.analyzers.redundant_calls import _is_optimus_own

		assert _is_optimus_own([OPTIMUS_HELPER, AI_FIX, APP], (), INSTALLED)
		assert _is_optimus_own([OPTIMUS_HELPER, PROTECT_FILE, USER], (), INSTALLED)

	def test_a_looped_user_lookup_through_an_unregistered_wrapper_still_reports(self, empty_context, monkeypatch):
		from optimus.analyzers import redundant_calls
		from optimus.analyzers.redundant_calls import analyze as redundant_analyze

		monkeypatch.setattr(redundant_calls, "installed_apps_allowlist", lambda: INSTALLED)
		stack = [
			F("/b/apps/frappe/frappe/__init__.py", "get_doc", 90),
			F("/b/apps/optimus/optimus/new_feature.py", "forward_get_doc", 30),
			F("/b/apps/myapp/myapp/api.py", "loop", 10),
		]
		recording = {"sidecar": [self._sidecar(stack) for _ in range(30)]}
		assert len(redundant_analyze([recording], empty_context).findings) == 1
