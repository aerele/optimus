# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""One own-query rule: Optimus's wrappers pass through to Frappe, any other Optimus frame decides."""

import pytest

from optimus.analyzers.base import (
	is_profiler_own_query,
	strip_profiler_own_calls,
	walk_callsite,
)
from optimus.analyzers.redundant_calls import analyze as redundant_analyze


def F(filename, function="f", lineno=10):
	return {"filename": filename, "lineno": lineno, "function": function}


USER = F("myapp/myapp/api.py", "attach_pdf", 12)
RUN_METHOD = F("frappe/frappe/model/document.py", "run_method", 1100)
SQL = F("frappe/frappe/database/database.py", "sql", 200)
WRAPPED = F("optimus/optimus/capture.py", "wrapped", 216)
GET_DOC = F("frappe/frappe/__init__.py", "get_doc", 90)
BEFORE_INSERT_WRAP = F("optimus/optimus/managed.py", "before_insert", 157)
FILE_BEFORE_INSERT = F("frappe/frappe/core/doctype/file/file.py", "before_insert", 100)
ENQUEUE_WRAP = F("optimus/optimus/__init__.py", "_profiler_enqueue", 198)
BG_ENQUEUE = F("frappe/frappe/utils/background_jobs.py", "enqueue", 120)


def _own(stack):
	return is_profiler_own_query(stack)


class TestPassThroughToFrappe:
	def test_frappe_only_get_doc_through_wrapper_is_not_own(self):
		stack = [
			F("frappe/frappe/app.py", "application"),
			F("frappe/frappe/desk/form/load.py", "getdoc"),
			WRAPPED,
			GET_DOC,
			F("frappe/frappe/model/document.py", "load_from_db"),
			SQL,
		]
		assert not _own(stack)
		assert walk_callsite(stack)["filename"] == SQL["filename"]

	def test_frappe_file_insert_through_before_insert_is_not_own(self):
		stack = [
			F("frappe/frappe/handler.py", "upload_file"),
			RUN_METHOD,
			BEFORE_INSERT_WRAP,
			FILE_BEFORE_INSERT,
			SQL,
		]
		assert not _own(stack)
		assert walk_callsite(stack)["filename"] == SQL["filename"]

	def test_frappe_enqueue_through_profiler_enqueue_is_not_own(self):
		stack = [F("frappe/frappe/handler.py", "handle"), ENQUEUE_WRAP, BG_ENQUEUE, SQL]
		assert not _own(stack)
		assert walk_callsite(stack)["filename"] == SQL["filename"]

	@pytest.mark.parametrize("wrapper,target", [
		(WRAPPED, GET_DOC),
		(BEFORE_INSERT_WRAP, FILE_BEFORE_INSERT),
		(ENQUEUE_WRAP, BG_ENQUEUE),
	])
	def test_user_query_through_a_wrapper_stays_the_users(self, wrapper, target):
		stack = [USER, RUN_METHOD, wrapper, target, SQL]
		assert not _own(stack)
		assert walk_callsite(stack) == USER

	def test_wrapper_over_a_user_function_of_the_same_name_is_not_a_target(self):
		# the next-inner frame is the user's get_doc, not Frappe's: the user frame decides
		user_get_doc = F("myapp/myapp/api.py", "get_doc", 5)
		assert not _own([WRAPPED, user_get_doc, SQL])


