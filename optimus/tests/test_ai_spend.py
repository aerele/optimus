"""Atomic, portable usage updates and explicit attribution for refresh workers."""

from types import SimpleNamespace

import pytest

from optimus import ai_fix, analyze, api

pytestmark = pytest.mark.rq


@pytest.mark.parametrize("dialect,quote", [("mariadb", "`"), ("postgres", '"')])
def test_counter_query_adds_in_database_with_null_coalescing(monkeypatch, dialect, quote):
	qb = pytest.importorskip("frappe.query_builder.utils", exc_type=ImportError).get_query_builder(dialect)
	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(qb=qb))
	sql = analyze._session_increment_query("fake-doc", "ai_tokens_spent", 42).get_sql()
	q = quote
	assert (
		sql
		== f"UPDATE {q}tabOptimus Session{q} SET {q}ai_tokens_spent{q}=COALESCE({q}ai_tokens_spent{q},0)+42 WHERE {q}name{q}='fake-doc'"
	)
	assert "modified" not in sql


@pytest.mark.parametrize(
	"field,n", [("status", 1), ("ai_tokens_spent", -1), ("ai_tokens_spent", True), ("ai_tokens_spent", "3")]
)
def test_counter_rejects_non_counter_fields_and_invalid_counts(field, n):
	with pytest.raises(ValueError):
		analyze._session_increment_query("fake-doc", field, n)


def test_counters_do_not_commit_and_database_failures_cannot_be_ignored(monkeypatch):
	seen = []

	def query(doc, field, n):
		seen.append((doc, field, n))
		return SimpleNamespace(run=lambda: seen.append("update"))

	monkeypatch.setattr(analyze, "_session_increment_query", query)
	# No commit/rollback method: ownership belongs to the result transaction.
	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(db=SimpleNamespace()))
	analyze._add_ai_spend("fake-doc", 12)
	analyze._bump_ai_refresh_count("fake-doc")
	assert seen == [
		("fake-doc", "ai_tokens_spent", 12),
		"update",
		("fake-doc", "ai_refresh_count", 1),
		"update",
	]

	def failed(*a):
		raise RuntimeError("fake transaction failure")

	monkeypatch.setattr(analyze, "_session_increment_query", failed)
	with pytest.raises(RuntimeError):
		analyze._add_ai_spend("fake-doc", 3)


def test_counter_rq_timeout_is_fresh(monkeypatch):
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = Timeout("fake timeout")

	def failed(*a):
		raise original

	monkeypatch.setattr(analyze, "_session_increment_query", failed)
	with pytest.raises(Timeout) as caught:
		analyze._add_ai_spend("fake-doc", 3)
	assert caught.value is not original and caught.value.__context__ is None


@pytest.mark.parametrize("entry", ["fix", "steps"])
def test_public_call_explicit_session_reaches_protocol(monkeypatch, entry):
	provider = {
		"name": "fake",
		"protocol": "openai",
		"base_url": "https://fake.invalid/v1",
		"model": "fake",
		"needs_key": False,
		"context_tokens": 128000,
	}
	monkeypatch.setattr(ai_fix, "_provider_config", lambda: provider)
	monkeypatch.setattr(ai_fix, "_resolve_display_threshold_ms", lambda: 1000)
	monkeypatch.setattr(ai_fix, "_get_api_key", lambda *a: "")
	monkeypatch.setattr(ai_fix, "is_finding_type_excluded", lambda *a: False)
	seen = []

	def call(*a, **kw):
		seen.append(kw)
		return "## Diagnosis\nRepeated work.\n## Fix\nBatch the call.\n## Why it works\nFewer calls.\n## Verify\nProfile again."

	monkeypatch.setattr(ai_fix, "_call_openai_chat", call)
	if entry == "fix":
		ai_fix.suggest_fix(
			{"finding_type": "N+1 Query"}, session_uuid="fake-session", docname="fake-doc", timeout=17
		)
	else:
		ai_fix.humanize_steps(
			[{"label": "fake"}], session_uuid="fake-session", docname="fake-doc", timeout=17
		)
	assert seen[0]["session_uuid"] == "fake-session" and seen[0]["timeout"] == 17


def test_health_window_uses_stopped_at_not_ai_modified(monkeypatch):
	qb = pytest.importorskip("frappe.query_builder.utils", exc_type=ImportError).get_query_builder("postgres")
	queries = []
	monkeypatch.setattr(api, "frappe", SimpleNamespace(qb=qb))
	monkeypatch.setattr(api, "now_datetime", lambda: __import__("datetime").datetime(2026, 1, 3))
	monkeypatch.setattr(api, "add_to_date", lambda *a, **kw: "2026-01-02")
	# QueryBuilder instances carry run on the returned query class.
	query = qb.from_(qb.DocType("Optimus Session"))
	monkeypatch.setattr(
		type(query), "run", lambda self, **kw: queries.append(self.get_sql()) or [], raising=False
	)
	api._session_perf_24h()
	assert '"stopped_at"' in queries[0] and '"modified"' not in queries[0]
