"""Preserve PostgreSQL schema and parameter identity without sending values."""
import json
from types import SimpleNamespace

import pytest

from optimus import ai_fix, ai_privacy


@pytest.fixture
def sql_site(monkeypatch):
	import frappe
	monkeypatch.setattr(frappe, "db", SimpleNamespace(db_type="postgres"), raising=False)
	monkeypatch.setattr(ai_privacy, "_fresh_sensitive_columns", lambda: "")
	return frappe


@pytest.mark.parametrize("send_raw", [False, True])
def test_postgres_names_and_parameters_survive_sensitive_redaction(sql_site, send_raw):
	query = 'SELECT "name", "odd""column" FROM "tabUser" WHERE "password" = $1 AND "enabled" = 123'
	result = ai_privacy.query_text(query, send_raw=send_raw)
	assert '"tabUser"' in result and '"password"' in result and '"odd""column"' in result
	assert "$1" in result and "123" not in result


def test_postgres_prompt_preserves_primary_and_example_schema(sql_site):
	finding = {"finding_type": "Slow Query", "technical_detail": {
		"normalized_query": 'SELECT "name" FROM "tabUser" WHERE "email" = $1',
		"example_queries": ['SELECT "email" FROM "tabUser" WHERE "name" = \'PRIVATE-PG-CANARY\''],
	}}
	_, messages, _ = ai_fix._build_fix_request(finding, threshold_ms=1000, context_tokens=32768)
	text = json.dumps(messages)
	assert "tabUser" in text and "email" in text and "name" in text and "$1" in text
	assert "Query (normalized)" in text and "Example affected queries" in text
	assert "PRIVATE-PG-CANARY" not in text


@pytest.mark.parametrize("dialect", ["postgres", "mariadb"])
@pytest.mark.parametrize("literal", ["'PRIVATE-PG-CANARY'", "$$PRIVATE-PG-CANARY$$", "$tag$PRIVATE-PG-CANARY$tag$", "E'PRIVATE-PG-CANARY'"])
def test_values_remain_private_in_both_dialects(sql_site, dialect, literal):
	sql_site.db.db_type = dialect
	result = ai_privacy.query_text(f'SELECT name FROM tabUser WHERE name = {literal}', send_raw=False)
	assert result and "PRIVATE-PG-CANARY" not in result


def test_mysql_double_quoted_values_are_not_identifiers(sql_site):
	sql_site.db.db_type = "mariadb"
	assert "PRIVATE-PG-CANARY" not in ai_privacy.query_text('SELECT name FROM tabUser WHERE name = "PRIVATE-PG-CANARY"', send_raw=False)


def test_postgres_json_operator_is_not_a_comment(sql_site):
	result = ai_privacy.query_text('SELECT "payload" #>> \'{PRIVATE-PG-CANARY}\' FROM "tabEvent"', send_raw=False)
	assert '"payload" #>> ?' in result and '"tabEvent"' in result
	assert "PRIVATE-PG-CANARY" not in result


def test_analysis_repairs_recorder_identifier_loss_before_finding_json_and_prompt(sql_site, monkeypatch):
	from optimus import analyze
	from optimus.tests.test_analyze_runtime import _recording
	query = 'SELECT "name" FROM "tabUser" WHERE "email" = \'PRIVATE-PG-CANARY\''
	# This is the actual lossy shape received from Frappe's literal normalizer.
	rec = _recording([query, query.replace("PRIVATE-PG-CANARY", "SECOND-PRIVATE-CANARY")])
	for call in rec["calls"]:
		call.update(normalized_query="SELECT ? FROM ? WHERE ? = ?", explain_result=[{"rows": 1}])
	monkeypatch.setattr(sql_site, "conf", {"optimus_explain_cache_ttl_seconds": 0}, raising=False)
	monkeypatch.setattr("optimus.settings.get_config", lambda: SimpleNamespace(max_queries_per_recording=1))
	analyze._enrich_recordings([rec])
	assert all('"tabUser"' in c["normalized_query"] and c["normalized_copies"] == 2 for c in rec["calls"])
	assert rec["calls"][1]["past_enrich_cap"]
	# A finding's persisted JSON is the boundary subsequent refreshes use.
	detail = json.loads(json.dumps({"normalized_query": rec["calls"][0]["normalized_query"]}))
	finding = {"finding_type": "Slow Query", "technical_detail": detail}
	_, messages, _ = ai_fix._build_fix_request(finding, threshold_ms=1000, context_tokens=32768)
	text = json.dumps(messages)
	assert "tabUser" in text and "email" in text and "PRIVATE-CANARY" not in text


