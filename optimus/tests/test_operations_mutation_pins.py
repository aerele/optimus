# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Boundaries that mutation testing showed no test pinned: the retention sweep's batch
size, the N+1 time floor, and the context-truncation ratio."""

import types

from optimus import ai_budget, janitor
from optimus.analyzers import n_plus_one
from optimus.analyzers.base import AnalyzeContext

# --- retention sweep batch size ---------------------------------------------------------


class _Cache:
	def __init__(self):
		self.writes = []

	def set_value(self, key, value, **kw):
		self.writes.append((key, value, kw))


def _sweep(monkeypatch, row_count):
	"""Run ``_sweep_old_sessions`` over ``row_count`` stale rows; return (get_all kwargs, cache)."""
	import frappe

	seen = {}
	cache = _Cache()

	def get_all(doctype, **kw):
		seen.update(kw)
		return [{"name": f"S{i}"} for i in range(min(row_count, kw.get("limit") or row_count))]

	db = types.SimpleNamespace(
		get_all=get_all, count=lambda *a, **k: 250, commit=lambda: None, rollback=lambda: None
	)
	monkeypatch.setattr(
		janitor,
		"frappe",
		types.SimpleNamespace(
			db=db,
			cache=cache,
			conf={},
			logger=lambda *a: types.SimpleNamespace(info=lambda m: None),
			delete_doc=lambda *a, **k: None,
			log_error=lambda **k: None,
		),
	)
	monkeypatch.setattr(janitor, "now_datetime", lambda: __import__("datetime").datetime(2026, 1, 1))
	monkeypatch.setattr(
		"optimus.settings.get_config", lambda: types.SimpleNamespace(session_retention_days=30)
	)
	monkeypatch.setattr(janitor.safe_call, "log_error_line", lambda *a, **k: None)
	janitor._sweep_old_sessions()
	assert frappe is not None
	return seen, cache


def test_the_retention_sweep_asks_for_exactly_the_batch_cap(monkeypatch):
	seen, _ = _sweep(monkeypatch, 3)
	assert seen["limit"] == janitor.MAX_DELETIONS_PER_RUN == 100


def test_a_full_batch_writes_the_backlog_and_a_short_one_resets_it(monkeypatch):
	_, full = _sweep(monkeypatch, 100)
	assert [w[1]["data"] for w in full.writes] == [150]
	assert full.writes[0][2]["expires_in_sec"] == janitor.RETENTION_BACKLOG_TTL_SECONDS
	_, short = _sweep(monkeypatch, 99)
	assert [w[1]["data"] for w in short.writes] == [0]


# --- N+1 time floor ----------------------------------------------------------------------


def _loop(per_query_ms):
	stack = [{"filename": "apps/myapp/foo.py", "lineno": 10, "function": "loop"}]
	calls = [
		{
			"normalized_query": "SELECT * FROM `tab0` WHERE name=?",
			"duration": per_query_ms,
			"stack": stack,
			"exact_copies": 1,
			"normalized_copies": 1,
		}
		for _ in range(20)
	]
	return {
		"uuid": "t",
		"path": "/",
		"method": "GET",
		"cmd": None,
		"event_type": "HTTP Request",
		"duration": 100.0,
		"calls": calls,
	}


def test_a_loop_just_under_the_time_floor_is_not_flagged_and_at_it_is():
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	assert n_plus_one.DEFAULT_MIN_TOTAL_TIME_MS == 20
	assert n_plus_one.analyze([_loop(0.995)], ctx).findings == []  # 19.9 ms
	assert len(n_plus_one.analyze([_loop(1.0)], ctx).findings) == 1  # 20.0 ms


# --- context truncation ratio --------------------------------------------------------------


def test_truncation_is_reported_only_below_the_ratio_of_the_estimate():
	sent_size = 10_000
	estimate = sent_size / ai_budget.CHARS_PER_TOKEN_CENTRAL
	assert ai_budget.TRUNCATION_RATIO == 0.6
	assert not ai_budget.context_truncated({"prompt_tokens": int(0.6 * estimate)}, sent_size)
	assert ai_budget.context_truncated({"prompt_tokens": int(0.6 * estimate) - 1}, sent_size)
