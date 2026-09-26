# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The whitelisted AI surface of optimus.api after PR-1.

* Every whitelisted AI endpoint has a caller in Optimus's own Desk JS, or sits on
  ``AI_ENDPOINT_ALLOWLIST`` with a written reason. An endpoint nothing calls is attack surface
  and maintenance cost (the four PR-1 deleted had no caller in Optimus, the other bench apps or
  the docs).
* The four deleted endpoints stay deleted; their library functions in ``ai_fix`` stay.
* ``test_ai_connection`` is System-Manager-only and limited per user; ``ai_capabilities`` is a
  read-only role-gated GET.
* ``_humanize_steps_core`` (kept: ``refill_ai_suggestions`` uses it) persists the notes and the
  token count, and turns an AI error into a reason.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from optimus import ai_fix, api
from optimus.tests.gate_fakes import (
	DOCNAME,
	FakePermissionError,
	FakeRateLimitExceededError,
	fake_session_doc,
	install,
	install_module,
	make_fake_frappe,
)

APP_DIR = Path(__file__).resolve().parent.parent
API_PATH = APP_DIR / "api.py"
DELETED = ("suggest_fix", "backfill_ai_fixes", "humanize_steps", "suggest_index")

# name -> why this whitelisted AI endpoint has no caller in Optimus's own JS. Keep it empty
# unless an endpoint exists for scripts on purpose; a stale entry fails the test below.
AI_ENDPOINT_ALLOWLIST: dict[str, str] = {}

_AI_NAMES = frozenset({"ai_fix", "_ai_session_gate", "_AI_LIMITS"})


def _is_whitelisted(fn: ast.FunctionDef) -> bool:
	for dec in fn.decorator_list:
		target = dec.func if isinstance(dec, ast.Call) else dec
		if ast.unparse(target) == "frappe.whitelist":
			return True
	return False


def ai_endpoints(tree: ast.Module) -> set[str]:
	"""Whitelisted functions that are part of the AI surface: an ``ai`` word in the name, or a
	body that uses ``ai_fix`` (attribute or import), ``_ai_session_gate`` or ``_AI_LIMITS``."""
	found = set()
	for fn in tree.body:
		if not isinstance(fn, ast.FunctionDef) or not _is_whitelisted(fn):
			continue
		uses_ai = "ai" in fn.name.split("_")
		for node in ast.walk(fn):
			if isinstance(node, ast.Name) and node.id in _AI_NAMES:
				uses_ai = True
			elif isinstance(node, ast.ImportFrom) and (
				node.module == "optimus.ai_fix"
				or (node.module == "optimus" and any(a.name == "ai_fix" for a in node.names))
			):
				uses_ai = True
		if uses_ai:
			found.add(fn.name)
	return found


def js_api_refs() -> set[str]:
	refs = set()
	for path in APP_DIR.rglob("*.js"):
		if "node_modules" in path.parts:
			continue
		refs |= set(re.findall(r"optimus\.api\.(\w+)", path.read_text(encoding="utf-8")))
	return refs


def unreferenced_ai_endpoints(tree: ast.Module, refs: set[str], allowlist: dict[str, str]) -> list[str]:
	"""Pure: AI endpoints with neither a JS caller nor an allowlist reason, plus stale allowlist
	entries (no longer an AI endpoint, or now called from JS)."""
	endpoints = ai_endpoints(tree)
	problems = [f"{n}: no Desk JS caller and not allowlisted" for n in sorted(endpoints - refs - set(allowlist))]
	for name in sorted(allowlist):
		if name not in endpoints:
			problems.append(f"{name}: allowlisted but not a whitelisted AI endpoint")
		elif name in refs:
			problems.append(f"{name}: allowlisted but called from JS (drop the entry)")
	return problems


def _tree() -> ast.Module:
	return ast.parse(API_PATH.read_text(encoding="utf-8"))


def test_every_ai_endpoint_has_a_js_caller_or_a_reason():
	assert unreferenced_ai_endpoints(_tree(), js_api_refs(), AI_ENDPOINT_ALLOWLIST) == []


def test_the_inventory_sees_the_current_ai_endpoints():
	assert {"refill_ai_suggestions", "test_ai_connection", "ai_capabilities"} <= ai_endpoints(_tree())


@pytest.mark.parametrize(
	"src",
	[
		'@frappe.whitelist(methods=["POST"])\ndef humanize_steps(session_uuid: str) -> dict:\n\tfrom optimus import ai_fix\n\treturn {}\n',
		'@frappe.whitelist()\ndef ai_probe() -> dict:\n\treturn {}\n',
		'@frappe.whitelist(methods=["POST"])\ndef nudge(session_uuid: str) -> dict:\n\t_ai_session_gate(session_uuid, section=None, action="nudge")\n\treturn {}\n',
	],
	ids=["imports_ai_fix", "ai_in_name", "calls_ai_gate"],
)
def test_an_uncalled_ai_endpoint_is_caught(src):
	tree = _tree()
	tree.body.extend(ast.parse(src).body)
	name = tree.body[-1].name
	problems = unreferenced_ai_endpoints(tree, js_api_refs(), AI_ENDPOINT_ALLOWLIST)
	assert any(p.startswith(f"{name}:") for p in problems), problems


