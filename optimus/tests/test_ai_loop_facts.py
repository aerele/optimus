# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""loop facts reach the user message for N+1 Query / Redundant Call /
Hot Line, never for other types, and never from a gapped window."""

from optimus import ai_fix, ai_prompts

_DEMO = [
	"class DemoOrder(Document):",
	"\tdef validate(self):",
	"\t\tfor d in self.items:",
	"\t\t\tuom = frappe.db.get_value(\"Item\", d.item_code, \"stock_uom\")",
	"\t\t\td.uom = uom",
]


def _window(first=40, target_index=3):
	return [
		{"lineno": first + i, "content": text, "is_target": i == target_index}
		for i, text in enumerate(_DEMO)
	]


def _finding(ftype="N+1 Query", window=None):
	return {
		"finding_type": ftype, "severity": "High", "title": "t",
		"technical_detail": {"callsite": {"filename": "apps/myapp/myapp/order.py", "lineno": 43, "function": "validate"}},
		"source_window": window if window is not None else _window(),
	}


def test_loop_facts_text_uses_file_line_numbers():
	text = ai_fix._loop_facts_text(_finding())
	assert text.startswith("Loop facts computed by the profiler from the code shown:")
	assert "starts on line 42" in text
	assert "The call frappe.db.get_value uses these loop variables: d." in text


def test_only_loop_shaped_types_get_facts():
	for ftype in ("N+1 Query", "Redundant Call", "Hot Line"):
		assert ai_fix._loop_facts_text(_finding(ftype))
	for ftype in ("Slow Query", "Missing Index", "Framework N+1"):
		assert ai_fix._loop_facts_text(_finding(ftype)) == ""


def test_gapped_or_targetless_window_gives_nothing():
	gapped = _window()
	gapped[2]["lineno"] += 5
	assert ai_fix._loop_facts_text(_finding(window=gapped)) == ""
	no_target = [dict(r, is_target=False) for r in _window()]
	no_target_finding = _finding(window=no_target)
	no_target_finding["technical_detail"]["callsite"]["lineno"] = 999
	assert ai_fix._loop_facts_text(no_target_finding) == ""


def test_user_message_carries_the_facts_for_n_plus_one_only():
	_system, messages = ai_fix._build_messages(_finding())
	assert "Loop facts computed by the profiler" in messages[-1]["content"]
	_system, messages = ai_fix._build_messages(_finding("Slow Query"))
	assert "Loop facts computed by the profiler" not in messages[-1]["content"]


def test_prompt_version_bumped():
	assert ai_prompts.PROMPT_VERSION == 4
