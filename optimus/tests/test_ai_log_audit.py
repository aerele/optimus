# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Static guard (PR-0a): every Error Log write on the AI surface goes through
``ai_fix.log_ai_failure``, and none runs while an exception is being handled.

A bare ``frappe.log_error(title=...)`` stores Frappe's with-context traceback,
which prints the locals of every frame: that is how API keys and prompts
reached the Error Log. And ``frappe.log_error`` calls Sentry's
``capture_exception``, which ships the ACTIVE exception's frame locals even
when an explicit message is passed, so logging inside an ``except`` block
sends them to Sentry. The rules:

1. In ``ai_fix.py`` only ``log_ai_failure`` calls ``frappe.log_error``.
2. In the scanned modules (``analyze.py``, ``api.py``, ``maintenance.py``,
   ``error_log_mask.py``, and ``ai_jobs.py`` as soon as it exists) an AI function never calls
   ``frappe.log_error``. An AI function references the ``ai_fix`` module or
   any name imported from ``optimus.ai_fix``. Every non-test module that
   imports ``optimus.ai_fix`` must be scanned (a guard test enforces it).
3. No logging call runs inside an ``except`` handler of any ``ai_fix.py``
   function, of any AI function, or of any ``try`` whose body runs an AI
   step (an AI function that uses ai_fix for more than ``log_ai_failure``,
   or a wrapper in ``_AI_WRAPPERS``). This reaches ``analyze.run``, which
   keeps ``frappe.log_error`` for its own non-AI failures. A logging
   call is a call to ``log_error``, ``log_ai_failure`` or any function of
   these modules that reaches one of them (``_log_http_error``,
   ``_http_post``, ``suggest_fix``, ...). Record the exception inside the
   handler; log, retry or raise after the ``try``.
4. No provider request is sent inside an ``except`` handler of any function
   of these modules (AI function or not), whether or not it logs: no call to
   ``_http_post``, ``_call_openai_chat``, ``_call_anthropic`` or any function
   of these modules that reaches one of them. ``_http_post`` lets a
   worker-timeout ``SystemExit`` (or a ``KeyboardInterrupt``, a gevent
   ``Timeout``) out with its context cleared, but a raise while an exception
   is being handled sets ``__context__`` again, so the handled exception
   would ride along with it.
