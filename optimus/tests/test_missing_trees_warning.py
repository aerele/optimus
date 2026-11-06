# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The report says when Python call trees are missing, and tells expiry from never captured."""

from datetime import datetime, timedelta, timezone

import pytest

from optimus import analyze, safe_call
from optimus.analyzers.base import AnalyzeContext
from optimus.timing import RECORDING_DATA_TTL_SECONDS

NOW = datetime(2026, 11, 6, 12, 0, 0)
TREE = object()


def _rec(age_seconds, tree=None, **extra):
	return {"time": NOW - timedelta(seconds=age_seconds), "pyi_session": tree, **extra}


@pytest.fixture
def errors(monkeypatch):
	lines = []
	monkeypatch.setattr(safe_call, "log_error_line", lines.append)
	return lines


def _notes(recordings, meta=None):
	context = AnalyzeContext(session_uuid="s1", docname="d1")
	analyze._report_missing_trees(context, recordings, meta or {}, NOW)
	return context.warnings


def test_trees_older_than_the_ttl_are_reported_as_expired_with_the_limit_named_and_an_error_line(errors):
	old = RECORDING_DATA_TTL_SECONDS + 60
	warnings = _notes([_rec(old), _rec(old), _rec(10, TREE)])

	assert len(warnings) == 1
	assert "2 of 3" in warnings[0] and "6 hours" in warnings[0] and "expired" in warnings[0]
	assert "SQL findings are complete" in warnings[0]
	assert len(errors) == 1


def test_trees_that_were_never_captured_get_a_neutral_note_and_no_error(errors):
	warnings = _notes([_rec(10), _rec(20, TREE)])

	assert len(warnings) == 1
	assert "1 of 2" in warnings[0] and "not captured" in warnings[0] and "expired" not in warnings[0]
	assert errors == []


def test_expired_and_never_captured_are_counted_separately(errors):
	warnings = _notes([_rec(RECORDING_DATA_TTL_SECONDS + 1), _rec(5), _rec(5, TREE)])

	assert len(warnings) == 2
	assert "1 of 3" in warnings[0] and "expired" in warnings[0]
	assert "1 of 3" in warnings[1] and "not captured" in warnings[1]
	assert len(errors) == 1


def test_a_recording_exactly_at_the_ttl_is_not_called_expired(errors):
	warnings = _notes([_rec(RECORDING_DATA_TTL_SECONDS)])

	assert "not captured" in warnings[0]
	assert errors == []


def test_a_recording_time_stored_as_text_is_read(errors):
	text_time = (NOW - timedelta(seconds=RECORDING_DATA_TTL_SECONDS + 5)).isoformat(sep=" ")
	warnings = _notes([{"time": text_time, "pyi_session": None}])

	assert "expired" in warnings[0]


def test_a_time_zone_aware_recording_time_is_compared_without_error(errors):
	aware = (NOW - timedelta(seconds=RECORDING_DATA_TTL_SECONDS + 5)).replace(tzinfo=timezone.utc)
	warnings = _notes([{"time": aware, "pyi_session": None}])

	assert "expired" in warnings[0]


def test_an_iso_time_with_an_offset_is_compared_without_error(errors):
	text = (NOW - timedelta(seconds=RECORDING_DATA_TTL_SECONDS + 5)).isoformat() + "+05:30"
	warnings = _notes([{"time": text, "pyi_session": None}])

	assert "expired" in warnings[0]


def test_the_neutral_note_also_names_eviction_and_an_encryption_key_change(errors):
	note = _notes([_rec(10)])[0]

	assert "evicted" in note and "encryption key" in note
	assert "pyinstrument" in note and "Recorder" in note


def test_a_diagnostic_failure_never_fails_analyze_and_is_logged(errors):
	context = AnalyzeContext(session_uuid="s1", docname="d1")

	analyze._note_missing_trees(context, ["not a recording"], {})

	assert context.warnings == []
	assert len(errors) == 1 and "missing-tree check failed" in errors[0]


def test_a_missing_or_unreadable_recording_time_counts_as_not_captured(errors):
	warnings = _notes([{"pyi_session": None}, {"time": "garbage", "pyi_session": None}])

	assert "2 of 2" in warnings[0] and "not captured" in warnings[0]
	assert errors == []


def test_nothing_is_said_when_every_request_has_a_tree(errors):
	assert _notes([_rec(RECORDING_DATA_TTL_SECONDS + 1, TREE), _rec(1, TREE)]) == []
	assert errors == []


def test_nothing_is_said_for_a_session_that_chose_sql_only_capture(errors):
	assert _notes([_rec(RECORDING_DATA_TTL_SECONDS + 1)], {"capture_python_tree": False}) == []
	assert errors == []
