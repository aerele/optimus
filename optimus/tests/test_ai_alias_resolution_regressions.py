"""Unsafe suggestions must retain their actual Frappe identity across scopes/imports."""

import ast
import json

import pytest

from optimus import ai_guardrails as G
from optimus import ai_prompts as P
from optimus.ai_aliases import Aliases
from optimus.renderer import _internal, finding_enrichment
from optimus.tests.test_ai_guardrail_gaps import H
from optimus.tests.test_renderer_structure_snapshot import _finding
from optimus.tests.test_renderer_v3 import _fake_doc

IMPORTS = [
	'frappe = __import__("frappe")',
	'frappe = importlib.import_module("frappe")',
	'import importlib\nfrappe = importlib.import_module("frappe")',
	'import importlib as modules\nfrappe = modules.import_module("frappe")',
	'frappe = importlib.__import__("frappe")',
	'from importlib import __import__ as load\nfrappe = load("frappe")',
	'from importlib import import_module as load\nfrappe = load("frappe")',
	'load = __import__\nfrappe = load("frappe")',
	'from builtins import __import__ as load\nfrappe = load("frappe")',
	'import builtins as b\nfrappe = b.__import__("frappe")',
]

NESTED_CLASS = '''import frappe
class Outer:
    frappe = None
    class Inner:
        frappe.db.sql(query)
'''


def _assert_withheld(code, expected):
	text = H.format("```python\n" + code + "\n```")
	for result in (G.stored_policy_violations(text), G.verify_fix(text, source_lines=[])):
		assert expected in {v.code for v in result}
		assert G.strips_code(result)
		assert not G.rendered_blocks(G.apply_fallback(text, result))
	stored = dict(suggestion=text, prompt_version=P.PROMPT_VERSION, completion_status="complete")
	assert P.fix_state(stored)[0] == P.FIX_OUTDATED
	child = _finding(llm_fix_json=json.dumps(stored))
	before = child.llm_fix_json
	finding = finding_enrichment._finding_to_dict(child)
	assert expected in finding["llm_fix"]["policy_codes"]
	assert not G.rendered_blocks(finding["llm_fix"]["suggestion_html"])
	assert child.llm_fix_json == before
	return child


@pytest.mark.parametrize("binding", IMPORTS)
@pytest.mark.parametrize("call,expected", [
	('frappe.db.delete("User", {})', "unbounded-write"),
	('frappe.db.sql(query)', "raw-sql"),
])
def test_literal_dynamic_frappe_import_cannot_hide_policy_calls(binding, call, expected):
	_assert_withheld(binding + "\n" + call, expected)


def test_nested_class_body_skips_outer_class_namespace():
	_assert_withheld(NESTED_CLASS, "raw-sql")


@pytest.mark.parametrize("code", [
	'frappe = __import__("frappe")\nfrappe.db.delete("User", {})',
	NESTED_CLASS,
])
def test_real_report_withholds_both_alias_regressions_without_writes(monkeypatch, code):
	import frappe

	from optimus import ai_fix

	attempts = []

	def forbidden(*args, **kwargs):
		attempts.append(True)
		raise AssertionError("report attempted a write or model request")

	previous_db = frappe.db

	class ReadOnlyDB:
		def __getattr__(self, name):
			if name in {"set_value", "sql", "commit", "rollback", "insert", "delete"}:
				return forbidden
			try:
				return getattr(previous_db, name)
			except RuntimeError:
				raise AttributeError(name) from None

	monkeypatch.setattr(frappe, "db", ReadOnlyDB())
	monkeypatch.setattr(ai_fix, "_http_post", forbidden)
	text = H.format("```python\n" + code + "\n```")
	child = _finding(llm_fix_json=json.dumps(dict(suggestion=text, prompt_version=P.PROMPT_VERSION)))
	before = child.llm_fix_json
	report = _internal.render(_fake_doc(findings=[child]), [], generated_at="2026-10-19T00:00:00")
	assert "Code withheld" in report
	assert 'frappe.db.delete' not in report and 'frappe.db.sql' not in report
	assert child.llm_fix_json == before
	assert not attempts


