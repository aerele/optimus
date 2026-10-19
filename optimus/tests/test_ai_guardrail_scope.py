"""Scope and HTML recovery regressions through the real answer verifier."""

import pytest

from optimus import ai_guardrails as G
from optimus.tests.test_ai_guardrail_gaps import answer, violations


@pytest.mark.parametrize(
	"code",
	[
		'import frappe as f\nf.db.sql("SELECT name FROM tabUser")',
		'from frappe import db\ndb.sql("SELECT name FROM tabUser")',
		'from frappe.db import sql as q\nq("SELECT name FROM tabUser")',
		'q = frappe.db.sql\nq("SELECT name FROM tabUser")',
		'f = frappe\ndb = f.db\nq = db.sql\nq("SELECT name FROM tabUser")',
		'def f():\n    from frappe import db\n    db.sql("SELECT name FROM tabUser")',
		'from frappe import get_all as read\nread("User", ignore_permissions=True)',
		'from frappe import db\ndb.delete("User", {})',
	],
)
def test_uniform_aliases_reach_the_existing_policy_checks(code):
	assert G.strips_code(violations(["old_work()"], code.splitlines()))


@pytest.mark.parametrize(
	"binding",
	[
		"db = other",
		"for db in values:\n    pass",
		"with manager() as db:\n    pass",
		"try:\n    work()\nexcept Exception as db:\n    pass",
		"del db",
		"if (db := other):\n    pass",
		'match value:\n    case {"db": db}:\n        pass',
	],
)
def test_every_mixed_binding_withholds_policy_shaped_calls(binding):
	code = "db = frappe.db\n" + binding + '\ndb.sql("SELECT name FROM tabUser")'
	assert "unverified-safety" in {v.code for v in violations(["old_work()"], code.splitlines())}


@pytest.mark.parametrize(
	"code",
	[
		'db.sql("hello")',
		'def work(db):\n    db.sql("hello")',
		'for db in values:\n    db.sql("hello")',
		'def work(frappe):\n    frappe.db.sql("hello")',
		'class Example:\n    db = frappe.db\n    def work(self, db):\n        db.sql("hello")',
		"frappe.qb.from_(T).delete().where(T.name == name).run()",
	],
)
def test_unrelated_objects_and_filtered_query_builder_remain_allowed(code):
	assert not G.strips_code(violations(["old_work()"], code.splitlines()))


@pytest.mark.parametrize(
	"code",
	[
		'getattr(frappe, "db").sql(query)',
		'f = frappe\nvars(f)["db"].sql(query)',
		'from frappe import db\nsetattr(db,"sql",replacement)',
		'frappe.__dict__["db"].sql(query)',
		'import operator\noperator.attrgetter("db.sql")(frappe)(query)',
	],
)
def test_dynamic_access_fails_closed(code):
	assert "unverified-safety" in {v.code for v in violations(["old_work()"], code.splitlines())}


@pytest.mark.parametrize(
	"after",
	[
		['allowed = frappe.has_permission("User")', "work()"],
		['allowed = frappe.has_permission("User")', "if allowed:", "    raise PermissionError()"],
		['allowed = frappe.has_permission("User")', "if not allowed:", "    work()"],
	],
)
def test_assigned_permission_denial_cannot_be_removed_or_inverted(after):
	before = ['allowed = frappe.has_permission("User")', "if not allowed:", "    raise PermissionError()"]
	assert "permission-behavior-change" in {v.code for v in violations(before, after)}


@pytest.mark.parametrize(
	"shape",
	[
		lambda code: "```python\n" + code + "\n```",
		lambda code: "> ```python\n" + "\n".join("> " + line for line in code.splitlines()) + "\n> ```",
		lambda code: "\n".join("    " + line for line in code.splitlines()),
	],
)
def test_html_like_strings_do_not_hide_formatted_sql(shape):
	code = 'value = "a <b c> d"\nfrappe.db.sql(f"SELECT {value}")'
	text = "**Diagnosis**: d\n**Fix**\n\n" + shape(code) + "\n\n**Why it works**: w\n**Verify**: v"
	recovered = G._rendered_code(text)
	assert any("a <b c> d" in block for block in recovered)
	assert {"raw-sql", "sql-format-injection"} <= {v.code for v in G.verify_fix(text, source_lines=[])}


def test_only_a_whole_blank_diff_line_loses_its_zero_width_placeholder():
	from optimus.ai_code_html import rendered_code

	assert rendered_code(
		'<pre><code><span class="dh-line">a\u200bb</span><span class="dh-line">\u200b</span><span class="dh-line">x</span></code></pre>'
	) == ["a\u200bb\n\nx"]


def test_malformed_rendered_code_is_withheld(monkeypatch):
	from optimus.renderer import finding_enrichment

	monkeypatch.setattr(finding_enrichment, "_markdown_to_safe_html", lambda text: "<pre><code>work()</pre>")
	assert "unverified-safety" in {v.code for v in G.verify_fix("**Fix**\nwork()", source_lines=[])}