class TestRealFrappeFrameShapes:
	"""Frames as Frappe v16 really records them: ``get_doc`` is a ``simple_singledispatch``
	``wrapper`` and ``frappe.permissions.has_permission`` a ``print_has_permission_check_logs``
	``inner``, so the frame inside Optimus's wrapper is not named after the wrapped function."""

	GET_DOC_STACK = [
		USER,
		WRAPPED,
		F("frappe/frappe/model/utils/__init__.py", "wrapper", 226),
		F("frappe/frappe/model/document.py", "get_doc_str", 356),
		SQL,
	]
	HAS_PERMISSION_STACK = [
		USER,
		F("frappe/frappe/__init__.py", "has_permission", 800),
		WRAPPED,
		F("frappe/frappe/permissions.py", "inner", 46),
		F("frappe/frappe/permissions.py", "has_permission", 120),
		SQL,
	]

	@pytest.mark.parametrize("stack", [GET_DOC_STACK, HAS_PERMISSION_STACK])
	def test_a_users_query_through_the_real_frappe_frames_is_not_own(self, stack):
		assert not _own(stack)
		assert walk_callsite(stack) == USER

	def test_a_frappe_only_flow_through_the_real_get_doc_frames_is_not_own(self):
		stack = [F("frappe/frappe/app.py", "application"), *self.GET_DOC_STACK[1:]]
		assert not _own(stack)
		assert walk_callsite(stack)["filename"] == SQL["filename"]

	def test_the_real_frames_survive_strip_top_queries_and_per_action(self, empty_context):
		from optimus.analyzers import top_queries
		from optimus.renderer.queries_per_action import _is_own_query

		calls = [{"query": "select a", "normalized_query": "select a", "duration": 50, "stack": stack}
			for stack in (self.GET_DOC_STACK, self.HAS_PERMISSION_STACK)]
		assert not any(_is_own_query(c["stack"]) for c in calls)
		assert strip_profiler_own_calls({"calls": list(calls)}) == 0
		top = top_queries.analyze([{"calls": calls}], empty_context).aggregate["top_queries"]
		assert [q["normalized_query"] for q in top] == ["select a", "select a"]

	def test_the_wrappers_own_work_between_it_and_its_frappe_frame_is_still_own(self):
		own = F("optimus/optimus/settings.py", "get_config", 40)
		assert _own([USER, WRAPPED, own, self.GET_DOC_STACK[2], SQL])

	def test_a_wrapper_reached_by_a_user_frame_below_it_is_the_users(self):
		assert not _own([USER, WRAPPED, F("myapp/myapp/hooks.py", "wrapper", 3), SQL])


class TestWrapperOwnWork:
	def test_get_installed_apps_miss_inside_before_insert_is_own(self):
		stack = [
			USER,
			RUN_METHOD,
			BEFORE_INSERT_WRAP,
			F("frappe/frappe/__init__.py", "get_installed_apps", 1500),
			F("frappe/frappe/defaults.py", "get_defaults_for"),
			SQL,
		]
		assert _own(stack)
		assert walk_callsite(stack) is None

	def test_a_wrapper_frame_nearest_the_query_is_own(self):
		assert _own([USER, WRAPPED])

	def test_a_pass_through_name_in_another_optimus_file_is_own(self):
		# the table is keyed by file suffix and function, never the function alone
		assert _own([USER, F("optimus/optimus/analyze.py", "wrapped"), SQL])

	def test_wrapper_name_and_frappe_target_in_another_optimus_file_is_own(self):
		assert _own([USER, F("optimus/optimus/analyze.py", "wrapped"), GET_DOC, SQL])

	def test_stray_wrapper_name_in_a_user_app_stays_the_users(self):
		assert not _own([USER, F("myapp/myapp/capture.py", "wrapped"), SQL])


class TestOwnHooksInsideUserFlows:
	def test_protect_file_get_all_inside_a_user_flow_is_own(self):
		stack = [
			USER,
			F("frappe/frappe/model/document.py", "insert", 300),
			RUN_METHOD,
			F("optimus/optimus/managed.py", "protect_file", 234),
			F("frappe/frappe/__init__.py", "get_all", 2000),
			SQL,
		]
		assert _own(stack)
		assert walk_callsite(stack) is None

	def test_settings_read_under_the_recorder_register_wrapper_is_own(self):
		stack = [
			USER,
			F("frappe/frappe/recorder.py", "record_sql", 70),
			F("optimus/optimus/__init__.py", "_profiler_register", 360),
			F("optimus/optimus/settings.py", "get_config", 40),
			F("frappe/frappe/__init__.py", "get_doc", 90),
			SQL,
		]
		assert _own(stack)

	def test_file_permission_hook_query_inside_a_user_file_read_is_own(self):
		stack = [
			USER,
			F("frappe/frappe/permissions.py", "has_permission", 60),
			F("optimus/optimus/permissions.py", "file_has_permission", 87),
			F("frappe/frappe/__init__.py", "get_all", 2000),
			SQL,
		]
		assert _own(stack)
		assert walk_callsite(stack) is None

	def test_error_log_hook_read_inside_log_error_is_own(self):
		stack = [USER, F("optimus/optimus/error_log_mask.py", "hook", 50), SQL]
		assert _own(stack)

	def test_profiler_snapshot_with_only_framework_frames_outside_is_own(self):
		stack = [F("frappe/frappe/app.py", "application"), F("optimus/optimus/infra_capture.py", "snapshot"), SQL]
		assert _own(stack)


