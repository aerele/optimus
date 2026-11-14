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
	("__init__.py", "_omit_unredacted_request", "setattr(recorder, name, dict(marker))"): (
		"excluded", "stores a marker dict on a recording's form data; it installs no callable"),
	("capture.py", "install_wraps", "setattr(module, attr, new_wrap)"): ("table", (CAPTURE, "wrapped")),
	("capture.py", "uninstall_wraps", "setattr(module, attr, current._profiler_original)"): (
		"excluded", "restores the saved original; it installs no wrapper"),
	("capture.py", "install_lock_capture", "database.sql"): (
		"excluded", "the lock wrap has returned before the recorder captures the SQL stack"),
	("managed.py", "install_file_input_guard", "File.before_insert"): ("table", (MANAGED, "before_insert")),
}


# Per-request state holders. An assignment into one is data, not a patch of Frappe code.
STATE_NAMESPACES = frozenset({"local", "flags", "conf", "session", "response", "form_dict", "request"})
# Data, never a callable: assigning one cannot install a wrapper.
_DATA_VALUES = (ast.Constant, ast.JoinedStr)
_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _patch_sites(package=PACKAGE):
	"""(file, enclosing function, target) for each monkey-patch in the non-test source of ``package``.

	Any attribute assignment or ``setattr`` that can install a callable counts, whatever the wrapper
	or its factory is named; ``_sites_in_source`` has the exact rule. Missing a wrapper here is the
	dangerous failure: an unclassified wrapper under a user's SQL call hides that query as Optimus's own.
	"""
	found = set()
	for path in sorted(package.rglob("*.py")):
		rel = path.relative_to(package).as_posix()
		if rel.startswith(("tests/", "tests_integration/")):
			continue
		found |= {(rel, scope, site) for scope, site in _sites_in_source(path.read_text())}
	return found


class _FileNames:
	"""The names in one file that can be a patch target or a patch value."""

	def __init__(self, tree):
		nodes = list(ast.walk(tree))
		self.imported = {
			(alias.asname or alias.name).split(".")[0]
			for node in nodes if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names
		}
		self.classes = {node.name for node in nodes if isinstance(node, ast.ClassDef)}
		self.functions = {node.name for node in nodes if isinstance(node, _FUNCTION_NODES)}
		self.made = {
			target.id for node in nodes if isinstance(node, ast.Assign) and _is_wrapper_call(node.value)
			for target in node.targets if isinstance(target, ast.Name)
		}

	def is_patch_target(self, root):
		"""An imported module or class (or a class defined in the file): what a monkey-patch lands on."""
		return root in self.imported or root in self.classes

	def is_callable_value(self, value):
		"""A value that is plainly a callable: a lambda, a function or imported name (not an
		``UPPER_CASE`` constant), a ``_make_*`` result."""
		if isinstance(value, ast.Lambda) or _is_wrapper_call(value):
			return True
		return isinstance(value, ast.Name) and not value.id.isupper() and value.id in (
			self.functions | self.made | self.imported
		)


def _sites_in_source(source):
	"""(enclosing function, target) for each monkey-patch in ``source``.

	An attribute assignment ``X.attr = value`` or ``setattr(X, name, value)`` is a patch site when
	``value`` is not data (a constant or f-string), ``X`` is not ``self``/``cls`` or a function
	defined in the file (a tag on a wrapper), the target does not pass through a per-request state
	namespace (``frappe.local``, ``frappe.flags``, ...), and any of these holds:
	  1. ``X`` is an imported module or class, however the wrapper is built or named;
	  2. ``value`` wraps the target's current value, ``X.attr = factory(X.attr)``, or the same
	     function reads ``getattr(X, name)`` before a ``setattr``, or saves the original into a
	     variable (``original = X.attr``) that a later ``X.attr = factory(original)`` or
	     ``setattr(X, "attr", factory(original))`` passes on; this also covers a target reached
	     through a helper (``database = _database_class()``);
	  3. ``value`` is a lambda, a function or imported name, or a ``_make_*``/``_wrap_*`` result.
	Plain attribute writes on objects the function was handed or built (``doc.total = n``) match
	none of these, so they need no classification.
	"""
	tree = ast.parse(source)
	names = _FileNames(tree)
	reads = {}
	saved = {}
	sites = set()
	for scope, node in _scoped_nodes(tree):
		if scope not in reads:
			reads[scope] = _getattr_reads(tree, scope)
			saved[scope] = _saved_originals(tree, scope)
		sites |= {(scope, site) for site in _node_sites(node, names, reads[scope], saved[scope])}
	return sites


def _scoped_nodes(node, scope="<module>"):
	"""(innermost enclosing function name or ``<module>``, node) for every node under ``node``."""
	for child in ast.iter_child_nodes(node):
		inner = child.name if isinstance(child, _FUNCTION_NODES) else scope
		yield inner, child
		yield from _scoped_nodes(child, inner)


