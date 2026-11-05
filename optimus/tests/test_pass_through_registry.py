# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Every Optimus monkey-patch is a pass-through wrapper for the own-query rule or a documented exclusion."""

import ast
from pathlib import Path

import pytest

from optimus.analyzers.base import _OPTIMUS_PASS_THROUGH, _pass_through_targets

PACKAGE = Path(__file__).resolve().parents[1]

CAPTURE = "optimus/capture.py"
INIT = "optimus/__init__.py"
MANAGED = "optimus/managed.py"

# Each patch site found in the source -> how the own-query rule treats the wrapper it installs.
# ("table", key): a pass-through entry. ("excluded", reason): never a frame under a user's SQL call.
SITES = {
	("__init__.py", "_patch_enqueue", "_bg.enqueue"): ("table", (INIT, "_profiler_enqueue")),
	("__init__.py", "_patch_enqueue", "frappe.enqueue"): ("table", (INIT, "_profiler_enqueue")),
	("__init__.py", "_patch_recorder", "Recorder.__init__"): (
		"excluded", "runs before any query it records; the stack is captured in record_sql"),
	("__init__.py", "_patch_recorder", "Recorder.register"): (
		"excluded", "Recorder.register runs after the SQL stack is captured (frappe recorder.py:61-79)"),
	("capture.py", "install_wraps", "setattr(module, attr, new_wrap)"): ("table", (CAPTURE, "wrapped")),
	("capture.py", "install_lock_capture", "database.sql"): (
		"excluded", "the lock wrap has returned before the recorder captures the SQL stack"),
	("managed.py", "install_file_input_guard", "File.before_insert"): ("table", (MANAGED, "before_insert")),
}


def _patch_sites():
	"""(file, enclosing function, target) for each ``X.attr = <wrapper>`` / ``setattr(.., wrapper)``."""
	found = set()
	for path in sorted(PACKAGE.rglob("*.py")):
		rel = path.relative_to(PACKAGE).as_posix()
		if rel.startswith(("tests/", "tests_integration/")):
			continue
		for fn in ast.walk(ast.parse(path.read_text())):
			if not isinstance(fn, ast.FunctionDef):
				continue
			nested = {n.name for n in ast.walk(fn) if isinstance(n, ast.FunctionDef) and n is not fn}
			made = _wrapper_names(fn)
			for node in ast.walk(fn):
				site = _site(node, nested | made)
				if site:
					found.add((rel, fn.name, site))
	return found


def _wrapper_names(fn):
	"""Names bound in ``fn`` to a ``_make_*`` / ``_wrap_*`` call."""
	names = set()
	for node in ast.walk(fn):
		if isinstance(node, ast.Assign) and _is_wrapper_call(node.value):
			names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
	return names


def _is_wrapper_call(value):
	return isinstance(value, ast.Call) and getattr(value.func, "id", "").startswith(("_make_", "_wrap_"))


def _site(node, wrappers):
	if isinstance(node, ast.Assign):
		value = node.value
		if isinstance(value, ast.Name) and value.id in wrappers or _is_wrapper_call(value):
			targets = [ast.unparse(t) for t in node.targets if isinstance(t, ast.Attribute)]
			return targets[0] if targets else None
	if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "setattr" and len(node.args) == 3:
		if isinstance(node.args[2], ast.Name) and node.args[2].id in wrappers:
			return ast.unparse(node)
	return None


def test_every_installed_patch_is_classified():
	assert _patch_sites() == set(SITES), (
		"A monkey-patch was added or removed: list it in SITES as a pass-through entry "
		"(optimus.analyzers.base._OPTIMUS_PASS_THROUGH) or as an exclusion with its reason."
	)


def test_pass_through_table_has_no_stale_entries():
	used = {value[1] for value in SITES.values() if value[0] == "table"}
	assert used == set(_OPTIMUS_PASS_THROUGH)


def test_exclusions_state_their_reason():
	assert all(value[1] for value in SITES.values() if value[0] == "excluded")


def _function_defs(suffix, name):
	tree = ast.parse((PACKAGE.parent / suffix).read_text())
	return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]


@pytest.mark.parametrize("key", sorted(_OPTIMUS_PASS_THROUGH))
def test_a_pass_through_body_makes_no_direct_frappe_call_except_the_wrapped_original(key):
	defs = _function_defs(*key)
	assert defs, f"{key} no longer defines that function: update the table and SITES"
	for fn in defs:
		for node in ast.walk(fn):
			if isinstance(node, ast.Call):
				root = node.func
				while isinstance(root, (ast.Attribute, ast.Call)):
					root = root.func if isinstance(root, ast.Call) else root.value
				assert not (isinstance(root, ast.Name) and root.id == "frappe"), (
					f"{key} calls {ast.unparse(node.func)}() directly; move it into a helper so "
					"the wrapper's own Frappe work is not billed to the user"
				)


def test_rename_guard_matches_code_names_not_dunder_name():
	from optimus import capture, managed

	def original(*args, **kwargs):
		return 1

	sidecar = capture._make_wrap(original, "get_doc", local_proxy=object())
	file_guard = managed._wrap_file_input(original)
	for code, key in ((sidecar.__code__, (CAPTURE, "wrapped")), (file_guard.__code__, (MANAGED, "before_insert"))):
		assert key in _OPTIMUS_PASS_THROUGH
		filename = Path(code.co_filename).as_posix()
		assert _pass_through_targets(filename, code.co_name) == _OPTIMUS_PASS_THROUGH[key]
	# functools.wraps copies __name__ from the original: the table must not depend on it
	file_guard.__name__ = "renamed"
	assert file_guard.__code__.co_name == "before_insert"


def test_enqueue_wrapper_is_defined_under_the_table_name():
	assert _function_defs(INIT, "_profiler_enqueue")
