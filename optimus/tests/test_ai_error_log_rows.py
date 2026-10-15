# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""What an operator reads in an AI failure's Error Log row: the failure's ``kind``, whether it is
``fatal``, a short fixed ``hint``, the tokens a failed call was billed (``tokens=``) and, for a
call that went through the parameter ladder, how many posts it took (``attempts=``) and which
parameters were changed (``dropped=``). Never the reply body. The docs' runbook table lists the
same kinds."""

from pathlib import Path

import pytest

from optimus import ai_fix
from optimus.tests import test_ai_error_kinds as _kinds
from optimus.tests import test_ai_log_failure as _log_tests

pytestmark = pytest.mark.rq

# the fixtures of the two suites this one builds on (the HTTP wire fake; the Error Log fake)
wire = _kinds.wire
logs = _log_tests.logs
breadcrumbs = _log_tests.breadcrumbs
KEY = _log_tests.KEY
_rejected = _kinds._rejected

_DOCS = Path(__file__).resolve().parents[2] / "docs" / "AI-FIXING.md"
_ALL_KINDS = {
	"config", "context", "not_eligible", "auth", "quota", "rate_limited", "not_found", "server",
	"bad_request", "refused", "transport", "timeout", "bad_response", "internal", "unknown",
}


def _lines(row):
	return row["message"].split("\n")


@pytest.mark.parametrize("kind", sorted(_ALL_KINDS))
def test_a_row_for_every_kind_carries_kind_fatal_and_its_hint(logs, kind):
	exc = ai_fix.AiFixError("The AI provider said no.", kind=kind)
	ai_fix.log_ai_failure("optimus ai backfill", exc, session_uuid="uuid-1")
	lines = _lines(logs[0])
	assert f"kind={kind}" in lines
	assert f"fatal={kind in ai_fix.AI_FATAL_KINDS}" in lines
	assert f"hint={ai_fix.KIND_HINTS[kind]}" in lines


def test_every_kind_has_one_fixed_plain_hint():
	assert set(ai_fix.KIND_HINTS) == _ALL_KINDS
	for hint in ai_fix.KIND_HINTS.values():
		assert isinstance(hint, str) and 10 < len(hint) <= 120 and "\n" not in hint and "{" not in hint


def test_a_kind_without_a_hint_still_gets_its_row(logs):
	exc = ai_fix.AiFixError("x", kind="martian")
	ai_fix.log_ai_failure("optimus ai backfill", exc)
	lines = _lines(logs[0])
	assert "kind=martian" in lines and f"hint={ai_fix.KIND_HINTS['unknown']}" in lines


def test_an_exception_that_is_not_an_ai_failure_gets_no_kind_lines(logs):
	ai_fix.log_ai_failure("optimus ai backfill", ValueError("boom"), session_uuid="uuid-1")
	text = logs[0]["message"]
	assert "kind=" not in text and "fatal=" not in text and "hint=" not in text and "tokens=" not in text


def test_the_callers_own_context_wins_over_the_derived_lines(logs):
	exc = ai_fix.AiFixError("x", kind="quota")
	ai_fix.log_ai_failure("optimus ai backfill", exc, kind="mine")
	lines = _lines(logs[0])
	assert "kind=mine" in lines and "kind=quota" not in lines


@pytest.mark.parametrize("usage,expected", [
	({"total_tokens": 42}, "tokens=42"),
	({"prompt_tokens": 40, "completion_tokens": 2, "total_tokens": 42}, "tokens=42"),
])
def test_the_tokens_a_failed_call_was_billed_are_on_the_row(logs, usage, expected):
	exc = ai_fix.AiFixError("empty", kind="bad_response", usage=usage)
	ai_fix.log_ai_failure("optimus ai backfill", exc)
	assert expected in _lines(logs[0])


@pytest.mark.parametrize("usage", [None, {}, {"total_tokens": 0}, {"prompt_tokens": 3}])
def test_no_billed_tokens_means_no_tokens_line(logs, usage):
	exc = ai_fix.AiFixError("empty", kind="bad_response", usage=usage)
	ai_fix.log_ai_failure("optimus ai backfill", exc)
	assert not any(line.startswith("tokens=") for line in _lines(logs[0]))


def test_the_http_layers_row_carries_kind_fatal_and_hint(logs):
	exc = ai_fix.AiFixError("quota gone", kind="quota", status_code=429)
	ai_fix._log_http_error("openai", "chat/completions", 429, "", exc=exc, session_uuid="uuid-1")
	lines = _lines(logs[0])
	assert logs[0]["title"] == "optimus ai_fix"
	assert "kind=quota" in lines and "fatal=True" in lines and f"hint={ai_fix.KIND_HINTS['quota']}" in lines
	assert "status=429" in lines and "provider=openai" in lines


def test_a_caller_logging_the_same_failure_again_adds_no_second_row_and_no_duplicate_lines(logs):
	exc = ai_fix.AiFixError("quota gone", kind="quota", status_code=429)
	ai_fix._log_http_error("openai", "chat/completions", 429, "", exc=exc, session_uuid="uuid-1")
	ai_fix.log_ai_failure("optimus ai auto-suggest", exc, session_uuid="uuid-1", finding_type="N+1 Query")
	assert len(logs) == 1
	assert logs[0]["message"].count("kind=quota") == 1


# --- the parameter ladder: attempts= and dropped= ----------------------------------------


def _logged_context(wire):
	"""The ``**context`` of the single HTTP-layer row the wire fixture recorded."""
	assert len(wire.logs) == 1
	args, kwargs = wire.logs[0]
	return kwargs


def _call(model="custom", **kw):
	return ai_fix._call_openai_chat("https://p.invalid/v1", "", model, "system", [], **kw)


def test_a_first_post_failure_says_one_attempt_and_nothing_dropped(wire):
	wire.install(_rejected("invalid x-api-key", status=401))
	with pytest.raises(ai_fix.AiFixError):
		_call()
	context = _logged_context(wire)
	assert context["attempts"] == 1 and context["dropped"] == "none"


def test_the_final_row_of_a_full_ladder_names_the_attempts_and_the_dropped_parameters(wire):
	wire.install(
		_rejected("Unsupported value: 'temperature' does not support 0.1"),
		_rejected("Unsupported parameter: 'max_tokens' is not supported with this model."),
		_rejected("Unsupported value: 'temperature' does not support 0.1"),
	)
	with pytest.raises(ai_fix.AiFixError):
		_call()
	context = _logged_context(wire)
	assert len(wire.posts) == 3
	assert context["attempts"] == 3 and context["dropped"] == "temperature,max_tokens"


def test_a_terminal_failure_after_one_change_is_logged_with_it(wire):
	# the second post fails with a status the ladder does not retry: the HTTP layer logs it
	wire.install(_rejected("Unsupported value: 'temperature' does not support 0.1"), _rejected("no", status=401))
	with pytest.raises(ai_fix.AiFixError) as caught:
		_call()
	assert caught.value.kind == "auth"
	context = _logged_context(wire)
	assert context["attempts"] == 2 and context["dropped"] == "temperature"


@pytest.mark.parametrize("second,kind", [
	(ai_fix.requests.exceptions.ReadTimeout("slow"), "timeout"),
	(ai_fix.requests.exceptions.ConnectionError("down"), "transport"),
	(_kinds.NonJson(200), "bad_response"),
])
def test_every_failure_after_a_change_says_how_far_the_ladder_got(wire, second, kind):
	wire.install(_rejected("Unsupported value: 'temperature' does not support 0.1"), second)
	with pytest.raises(ai_fix.AiFixError) as caught:
		_call()
	assert caught.value.kind == kind
	context = _logged_context(wire)
	assert context["attempts"] == 2 and context["dropped"] == "temperature"


def test_changes_a_re_ask_inherits_are_named_too(wire):
	wire.install(_rejected("nope", status=401))
	with pytest.raises(ai_fix.AiFixError):
		_call(adapted_params=["temperature"])
	assert _logged_context(wire)["dropped"] == "temperature"
	assert "temperature" not in wire.posts[0][1]["json"]


def test_an_exhausted_budget_row_says_how_far_the_ladder_got(wire):
	wire.use_clock()
	wire.install((_rejected("Unsupported value: 'temperature' does not support 0.1"), 61))
	with pytest.raises(ai_fix.AiFixError) as caught:
		_call(timeout=60)
	assert caught.value.kind == "timeout"
	context = _logged_context(wire)
	assert context["attempts"] == 1 and context["dropped"] == "temperature"


def test_the_row_text_carries_attempts_and_dropped(logs):
	exc = ai_fix.AiFixError("rejected", kind="bad_request", status_code=400)
	ai_fix._log_http_error(
		"openai", "chat/completions", 400, "", exc=exc, attempts=3, dropped="temperature,max_tokens",
	)
	lines = _lines(logs[0])
	assert "attempts=3" in lines and "dropped=temperature,max_tokens" in lines


def test_a_real_ladder_failure_writes_attempts_and_dropped_into_the_error_log_row(logs, monkeypatch):
	posts = iter([
		_rejected("Unsupported value: 'temperature' does not support 0.1"),
		_rejected("Unsupported parameter: 'max_tokens' is not supported with this model."),
		_rejected("Unsupported value: 'temperature' does not support 0.1"),
	])
	monkeypatch.setattr(ai_fix.requests, "post", lambda url, **kw: next(posts))
	monkeypatch.setattr(ai_fix, "_current_key_or_empty", lambda: "")
	with pytest.raises(ai_fix.AiFixError):
		_call()
	assert len(logs) == 1
	lines = _lines(logs[0])
	assert "attempts=3" in lines and "dropped=temperature,max_tokens" in lines and "kind=bad_request" in lines
	assert KEY not in logs[0]["message"]


# --- the docs' runbook ------------------------------------------------------------------


def _runbook_rows():
	import re

	rows = {}
	for line in _DOCS.read_text().splitlines():
		match = re.match(r"\| `kind=([a-z_]+)` \|", line)
		if match:
			rows[match.group(1)] = [c.strip() for c in line.strip().strip("|").split("|")]
	return rows


def test_the_runbook_has_a_row_per_kind_and_marks_the_fatal_ones():
	rows = _runbook_rows()
	assert set(rows) == _ALL_KINDS - {"not_eligible"}
	for kind, cells in rows.items():
		assert len(cells) == 4, kind
		fatal_cell = cells[1]
		assert fatal_cell == str(kind in ai_fix.AI_FATAL_KINDS), kind  # the rows' own spelling
		assert cells[2] and cells[3]


def _error_log_pointers(path):
	"""Every "Error Log" pointer in a source file: the text from "Error Log" to the closing quote."""
	import re

	return re.findall(r"Error Log[^\n]*", path.read_text())


_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("path", [
	_ROOT / "analyze.py", _ROOT / "optimus" / "doctype" / "optimus_session" / "optimus_session.js",
])
def test_operators_are_told_to_search_for_the_title_a_provider_failure_row_has(path):
	"""A provider failure is logged by the HTTP layer under the title ``optimus ai_fix``; the step
	title (``optimus ai backfill``, ``optimus ai auto-suggest``) is appended inside that row, and
	the other AI steps write rows of their own. Every AI title starts with ``optimus ai``: a
	pointer to the Error Log names that prefix, which finds them all."""
	# the outer auto-suggest step logs its own unexpected error under its own title
	pointers = [p for p in _error_log_pointers(path) if ("title" in p or "titled" in p) and "(outer)" not in p]
	assert pointers
	for pointer in pointers:
		assert "titles starting with" in pointer and "optimus ai" in pointer, pointer


def test_a_provider_failure_row_has_the_title_operators_are_sent_to(logs):
	exc = ai_fix.AiFixError("rejected", kind="bad_request", status_code=400)
	ai_fix._log_http_error("openai", "chat/completions", 400, "", exc=exc, session_uuid="uuid-1")
	ai_fix.log_ai_failure("optimus ai backfill", exc, session_uuid="uuid-1", finding="F1")
	import frappe

	assert [row["title"] for row in logs] == ["optimus ai_fix"]
	# the step's title and context are appended inside that row
	stored = frappe.db.rows["ERR-0001"]["error"]
	assert "optimus ai backfill" in stored and "finding=F1" in stored
