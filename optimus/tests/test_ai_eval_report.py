# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""scripts/ai_eval/report.py: disposition, LLM calls, the Q1 bar and the Markdown table."""

import pytest

from optimus.tests.ai_eval_support import load


@pytest.fixture(scope="module")
def corpus():
	return load("_corpus").load_corpus()


# ---------------------------------------------------------------- disposition, calls, bar
def rec(**kw):
	base = {"name": "c", "finding_type": "N+1 Query", "outcome": None, "result": None, "error": None, "elapsed_s": 1.0}
	base.update(kw)
	return base


def test_disposition_and_calls():
	rep = load("report")
	assert rep.disposition(rec(outcome="gated")) == "gated"
	assert rep.disposition(rec(outcome="recipe")) == "recipe"
	assert rep.disposition(rec(result={"suggestion": "text"})) == "AI clean"  # develop: no guardrail key
	assert rep.disposition(rec(result={"suggestion": "t\n\n> **Profiler note:** x"})) == "AI with notes"
	assert rep.disposition(rec(result={"suggestion": "t", "guardrail": {"violations": [], "reasked": True, "fallback": True}})) == "AI fallback"
	assert rep.disposition(rec(error={"kind": "timeout"})) is None
	assert rep.calls(rec(outcome="gated")) == 0
	assert rep.calls(rec(result={"suggestion": "t"})) == 1
	assert rep.calls(rec(result={"suggestion": "t", "guardrail": {"reasked": True}})) == 2
	assert rep.calls(rec(error={"kind": "timeout"})) == 1
	assert rep.DISPOSITIONS == ("recipe", "gated", "AI clean", "AI fallback", "AI with notes")


def test_truncated_and_violation_codes_accept_strings_and_dicts():
	"""#54 stores guardrail.violations as code strings; dicts with "code" are tolerated."""
	rep = load("report")

	def g(violations, **result):
		return rec(result={"suggestion": "t", "guardrail": {"violations": violations, "reasked": False, "fallback": False}, **result})

	assert rep.truncated(g(["truncated"]))
	assert rep.truncated(g(["headings", "context-truncated"]))
	assert rep.truncated(g([{"code": "context-truncated", "detail": ""}]))
	assert rep.truncated(g([{"code": "truncated"}, "raw-sql"]))
	assert not rep.truncated(g(["raw-sql", {"code": "headings"}, {"detail": "no code"}, 7]))
	assert rep.truncated(rec(result={"suggestion": "t", "finish_reason": "length"}))
	assert rep.truncated(rec(result={"suggestion": "t", "finish_reason": "max_tokens"}))
	assert not rep.truncated(rec(result={"suggestion": "t"}))  # develop: no guardrail, no finish_reason
	assert rep.violation_codes(g(["raw-sql", {"code": "headings"}, "raw-sql"])["result"]) == ["raw-sql", "headings"]
	assert rep.violation_codes(None) == []


def row(name, **kw):
	base = {"name": name, "finding_type": "N+1 Query", "disposition": "AI clean", "gate_note": None,
		"label": "correct", "draft_label": None, "code": True, "fabricated": False,
		"still_in_loop": None, "semgrep": [], "unparsed": 0, "calls": 1, "prompt_tokens": 10,
		"completion_tokens": 5, "elapsed_s": 1.0, "truncated": False}
	base.update(kw)
	return base


def test_bar_rules():
	rep = load("report")
	assert rep.bar([row("a"), row("b")])["verdict"] == "PASS"
	assert rep.bar([row("a", label="harmful")])["verdict"] == "FAIL"
	assert rep.bar([row("a", label="harmful", code=False, disposition="AI fallback")])["verdict"] == "PASS"
	assert rep.bar([row("a", fabricated=True)])["verdict"] == "FAIL"
	assert rep.bar([row("a", semgrep=["frappe-manual-commit"])])["verdict"] == "FAIL"
	assert rep.bar([row(n, label="wrong") for n in "ab"])["verdict"] == "PASS"
	assert rep.bar([row(n, label="wrong") for n in "abc"])["verdict"] == "FAIL"
	assert rep.bar([row("a", label=None)])["verdict"] == "INCOMPLETE"
	assert rep.bar([row("a", semgrep=None)])["verdict"] == "INCOMPLETE"
	assert rep.bar([row("a", unparsed=1)])["verdict"] == "INCOMPLETE"
	assert rep.bar([row("a", disposition=None, label=None)])["verdict"] == "INCOMPLETE"
	assert rep.bar([row("a", label="safe-directional")], [row("a")])["regressions"] == ["a"]
	assert rep.bar([row("a", disposition="gated", label=None, code=False)], [row("a")])["regressions"] == []
	assert rep.bar([row("a", disposition="recipe", label=None, code=True)], [row("a")])["regressions"] == []
	fits = rep.bar([row("a"), row("b", prompt_tokens=4000, completion_tokens=200)], context=4096)
	assert fits["within_context"] == "1/2"
	assert rep.bar([row("a")])["within_context"] is None


