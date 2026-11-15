# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Every Optimus handler Frappe runs inside a user's flow is a hook entry point for the own-query rule
or a documented exclusion, so a new one cannot silently show up as a suspicious (kept) query."""

import ast
from pathlib import Path

import pytest

from optimus import hooks
from optimus.analyzers.base import _OPTIMUS_HOOK_ENTRY_POINTS, _is_hook_entry_point

PACKAGE = Path(__file__).resolve().parents[1]

# Hook kinds whose optimus.* handlers Frappe never calls from inside a user's code: an install or migrate
# step, the boot, the request and job entry points and the scheduler run with only framework frames outside.
# Every other key in hooks.py is examined, so a NEW hook kind (jinja, on_session_creation, a permission or
# override kind) fails the guard until it is classified here or its handlers are registered.
NON_USER_FLOW_HOOKS = frozenset({
	"after_install",
	"before_uninstall",
	"after_migrate",
	"boot_session",
	"before_request",
	"after_request",
	"before_job",
	"after_job",
	"scheduler_events",
})

# Entry points that are not hooks.py values, with why they are still a frame under a user's SQL call.
EXTRA_ENTRY_POINTS = {
	("optimus/__init__.py", "_profiler_register"): "the Recorder.register patch reads settings under the user's recorded query",
}

# hooks.py handlers that are classified as not being a user-flow own issuer, with the reason.
EXCLUDED = {}


def _dotted_values(value):
	if isinstance(value, str):
		yield value
	elif isinstance(value, dict):
		for inner in value.values():
			yield from _dotted_values(inner)
	elif isinstance(value, (list, tuple)):
		for inner in value:
			yield from _dotted_values(inner)


def _entry_key(dotted):
	module, _, function = dotted.rpartition(".")
	return module.replace(".", "/") + ".py", function


def _hook_kinds(module=hooks):
	"""Every hook key ``module`` defines (its public attributes), whatever its kind."""
	return {name for name in vars(module) if not name.startswith("_")}


def _user_flow_handlers(module=hooks):
	found = set()
	for kind in _hook_kinds(module) - NON_USER_FLOW_HOOKS:
		for dotted in _dotted_values(getattr(module, kind, None) or {}):
			if dotted.startswith("optimus."):
				found.add(_entry_key(dotted))
	return found


def test_there_are_user_flow_hooks_to_classify():
	assert {"protect_file", "file_has_permission", "mask_error_log"} <= {f for _p, f in _user_flow_handlers()}
	assert {"doc_events", "has_permission"} <= _hook_kinds() - NON_USER_FLOW_HOOKS


def test_every_user_flow_hook_is_classified():
	unclassified = _user_flow_handlers() - set(_OPTIMUS_HOOK_ENTRY_POINTS) - set(EXCLUDED)
	assert not unclassified, (
		f"{sorted(unclassified)} run inside a user's flow: add each to "
		"optimus.analyzers.base._OPTIMUS_HOOK_ENTRY_POINTS or to EXCLUDED with a reason."
	)


def _unclassified(module):
	return _user_flow_handlers(module) - set(_OPTIMUS_HOOK_ENTRY_POINTS) - set(EXCLUDED)


def test_a_newly_added_hook_fails_the_guard():
	class Planted:
		doc_events = {"ToDo": {"validate": "optimus.new_module.check_todo"}}

	assert _unclassified(Planted) == {("optimus/new_module.py", "check_todo")}


@pytest.mark.parametrize("kind,value", [
	("jinja", {"methods": ["optimus.jinja_utils.format_value"]}),
	("on_session_creation", "optimus.session_hooks.on_login"),
	("permission_query_conditions", {"ToDo": "optimus.perms.query"}),
	("override_whitelisted_methods", {"frappe.client.get": "optimus.overrides.get"}),
	("some_future_hook_kind", ["optimus.future.handler"]),
])
def test_a_newly_added_hook_kind_fails_the_guard(kind, value):
	"""A handler under a hook KIND the guard has never seen is unclassified by default."""
	Planted = type("Planted", (), {kind: value})
	assert len(_unclassified(Planted)) == 1


def test_a_non_user_flow_kind_is_not_examined():
	Planted = type("Planted", (), {"before_request": ["optimus.hooks_callbacks.new_step"]})
	assert _unclassified(Planted) == set()


def test_the_non_user_flow_set_names_only_kinds_hooks_py_defines():
	assert NON_USER_FLOW_HOOKS <= _hook_kinds()


def test_every_handler_under_a_non_user_flow_kind_is_a_function_that_exists():
	for kind in NON_USER_FLOW_HOOKS:
		for dotted in _dotted_values(getattr(hooks, kind)):
			if not dotted.startswith("optimus."):
				continue
			suffix, name = _entry_key(dotted)
			tree = ast.parse((PACKAGE.parent / suffix).read_text())
			assert any(isinstance(n, ast.FunctionDef) and n.name == name for n in ast.walk(tree)), dotted


def test_the_registry_has_no_stale_entries():
	known = _user_flow_handlers() | set(EXTRA_ENTRY_POINTS)
	assert set(_OPTIMUS_HOOK_ENTRY_POINTS) == known


def test_exclusions_state_their_reason():
	assert all(EXCLUDED.values()) and all(EXTRA_ENTRY_POINTS.values())


@pytest.mark.parametrize("key", sorted(_OPTIMUS_HOOK_ENTRY_POINTS))
def test_each_entry_point_is_defined_where_the_registry_says(key):
	suffix, name = key
	tree = ast.parse((PACKAGE.parent / suffix).read_text())
	assert any(
		isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
		for node in ast.walk(tree)
	), f"{suffix} no longer defines {name}: update the registry"


def test_entry_matching_is_by_file_suffix_and_function():
	assert _is_hook_entry_point("optimus/optimus/managed.py", "protect_file")
	assert _is_hook_entry_point("/b/apps/optimus/optimus/managed.py", "protect_file")
	assert not _is_hook_entry_point("optimus/optimus/analyze.py", "protect_file")
	assert not _is_hook_entry_point("optimus/optimus/managed.py", 5)
