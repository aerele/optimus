# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Phase-2 recheck of phase-1 findings plus idempotent Hot Line merging."""

import json
from types import SimpleNamespace

from optimus.analyzers.base import AnalyzerResult
from optimus.line_profile import persist, recheck


def _fn(dotted, total_ms, hits=3, file="/app/mod.py"):
	lines = [{"lineno": 10, "content": "slow()", "hits": hits, "total_ms": total_ms}]
	if total_ms:
		lines.append({"lineno": 11, "content": "x = 1", "hits": hits, "total_ms": 0.0})
	return {"dotted_path": dotted, "qualname": dotted.rsplit(".", 1)[-1], "file": file, "lines": lines}


def _finding(function="work", impact=1000.0, severity="High", filename="/other/path/mod.py", ftype="Slow Hot Path"):
	detail = {"callsite": {"filename": filename, "function": function, "lineno": 5}}
	return {
		"finding_type": ftype,
		"severity": severity,
		"estimated_impact_ms": impact,
		"technical_detail_json": json.dumps(detail),
	}


def _rc(finding):
	return json.loads(finding["technical_detail_json"])["phase2_recheck"]


def _run(findings, functions, chains=None):
	index = recheck.build_function_index([functions])
	return recheck.recheck_findings(findings, index, chains)


class TestRule:
	def test_confirmed_keeps_severity_and_attaches_hot_line(self):
		f = _finding(impact=1000.0)
		stats = _run([f], [_fn("app.mod.work", 800.0, hits=4)])
		assert stats["confirmed"] == 1
		assert f["severity"] == "High" and f["estimated_impact_ms"] == 1000.0
		rc = _rc(f)
		assert rc["status"] == "confirmed"
		assert rc["hot_line"] == {"lineno": 10, "content": "slow()", "total_ms": 800.0, "hits": 4}

	def test_measurement_far_above_estimate_never_upgrades(self):
		f = _finding(impact=100.0, severity="Low")
		_run([f], [_fn("app.mod.work", 5000.0)])
		assert f["severity"] == "Low" and f["estimated_impact_ms"] == 100.0

	def test_contradicted_downgrades_one_step_and_sets_measured_impact(self):
		f = _finding(impact=1000.0, severity="High")
		stats = _run([f], [_fn("app.mod.work", 50.0)])
		assert stats["downgraded"] == 1
		assert f["severity"] == "Medium" and f["estimated_impact_ms"] == 50.0
		assert _rc(f)["phase1_severity"] == "High"

	def test_low_stays_low_on_downgrade(self):
		f = _finding(impact=1000.0, severity="Low")
		_run([f], [_fn("app.mod.work", 1.0)])
		assert f["severity"] == "Low"

	def test_boundary_ratio_is_confirmed(self):
		f = _finding(impact=1000.0)
		_run([f], [_fn("app.mod.work", 250.0)])
		assert _rc(f)["status"] == "confirmed"

	def test_no_estimate_is_measured_only(self):
		f = _finding(impact=0)
		_run([f], [_fn("app.mod.work", 5.0)])
		assert _rc(f)["status"] == "measured" and f["severity"] == "High"

	def test_call_chain_attached_and_capped(self):
		chain = [{"dotted_path": f"a.f{i}", "qualname": f"f{i}"} for i in range(30)]
		chain[-1]["dotted_path"] = "app.mod.work"
		hot = {"finding_type": "Hot Line", "technical_detail_json": json.dumps({"call_chain": chain})}
		f = _finding()
		_run([f], [_fn("app.mod.work", 900.0)], recheck.collect_call_chains([hot]))
		assert len(_rc(f)["call_chain"]) == recheck.CALL_CHAIN_CAP