def _one_case_run(labels):
	return {"meta": {}, "cases": [{"name": "3q1nfc4d2l", "finding_type": "N+1 Query", "outcome": None,
		"result": {"suggestion": "**Diagnosis**: x\n**Fix**: y\n**Why it works**: y\n**Verify**: z"},
		"error": None, "elapsed_s": 1.0}], "labels": labels}


def test_draft_labels_count_as_unlabelled(corpus):
	rep = load("report")
	rows = rep.score(_one_case_run({"3q1nfc4d2l": {"label": "correct", "reason": "DRAFT: hoists the query"}}), corpus, semgrep_rules="")
	assert rows[0]["label"] is None and rows[0]["draft_label"] == "correct"
	summary = rep.bar([dict(rows[0], semgrep=[])])
	assert summary["unlabelled"] == ["3q1nfc4d2l"] and summary["verdict"] == "INCOMPLETE"
	assert "DRAFT correct" in rep.markdown({"meta": {}}, rows, summary)
	rows = rep.score(_one_case_run({"3q1nfc4d2l": {"label": "correct", "reason": "hoists the query"}}), corpus, semgrep_rules="")
	assert rows[0]["label"] == "correct" and rows[0]["draft_label"] is None


def test_label_sheet_drafts_every_reason(corpus):
	import json

	rep = load("report")
	run = rep.load_run("stored", corpus)
	sheet = rep.label_sheet(run, corpus)
	drafts = json.loads(sheet.split("```json\n", 1)[1].split("\n```", 1)[0])
	assert set(drafts) == {c["name"] for c in corpus["cases"]}
	assert all(v["reason"].startswith("DRAFT:") and v["label"] in rep._corpus.LABELS for v in drafts.values())
	run["labels"] = drafts
	assert all(r["label"] is None for r in rep.score(run, corpus, semgrep_rules=""))


def test_still_in_loop_with_a_passing_label_is_incomplete():
	rep = load("report")
	for label in ("correct", "safe-directional"):
		summary = rep.bar([row("a", still_in_loop=True, label=label)])
		assert summary["loop_label_conflicts"] == ["a"] and summary["verdict"] == "INCOMPLETE", label
	assert rep.bar([row("a", still_in_loop=True, label="wrong")])["verdict"] == "PASS"
	assert rep.bar([row("a", still_in_loop=False, label="correct")])["loop_label_conflicts"] == []


def test_coverage_losses_and_correct_as_code():
	rep = load("report")
	before = [row("a"), row("b", disposition="gated", label=None, code=False)]
	pre_l5 = row("a", finding_type="Redundant Call", disposition="gated", label=None, code=False,
		gate_note="Analyzed before the callsite fix; re-record the flow.")
	summary = rep.bar([pre_l5, row("b", disposition="gated", label=None, code=False)], before)
	assert summary["coverage_losses"] == ["a"] and summary["unaccepted_losses"] == [] and summary["verdict"] == "PASS"
	other_gate = dict(pre_l5, finding_type="Hot Line", gate_note="framework file")
	assert rep.bar([other_gate], before)["unaccepted_losses"] == ["a"]
	assert rep.bar([other_gate], before)["verdict"] == "INCOMPLETE"
	assert rep.bar([other_gate], before, accepted_losses={"a": "owner: framework Hot Line"})["verdict"] == "PASS"
	assert rep.bar([other_gate], before, accepted_losses={"a": "looks fine to me"})["unaccepted_losses"] == ["a"]
	not_pre_l5 = dict(pre_l5, gate_note="type excluded by the owner")
	assert rep.bar([not_pre_l5], before)["unaccepted_losses"] == ["a"]
	recipe = row("a", disposition="recipe", label=None)
	assert rep.bar([recipe], before)["unaccepted_losses"] == ["a"]
	summary = rep.bar([row("a"), row("b", code=False), row("c", label="wrong")])
	assert summary["correct_as_code"] == ["a"]


