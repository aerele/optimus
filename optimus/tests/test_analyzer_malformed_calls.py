"""Malformed call entries must not suppress the valid calls in a recording."""

import copy
import json

import pytest

from optimus.analyzers import call_tree, index_suggestions, per_action, table_breakdown
from optimus.tests.test_index_suggestions import _FakeDBIndex, _install_fake_recorder_module


@pytest.mark.parametrize("analyzer", [call_tree, index_suggestions, table_breakdown, per_action])
@pytest.mark.parametrize("bad", [None, "bad-call", 42, False, [], ["stack"]])
def test_malformed_entries_are_skipped_without_losing_valid_calls(analyzer, bad, monkeypatch, empty_context):
	query = "SELECT name FROM tabLead WHERE status = 'Open'"
	valid = {"query": query, "normalized_query": query, "duration": 23.0, "stack": []}
	hook = {**valid, "duration": 1000, "stack": [{"filename": "apps/optimus/optimus/error_log_mask.py"}]}
	recording = {"uuid": "mixed", "duration": 50, "path": "/app/lead", "calls": [valid, hook, valid]}
	optimized = []
	def optimize(query):
		optimized.append(query)
		return _FakeDBIndex("tabLead", "status")
	_install_fake_recorder_module(monkeypatch, optimize)
	baseline = analyzer.analyze([copy.deepcopy(recording)], empty_context)
	recording["calls"] = [bad, valid, hook, bad, valid, bad]
	optimized.clear()
	actual = analyzer.analyze([recording], empty_context)
	assert actual == baseline
	if analyzer is call_tree:
		assert actual.aggregate["total_sql_ms"] == 46.0
	elif analyzer is per_action:
		assert actual.actions[0]["queries_count"] == 2
		assert actual.actions[0]["query_time_ms"] == 46.0
	elif analyzer is table_breakdown:
		assert actual.aggregate["table_breakdown"][0]["queries"] == 2
		assert actual.aggregate["table_breakdown"][0]["consolidated_time_ms"] == 46.0
	else:
		assert optimized == [query]
		finding = next(f for f in actual.findings if f["finding_type"] == "Missing Index")
		assert json.loads(finding["technical_detail_json"])["affected_query_count"] == 2