class TestUndecided:
	def test_a_stack_of_only_frappe_frames_is_not_own(self):
		assert not _own([F("frappe/frappe/app.py", "application"), SQL])

	def test_empty_and_malformed_stacks_are_not_own(self):
		assert not _own(None)
		assert not _own([])
		assert not _own(["junk", {"filename": ""}])


class TestMalformedFunction:
	@pytest.mark.parametrize("function", [[1], {}, None, 5, "missing"])
	def test_unhashable_or_missing_function_does_not_raise(self, function):
		frame = {"filename": "optimus/optimus/capture.py", "lineno": 1}
		if function != "missing":
			frame["function"] = function
		stack = [USER, frame, GET_DOC, SQL]
		# the frame is no pass-through: the Optimus frame decides own
		assert _own(stack)
		rec = {"calls": [{"query": "select 1", "stack": stack, "duration": 1}]}
		assert strip_profiler_own_calls(rec) == 1
		assert walk_callsite(stack) is None

	@pytest.mark.parametrize("function", [[1], {}, None, 5, "missing"])
	def test_malformed_function_on_the_frappe_frame_inside_a_wrapper_does_not_raise(self, function):
		inner = {"filename": "frappe/frappe/__init__.py", "lineno": 90}
		if function != "missing":
			inner["function"] = function
		# the capture wrapper only calls its original: any Frappe frame inside it is the caller's
		stack = [USER, WRAPPED, inner, SQL]
		assert not _own(stack)
		assert walk_callsite(stack) == USER

	@pytest.mark.parametrize("function", [[1], {}, None, 5, "missing"])
	def test_malformed_function_on_the_frappe_frame_inside_a_named_wrapper_is_own(self, function):
		# the enqueue and before_insert wrappers name their targets: an unnamed frame is not one
		inner = {"filename": "frappe/frappe/__init__.py", "lineno": 90}
		if function != "missing":
			inner["function"] = function
		stack = [USER, ENQUEUE_WRAP, inner, SQL]
		assert _own(stack)
		assert walk_callsite(stack) is None

	def test_analyze_completes_with_a_malformed_function_in_a_stack(self, empty_context):
		from optimus.analyzers import top_queries

		frame = {"filename": "optimus/optimus/capture.py", "lineno": 1, "function": [1]}
		rec = {"calls": [{"query": "select 1", "normalized_query": "select ?", "duration": 1,
			"stack": [USER, frame, SQL]}]}
		top_queries.analyze([rec], empty_context)


class TestSharedByEverySurface:
	def test_strip_keeps_frappe_only_wrapper_queries_and_drops_own_ones(self):
		kept = {"query": "select a", "stack": [F("frappe/frappe/handler.py", "handle"), WRAPPED, GET_DOC, SQL]}
		own = {"query": "select b", "stack": [USER, F("optimus/optimus/managed.py", "protect_file"), SQL]}
		rec = {"calls": [kept, own]}
		assert strip_profiler_own_calls(rec) == 1
		assert rec["calls"] == [kept]

	def test_top_queries_drops_an_own_hook_query_inside_a_user_flow_and_keeps_the_users(self, empty_context):
		from optimus.analyzers import top_queries

		own = {"query": "select own", "normalized_query": "select own", "duration": 90,
			"stack": [USER, F("optimus/optimus/managed.py", "protect_file"), SQL]}
		users = {"query": "select a", "normalized_query": "select a", "duration": 50,
			"stack": [USER, RUN_METHOD, WRAPPED, GET_DOC, SQL]}
		top = top_queries.analyze([{"calls": [own, users]}], empty_context).aggregate["top_queries"]
		assert [q["normalized_query"] for q in top] == ["select a"]

	def test_queries_per_action_drops_an_own_hook_query_inside_a_user_flow(self):
		from optimus.renderer.queries_per_action import _is_own_query

		stack = [USER, F("optimus/optimus/managed.py", "protect_file"), SQL]
		assert _is_own_query(stack)
		assert not _is_own_query([USER, WRAPPED, GET_DOC, SQL])

	def test_predicate_visits_each_frame_at_most_once(self):
		deep = [F(f"frappe/frappe/m{i}.py", "f") for i in range(2000)]
		stack = _CountingStack([USER, *deep, WRAPPED, GET_DOC, SQL])
		assert not _own(stack)
		assert stack.reads == len(stack)