def test_table_header_records_server_facts_and_label_status(corpus, tmp_path):
	import json

	rep = load("report")
	run = _one_case_run({})
	del run["labels"]
	(tmp_path / "run.json").write_text(json.dumps(run))
	(tmp_path / "labels.json").write_text(json.dumps({"3q1nfc4d2l": {"label": "correct", "reason": "hoists the query"}}))
	(tmp_path / "server.json").write_text(json.dumps({"server": "Ollama", "version": "0.34.3", "model": "qwen3-coder:30b",
		"digest": "06c1097efce0431c2045fe7b2e5108366e43bee1b4603a7aded8f21689e90bca", "num_ctx": 4096}))
	loaded = rep.load_run(str(tmp_path), corpus)
	rows = rep.score(loaded, corpus, semgrep_rules="")
	head = rep.markdown(loaded, rows, rep.bar(rows)).split("| Case |")[0]
	for needle in ("Ollama `0.34.3`", "model digest `06c1097efce0`", "num_ctx `4096`", "Labels: 1 owner-confirmed, 0 DRAFT"):
		assert needle in head, needle


def test_ddl_in_a_bash_block_scores_harmful_as_code(corpus):
	rep = load("report")
	run = {"meta": {}, "cases": [{"name": "5qih2paean", "finding_type": "Hot Line", "outcome": None,
		"result": {"suggestion": "**Diagnosis**: x\n\n**Fix**\n\n```bash\nbench --site s mariadb -e "
		"\"ALTER TABLE `tabSales Invoice` ADD INDEX i (customer)\"\n```\n\n**Verify**: z"},
		"error": None, "elapsed_s": 1.0}],
		"labels": {"5qih2paean": {"label": "harmful", "reason": "raw DDL on a core table"}}}
	rows = rep.score(run, corpus, semgrep_rules="")
	assert rows[0]["code"] is True and rep.bar(rows)["harmful_as_code"] == ["5qih2paean"]


NESTED_HARMFUL = (
	"**Diagnosis**: the customer lookups are slow.\n\n**Fix**\n\n1. Add the index, then commit so it sticks:\n\n"
	"    ```python\n"
	"    frappe.db.sql(\"ALTER TABLE `tabSales Invoice` ADD INDEX idx_customer (customer)\")\n"
	"    frappe.db.commit()\n"
	"    ```\n\n**Why it works**: y\n\n**Verify**: z"
)


def test_list_nested_harmful_code_scores_harmful_as_code(corpus):
	"""A fence indented 4 spaces inside a list item renders as code; it must count."""
	rep = load("report")
	run = {"meta": {}, "cases": [{"name": "5qih2paean", "finding_type": "Hot Line", "outcome": None,
		"result": {"suggestion": NESTED_HARMFUL}, "error": None, "elapsed_s": 1.0}],
		"labels": {"5qih2paean": {"label": "harmful", "reason": "raw DDL and a manual commit"}}}
	rows = rep.score(run, corpus, semgrep_rules="")
	assert rows[0]["code"] is True
	assert rep.bar(rows)["harmful_as_code"] == ["5qih2paean"]


def test_stored_run_scores_without_semgrep(corpus):
	rep = load("report")
	run = rep.load_run("stored", corpus)
	run["labels"] = {c["name"]: c["label"] or c["draft_label"] for c in corpus["cases"]}
	rows = rep.score(run, corpus, semgrep_rules="")
	summary = rep.bar(rows)
	assert len(rows) == 15 and all(r["disposition"] == "AI clean" for r in rows)
	assert summary["fabricated_as_code"] == ["3q1dsfrmti", "3q1j3utnoa"]
	assert summary["still_in_loop"] == ["3q1ln8bv2o"]
	assert summary["verdict"] == "FAIL"
	table = rep.markdown(run, rows, summary)
	assert table.count("\n| ") == 16 and "**Bar: FAIL.**" in table and "Semgrep hits: not run" in table


def test_empty_bar_cannot_pass():
	assert load("report").bar([])["verdict"] == "INCOMPLETE"


def test_partial_run_records_missing_cases(corpus, monkeypatch):
	rep = load("report")
	monkeypatch.setattr(rep._semgrep, "available", lambda rules: True)
	monkeypatch.setattr(rep._semgrep, "scan_texts", lambda *a: {})
	run = _one_case_run({"3q1nfc4d2l": {"label": "correct", "reason": "owner confirmed"}})
	rows = rep.score(run, corpus)
	assert len(rows) == len(corpus["cases"])
	assert sum(r["error"] == "missing_case" for r in rows) == 14
	assert rep.bar(rows)["verdict"] == "INCOMPLETE"


def test_duplicate_case_records_are_refused(corpus):
	rep = load("report")
	run = _one_case_run({})
	run["cases"] *= 2
	with pytest.raises(ValueError, match="duplicate"):
		rep.score(run, corpus)


@pytest.mark.parametrize("label, reason", [("typo", "confirmed"), ("correct", ""), ("correct", "  ")])
def test_invalid_owner_label_stays_unlabelled(corpus, label, reason):
	run = _one_case_run({"3q1nfc4d2l": {"label": label, "reason": reason}})
	assert load("report").score(run, corpus, semgrep_rules="")[0]["label"] is None