@pytest.mark.parametrize(
	"code",
	[
		"db = frappe.db if flag else other\ndb.sql(query)",
		"db = frappe.db or other\ndb.sql(query)",
		"def work(db=frappe.db):\n    db.sql(query)",
		"def work(*,db=frappe.db):\n    db.sql(query)",
		"from frappe import db\ndef outer():\n    global db\n    db.sql(query)",
		"def outer():\n    db=frappe.db\n    def inner():\n        nonlocal db\n        db.sql(query)",
		"class Example:\n    db=other\n    def method(self):\n        frappe.db.sql(query)",
	],
)
def test_scope_redirects_and_conditional_bindings_cannot_hide_sql(code):
	assert G.strips_code(violations(["old_work()"], code.splitlines()))


def test_global_declaration_does_not_shadow_the_implicit_frappe_root():
	code=['def work():','    global frappe','    frappe.db.sql(query)']
	assert 'raw-sql' in {v.code for v in violations(['old_work()'],code)}


@pytest.mark.parametrize("count,attributes", [(300, 0), (500, 1), (40, 40)])
def test_logical_alias_depth_is_bounded_below_the_ast_size_limit(count, attributes):
	import ast

	code = [f"b{i}=b{i + 1}" + ".db" * attributes for i in range(count)]
	code += [f"b{count}=frappe.db", "b0.sql(query)"]
	assert len(list(ast.walk(ast.parse("\n".join(code))))) < 4000
	text = "**Fix**\n```python\n" + "\n".join(code) + "\n```"
	assert len(text) < 16384
	for result in (G.stored_policy_violations(text), G.verify_fix(text, source_lines=[])):
		assert "unverified-safety" in {v.code for v in result}
		assert G.strips_code(result)
		assert not G.rendered_blocks(G.apply_fallback(text, result))


def test_short_forward_aliases_keep_their_real_identity():
	code = [f"b{i}=b{i + 1}" for i in range(50)] + ["b50=frappe.db", "b0.sql(query)"]
	text = "**Fix**\n```python\n" + "\n".join(code) + "\n```"
	assert {v.code for v in G.stored_policy_violations(text)} == {"raw-sql"}


@pytest.mark.parametrize("count,attributes", [(500, 0), (500, 1), (40, 40)])
def test_alias_limit_fires_before_native_stack_exhaustion(count, attributes):
	import ast
	import sys

	from optimus.ai_aliases import Aliases, AnalysisLimitExceeded

	code = [f"b{i}=b{i + 1}" + ".db" * attributes for i in range(count)]
	tree = ast.parse("\n".join(code + [f"b{count}=frappe.db", "b0.sql(query)"]))
	resolver = Aliases(tree)
	depth, frame = 0, sys._getframe()
	while frame:
		depth += 1
		frame = frame.f_back
	previous = sys.getrecursionlimit()
	try:
		sys.setrecursionlimit(depth + 400)
		with pytest.raises(AnalysisLimitExceeded):
			resolver.values(tree.body[-1].value.func)
	finally:
		sys.setrecursionlimit(previous)
	assert not resolver.cache  # interrupted resolutions never publish partial identities


def test_deep_alias_result_is_withheld_and_memoized_across_report_gates(monkeypatch):
	import json

	from optimus import ai_prompts as P
	from optimus.renderer import finding_enrichment
	from optimus.tests.test_renderer_structure_snapshot import _finding

	code = [f"b{i}=b{i + 1}.db" for i in range(500)] + ["b500=frappe", "b0.sql(query)"]
	text = "**Fix**\n```python\n" + "\n".join(code) + "\n```"
	stored = dict(suggestion=text, prompt_version=P.PROMPT_VERSION)
	calls = []
	check = G._stored_policy_violations
	monkeypatch.setattr(G, "_stored_policy_violations", lambda text: calls.append(text) or check(text))
	with G.render_validation_budget():
		assert P.fix_state(stored)[0] == P.FIX_OUTDATED
		finding = finding_enrichment._finding_to_dict(_finding(llm_fix_json=json.dumps(stored)))
		assert finding["llm_fix"]["policy_codes"] == ["unverified-safety"]
		assert "b0.sql" not in finding["llm_fix"]["suggestion_html"]
		assert len(calls) == 1


def test_normal_query_builder_chain_through_aliases_remains_allowed():
	code = ["a=frappe.qb", "b=a.from_(T)", "c=b.select(T.name)", "c" + ".where(T.name == name)" * 20 + ".run()"]
	text = "**Fix**\n```python\n" + "\n".join(code) + "\n```"
	assert not G.stored_policy_violations(text)


