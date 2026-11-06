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

# Hook kinds whose handlers Frappe calls from inside the user's code (a save, a read, a permission check).
USER_FLOW_HOOKS = (
	"doc_events",
	"has_permission",
	"permission_query_conditions",
	"override_whitelisted_methods",
	"override_doctype_class",
	"override_doctype_dashboards",
	"extend_doctype_class",
)

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


def _user_flow_handlers(module=hooks):
	found = set()
	for kind in USER_FLOW_HOOKS:
		for dotted in _dotted_values(getattr(module, kind, None) or {}):
			if dotted.startswith("optimus."):
				found.add(_entry_key(dotted))
	return found


def test_there_are_user_flow_hooks_to_classify():
	assert {"protect_file", "file_has_permission", "mask_error_log"} <= {f for _p, f in _user_flow_handlers()}


def test_every_user_flow_hook_is_classified():
	unclassified = _user_flow_handlers() - set(_OPTIMUS_HOOK_ENTRY_POINTS) - set(EXCLUDED)
	assert not unclassified, (
		f"{sorted(unclassified)} run inside a user's flow: add each to "
		"optimus.analyzers.base._OPTIMUS_HOOK_ENTRY_POINTS or to EXCLUDED with a reason."
	)


def test_a_newly_added_hook_fails_the_guard():
	class Planted:
		doc_events = {"ToDo": {"validate": "optimus.new_module.check_todo"}}

	new = _user_flow_handlers(Planted) - set(_OPTIMUS_HOOK_ENTRY_POINTS) - set(EXCLUDED)
	assert new == {("optimus/new_module.py", "check_todo")}


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