class TestNotRechecked:
	def test_zero_profiled_functions(self):
		f = _finding()
		stats = _run([f], [])
		assert stats["not_rechecked"] == 1
		assert _rc(f)["reason"] == "not_profiled" and f["severity"] == "High"

	def test_function_never_ran_or_vanished(self):
		f = _finding()
		_run([f], [{"dotted_path": "app.mod.work", "qualname": "work", "file": "/app/mod.py", "lines": []}])
		assert _rc(f)["reason"] == "not_exercised"

	def test_findings_without_callsite_or_with_string_callsite(self):
		a = {"finding_type": "Slow Query", "severity": "Low", "estimated_impact_ms": 5, "technical_detail_json": ""}
		b = {"finding_type": "Slow Query", "severity": "Low", "estimated_impact_ms": 5,
			"technical_detail_json": json.dumps({"callsite": "a/b.py:12"})}
		_run([a, b], [_fn("app.mod.work", 9.0)])
		assert _rc(a)["reason"] == "no_callsite" and _rc(b)["reason"] == "no_callsite"

	def test_ambiguous_bare_name_is_not_guessed(self):
		fns = [_fn("app.mod.A.validate", 900.0), _fn("app.mod.B.validate", 1.0)]
		f = _finding(function="validate")
		_run([f], fns)
		assert _rc(f)["reason"] == "ambiguous" and f["severity"] == "High"

	def test_corrupt_legacy_payload_left_untouched(self):
		f = {"finding_type": "Slow Query", "severity": "High", "estimated_impact_ms": 5,
			"technical_detail_json": "not json{"}
		_run([f], [_fn("app.mod.work", 1.0)])
		assert f["technical_detail_json"] == "not json{" and f["severity"] == "High"

	def test_hot_line_findings_are_skipped(self):
		f = _finding(ftype="Hot Line")
		_run([f], [_fn("app.mod.work", 1.0)])
		assert "phase2_recheck" not in json.loads(f["technical_detail_json"])

	def test_cap_limits_work_and_prefers_highest_impact(self, monkeypatch):
		monkeypatch.setattr(recheck, "MAX_RECHECKED_FINDINGS", 2)
		fs = [_finding(impact=i) for i in (1, 500, 300)]
		stats = _run(fs, [_fn("app.mod.work", 900.0)])
		assert stats["skipped_cap"] == 1
		assert "phase2_recheck" not in json.loads(fs[0]["technical_detail_json"])


class TestIdempotence:
	def test_second_pass_does_not_compound_downgrade(self):
		f = _finding(impact=1000.0, severity="High")
		fns = [_fn("app.mod.work", 50.0)]
		_run([f], fns)
		first = dict(f)
		_run([f], fns)
		assert f == first and f["severity"] == "Medium"

	def test_function_disappearing_restores_phase1_values(self):
		f = _finding(impact=1000.0, severity="High")
		_run([f], [_fn("app.mod.work", 50.0)])
		_run([f], [])
		assert f["severity"] == "High" and f["estimated_impact_ms"] == 1000.0
		assert _rc(f)["status"] == "not_rechecked"

	def test_larger_measurement_in_later_run_re_confirms(self):
		f = _finding(impact=1000.0, severity="High")
		_run([f], [_fn("app.mod.work", 50.0)])
		index = recheck.build_function_index([[_fn("app.mod.work", 50.0)], [_fn("app.mod.work", 700.0)]])
		recheck.recheck_findings([f], index)
		assert f["severity"] == "High" and _rc(f)["status"] == "confirmed"


class TestHotLineMergePlan:
	def test_first_run_appends(self):
		plan = recheck.plan_hot_line_merge([], ["a", "b"], {"a", "b"})
		assert plan == {"update": {}, "append": [0, 1], "remove": []}

	def test_rerun_updates_in_place_and_removes_legacy_duplicates(self):
		plan = recheck.plan_hot_line_merge(["a", "a", "b"], ["a"], {"a"})
		assert plan["update"] == {0: 0} and plan["append"] == [] and plan["remove"] == [1]

	def test_profiled_without_new_finding_is_removed_unprofiled_kept(self):
		plan = recheck.plan_hot_line_merge(["a", "b"], [], {"a"})
		assert plan["remove"] == [0]


class _Row(dict):
	def get(self, k, d=None):
		return dict.get(self, k, d)

	def set(self, k, v):
		self[k] = v

	def __getattr__(self, k):
		return self.get(k)


class _Parent:
	def __init__(self, findings, runs):
		self.findings = [_Row(r) for r in findings]
		self.phase_2_runs = runs

	def append(self, _table, row):
		self.findings.append(_Row(row))

	def set(self, _table, rows):
		self.findings = rows