class _CountingStack:
	"""A frame sequence that counts how many frames the predicate reads."""

	def __init__(self, frames):
		self.frames = frames
		self.reads = 0

	def __len__(self):
		return len(self.frames)

	def __getitem__(self, index):
		self.reads += 1
		return self.frames[index]


def _sidecar(caller_stack):
	"""Sidecar caller stacks are innermost-first and carry absolute bench paths."""
	return {"fn_name": "get_doc", "identifier_safe": ("Note", "x"), "identifier_raw": ("Note", "x"),
		"caller_stack": caller_stack}


def _loop(stack, count=30):
	return {"sidecar": [_sidecar(stack) for _ in range(count)]}


class TestRedundantCallsUsesTheSameRule:
	def test_frappe_core_work_under_the_before_insert_wrapper_is_kept(self, empty_context):
		inner_first = [
			F("/b/apps/frappe/frappe/core/doctype/file/file.py", "before_insert", 100),
			F("/b/apps/optimus/optimus/managed.py", "before_insert", 157),
			F("/b/apps/frappe/frappe/model/document.py", "run_method", 300),
			F("/b/apps/myapp/myapp/api.py", "upload", 10),
		]
		result = redundant_analyze([_loop(inner_first)], empty_context)
		assert len(result.findings) == 1

	def test_the_sidecar_check_applies_the_shared_rule_to_an_innermost_first_stack(self):
		from optimus.analyzers.redundant_calls import _is_optimus_own

		core_work = [
			F("/b/apps/frappe/frappe/core/doctype/file/file.py", "before_insert", 100),
			F("/b/apps/optimus/optimus/managed.py", "before_insert", 157),
			F("/b/apps/myapp/myapp/api.py", "upload", 10),
		]
		wrapper_work = [F("/b/apps/frappe/frappe/__init__.py", "get_installed_apps", 1500), *core_work[1:]]
		assert not _is_optimus_own(core_work)
		assert _is_optimus_own(wrapper_work)

	def test_wrapper_own_cache_lookup_is_dropped(self, empty_context):
		inner_first = [
			F("/b/apps/frappe/frappe/__init__.py", "get_installed_apps", 1500),
			F("/b/apps/optimus/optimus/managed.py", "before_insert", 157),
			F("/b/apps/myapp/myapp/api.py", "upload", 10),
		]
		result = redundant_analyze([_loop(inner_first)], empty_context)
		assert result.findings == []

	def test_settings_read_under_a_user_query_is_dropped(self, empty_context):
		inner_first = [
			F("/b/apps/optimus/optimus/settings.py", "get_config", 40),
			F("/b/apps/optimus/optimus/__init__.py", "_read_extras", 300),
			F("/b/apps/optimus/optimus/__init__.py", "_profiler_register", 360),
			F("/b/apps/myapp/myapp/api.py", "run", 10),
		]
		result = redundant_analyze([_loop(inner_first)], empty_context)
		assert result.findings == []

	def test_user_loop_through_the_get_doc_wrapper_still_reports(self, empty_context):
		# the sidecar skips the wrapper frame itself: the stack starts at the caller
		inner_first = [F("/b/apps/myapp/myapp/api.py", "loop_body", 77),
			F("/b/apps/myapp/myapp/api.py", "run", 70)]
		result = redundant_analyze([_loop(inner_first)], empty_context)
		assert len(result.findings) == 1

	def test_the_sidecar_check_stops_at_the_first_deciding_frame(self):
		from optimus.analyzers.redundant_calls import _is_optimus_own

		def own_then_boom():
			yield F("/b/apps/frappe/frappe/__init__.py", "get_all", 2000)
			yield F("/b/apps/optimus/optimus/managed.py", "protect_file", 234)
			raise AssertionError("read past the deciding frame")

		def user_then_boom():
			yield F("/b/apps/frappe/frappe/__init__.py", "get_all", 2000)
			yield F("/b/apps/myapp/myapp/api.py", "upload", 10)
			raise AssertionError("read past the deciding frame")

		assert _is_optimus_own(own_then_boom())
		assert not _is_optimus_own(user_then_boom())
