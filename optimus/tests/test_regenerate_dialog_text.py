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


def test_the_pointer_to_refresh_is_for_ready_sessions_only():
	text = _confirm_text()
	pointer = "For new or updated AI suggestions use AI > Refresh AI suggestions (available when the session is Ready)."
	assert pointer in text
	index = text.index(pointer)
	# the pointer sits behind a Ready check, so a Failed session is not sent to a button it does not have
	assert re.search(r'frm\.doc\.status\s*===?\s*"Ready"', text[:index][-300:])


def test_the_dialog_text_is_translatable():
	text = _confirm_text()
	assert text.count("__(") >= 3
	assert "Saved AI suggestions are retained" not in text
