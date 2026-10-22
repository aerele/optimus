# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Runtime smoothing for analyze: lowering the analyze worker's CPU priority
(os.nice, gated to the async path, sticky per-process) so a heavy analyze
doesn't starve live web traffic, plus optionally throttling the
EXPLAIN/sqlparse burst in _enrich_recordings. Both default to inert and are
result-invariant.
"""

import inspect
import os
import re
import sys
import types

import frappe
import pytest

from optimus import analyze

# ---------------------------------------------------------------------------
# M5 os.nice
# ---------------------------------------------------------------------------

class TestApplyNice:
	def test_calls_os_nice_with_conf_value(self, monkeypatch):
		monkeypatch.setattr(frappe, "conf",
			types.SimpleNamespace(get=lambda k, d=None: 7 if k == "optimus_analyze_nice" else d),
			raising=False)
		recorded = []
		monkeypatch.setattr(os, "nice", lambda inc: recorded.append(inc))
		analyze._apply_nice()
		assert recorded == [7]

	def test_zero_is_noop(self, monkeypatch):
		monkeypatch.setattr(frappe, "conf",
			types.SimpleNamespace(get=lambda k, d=None: 0), raising=False)
		recorded = []
		monkeypatch.setattr(os, "nice", lambda inc: recorded.append(inc))
		analyze._apply_nice()
		assert recorded == []

	def test_swallows_oserror(self, monkeypatch):
		monkeypatch.setattr(frappe, "conf",
			types.SimpleNamespace(get=lambda k, d=None: 5), raising=False)
		def boom(inc):
			raise PermissionError("nope")
		monkeypatch.setattr(os, "nice", boom)
		analyze._apply_nice()  # must not raise

	def test_run_applies_nice_gated_to_async(self):
		src = inspect.getsource(analyze.run)
		assert "_apply_nice" in src
		# Must be guarded by the inline check (os.nice is sticky per-process).
		assert "is_scheduler_disabled" in src


# ---------------------------------------------------------------------------
# M6 enrich throttle
# ---------------------------------------------------------------------------

class FakeCache:
	def __init__(self):
		self.store = {}

	def get_value(self, k):
		return self.store.get(k)

	def set_value(self, k, v, expires_in_sec=None):
		self.store[k] = v

	def delete_value(self, k):
		self.store.pop(k, None)


def _recording(queries):
	return {"uuid": "r", "calls": [{"query": q, "duration": 1} for q in queries]}


@pytest.fixture
def enrich_env(monkeypatch):
	"""Fakes so _enrich_recordings runs without a bench: every distinct query
	shape issues one fake EXPLAIN; sleeps and conf knobs are observable."""
	explain_queries = []
	slept = []

	class FakeDB:
		def sql(self, q, as_dict=False):
			explain_queries.append(q)
			return [{"id": 1}]

	conf_values = {"optimus_explain_cache_ttl_seconds": 0}  # shared cache off

	monkeypatch.setattr(frappe, "db", FakeDB(), raising=False)
	monkeypatch.setattr(frappe, "cache", FakeCache(), raising=False)
	monkeypatch.setattr(frappe, "conf",
		types.SimpleNamespace(get=lambda k, d=None: conf_values.get(k, d)), raising=False)
	monkeypatch.setattr(analyze, "is_query_type", lambda q, t: True, raising=False)
	monkeypatch.setattr(analyze, "mark_duplicates", lambda rec: None, raising=False)
	monkeypatch.setattr(analyze, "safe_commit", lambda: None, raising=False)
	monkeypatch.setattr(analyze.time, "sleep", lambda s: slept.append(s))

	import optimus.settings as settings_mod
	monkeypatch.setattr(settings_mod, "get_config",
		lambda: types.SimpleNamespace(max_queries_per_recording=2000), raising=False)

	return types.SimpleNamespace(
		explain_queries=explain_queries, slept=slept, conf=conf_values,
	)


# Five distinct query shapes → five EXPLAINs.
_FIVE = [f"SELECT * FROM tab{i} WHERE x = {i}" for i in range(5)]


class TestEnrichThrottle:
	def test_disabled_by_default_never_sleeps(self, enrich_env):
		analyze._enrich_recordings([_recording(_FIVE)])
		assert len(enrich_env.explain_queries) == 5
		assert enrich_env.slept == []

	def test_sleeps_every_n_explains(self, enrich_env):
		enrich_env.conf["optimus_enrich_throttle_every"] = 2
		enrich_env.conf["optimus_enrich_throttle_sleep_ms"] = 5
		analyze._enrich_recordings([_recording(_FIVE)])
		# 5 EXPLAINs, sleep after the 2nd and 4th → 2 sleeps of ~5ms.
		assert len(enrich_env.slept) == 2
		assert all(abs(s - 0.005) < 1e-9 for s in enrich_env.slept)

	def test_no_sleep_on_cache_hits(self, enrich_env):
		enrich_env.conf["optimus_enrich_throttle_every"] = 1
		# Same shape three times → only ONE real EXPLAIN → one sleep, not three.
		same = ["SELECT * FROM tabFoo WHERE x = 1"] * 3
		analyze._enrich_recordings([_recording(same)])
		assert len(enrich_env.explain_queries) == 1
		assert len(enrich_env.slept) == 1

	def test_results_invariant_to_throttle(self, enrich_env):
		off = _recording(_FIVE)
		analyze._enrich_recordings([off])
		results_off = [c.get("explain_result") for c in off["calls"]]

		enrich_env.conf["optimus_enrich_throttle_every"] = 2
		on = _recording(_FIVE)
		analyze._enrich_recordings([on])
		results_on = [c.get("explain_result") for c in on["calls"]]

		assert results_off == results_on


class TestMaxQueriesPerRecordingZeroIsUnlimited:
	"""``max_queries_per_recording = 0`` (Strict preset) means no cap: every
	query gets enriched, rather than silently falling back to the 2000
	default."""

	def test_zero_cap_does_not_truncate_large_recording(self, enrich_env, monkeypatch):
		# Override the fixture's max_queries default.
		import optimus.settings as settings_mod
		monkeypatch.setattr(
			settings_mod, "get_config",
			lambda: types.SimpleNamespace(max_queries_per_recording=0),
			raising=False,
		)

		# A recording with 3000 queries (way over the legacy 2000 default).
		# With cap = 0, every one must be enriched nothing truncated.
		big = _recording([
			f"SELECT * FROM tab{i} WHERE x = {i}" for i in range(3000)
		])
		warnings = analyze._enrich_recordings([big])

		# All 3000 calls remain in the recording (no slice off).
		assert len(big["calls"]) == 3000
		# No truncation warning emitted.
		truncation_warnings = [w for w in warnings if "TRUNCATED" in w]
		assert truncation_warnings == [], (
			f"cap = 0 must not truncate, got {truncation_warnings!r}"
		)

	def _cap(self, monkeypatch, cap):
		import optimus.settings as settings_mod
		monkeypatch.setattr(
			settings_mod, "get_config",
			lambda: types.SimpleNamespace(max_queries_per_recording=cap),
			raising=False,
		)
		normalized = []

		def fake_normalize(q):
			normalized.append(q)
			return re.sub(r"\d+", "?", q)

		def fake_mark(rec):
			for c in rec["calls"]:
				c["normalized_query"] = fake_normalize(c["query"])

		monkeypatch.setattr(sys.modules["frappe.recorder"], "normalize_query", fake_normalize, raising=False)
		monkeypatch.setattr(analyze, "mark_duplicates", fake_mark, raising=False)
		return normalized

	def test_positive_cap_limits_explain_and_keeps_every_call(self, enrich_env, monkeypatch):
		"""A positive cap still bounds EXPLAIN and normalize; past-cap calls stay and are grouped by shape."""
		normalized = self._cap(monkeypatch, 100)
		big = _recording([f"SELECT * FROM tabFoo WHERE x = {i}" for i in range(250)])
		warnings = analyze._enrich_recordings([big])

		assert len(big["calls"]) == 250
		assert len(enrich_env.explain_queries) == 100
		# Past-cap calls reuse the enriched shape: no format or normalize each.
		assert len(normalized) == 100
		past = big["calls"][100:]
		assert all(c["past_enrich_cap"] and "explain_result" not in c for c in past)
		assert past[0]["query"] == "SELECT * FROM tabFoo WHERE x = 100"
		assert len({c["normalized_query"] for c in big["calls"]}) == 1
		assert big["calls"][0]["normalized_copies"] == 250
		assert [c["index"] for c in big["calls"]] == list(range(250))
		truncation_warnings = [w for w in warnings if "TRUNCATED" in w]
		assert truncation_warnings, "positive cap must emit a truncation warning"
		assert "still count in query totals" in truncation_warnings[0]

	def test_new_shape_past_cap_is_normalized_once(self, enrich_env, monkeypatch):
		normalized = self._cap(monkeypatch, 2)
		queries = ["SELECT * FROM tabFoo WHERE x = 1", "SELECT * FROM tabFoo WHERE x = 2"]
		queries += [f"SELECT name FROM tabBar WHERE y IN ('a', '{i}')" for i in range(5)]
		rec = _recording(queries)
		analyze._enrich_recordings([rec])

		assert len(normalized) == 3  # two enriched + one for the new tabBar shape
		bar = rec["calls"][2:]
		assert len({c["normalized_query"] for c in bar}) == 1
		assert bar[0]["normalized_copies"] == 5
		assert bar[0]["exact_copies"] == 1