def test_stale_allowlist_entries_are_caught():
	problems = unreferenced_ai_endpoints(
		_tree(), js_api_refs(), {"refill_ai_suggestions": "x", "no_such_endpoint": "y"}
	)
	assert "refill_ai_suggestions: allowlisted but called from JS (drop the entry)" in problems
	assert "no_such_endpoint: allowlisted but not a whitelisted AI endpoint" in problems


@pytest.mark.parametrize("name", DELETED)
def test_unused_ai_endpoints_are_deleted(name):
	assert not hasattr(api, name), f"api.{name} was deleted in PR-1; use refill_ai_suggestions"


@pytest.mark.parametrize("name", ["suggest_fix", "humanize_steps"])
def test_the_ai_library_functions_stay(name):
	assert callable(getattr(ai_fix, name))


# --- test_ai_connection ----------------------------------------------------------------------


@pytest.fixture
def probe(monkeypatch):
	def _make(roles, conf=None):
		fake = make_fake_frappe(user="manager@example.com", roles=roles, conf=conf)
		install(monkeypatch, fake)
		calls, marks = [], []
		monkeypatch.setattr(
			ai_fix, "test_connection",
			lambda: calls.append(True) or {"ok": True, "message": "Reachable.", "model": "m"},
		)
		install_module(monkeypatch, "optimus.analyze", SimpleNamespace(_mark_ai_spend_session=lambda u: marks.append(u)))
		return fake, calls, marks

	return _make


def test_system_manager_can_probe(probe):
	_, calls, marks = probe(("System Manager",))
	assert api.test_ai_connection() == {"ok": True, "message": "Reachable.", "model": "m"}
	assert calls == [True] and marks == [None]


def test_non_manager_is_refused_without_probing_or_counting(probe):
	fake, calls, marks = probe(("Optimus User",))
	with pytest.raises(FakePermissionError):
		api.test_ai_connection()
	assert calls == [] and marks == [] and fake.cache.calls == []
	assert fake.spies.throws[-1]["title"] == "Optimus"


def test_probe_is_rate_limited_per_user(probe):
	probe(("System Manager",), conf={"optimus_rate_limits": {"test_ai_connection": [1, 60]}})
	api.test_ai_connection()
	with pytest.raises(FakeRateLimitExceededError):
		api.test_ai_connection()


# --- ai_capabilities -------------------------------------------------------------------------


def test_ai_capabilities_needs_the_profiler_role(monkeypatch):
	install(monkeypatch, make_fake_frappe(roles=()))
	with pytest.raises(FakePermissionError):
		api.ai_capabilities()


def test_ai_capabilities_reports_the_toggles_without_writing(monkeypatch):
	fake = make_fake_frappe()
	install(monkeypatch, fake)
	cfg = SimpleNamespace(ai_enabled=True, ai_suggest_findings=True, ai_suggest_indexes=False, ai_humanize_steps=True)
	monkeypatch.setattr("optimus.settings.get_config", lambda: cfg)
	assert api.ai_capabilities() == {"enabled": True, "findings": True, "indexes": False, "humanize": True}
	assert fake.spies.set_value == [] and fake.cache.calls == []


# --- _humanize_steps_core (kept: refill_ai_suggestions uses it) ------------------------------


@pytest.fixture
def core(monkeypatch):
	fake = make_fake_frappe()
	install(monkeypatch, fake)
	commits, logged = [], []
	monkeypatch.setattr(api, "safe_commit", lambda: commits.append(True))
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda title, exc=None, **kw: logged.append(title))
	install_module(monkeypatch, "optimus.analyze", SimpleNamespace(
		_mark_ai_spend_session=lambda session_uuid: None,
		_fetch_recordings=lambda uuids, recordings_bundle=None: [{"uuid": u} for u in uuids],
		_load_recordings_bundle=lambda d: None,
		_actions_for_humanizer=lambda recordings: [{"method": "POST", "path": "/api/method/x"}] if recordings else [],
		_assemble_humanized_notes=lambda md: "NOTES\n" + md,
	))
	doc = fake_session_doc(actions=[SimpleNamespace(recording_uuid="rec-1")])
	return SimpleNamespace(fake=fake, commits=commits, logged=logged, doc=doc)


def test_humanize_core_persists_the_notes_and_the_tokens(core, monkeypatch):
	def humanize(actions, *, session_title=None, usage_out=None, **kw):
		assert session_title == "Checkout flow" and actions
		usage_out["total_tokens"] = 42
		return "1. Open the Sales Invoice form"

	monkeypatch.setattr(ai_fix, "humanize_steps", humanize)
	assert api._humanize_steps_core(core.doc, title="Checkout flow") == {"updated": True, "reason": None}
	((args, _kwargs),) = core.fake.spies.set_value
	assert args == (
		"Optimus Session", DOCNAME,
		{"notes": "NOTES\n1. Open the Sales Invoice form", "ai_steps_tokens": 42},
	)
	assert core.commits == [True]


def test_humanize_core_turns_an_ai_error_into_a_reason(core, monkeypatch):
	def fail(actions, **kw):
		raise ai_fix.AiFixError("provider said no")

	monkeypatch.setattr(ai_fix, "humanize_steps", fail)
	assert api._humanize_steps_core(core.doc, title=None) == {"updated": False, "reason": "provider said no"}
	assert core.fake.spies.set_value == [] and core.commits == []
