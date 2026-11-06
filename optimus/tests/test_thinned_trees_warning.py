# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The report says when a request's Python call tree was thinned to fit the size cap."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from optimus import analyze, safe_call
from optimus.analyzers.base import AnalyzeContext


def _rec(every=None, samples=1000):
	tree = SimpleNamespace()
	if every is not None:
		tree.optimus_thinned = {"every": every, "samples": samples}
	return {"pyi_session": tree}


@pytest.fixture
def errors(monkeypatch):
	lines = []
	monkeypatch.setattr(safe_call, "log_error_line", lines.append)
	return lines


def _notes(recordings):
	context = AnalyzeContext(session_uuid="s1", docname="d1")
	analyze._report_thinned_trees(context, recordings)
	return context.warnings


def test_thinned_trees_give_one_warning_with_the_counts_and_the_largest_step():
	recordings = [_rec(8), _rec(2), _rec(), _rec(), {"pyi_session": None}]

	warnings = _notes(recordings)

	assert len(warnings) == 1
	assert "2 of 5 requests" in warnings[0]
	assert "1 in every 8" in warnings[0]
	assert "SQL findings are complete" in warnings[0]
	assert "under a parent function" in warnings[0]


def test_nothing_is_said_when_no_tree_was_thinned():
	assert _notes([_rec(), _rec()]) == []


def test_an_old_blob_without_the_attribute_is_not_thinned():
	assert _notes([{"pyi_session": object()}, {"pyi_session": None}]) == []


def test_a_malformed_attribute_is_ignored():
	tree = SimpleNamespace(optimus_thinned="bad")
	assert _notes([{"pyi_session": tree}]) == []


def test_a_helper_failure_never_fails_analyze(errors, monkeypatch):
	context = AnalyzeContext(session_uuid="s1", docname="d1")
	monkeypatch.setattr(analyze, "_report_thinned_trees", lambda *a: 1 / 0)

	analyze._note_missing_trees(context, [], {})

	assert context.warnings == []
	assert any("thinned-tree check failed" in line for line in errors)


def test_the_never_captured_note_mentions_the_size_limit(errors):
	context = AnalyzeContext(session_uuid="s1", docname="d1")
	analyze._report_missing_trees(context, [{"pyi_session": None}], {}, datetime(2026, 1, 1))
	assert "too large to keep" in context.warnings[0]