"""

import ast
import re
from pathlib import Path

_PKG = Path(__file__).resolve().parents[1]
_REQUIRED = (
	"analyze.py", "api.py", "maintenance.py", "error_log_mask.py",
	"renderer/source.py", "renderer/source_resolution.py", "server_script_source.py",
	"settings.py", "report_refresh.py", "line_profile/jobs.py", "line_profile/capture.py", "line_profile/hooks.py", "ai_privacy.py",
	"patches/v0_12_0/seed_ai_refresh_max_findings.py",
	"patches/v0_12_0/warn_ai_endpoint_policy.py",
	"optimus/doctype/optimus_settings/optimus_settings.py", "renderer/fix_recipes.py", "line_profile/analyzer.py",
)
_OPTIONAL = ("ai_jobs.py",)  # scanned as soon as a later PR adds it
_AI_WRAPPERS = frozenset()  # Session AI now runs only in ai_jobs.py.
_BASE_LOGGERS = frozenset({"log_error", "log_ai_failure"})
_SENDERS = frozenset({"_http_post", "_call_openai_chat", "_call_anthropic"})  # ai_fix.py
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_TRIES = (ast.Try, ast.TryStar)


def _scanned() -> tuple[str, ...]:
	return _REQUIRED + tuple(m for m in _OPTIONAL if (_PKG / m).exists())


def _tree(name: str) -> ast.Module:
	return ast.parse((_PKG / name).read_text(encoding="utf-8"), filename=name)


def _functions(tree: ast.AST) -> list[ast.FunctionDef]:
	return [n for n in ast.walk(tree) if isinstance(n, _FUNCS)]


def _callee(call: ast.Call) -> str | None:
	if isinstance(call.func, ast.Name):
		return call.func.id
	if isinstance(call.func, ast.Attribute):
		return call.func.attr
	return None


def _is_log_error(node: ast.AST) -> bool:
	return isinstance(node, ast.Call) and _callee(node) == "log_error" and isinstance(node.func, ast.Attribute)


def _own_nodes(node: ast.AST):
	"""Every node under ``node``, skipping nested functions, lambdas and classes
	(their bodies run later, not in this block)."""
	stack = list(ast.iter_child_nodes(node))
	while stack:
		n = stack.pop()
		if isinstance(n, _SCOPES):
			continue
		yield n
		stack.extend(ast.iter_child_nodes(n))


def _ai_fix_names(tree: ast.Module) -> set[str]:
	"""Names that stand for the ai_fix module, or for anything imported from
	it, anywhere in the module (module-level or function-level imports)."""
	names = {"ai_fix"}
	for n in ast.walk(tree):
		if isinstance(n, ast.ImportFrom) and n.module == "optimus.ai_fix":
			names |= {a.asname or a.name for a in n.names}
		elif isinstance(n, ast.ImportFrom) and n.module == "optimus":
			names |= {a.asname or a.name for a in n.names if a.name == "ai_fix"}
		elif isinstance(n, ast.Import):
			names |= {a.asname for a in n.names if a.name == "optimus.ai_fix" and a.asname}
	return names


def _is_ai_function(fn: ast.AST, names: set[str]) -> bool:
	return any(
		(isinstance(n, ast.Name) and n.id in names) or (isinstance(n, ast.Attribute) and n.attr == "ai_fix")
		for n in ast.walk(fn)
	)


def _ai_functions(tree: ast.Module) -> list[ast.FunctionDef]:
	names = _ai_fix_names(tree)
	return [fn for fn in _functions(tree) if _is_ai_function(fn, names)]


def _runs_an_ai_step(fn: ast.AST, names: set[str]) -> bool:
	"""True when ``fn`` uses ai_fix for more than ``log_ai_failure`` (a
	function that only logs through the chokepoint is a logger, not an AI
	step: ``analyze._log_ai_step_failure``)."""
	bases = {id(n.value) for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
	for n in ast.walk(fn):
		if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in names:
			if n.attr != "log_ai_failure":
				return True
		elif isinstance(n, ast.Name) and n.id in names and n.id != "log_ai_failure" and id(n) not in bases:
			return True
	return False


def _reaching(trees: list[ast.Module], base: frozenset[str]) -> set[str]:
	"""``base`` and every function in ``trees`` that reaches one of them
	(fixpoint over direct calls, matched by name)."""
	reached = set(base)
	functions = [fn for tree in trees for fn in _functions(tree)]
	grew = True
	while grew:
		grew = False
		for fn in functions:
			if fn.name in reached:
				continue
			if any(isinstance(n, ast.Call) and _callee(n) in reached for n in _own_nodes(fn)):
				reached.add(fn.name)
				grew = True
	return reached


def _loggers(trees: list[ast.Module]) -> set[str]:
	"""``log_error``, ``log_ai_failure`` and every function in ``trees`` that
	reaches one of them."""
	return _reaching(trees, _BASE_LOGGERS)


def _unguarded_calls(stmts: list[ast.stmt]) -> set[str | None]:
	"""Callee names in ``stmts`` that are not inside a nested ``try`` (whose own
	handlers are the ones that catch them) or a nested function."""
	out: set[str | None] = set()
	stack: list[ast.AST] = list(stmts)
	while stack:
		node = stack.pop()
		if isinstance(node, _TRIES + _SCOPES):
			continue
		if isinstance(node, ast.Call):
			out.add(_callee(node))
		stack.extend(ast.iter_child_nodes(node))
	return out


def _calls_in_handlers(scope: ast.AST, names: set[str]) -> list[int]:
	"""Line numbers of calls to ``names`` inside any ``except`` handler of
	``scope`` (nested functions excluded)."""
	lines = []
	for node in _own_nodes(scope):
		if isinstance(node, ast.ExceptHandler):
			lines += [
				n.lineno for n in _own_nodes(node)
				if isinstance(n, ast.Call) and _callee(n) in names
			]
	return lines


def _handler_offenders(module: str, tree: ast.Module, loggers: set[str], helpers: set[str]) -> list[str]:
	"""Rule 3 for one module."""
	offenders: set[str] = set()
	scopes = _functions(tree) if module == "ai_fix.py" else _ai_functions(tree)
	for fn in scopes:
		offenders |= {f"{module}:{fn.name}:{line}" for line in _calls_in_handlers(fn, loggers)}
	for node in ast.walk(tree):
		if isinstance(node, _TRIES) and _unguarded_calls(node.body) & helpers:
			for handler in node.handlers:
				offenders |= {
					f"{module}:{n.lineno}" for n in [handler, *_own_nodes(handler)]
					if isinstance(n, ast.Call) and _callee(n) in loggers
				}
	return sorted(offenders)


def _request_offenders(module: str, tree: ast.Module, senders: set[str]) -> list[str]:
	"""Rule 4 for one module: every function's handlers, not only AI
	functions' (a request sent from any handler rides on its exception)."""
	return sorted(
		f"{module}:{fn.name}:{line}" for fn in _functions(tree) for line in _calls_in_handlers(fn, senders)
	)