def test_old_recording_examples_are_redacted_from_raw_instead_of_erased_normalized(sql_site):
	from optimus import analyze
	payload = {}
	analyze._maybe_attach_recorded_queries(payload, action_ref=0, send_raw=False,
		actions_by_idx={0: {"recording_uuid": "r"}}, recordings_by_uuid={"r": {"calls": [{
		"duration": 500, "query": 'SELECT "name" FROM "tabUser" WHERE "email" = \'PRIVATE-PG-CANARY\'',
		"normalized_query": "SELECT ? FROM ? WHERE ? = ?"}]}})
	text = json.dumps(payload)
	assert "tabUser" in text and "email" in text and "PRIVATE-PG-CANARY" not in text


@pytest.mark.parametrize("query", ['SELECT "unclosed', "SELECT $tag$PRIVATE-PG-CANARY", "SELECT 'PRIVATE-PG-CANARY", "SELECT 1 /* PRIVATE-PG-CANARY"])
def test_malformed_quoted_inputs_fail_closed(sql_site, query):
	assert ai_privacy.query_text(query, send_raw=False) == ""


@pytest.mark.parametrize("dialect,slashes", [("postgres", "\\"), ("mariadb", "\\\\")])
def test_lexer_backslash_ambiguity_never_exposes_a_later_value(sql_site, dialect, slashes):
	sql_site.db.db_type = dialect
	query = f"SELECT name FROM tabNote WHERE title = 'C:{slashes}' AND content = 'PRIVATE-PG-CANARY -- ' LIMIT 1"
	assert ai_privacy.query_text(query, send_raw=False) == ""
	assert ai_privacy.query_text("SELECT ? WHERE title = ? PRIVATE-PG-CANARY -- ' LIMIT 1", send_raw=False) == ""


def test_past_cap_postgres_grouping_does_not_parse_each_n_plus_one_value(sql_site, monkeypatch):
	from optimus import analyze
	calls = [{"query": f'SELECT "name" FROM "tabUser" WHERE "name" = \'row-{i}\''} for i in range(5000)]
	raw = calls[0]["query"]
	count = []
	original = ai_privacy.query_text
	def counted(*a, **kw):
		count.append(1)
		return original(*a, **kw)
	monkeypatch.setattr(ai_privacy, "query_text", counted)
	analyze._mark_postgres_duplicates({"calls": calls}, calls[:1], [raw], calls[1:])
	assert len(count) == 1
	assert all(c["normalized_copies"] == 5000 for c in calls)
	assert all(c["exact_copies"] == 1 for c in calls)
	assert '"tabUser"' in calls[-1]["normalized_query"]


@pytest.mark.parametrize("number", ["1e10", "1e+10", "-2.5E-10", "0x1F", "0b101", "0o777", "123_456_789"])
def test_past_cap_pg_numeric_literals_are_removed(number):
	from optimus import analyze
	shape = analyze._postgres_literal_shape(f'SELECT "name" FROM "tabUser" WHERE "number" = {number}')
	assert number not in shape and '"tabUser"' in shape and "?" in shape


def test_pg_shape_cache_keeps_all_in_cap_shapes_for_later_duplicates(sql_site):
	from optimus import analyze
	calls = [{"query": f'SELECT "column{i}" FROM "tabUser" WHERE "name" = \'private\''} for i in range(300)]
	calls.append({"query": calls[0]["query"]})
	analyze._mark_postgres_duplicates({"calls": calls}, calls[:300], [c["query"] for c in calls[:300]], calls[300:])
	assert calls[0]["normalized_query"] == calls[-1]["normalized_query"]
	assert calls[0]["normalized_copies"] == calls[-1]["normalized_copies"] == 2


def test_unclosed_pg_comments_are_omitted_without_regex_backtracking():
	from optimus import analyze
	assert analyze._postgres_literal_shape("SELECT " + "/*" * 7000) == ""



def test_pg_in_list_shapes_keep_duplicate_contract_without_rewriting_identifiers(sql_site):
	from optimus import analyze
	calls = [{"query": f'SELECT "in (?, ?)" FROM "tabUser" WHERE "name" IN ({values})'} for values in ("1,2", "3,4,5")]
	analyze._mark_postgres_duplicates({"calls": calls}, calls[:1], [calls[0]["query"]], calls[1:])
	assert calls[0]["normalized_query"] == calls[1]["normalized_query"]
	assert calls[0]["normalized_copies"] == calls[1]["normalized_copies"] == 2
	assert '"in (?, ?)"' in calls[1]["normalized_query"]



def test_nested_pg_comment_tail_never_reaches_prompts(sql_site):
	from optimus import analyze
	query = 'SELECT "name" FROM "tabUser" /* outer /* inner */ PRIVATE-PG-CANARY */'
	assert ai_privacy.query_text(query, send_raw=False) == ""
	assert analyze._postgres_literal_shape(query) == ""