@pytest.mark.parametrize("binding,identity", [
	('f=__import__("frappe.db")', "frappe"),
	('f=__import__("frappe.db", fromlist=["delete"])', "frappe.db"),
	('f=__import__("frappe.db", None, None, ("delete",), 0)', "frappe.db"),
	('f=importlib.import_module(name="frappe.db")', "frappe.db"),
])
def test_dynamic_import_identity_matches_python_return_shape(binding, identity):
	tree = ast.parse(binding + "\nf")
	assert Aliases(tree).values(tree.body[-1].value) == {identity}


@pytest.mark.parametrize("binding", [
	'f=__import__(module_name)',
	'f=importlib.import_module(module_name)',
	'f=__import__("frappe.db", fromlist=unknown)',
	'f=__import__("frappe", level=relative)',
])
def test_unknown_import_shape_cannot_be_classified_as_unrelated(binding):
	_assert_withheld(binding + '\nf.db.delete("User", {})', "unverified-safety")


@pytest.mark.parametrize("code", [
	'frappe=__import__("other")\nfrappe.db.delete("User", {})',
	'frappe=importlib.import_module("other")\nfrappe.db.delete("User", {})',
	'def work(__import__):\n    frappe=__import__("frappe")\n    frappe.db.delete("User", {})',
	'importlib=other\nfrappe=importlib.import_module("frappe")\nfrappe.db.delete("User", {})',
	'class Outer:\n    frappe=None\n    class Inner:\n        frappe=other\n        frappe.db.sql(query)',
	'frappe=other\nclass Outer:\n    import frappe\n    class Inner:\n        frappe.db.sql(query)',
])
def test_unrelated_imports_and_inner_class_bindings_remain_unrelated(code):
	assert not G.strips_code(G.stored_policy_violations(H.format("```python\n" + code + "\n```")))


def test_class_in_function_keeps_function_binding_while_skipping_outer_class():
	code = '''def outer():
    import frappe
    class Outer:
        frappe = None
        class Inner:
            frappe.db.delete("User", {})
'''
	_assert_withheld(code, "unbounded-write")


def test_nested_class_bases_still_evaluate_in_outer_class_namespace():
	tree = ast.parse('import frappe\nclass Outer:\n    frappe=other\n    class Inner(frappe.Base):\n        frappe.db.sql(query)')
	resolver = Aliases(tree)
	inner = tree.body[-1].body[-1]
	assert resolver.values(inner.bases[0].value) == {None}
	assert resolver.values(inner.body[0].value.func.value.value) == {"frappe"}


@pytest.mark.parametrize("binding", [
	'load=__import__\nload=other\nf=load("frappe")',
	'f=__import__("frappe.db", **options)',
	'f=__import__("frappe.db", *options)',
	'f=__import__("frappe.db", fromlist=[*unknown])',
])
def test_ambiguous_loader_or_options_cannot_hide_an_unbounded_write(binding):
	_assert_withheld(binding + '\nf.delete("User", {})', "unverified-safety")


@pytest.mark.parametrize("binding", IMPORTS)
def test_harmless_dynamic_imports_remain_displayable(binding):
	text = H.format("```python\n" + binding + '\nfrappe.get_doc("User", name)\n```')
	assert not G.strips_code(G.stored_policy_violations(text))
	assert not G.strips_code(G.verify_fix(text, source_lines=[]))
	assert P.fix_state(dict(suggestion=text, prompt_version=P.PROMPT_VERSION))[0] == P.FIX_CURRENT


@pytest.mark.parametrize("binding", [
	'f=importlib.import_module(".", "frappe")',
	'f=importlib.import_module(".", package=pkg)',
	'f=__import__("", {"__package__": "frappe"}, None, (), 1)',
	'f=__import__("", **options)',
	'f=importlib.import_module(".", *options)',
])
def test_relative_or_unresolved_import_arguments_cannot_prove_unrelated(binding):
	_assert_withheld(binding + '\nf.db.delete("User", {})', "unverified-safety")


def test_importlib_star_import_cannot_hide_a_frappe_loader():
	_assert_withheld('from importlib import *\nf=import_module("frappe")\nf.db.delete("User", {})', "unverified-safety")


@pytest.mark.parametrize("binding", [
	'f=importlib.import_module(".x", "other")',
	'f=__import__("x", level=0)',
])
def test_known_unrelated_relative_or_absolute_imports_stay_displayable(binding):
	assert not G.strips_code(G.stored_policy_violations(H.format("```python\n" + binding + '\nf.db.delete("User", {})\n```')))