def _hot(dotted="app.mod.work", lineno=10):
	return {"finding_type": "Hot Line", "severity": "High", "estimated_impact_ms": 800.0,
		"technical_detail_json": json.dumps({"dotted_path": dotted, "lineno": lineno})}


def _result(findings, summaries):
	return AnalyzerResult(findings=findings, aggregate={"phase2_functions": summaries}, warnings=[])


def _run_row(functions, status="Ready"):
	return SimpleNamespace(status=status, results_json=json.dumps(functions))


class TestPersist:
	def test_phase1_kept_hot_line_not_duplicated_across_reruns(self):
		runs = [_run_row([_fn("app.mod.work", 50.0)])]
		parent = _Parent([_finding(impact=1000.0)], runs)
		summaries = [{"dotted_path": "app.mod.work", "hot_lines": [{"lineno": 10}]}]
		for _ in range(2):
			persist.merge_phase2_findings(parent, _result([_hot()], summaries))
		kinds = [r["finding_type"] for r in parent.findings]
		assert kinds.count("Hot Line") == 1 and kinds.count("Slow Hot Path") == 1
		phase1 = parent.findings[0]
		assert phase1["severity"] == "Medium"

	def test_zero_profiled_functions_marks_everything_not_rechecked(self):
		parent = _Parent([_finding()], [_run_row([])])
		persist.merge_phase2_findings(parent, _result([], []))
		assert json.loads(parent.findings[0]["technical_detail_json"])["phase2_recheck"]["status"] == "not_rechecked"

	def test_legacy_session_without_runs_or_details(self):
		parent = _Parent([{"finding_type": "Slow Query", "severity": "Low", "estimated_impact_ms": 1}], [])
		persist.merge_phase2_findings(parent, _result([], []))
		assert parent.findings[0]["severity"] == "Low"

	def test_stale_ai_fix_cleared_when_hot_line_moves(self):
		row = dict(_hot(lineno=10), llm_fix_json="{}")
		parent = _Parent([row], [_run_row([])])
		persist.merge_phase2_findings(parent, _result([_hot(lineno=22)], [{"dotted_path": "app.mod.work", "hot_lines": [1]}]))
		assert parent.findings[0].get("llm_fix_json") is None

	def test_failed_runs_do_not_feed_the_index(self):
		parent = _Parent([_finding(impact=1000.0)], [_run_row([_fn("app.mod.work", 1.0)], status="Failed")])
		persist.merge_phase2_findings(parent, _result([], []))
		assert parent.findings[0]["severity"] == "High"


class TestRender:
	def _html(self, recheck_blob):
		from optimus import renderer

		doc = SimpleNamespace(
			title="T", session_uuid="t", user="a", status="Ready", started_at="2026-04-17",
			stopped_at="2026-04-17", notes=None, top_severity="High", total_duration_ms=2000,
			total_query_time_ms=0, total_queries=20, total_requests=5, summary_html=None,
			top_queries_json="[]", table_breakdown_json="[]", hot_frames_json="[]",
			session_time_breakdown_json="{}", total_python_ms=0, total_sql_ms=0,
			analyzer_warnings=None, v5_aggregate_json="{}", actions=[],
		)
		detail = {
			"callsite": {"filename": "apps/myapp/foo.py", "lineno": 10, "function": "f"},
			"occurrences": 15, "average_time_ms": 3.0, "total_time_ms": 45.0,
			"phase2_recheck": recheck_blob,
		}
		doc.findings = [SimpleNamespace(
			finding_type="N+1 Query", severity="Medium", title="Same query ran 15x",
			customer_description="d", estimated_impact_ms=45.0, affected_count=15, action_ref="0",
			technical_detail_json=json.dumps(detail),
		)]
		return renderer.render(doc, recordings=[])

	def test_not_rechecked_label_rendered(self):
		assert "not rechecked" in self._html({"status": "not_rechecked", "reason": "not_profiled"})

	def test_downgrade_rendered_with_measured_values(self):
		html = self._html({
			"status": "downgraded", "function_total_ms": 50.0, "phase1_impact_ms": 1000.0,
			"hot_line": {"lineno": 10, "total_ms": 40.0, "hits": 2},
		})
		assert "Line-level recheck" in html
		assert "downgraded to Medium" in html
		assert "hottest line 10" in html
