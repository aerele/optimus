# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Structural audit of optimus/api.py (PR-1 session gate).

Pins, over the parsed source (no bench needed):

* every session- or run-scoped endpoint that writes or spends AI tokens calls its registered gate
  as its first statement after the docstring and local imports, passing ``action=<its own name>``;
* the gates themselves never write;
* every whitelisted function that writes, enqueues, commits or calls the LLM is POST-only;
* every whitelisted parameter is annotated (Frappe semgrep ``missing-argument-type-hint``);
* no endpoint calls another whitelisted endpoint (the nested-gate bug behind C3), except the
  Phase 2 batch retry, which loops over the single-run endpoint;
* no ``@rate_limit`` remains (its ``key=`` is a form field name, so callers could mint buckets);
* the Desk JS reaches every POST-only endpoint through ``frappe.call({method: ...})`` (POST).

``audit`` is a pure function over an ``ast.Module``; the mutation tests edit a parsed copy of
api.py and prove each rule fires.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from optimus.tests.gate_fakes import GATED_ENDPOINTS

APP_DIR = Path(__file__).resolve().parent.parent
API_PATH = APP_DIR / "api.py"
JS_FILES = (
	APP_DIR / "public" / "js" / "floating_widget.js",
	APP_DIR / "optimus" / "doctype" / "optimus_session" / "optimus_session.js",
	APP_DIR / "optimus" / "doctype" / "optimus_settings" / "optimus_settings.js",
)
GATE_FUNCTIONS = ("_session_action_gate", "_ai_session_gate", "_phase2_run_gate")
INTERNAL_RENDERERS = ("_render_session_report", "_rerender_after_ai")
ALLOWED_ENDPOINT_CALLS = frozenset({("retry_phase2_analyzes_batch", "retry_phase2_analyze")})
SESSION_PARAMS = frozenset({"session_uuid", "run_uuid"})
SINK_NAMES = frozenset({
	"safe_commit", "_save_parent_bypassing_perms", "_render_session_report", "_rerender_after_ai",
	"_enqueue_analyze", "_stop_session", "_humanize_steps_core", "_refill_indexes_for_doc",
})
SINK_ATTRS = frozenset({
	("frappe.db", "set_value"), ("frappe.db", "sql"), ("frappe.db", "delete"), ("frappe", "enqueue"),
	("frappe.cache", "set_value"), ("frappe.cache", "rpush"),
	("_lp_capture", "start_line_profile_pass"), ("_lp_capture", "stop_line_profile_pass"),
	("_lp_capture", "cleanup_run"), ("_lp_analyzer", "run_analyze"),
	("_analyze_mod", "_run_ai_backfill"), ("_analyze_mod", "_run_table_index_ai_backfill"),
	("_analyze_mod", "_backfill_ai_suggestions"), ("_analyze_mod", "_render_and_attach_reports"),
	("ai_fix", "suggest_fix"), ("ai_fix", "humanize_steps"), ("ai_fix", "test_connection"),
})
SINK_METHODS = frozenset({"save", "insert", "db_set"})


def _dotted(node) -> str | None:
	if isinstance(node, ast.Name):
		return node.id
	if isinstance(node, ast.Attribute):
		base = _dotted(node.value)
		return f"{base}.{node.attr}" if base else None
	return None


def _whitelist_decorator(fn: ast.FunctionDef):
	for dec in fn.decorator_list:
		target = dec.func if isinstance(dec, ast.Call) else dec
		if _dotted(target) == "frappe.whitelist":
			return dec
	return None


def _is_post_only(dec) -> bool:
	if not isinstance(dec, ast.Call):
		return False
	for kw in dec.keywords:
		if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
			return [e.value for e in kw.value.elts if isinstance(e, ast.Constant)] == ["POST"]
	return False


def _calls(node) -> list[ast.Call]:
	return [n for n in ast.walk(node) if isinstance(n, ast.Call)]