def _ai_steps(tree: ast.Module) -> set[str]:
	names = _ai_fix_names(tree)
	return {fn.name for fn in _functions(tree) if _runs_an_ai_step(fn, names)}


def _ai_helpers(trees: dict[str, ast.Module]) -> set[str]:
	names = set(_AI_WRAPPERS)
	for mod in _scanned():
		names |= _ai_steps(trees[mod])
	return names


def _all_trees() -> dict[str, ast.Module]:
	return {m: _tree(m) for m in ("ai_fix.py", *_scanned())}


# ---------------------------------------------------------------------------
# The rules, on the real modules
# ---------------------------------------------------------------------------

def test_only_log_ai_failure_calls_log_error_in_ai_fix():
	offenders = []
	for fn in _functions(_tree("ai_fix.py")):
		if fn.name == "log_ai_failure":
			continue
		offenders += [f"{fn.name}:{n.lineno}" for n in _own_nodes(fn) if _is_log_error(n)]
	assert offenders == [], f"frappe.log_error outside log_ai_failure in ai_fix.py: {offenders}"
	assert any(fn.name == "log_ai_failure" for fn in _functions(_tree("ai_fix.py")))


def test_ai_functions_never_call_log_error_directly():
	offenders = []
	for mod in _scanned():
		for fn in _ai_functions(_tree(mod)):
			offenders += [f"{mod}:{fn.name}:{n.lineno}" for n in ast.walk(fn) if _is_log_error(n)]
	assert offenders == [], (
		"Use ai_fix.log_ai_failure(title, e, session_uuid=...) instead of frappe.log_error "
		f"in AI code: {offenders}"
	)


def test_no_logging_inside_except_handlers_on_the_ai_surface():
	trees = _all_trees()
	loggers = _loggers(list(trees.values()))
	helpers = _ai_helpers(trees)
	offenders = []
	for mod, tree in trees.items():
		offenders += _handler_offenders(mod, tree, loggers, helpers)
	assert offenders == [], (
		"Record the exception inside the except block and log (or retry) after the try: "
		f"{offenders}"
	)


def test_no_request_is_sent_inside_an_except_handler_on_the_ai_surface():
	trees = _all_trees()
	ai_fix_functions = {fn.name for fn in _functions(trees["ai_fix.py"])}
	assert _SENDERS <= ai_fix_functions, f"renamed or removed: {sorted(_SENDERS - ai_fix_functions)}"
	senders = _reaching(list(trees.values()), _SENDERS)
	offenders = []
	for mod, tree in trees.items():
		offenders += _request_offenders(mod, tree, senders)
	assert offenders == [], (
		"Record the exception inside the except block and send the request after the try: "
		f"{offenders}"
	)


def _run_ai_step_errors(fn: ast.AST) -> list[tuple[ast.stmt, list[ast.stmt], int, str | None]]:
	"""``(statement, its block, its index, error name)`` for every
	``<result>, <error> = _run_ai_step(...)`` in ``fn`` (``error`` None when
	the call is not unpacked into two names)."""
	out = []
	for node in ast.walk(fn):
		for field in ("body", "orelse", "finalbody"):
			block = getattr(node, field, None)
			if not isinstance(block, list):
				continue
			for i, stmt in enumerate(block):
				value = stmt.value if isinstance(stmt, ast.Assign | ast.Expr) else None
				if not (isinstance(value, ast.Call) and _callee(value) == "_run_ai_step"):
					continue
				error = None
				if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Tuple):
					elts = stmt.targets[0].elts
					if len(elts) == 2 and isinstance(elts[1], ast.Name):
						error = elts[1].id
				out.append((stmt, block, i, error))
	return out


def test_ai_step_returns_only_a_boolean_failure_flag():
	# Callers can safely retain the flag when a later non-AI error logs locals.
	helper = next(fn for fn in _functions(_tree("analyze.py")) if fn.name == "_run_ai_step")
	returns = [n for n in ast.walk(helper) if isinstance(n, ast.Return)]
	assert len(returns) == 2
	for node in returns:
		assert isinstance(node.value, ast.Tuple) and len(node.value.elts) == 2
		flag = node.value.elts[1]
		assert isinstance(flag, ast.Constant) and type(flag.value) is bool