def _getattr_reads(tree, scope):
	"""The ``(object, name)`` texts passed to ``getattr`` in the function(s) named ``scope``."""
	reads = set()
	for fn in ast.walk(tree):
		if not isinstance(fn, _FUNCTION_NODES) or fn.name != scope:
			continue
		for node in ast.walk(fn):
			if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "getattr" and len(node.args) >= 2:
				reads.add((ast.unparse(node.args[0]), ast.unparse(node.args[1])))
	return reads


def _saved_originals(tree, scope):
	"""``(variable, object, name)`` texts for each ``variable = X.attr`` or ``variable = getattr(X, "attr")``
	in the function(s) named ``scope``: a saved original that a later wrapper factory can be handed."""
	saved = set()
	for fn in ast.walk(tree):
		if not isinstance(fn, _FUNCTION_NODES) or fn.name != scope:
			continue
		for node in ast.walk(fn):
			if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
				continue
			var, value = node.targets[0], node.value
			if not isinstance(var, ast.Name):
				continue
			if isinstance(value, ast.Attribute):
				saved.add((var.id, ast.unparse(value.value), repr(value.attr)))
			elif isinstance(value, ast.Call) and getattr(value.func, "id", "") == "getattr" and len(value.args) >= 2:
				saved.add((var.id, ast.unparse(value.args[0]), ast.unparse(value.args[1])))
	return saved


def _passes_saved_original(value, obj_text, name_text, saved):
	"""``value`` is a call that is handed a variable saved from the target ``obj_text.name_text``."""
	if not isinstance(value, ast.Call):
		return False
	used = {node.id for node in ast.walk(value) if isinstance(node, ast.Name)}
	return any((var, obj_text, name_text) in saved for var in used)


def _node_sites(node, names, reads, saved):
	if isinstance(node, ast.Assign) and not isinstance(node.value, _DATA_VALUES):
		return [
			ast.unparse(target) for target in node.targets
			if isinstance(target, ast.Attribute) and _is_patch(target, node.value, names, saved)
		]
	if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "setattr" and len(node.args) == 3:
		obj, name, value = node.args
		if not isinstance(value, _DATA_VALUES) and _is_setattr_patch(obj, name, value, names, reads, saved):
			return [ast.unparse(node)]
	return []


def _root_name(target):
	while isinstance(target, (ast.Attribute, ast.Subscript, ast.Call)):
		target = target.func if isinstance(target, ast.Call) else target.value
	return target.id if isinstance(target, ast.Name) else None


def _is_tag(target, names):
	"""A write onto self/cls, or onto a function defined in the file (a marker left on a wrapper)."""
	return _root_name(target) in {"self", "cls"} | names.functions


def _writes_into_state(obj):
	"""True when ``obj`` (the object an attribute is written ONTO) is a per-request state holder such
	as ``frappe.local`` or ``frappe.flags``: a write onto one of those stores data, it patches no code.

	Only the immediate object counts. A longer chain that passes THROUGH a holder to reach a real
	object (``frappe.local.db.sql`` patches the request's live db ``sql`` method) is a patch, so the
	holder name is not matched when it is merely a link in the path to the attribute being set."""
	return isinstance(obj, ast.Attribute) and obj.attr in STATE_NAMESPACES


def _is_patch(target, value, names, saved):
	if _is_tag(target, names) or _writes_into_state(target.value):
		return False
	target_text = ast.unparse(target)
	# the target text anywhere under a call in the value: ``f(x.a)``, ``f(x.a).call``, ``f(g(x.a))``;
	# a plain expression that merely reads the target (``x.a = x.a or y``) is no wrapper
	wraps_current = any(
		ast.unparse(inner) == target_text
		for call in ast.walk(value)
		if isinstance(call, ast.Call)
		for inner in ast.walk(call)
	)
	wraps_saved = _passes_saved_original(value, ast.unparse(target.value), repr(target.attr), saved)
	return (
		names.is_patch_target(_root_name(target)) or wraps_current or wraps_saved
		or names.is_callable_value(value)
	)


def _is_setattr_patch(obj, name, value, names, reads, saved):
	if _is_tag(obj, names) or _writes_into_state(obj):
		return False
	obj_text, name_text = ast.unparse(obj), ast.unparse(name)
	read = (obj_text, name_text) in reads
	wraps_saved = _passes_saved_original(value, obj_text, name_text, saved)
	return names.is_patch_target(_root_name(obj)) or read or wraps_saved or names.is_callable_value(value)


def _is_wrapper_call(value):
	return isinstance(value, ast.Call) and getattr(value.func, "id", "").startswith(("_make_", "_wrap_"))


def test_every_installed_patch_is_classified():
	assert _patch_sites() == set(SITES), (
		"A monkey-patch was added or removed: list it in SITES as a pass-through entry "
		"(optimus.analyzers.base._OPTIMUS_PASS_THROUGH) or as an exclusion with its reason."
	)


