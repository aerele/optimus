# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The Regenerate Reports confirmation says what the button does and does not do: it re-renders
from stored data, does not re-run the analyzer, does not call the AI provider, keeps the saved
AI suggestions, and (on a Ready session only, where the button exists) points at AI > Refresh AI
suggestions. Desk text goes through ``__()``."""

import re
from pathlib import Path

_JS = Path(__file__).resolve().parents[1] / "optimus" / "doctype" / "optimus_session" / "optimus_session.js"


def _function_body(name):
	source = _JS.read_text()
	start = source.index(f"function {name}(")
	return source[start:source.index("\n}\n", start)]


def _confirm_text():
	return re.sub(r"\s+", " ", _function_body("render_regenerate_report_button"))


def test_the_dialog_says_what_regenerate_does_and_does_not_do():
	text = _confirm_text()
	for sentence in (
		"Re-render the report from stored session data.",
		"This does not re-run the analyzer and does not call the AI provider;",
		"saved AI suggestions are kept as they are.",
	):
		assert f'__("{sentence}' in text or sentence in text, sentence


def test_the_pointer_to_refresh_is_for_ready_sessions_with_ai_enabled_only():
	text = _confirm_text()
	pointer = "For new or updated AI suggestions use AI > Refresh AI suggestions (available when AI is enabled and the session is Ready)."
	assert pointer in text
	index = text.index(pointer)
	# the pointer sits behind a Ready check and the AI switch, so neither a Failed session nor a
	# site with AI off is sent to a button it does not have
	guard = text[:index][-300:]
	assert re.search(r'frm\.doc\.status\s*===?\s*"Ready"', guard)
	assert "frm._optimus_ai_enabled" in guard


def test_the_ai_switch_the_pointer_reads_is_the_one_the_ai_button_uses():
	"""``render_ai_buttons`` asks ``ai_capabilities`` whether the master AI switch is on; it
	records the answer on the form (False until it is known, and on a non-Ready session)."""
	body = re.sub(r"\s+", " ", _function_body("render_ai_buttons"))
	reset = body.index("frm._optimus_ai_enabled = false;")
	assert reset < body.index("if (frm.is_new()) return;")
	assert "frm._optimus_ai_enabled = true;" in body[body.index("if (!c.enabled) return;"):]


def test_the_dialog_text_is_translatable():
	text = _confirm_text()
	assert text.count("__(") >= 3
	assert "Saved AI suggestions are retained" not in text