def _sinks(fn: ast.FunctionDef, endpoint_names: frozenset) -> set[str]:
	found = set()
	for call in _calls(fn):
		func = call.func
		if isinstance(func, ast.Name) and (func.id in SINK_NAMES or func.id in endpoint_names):
			found.add(func.id)
		elif isinstance(func, ast.Attribute):
			base = _dotted(func.value)
			if (base, func.attr) in SINK_ATTRS:
				found.add(f"{base}.{func.attr}")
			elif func.attr in SINK_METHODS:
				found.add(f".{func.attr}")
	return found


def _first_effective_stmt(fn: ast.FunctionDef):
	for i, stmt in enumerate(fn.body):
		if i == 0 and isinstance(stmt, ast.Expr) and isinstance(getattr(stmt, "value", None), ast.Constant) and isinstance(stmt.value.value, str):
			continue
		if isinstance(stmt, (ast.Import, ast.ImportFrom)):
			continue
		return stmt
	return None


def _gate_call(stmt):
	value = stmt.value if isinstance(stmt, (ast.Expr, ast.Assign, ast.AnnAssign)) else None
	if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in GATE_FUNCTIONS:
		return value
	return None


def audit(tree: ast.Module) -> list[str]:
	problems: list[str] = []
	funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
	whitelisted = {name: fn for name, fn in funcs.items() if _whitelist_decorator(fn) is not None}
	endpoint_names = frozenset(whitelisted)

	for name, fn in whitelisted.items():
		params = [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]
		for arg in params:
			if arg.annotation is None:
				problems.append(f"{name}: parameter {arg.arg!r} has no type annotation")
		sinks = _sinks(fn, endpoint_names - {name})
		if sinks and not _is_post_only(_whitelist_decorator(fn)):
			problems.append(f"{name}: writes or spends ({', '.join(sorted(sinks))}) but is not methods=['POST']")
		if SESSION_PARAMS & {a.arg for a in params} and sinks and name not in GATED_ENDPOINTS:
			problems.append(f"{name}: session-scoped endpoint writes ({', '.join(sorted(sinks))}) without a registered gate")
		for call in _calls(fn):
			callee = call.func.id if isinstance(call.func, ast.Name) else None
			if callee in endpoint_names and callee != name and (name, callee) not in ALLOWED_ENDPOINT_CALLS:
				problems.append(f"{name}: calls whitelisted endpoint {callee}() (use the internal helper)")

	for name, fn in funcs.items():
		for dec in fn.decorator_list:
			target = dec.func if isinstance(dec, ast.Call) else dec
			if (_dotted(target) or "").endswith("rate_limit"):
				problems.append(f"{name}: uses @rate_limit")

	for name, gate in GATED_ENDPOINTS.items():
		fn = whitelisted.get(name)
		if fn is None:
			problems.append(f"{name}: registered as gated but not a whitelisted function")
			continue
		stmt = _first_effective_stmt(fn)
		call = _gate_call(stmt) if stmt is not None else None
		if call is None or call.func.id != gate:
			problems.append(f"{name}: first statement must call {gate}()")
			continue
		action = next(
			(kw.value.value for kw in call.keywords if kw.arg == "action" and isinstance(kw.value, ast.Constant)),
			None,
		)
		if action != name:
			problems.append(f"{name}: {gate}() must pass action={name!r}, got {action!r}")

	for gname in GATE_FUNCTIONS:
		fn = funcs.get(gname)
		if fn is None:
			problems.append(f"{gname}: missing")
			continue
		if _whitelist_decorator(fn) is not None:
			problems.append(f"{gname}: must not be whitelisted")
		wrote = _sinks(fn, endpoint_names)
		if wrote:
			problems.append(f"{gname}: gates must not write ({', '.join(sorted(wrote))})")

	for internal in INTERNAL_RENDERERS:
		fn = funcs.get(internal)
		if fn is None or _whitelist_decorator(fn) is not None:
			problems.append(f"{internal}: must exist and must not be whitelisted")

	for node in tree.body:
		if isinstance(node, ast.ImportFrom) and node.module == "frappe.rate_limiter":
			problems.append("api.py imports frappe.rate_limiter")
	return problems


