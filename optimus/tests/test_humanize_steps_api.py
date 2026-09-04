# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Surface tests for the v0.6.0 ``humanize_steps`` API endpoint the manual
"rewrite the Steps to Reproduce with the LLM" action on a Optimus Session.

Like ``test_backfill_ai_fixes_api.py`` this is a source-inspection test (the
endpoint needs a live bench for a true integration run): we pin the contract
whitelisted, permission-gated, Ready-only, AI-enabled guard, re-fetches the
recordings, calls ``ai_fix.humanize_steps``, persists ``notes``, re-renders.

The internal mechanics (recording fetch, action build, LLM call, notes
persist) were extracted into ``_humanize_steps_core`` so the new
``refill_ai_suggestions`` endpoint can re-use them; the endpoint-level
checks still cover the validation envelope, and the helper-level checks
pin the mechanical moves.
"""

import os
import re

_API_PATH = os.path.join(os.path.dirname(__file__), "..", "api.py")


def _read_api_source() -> str:
	with open(_API_PATH) as f:
		return f.read()


def _fn_body(name: str) -> str:
	src = _read_api_source()
	start = src.index(f"def {name}(")
	search_from = src.find("\n", start) + 1
	nxt = re.search(r"\n(?:def |@frappe\.whitelist)", src[search_from:])
	end = search_from + (nxt.start() if nxt else len(src) - search_from)
	return src[start:end]


def test_whitelisted_and_signature():
	src = _read_api_source()
	assert re.search(r"@frappe\.whitelist\(\)\s*\ndef humanize_steps", src)
	assert "def humanize_steps(session_uuid: str)" in src


def test_permission_gate_mirrors_download_pdf():
	body = _fn_body("humanize_steps")
	assert "_require_profiler_user()" in body
	assert 'row["user"] != user' in body
	assert '"System Manager" not in roles' in body
	assert 'user != "Administrator"' in body
	assert "frappe.PermissionError" in body


def test_requires_ready_session():
	body = _fn_body("humanize_steps")
	assert 'row["status"] != "Ready"' in body


def test_ai_enabled_guard():
	body = _fn_body("humanize_steps")
	assert "ai_fix.is_available()" in body
	assert "AI Fix Suggestions" in body


def test_fetches_recordings_and_builds_actions():
	# Moved into ``_humanize_steps_core`` so the new
	# ``refill_ai_suggestions`` endpoint can re-use the same logic.
	body = _fn_body("_humanize_steps_core")
	assert "_fetch_recordings(" in body
	assert "_actions_for_humanizer(" in body


def test_calls_humanizer_and_converts_error():
	# The LLM call moved into ``_humanize_steps_core`` (returns a status
	# dict on AiFixError instead of throwing); the endpoint still
	# converts a non-updated status into a ``frappe.throw`` so the
	# legacy contract is preserved end-to-end.
	core_body = _fn_body("_humanize_steps_core")
	assert "ai_fix.humanize_steps(" in core_body
	assert "ai_fix.AiFixError" in core_body
	endpoint_body = _fn_body("humanize_steps")
	assert "frappe.throw(reason)" in endpoint_body


def test_persists_notes_and_re_renders():
	# Persistence moved into ``_humanize_steps_core``; the final
	# re-render stays at the endpoint level.
	core_body = _fn_body("_humanize_steps_core")
	assert 'frappe.db.set_value(' in core_body
	assert '"notes"' in core_body
	assert "_assemble_humanized_notes(" in core_body
	# v0.6.x: explicit commits now route through ``safe_commit`` (rollback
	# guard added in the audit-response round). The intent commit after
	# the set_value is preserved.
	assert ("frappe.db.commit()" in core_body) or ("safe_commit()" in core_body)
	endpoint_body = _fn_body("humanize_steps")
	assert "regenerate_reports(session_uuid)" in endpoint_body
