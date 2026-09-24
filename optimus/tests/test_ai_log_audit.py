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
2. In the scanned modules (``analyze.py``, ``api.py``, and ``ai_jobs.py`` as
   soon as it exists) an AI function never calls ``frappe.log_error``. An AI
   function references the ``ai_fix`` module or any name imported from
   ``optimus.ai_fix``.
3. No logging call runs inside an ``except`` handler of any ``ai_fix.py``
   function, of any AI function, or of any ``try`` whose body runs an AI
   step (an AI function that uses ai_fix for more than ``log_ai_failure``,
   or a wrapper in ``_AI_WRAPPERS``). This reaches ``analyze.run``, which
   keeps ``frappe.log_error`` for its own non-AI failures. A logging
   call is a call to ``log_error``, ``log_ai_failure`` or any function of
   these modules that reaches one of them (``_log_http_error``,
   ``_http_post``, ``suggest_fix``, ...). Record the exception inside the
   handler; log, retry or raise after the ``try``.
"""

import ast
from pathlib import Path

_PKG = Path(__file__).resolve().parents[1]
_REQUIRED = ("analyze.py", "api.py")
_OPTIONAL = ("ai_jobs.py",)  # scanned as soon as a later PR adds it
_AI_WRAPPERS = frozenset({"_backfill_ai_suggestions"})  # analyze.py; calls _run_ai_backfill
_BASE_LOGGERS = frozenset({"log_error", "log_ai_failure"})
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


def _loggers(trees: list[ast.Module]) -> set[str]:
	"""``log_error``, ``log_ai_failure`` and every function in ``trees`` that
	reaches one of them (fixpoint over direct calls, matched by name)."""
	loggers = set(_BASE_LOGGERS)
	functions = [fn for tree in trees for fn in _functions(tree)]
	grew = True
	while grew:
		grew = False
		for fn in functions:
			if fn.name in loggers:
				continue
			if any(isinstance(n, ast.Call) and _callee(n) in loggers for n in _own_nodes(fn)):
				loggers.add(fn.name)
				grew = True
	return loggers


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


def _logging_in_handlers(scope: ast.AST, loggers: set[str]) -> list[int]:
	"""Line numbers of logging calls inside any ``except`` handler of ``scope``
	(nested functions excluded)."""
	lines = []
	for node in _own_nodes(scope):
		if isinstance(node, ast.ExceptHandler):
			lines += [
				n.lineno for n in _own_nodes(node)
				if isinstance(n, ast.Call) and _callee(n) in loggers
			]
	return lines


def _handler_offenders(module: str, tree: ast.Module, loggers: set[str], helpers: set[str]) -> list[str]:
	"""Rule 3 for one module."""
	offenders: set[str] = set()
	scopes = _functions(tree) if module == "ai_fix.py" else _ai_functions(tree)
	for fn in scopes:
		offenders |= {f"{module}:{fn.name}:{line}" for line in _logging_in_handlers(fn, loggers)}
	for node in ast.walk(tree):
		if isinstance(node, _TRIES) and _unguarded_calls(node.body) & helpers:
			for handler in node.handlers:
				offenders |= {
					f"{module}:{n.lineno}" for n in [handler, *_own_nodes(handler)]
					if isinstance(n, ast.Call) and _callee(n) in loggers
				}
	return sorted(offenders)


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


def test_run_unbinds_each_ai_error_once_logged():
	# analyze.run's outer handler logs a later non-AI failure with Frappe's
	# with-context traceback, which prints run's locals. An AI step's error,
	# once logged through _log_ai_step_failure, must not stay bound there: a
	# prompt builder's exception can carry prompt text in its args.
	run = next(fn for fn in _functions(_tree("analyze.py")) if fn.name == "run")
	logged, offenders = 0, []
	for node in ast.walk(run):
		for field in ("body", "orelse", "finalbody"):
			block = getattr(node, field, None)
			if not isinstance(block, list):
				continue
			for i, stmt in enumerate(block):
				call = stmt.value if isinstance(stmt, ast.Expr) else None
				if not (isinstance(call, ast.Call) and _callee(call) == "_log_ai_step_failure"):
					continue
				logged += 1
				error = call.args[1].id if len(call.args) > 1 and isinstance(call.args[1], ast.Name) else None
				nxt = block[i + 1] if i + 1 < len(block) else None
				unbound = (
					isinstance(nxt, ast.Assign) and len(nxt.targets) == 1
					and isinstance(nxt.targets[0], ast.Name) and nxt.targets[0].id == error
					and isinstance(nxt.value, ast.Constant) and nxt.value.value is None
				)
				if not unbound:
					offenders.append(stmt.lineno)
	assert logged >= 2, "analyze.run no longer logs its AI steps through _log_ai_step_failure"
	assert offenders == [], f"set the logged error to None right after _log_ai_step_failure: analyze.py:{offenders}"


def test_scanned_modules_and_wrappers_exist():
	# A renamed module or wrapper would silently weaken the rules: fail loudly.
	assert all((_PKG / m).exists() for m in _REQUIRED)
	names = {fn.name for fn in _functions(_tree("analyze.py"))}
	assert _AI_WRAPPERS <= names


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


def test_record_then_log_after_the_try_passes():
	rule2, rule3 = _offenders_in(
		"from optimus import ai_fix\n"
		"def f():\n"
		"\terror = None\n"
		"\ttry:\n\t\tai_fix.suggest_fix({})\n\texcept Exception as e:\n\t\terror = e\n"
		"\tif error is not None:\n\t\tai_fix.log_ai_failure('t', error)\n"
	)
	assert (rule2, rule3) == ([], [])