def _tree() -> ast.Module:
	return ast.parse(API_PATH.read_text(encoding="utf-8"))


def _fn(tree: ast.Module, name: str) -> ast.FunctionDef:
	return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)


def _stmt(src: str) -> ast.stmt:
	return ast.parse(src).body[0]


def test_api_passes_the_audit():
	assert audit(_tree()) == []


def test_gated_registry_is_the_reviewed_set():
	assert GATED_ENDPOINTS == {
		"refill_ai_suggestions": "_ai_session_gate",
		"regenerate_reports": "_session_action_gate",
		"retry_analyze": "_session_action_gate",
		"start_line_profile_pass": "_session_action_gate",
		"stop_line_profile_pass": "_phase2_run_gate",
		"retry_phase2_analyze": "_phase2_run_gate",
	}


def _insert_first(name, src):
	def mutate(tree):
		_fn(tree, name).body.insert(0, _stmt(src))
	return mutate


def _append(name, src):
	def mutate(tree):
		_fn(tree, name).body.append(_stmt(src))
	return mutate


def _drop_post(name):
	def mutate(tree):
		dec = _whitelist_decorator(_fn(tree, name))
		dec.keywords = [k for k in dec.keywords if k.arg != "methods"]
	return mutate


def _drop_annotation(name, arg_name):
	def mutate(tree):
		for arg in _fn(tree, name).args.args:
			if arg.arg == arg_name:
				arg.annotation = None
	return mutate


def _append_endpoint(src):
	def mutate(tree):
		tree.body.extend(ast.parse(src).body)
	return mutate


def _set_gate_action(name, value):
	def mutate(tree):
		call = _gate_call(_first_effective_stmt(_fn(tree, name)))
		for kw in call.keywords:
			if kw.arg == "action":
				kw.value = ast.Constant(value)
	return mutate


def _add_import(src):
	def mutate(tree):
		tree.body.insert(0, _stmt(src))
	return mutate


MUTANTS = [
	("sink before gate", _insert_first("retry_analyze", 'frappe.db.set_value("Optimus Session", "x", "status", "Stopping")'), "first statement must call"),
	("POST dropped", _drop_post("refill_ai_suggestions"), "not methods=['POST']"),
	("annotation dropped", _drop_annotation("start_line_profile_pass", "auto_expand"), "no type annotation"),
	(
		"ungated session endpoint",
		_append_endpoint('@frappe.whitelist(methods=["POST"])\ndef wipe(session_uuid: str) -> dict:\n\tsafe_commit()\n\treturn {}\n'),
		"without a registered gate",
	),
	("gate writes", _insert_first("_session_action_gate", "safe_commit()"), "gates must not write"),
	("nested endpoint call", _append("refill_ai_suggestions", "regenerate_reports(session_uuid)"), "calls whitelisted endpoint regenerate_reports"),
	("wrong action", _set_gate_action("refill_ai_suggestions", "regenerate_reports"), "must pass action="),
	("rate_limit import", _add_import("from frappe.rate_limiter import rate_limit"), "frappe.rate_limiter"),
]


@pytest.mark.parametrize("label,mutate,expected", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_audit_catches_the_mutant(label, mutate, expected):
	tree = _tree()
	mutate(tree)
	problems = audit(tree)
	assert any(expected in p for p in problems), problems


def test_desk_js_reaches_post_only_endpoints_through_frappe_call():
	tree = _tree()
	post_only = {
		n.name for n in tree.body
		if isinstance(n, ast.FunctionDef) and _is_post_only(_whitelist_decorator(n))
	}
	for path in JS_FILES:
		text = path.read_text(encoding="utf-8")
		assert not re.search(r"""\btype\s*:\s*["']GET["']""", text), path.name
		refs = re.findall(r"optimus\.api\.(\w+)", text)
		via_call = re.findall(r"""method\s*:\s*["']optimus\.api\.(\w+)["']""", text)
		for name in set(refs) & post_only:
			assert refs.count(name) == via_call.count(name), (
				f"{path.name}: {name} is referenced outside frappe.call({{method: ...}})"
			)
