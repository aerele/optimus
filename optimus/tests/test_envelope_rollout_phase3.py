# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The cross-session EXPLAIN-result cache in ``analyze._enrich_recordings`` stores a list of
dicts in the ``wrap_value`` envelope and reads both the envelope and the legacy bare list
(docs/REDIS-SCHEMA.md). Each test runs the real enrichment against a dict-backed cache."""

from __future__ import annotations

from types import SimpleNamespace

import frappe
import pytest
import sqlparse

from optimus import analyze, redis_keys, redis_schema
from optimus.dbdialect.base import NormalizedPlan, PlanTable

QUERY = "SELECT name FROM tabUser WHERE name = 'a'"


class _DictCache:
	"""Dict-backed ``frappe.cache`` substitute that keeps what was written."""

	def __init__(self, store=None):
		self.store = dict(store or {})

	def get_value(self, key):
		return self.store.get(key)

	def set_value(self, key, value, **_):
		self.store[key] = value


@pytest.fixture
def enrich(monkeypatch):
	"""Run ``_enrich_recordings`` on one SELECT; return (call, cache, explains_issued)."""

	def run(cache):
		explains = []

		def run_explain(query):
			explains.append(query)
			return NormalizedPlan(tables=[PlanTable(table="tabUser", rows_examined=7)])

		monkeypatch.setattr(analyze, "safe_commit", lambda: None)
		monkeypatch.setattr(
			analyze, "is_query_type", lambda query, _types: query.lstrip().upper().startswith("SELECT")
		)
		monkeypatch.setattr(analyze, "get_dialect", lambda: SimpleNamespace(run_explain=run_explain))
		monkeypatch.setattr(frappe, "conf", {"optimus_explain_cache_ttl_seconds": 60}, raising=False)
		monkeypatch.setattr(frappe, "cache", cache, raising=False)
		monkeypatch.setattr(
			"optimus.settings.get_config", lambda: SimpleNamespace(max_queries_per_recording=100)
		)
		call = {"query": QUERY}
		analyze._enrich_recordings([{"calls": [call]}])
		return call, explains

	return run


def _shared_key():
	"""The shared-cache key of QUERY: enrichment formats the SQL before it builds the key."""
	formatted = sqlparse.format(QUERY.strip(), keyword_case="upper", reindent=True, strip_comments=True)
	return redis_keys.explain_cache(analyze._shape_key(formatted))


def test_a_fresh_explain_result_is_written_as_a_versioned_envelope(enrich):
	cache = _DictCache()
	call, explains = enrich(cache)

	assert len(explains) == 1
	assert call["explain_result"][0]["table"] == "tabUser"
	assert cache.store[_shared_key()] == {
		"_v": redis_schema.SCHEMA_VERSION,
		"data": call["explain_result"],
	}


@pytest.mark.parametrize(
	"stored",
	[
		redis_schema.wrap_value([{"table": "tabCached", "rows_examined": 3}]),
		[{"table": "tabCached", "rows_examined": 3}],
	],
	ids=["envelope", "legacy-bare-list"],
)
def test_a_cached_envelope_or_legacy_bare_list_is_read_without_a_new_explain(enrich, stored):
	call, explains = enrich(_DictCache({_shared_key(): stored}))

	assert explains == []
	assert call["explain_result"] == [{"table": "tabCached", "rows_examined": 3}]