@pytest.mark.parametrize("binding", ["b=a", "b=a if flag else frappe.db", "b=a or frappe.db"])
def test_cyclic_aliases_remain_ambiguous(binding):
	import ast

	from optimus.ai_aliases import Aliases

	tree = ast.parse("a=b\n" + binding + "\na.sql(query)")
	values = Aliases(tree).values(tree.body[-1].value.func.value)
	assert None in values


def test_worst_case_alias_inputs_stay_within_the_strict_work_budget():
	import time

	started = time.perf_counter()
	for count, attributes in [(300, 0), (500, 1), (40, 40)]:
		code = [f"b{i}=b{i + 1}" + ".db" * attributes for i in range(count)] + [f"b{count}=frappe.db", "b0.sql(query)"]
		text = "**Fix**\n```python\n" + "\n".join(code) + "\n```"
		assert "unverified-safety" in {v.code for v in G.stored_policy_violations(text)}
	assert time.perf_counter() - started < 2


def test_branching_aliases_are_bounded_without_exponential_identity_sets():
	import ast
	import time

	from optimus.ai_aliases import Aliases, AnalysisLimitExceeded

	code = ["b0=frappe"]
	for i in range(30):
		code += [f"b{i + 1}=b{i}.left", f"b{i + 1}=b{i}.right"]
	code += ["b30.sql(query)"]
	tree = ast.parse("\n".join(code))
	assert len(list(ast.walk(tree))) < 500
	resolver = Aliases(tree)
	started = time.perf_counter()
	with pytest.raises(AnalysisLimitExceeded):
		resolver.values(tree.body[-1].value.func)
	assert max(map(len, resolver.cache.values())) <= 128
	text = "**Fix**\n```python\n" + "\n".join(code) + "\n```"
	assert "unverified-safety" in {v.code for v in G.stored_policy_violations(text)}
	assert "unverified-safety" in {v.code for v in G.verify_fix(text, source_lines=[])}
	assert time.perf_counter() - started < 2


def test_wide_alias_alternatives_stop_before_combining_the_whole_answer():
	import ast

	from optimus.ai_aliases import Aliases, AnalysisLimitExceeded

	code = "a=" + " or ".join(f"frappe.attr{i}" for i in range(500)) + "\na.sql(query)"
	tree = ast.parse(code)
	assert len(list(ast.walk(tree))) < 4000
	resolver = Aliases(tree)
	with pytest.raises(AnalysisLimitExceeded):
		resolver.values(tree.body[-1].value.func)
	assert not resolver.cache


def test_cached_alias_checks_also_charge_the_aggregate_work_budget():
	import ast

	from optimus.ai_aliases import Aliases, AnalysisLimitExceeded

	tree = ast.parse("a=frappe.db\na.sql(query)")
	resolver = Aliases(tree)
	for _ in range(9998):
		assert resolver.values(tree.body[-1].value.func.value) == {"frappe.db"}
	with pytest.raises(AnalysisLimitExceeded):
		resolver.values(tree.body[-1].value.func.value)


@pytest.mark.parametrize("width", [5, 10, 20, 240])
def test_cached_alias_prefix_growth_has_a_fixed_memory_bound(width):
	import ast
	import time
	import tracemalloc

	from optimus.ai_aliases import Aliases, AnalysisLimitExceeded

	code = ["a=" + " or ".join(f"frappe.x{i}" for i in range(128))]
	for i in range(489):
		previous = f"b{i}" if i else "a"
		code.append(f"b{i + 1}={previous}." + "x" * width)
	code.append("b489.sql(query)")
	text = "**Fix**\n```python\n" + "\n".join(code) + "\n```"
	assert len(text) < 131072
	tree = ast.parse("\n".join(code))
	assert len(list(ast.walk(tree))) < 4000
	resolver = Aliases(tree)
	# Warm ordinary parser/checker imports before measuring untrusted growth.
	G.stored_policy_violations("**Fix**\n```python\nwork()\n```")
	tracemalloc.start()
	try:
		started = time.perf_counter()
		# Match check_code's breadth-first cache warm-up; direct resolution of
		# the final name would only exercise the independent depth cap.
		with pytest.raises(AnalysisLimitExceeded, match="size budget"):
			for node in ast.walk(tree):
				if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
					resolver.values(node)
		assert resolver.identity_chars <= 1048576 + 1024
		assert max(len(value) for values in resolver.cache.values() for value in values if value) <= 1024
		for result in (G.stored_policy_violations(text), G.verify_fix(text, source_lines=[])):
			assert "unverified-safety" in {v.code for v in result}
		peak = tracemalloc.get_traced_memory()[1]
		assert peak < 64 * 1024 * 1024
		assert time.perf_counter() - started < 2
	finally:
		tracemalloc.stop()


def test_oversized_imported_alias_identity_also_fails_closed():
	text = "**Fix**\n```python\nfrom frappe import " + "x" * 1100 + " as alias\nalias.sql(query)\n```"
	assert "unverified-safety" in {v.code for v in G.stored_policy_violations(text)}
