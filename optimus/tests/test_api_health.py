# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the health() ops endpoint: mock its three query-builder helpers and
pin that health() assembles their results into the documented response shape
(the qb queries themselves are verified on a real bench)."""

from __future__ import annotations

from optimus import api


def test_health_assembles_helper_results(monkeypatch):
	monkeypatch.setattr(api, "_require_profiler_user", lambda: "Administrator", raising=False)
	monkeypatch.setattr(api, "_session_count_by_status", lambda: {"Ready": 3, "Failed": 1}, raising=False)
	monkeypatch.setattr(api, "_session_count_by_severity", lambda: {"High": 2, "None": 1}, raising=False)
	monkeypatch.setattr(
		api, "_session_perf_24h",
		lambda: {"sessions_ready": 3, "analyze_avg_ms": 1200.5, "analyze_max_ms": 3000.0},
		raising=False,
	)

	assert api.health() == {
		"by_status": {"Ready": 3, "Failed": 1},
		"by_top_severity_ready": {"High": 2, "None": 1},
		"last_24h": {"sessions_ready": 3, "analyze_avg_ms": 1200.5, "analyze_max_ms": 3000.0},
	}


def test_health_empty_site(monkeypatch):
	"""No sessions → empty maps + zeroed perf, no crash."""
	monkeypatch.setattr(api, "_require_profiler_user", lambda: "Administrator", raising=False)
	monkeypatch.setattr(api, "_session_count_by_status", lambda: {}, raising=False)
	monkeypatch.setattr(api, "_session_count_by_severity", lambda: {}, raising=False)
	monkeypatch.setattr(
		api, "_session_perf_24h",
		lambda: {"sessions_ready": 0, "analyze_avg_ms": 0.0, "analyze_max_ms": 0.0},
		raising=False,
	)

	out = api.health()
	assert out["by_status"] == {}
	assert out["by_top_severity_ready"] == {}
	assert out["last_24h"] == {"sessions_ready": 0, "analyze_avg_ms": 0.0, "analyze_max_ms": 0.0}