# Wrappers a reviewer found the old scan could not see: a factory not named _make_*/_wrap_*, a direct
# assignment, a setattr with a built value, a target reached through a helper, a module-level patch.
UNREGISTERED_WRAPPERS = {
	"differently named factory": (
		"import frappe\n\ndef install():\n\toriginal = frappe.get_doc\n\tfrappe.get_doc = build_guard(original)\n",
		("install", "frappe.get_doc"),
	),
	"direct assignment of a function": (
		"from frappe.model.document import Document\n\ndef hook(self):\n\tpass\n\n"
		"def install():\n\tDocument.save = hook\n",
		("install", "Document.save"),
	),
	"target reached through a helper": (
		"def install():\n\tdatabase = find_database_class()\n\tdatabase.sql = harden(database.sql)\n",
		("install", "database.sql"),
	),
	"helper target with a saved original and a differently named factory": (
		"def install():\n\tdatabase = _database_class()\n\toriginal = database.sql\n"
		"\tdatabase.sql = build(original)\n",
		("install", "database.sql"),
	),
	"helper target with a saved original and setattr": (
		"def install():\n\tdatabase = _database_class()\n\toriginal = database.sql\n"
		"\tsetattr(database, 'sql', build(original))\n",
		("install", "setattr(database, 'sql', build(original))"),
	),
	"setattr with a built wrapper": (
		"def install(module, attr):\n\tcurrent = getattr(module, attr)\n\tsetattr(module, attr, build(current))\n",
		("install", "setattr(module, attr, build(current))"),
	),
	"setattr onto an imported module": (
		"import frappe.permissions as perms\n\ndef install():\n\tsetattr(perms, 'has_permission', make_it())\n",
		("install", "setattr(perms, 'has_permission', make_it())"),
	),
	"patch at module level": (
		"import frappe\n\nfrappe.cache_value = guard(frappe.cache_value)\n",
		("<module>", "frappe.cache_value"),
	),
	"wrap through the request-local db": (
		"import frappe\n\ndef install():\n\tfrappe.local.db.sql = build(frappe.local.db.sql)\n",
		("install", "frappe.local.db.sql"),
	),
	"setattr through the request-local db": (
		"import frappe\n\ndef install():\n\tsetattr(frappe.local.db, 'sql', build(frappe.local.db.sql))\n",
		("install", "setattr(frappe.local.db, 'sql', build(frappe.local.db.sql))"),
	),
	"bound method of a wrapper built around the target": (
		"def install():\n\tdatabase = find_database_class()\n\tdatabase.sql = Wrapper(database.sql).call\n",
		("install", "database.sql"),
	),
	"lambda onto a class": (
		"from frappe.model.document import Document\n\nDocument.on_trash = lambda self: None\n",
		("<module>", "Document.on_trash"),
	),
}


@pytest.mark.parametrize("case", sorted(UNREGISTERED_WRAPPERS))
def test_a_wrapper_the_old_scan_missed_is_now_detected(case):
	source, site = UNREGISTERED_WRAPPERS[case]
	assert site in _sites_in_source(source)


@pytest.mark.parametrize("case", sorted(UNREGISTERED_WRAPPERS))
def test_an_unclassified_wrapper_fails_the_guard(case, tmp_path):
	"""A new wrapper in a package that SITES does not list makes the guard's comparison fail."""
	source, (_scope, site) = UNREGISTERED_WRAPPERS[case]
	(tmp_path / "planted.py").write_text(source)
	unclassified = _patch_sites(tmp_path) - set(SITES)
	assert {found for _file, _scope, found in unclassified} == {site}


NOT_PATCHES = {
	"a constant on a module": "import frappe.utils as utils\n\ndef f():\n\tutils.DEBUG = True\n",
	"state on the request": (
		"import frappe\n\ndef f(user):\n\tfrappe.local.user = lookup(user)\n\tfrappe.flags.seen = fetch()\n"
	),
	"a value read back into itself": (
		"def f(shape, multi):\n\tshape.ambiguous = shape.ambiguous or multi\n"
	),
	"a tag on a wrapper": (
		"def make():\n\tdef wrapper():\n\t\tpass\n\twrapper._patched = original\n\twrapper.__wrapped__ = original\n"
		"\treturn wrapper\n"
	),
	"self": "class A:\n\tdef f(self, hook):\n\t\tself.hook = hook\n\t\tsetattr(self, 'x', hook)\n",
	"data on a value the function built": (
		"def f(doc, rows):\n\tdoc.total = len(rows)\n\tdoc.title = compute(rows)\n\tsetattr(doc, 'name', rows[0])\n"
	),
}


@pytest.mark.parametrize("case", sorted(NOT_PATCHES))
def test_plain_attribute_writes_are_not_patch_sites(case):
	assert _sites_in_source(NOT_PATCHES[case]) == set()


def test_the_scan_attributes_a_site_to_its_innermost_function():
	source = "import frappe\n\ndef outer():\n\tdef inner():\n\t\tfrappe.x = build()\n\treturn inner\n"
	assert _sites_in_source(source) == {("inner", "frappe.x")}


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