def test_the_ai_steps_log_through_the_one_helper():
	# The capture-then-log skeleton lives in analyze._run_ai_step alone: in
	# analyze.py and api.py the standalone recording readers also log their
	# own failures. All log calls stay outside exception handlers. The
	# helper's handler only records the error: its log call is after the try.
	callers = {}
	for mod in ("analyze.py", "api.py"):
		for fn in _functions(_tree(mod)):
			calls = {_callee(n) for n in _own_nodes(fn) if isinstance(n, ast.Call)}
			for name in calls & {"log_ai_failure", "_log_ai_step_failure"}:
				callers.setdefault(name, set()).add(f"{mod}:{fn.name}")
	assert callers == {
		"log_ai_failure": {"analyze.py:_deserialize_tree", "analyze.py:_fetch_recordings", "analyze.py:_save_report_file", "analyze.py:_persist_recordings_file", "analyze.py:_log_ai_step_failure", "analyze.py:load_recordings_light", "analyze.py:_load_recordings_bundle", "analyze.py:_phase2_index_for", "analyze.py:_queue_analyze_time_ai", "analyze.py:_auto_arm_phase2", "api.py:_cleanup_failed_capture"},
		"_log_ai_step_failure": {"analyze.py:_run_ai_step", "analyze.py:run"},
	}
	helper = next(fn for fn in _functions(_tree("analyze.py")) if fn.name == "_run_ai_step")
	tries = [n for n in _own_nodes(helper) if isinstance(n, _TRIES) and n.handlers]
	assert len(tries) == 1
	handler_calls = [n for h in tries[0].handlers for n in _own_nodes(h) if isinstance(n, ast.Call)]
	assert handler_calls == [], "_run_ai_step's handler must only record the error"
	after = helper.body[helper.body.index(tries[0]) + 1:]
	assert any(
		isinstance(n, ast.Call) and _callee(n) == "_log_ai_step_failure" for stmt in after for n in ast.walk(stmt)
	)


def test_the_helper_is_used_where_the_skeleton_was():
	# Every AI step of these modules that logs its failure goes through
	# _run_ai_step: no function keeps a "X = None / try / except: X = e /
	# if X is not None" skeleton around an AI call.
	uses = {
		mod: {fn.name for fn in _functions(_tree(mod)) if _run_ai_step_errors(fn)}
		for mod in ("analyze.py", "api.py")
	}
	assert uses == {
		"analyze.py": set(),
		"api.py": {"_render_session_report", "ai_refresh_status", "refill_ai_suggestions"},
	}


def test_scanned_modules_and_wrappers_exist():
	# A renamed module or wrapper would silently weaken the rules: fail loudly.
	assert all((_PKG / m).exists() for m in _REQUIRED)
	names = {fn.name for fn in _functions(_tree("analyze.py"))}
	assert _AI_WRAPPERS <= names


_AI_FIX_MODULE_PATH = re.compile(r"optimus\.ai_fix(?:\.\w+)*")


def _imports_ai_fix(tree: ast.Module) -> bool:
	"""True when ``tree`` imports ``optimus.ai_fix`` or a name from it:
	``import optimus.ai_fix``, ``from optimus import ai_fix``,
	``from optimus.ai_fix import x``, their relative forms inside the
	package, or a dotted-path string naming it (``importlib`` /
	``frappe.get_attr``)."""
	for n in ast.walk(tree):
		if isinstance(n, ast.Import):
			if any(a.name == "optimus.ai_fix" or a.name.startswith("optimus.ai_fix.") for a in n.names):
				return True
		elif isinstance(n, ast.ImportFrom):
			module = n.module or ""
			if n.level == 0 and module == "optimus.ai_fix":
				return True
			if n.level == 0 and module == "optimus" and any(a.name == "ai_fix" for a in n.names):
				return True
			if n.level > 0 and (module == "ai_fix" or (not module and any(a.name == "ai_fix" for a in n.names))):
				return True
		elif isinstance(n, ast.Constant) and isinstance(n.value, str) and _AI_FIX_MODULE_PATH.fullmatch(n.value):
			return True
	return False


def _unaudited_ai_fix_importers(scanned: tuple[str, ...]) -> list[str]:
	"""Non-test modules under ``optimus/`` that import ``optimus.ai_fix`` but are
	not scanned by the rules above."""
	out = []
	for path in sorted(_PKG.rglob("*.py")):
		rel = path.relative_to(_PKG).as_posix()
		if rel == "ai_fix.py" or rel.split("/", 1)[0] in ("tests", "tests_integration"):
			continue
		if _imports_ai_fix(ast.parse(path.read_text(encoding="utf-8"), filename=rel)) and rel not in scanned:
			out.append(rel)
	return out


def test_every_module_that_imports_ai_fix_is_audited():
	# The rules only see the modules they scan: a new caller of ai_fix (or a
	# module that grows an ai_fix import) must join _REQUIRED / _OPTIONAL, or
	# its log calls go unchecked.
	offenders = _unaudited_ai_fix_importers(_REQUIRED + _OPTIONAL)
	assert offenders == [], (
		f"these modules import optimus.ai_fix but test_ai_log_audit.py does not scan them: {offenders}. "
		"Add them to _REQUIRED (or _OPTIONAL)."
	)


def test_the_importer_detector_sees_every_import_form():
	forms = (
		"import optimus.ai_fix\n",
		"import optimus.ai_fix as a\n",
		"from optimus import ai_fix\n",
		"from optimus import settings, ai_fix as a\n",
		"from optimus.ai_fix import log_ai_failure\n",
		"def f():\n\tfrom optimus.ai_fix import suggest_fix\n",
		"from . import ai_fix\n",
		"from .ai_fix import AiFixError\n",
		"from ..ai_fix import AiFixError\n",
		"frappe.get_attr('optimus.ai_fix.suggest_fix')\n",
		"importlib.import_module('optimus.ai_fix')\n",
	)
	for src in forms:
		assert _imports_ai_fix(ast.parse(src)), src
	for src in (
		"from optimus import analyze\n", "import optimus.api\n", "x = 'uses optimus.ai_fix here'\n",
		"from optimus.ai_fixes import x\n", "ai_fix = f.get('llm_fix')\n",
	):
		assert not _imports_ai_fix(ast.parse(src)), src


# ---------------------------------------------------------------------------
# The detectors themselves, on small sources
# ---------------------------------------------------------------------------

def _offenders_in(src: str) -> tuple[list[str], list[str]]:
	tree = ast.parse(src)
	loggers = _loggers([tree])
	helpers = set(_AI_WRAPPERS) | _ai_steps(tree)
	rule2 = [fn.name for fn in _ai_functions(tree) if any(_is_log_error(n) for n in ast.walk(fn))]
	return rule2, _handler_offenders("mod.py", tree, loggers, helpers)


def test_a_name_imported_from_ai_fix_makes_a_function_ai_code():
	rule2, _ = _offenders_in(
		"def f():\n"
		"\tfrom optimus.ai_fix import suggest_fix as ask\n"
		"\ttry:\n\t\task({})\n\texcept Exception:\n\t\tpass\n"
		"\tfrappe.log_error(title='x')\n"
	)
	assert rule2 == ["f"]


def test_logging_inside_an_except_block_is_flagged_even_through_a_wrapper():
	_, rule3 = _offenders_in(
		"from optimus import ai_fix\n"
		"def _note(e):\n\tai_fix.log_ai_failure('t', e)\n"
		"def run():\n"
		"\ttry:\n\t\tstep()\n\texcept Exception as e:\n\t\t_note(e)\n"
		"def step():\n\tai_fix.suggest_fix({})\n"
	)
	assert rule3 == ["mod.py:8"]


def test_a_request_inside_an_except_block_is_flagged_even_when_nothing_logs():
	tree = ast.parse(
		"from optimus import ai_fix\n"
		"def f():\n"
		"\ttry:\n\t\tpass\n\texcept Exception:\n\t\tai_fix._call_anthropic('u', '', 'm', 's', [])\n"
		"def g():\n\tai_fix._http_post('u', {}, {}, provider='p', where='w')\n"
		"def h():\n"
		"\ttry:\n\t\tpass\n\texcept Exception:\n\t\tg()\n"
	)
	# h is not AI code itself: it reaches a request through g
	assert _request_offenders("mod.py", tree, _reaching([tree], _SENDERS)) == ["mod.py:f:6", "mod.py:h:13"]
	# nothing here logs, so the logging rule alone would not see them
	assert _handler_offenders("mod.py", tree, _loggers([tree]), set()) == []


def test_record_then_log_after_the_try_passes():
	rule2, rule3 = _offenders_in(
		"from optimus import ai_fix\n"
		"def f():\n"
		"\terror = None\n"
		"\ttry:\n\t\tai_fix.suggest_fix({})\n\texcept Exception as e:\n\t\terror = e\n"
		"\tif error is not None:\n\t\tai_fix.log_ai_failure('t', error)\n"
	)
	assert (rule2, rule3) == ([], [])
