# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""A Slow Background Job finding (the fallback card for a slow RQ job) is AI-eligible:
the emitter stamps the job method, the gate refuses a frame with nothing concrete or in
framework code and fails closed, the prompt carries the job evidence, and the report
renders the stored suggestion."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from optimus import ai_fix, ai_grounding, analyze, renderer, settings
from optimus.analyzers import call_tree

_USER_FILE = "/home/b/apps/myapp/myapp/jobs.py"


def _cfg(**kw):
	base = {"ai_enabled": True, "ai_auto_suggest": True, "ai_auto_suggest_max": 0,
	        "ai_suggest_findings": True, "tracked_apps": (), "ai_excluded_finding_types": ()}
	base.update(kw)
	return SimpleNamespace(**base)


def _job(detail=None, **kw):
	detail = {
		"function": "sync_rows", "filename": _USER_FILE, "lineno": 12,
		"cumulative_ms": 8000, "action_wall_time_ms": 10000,
		"is_bg_job_fallback": True, "job_method": "myapp.jobs.sync_all",
	} if detail is None else detail
	return {
		"finding_type": "Slow Background Job", "severity": "High",
		"title": "In job sync_all, sync_rows is the deepest hot frame (8s, 80% of the job)",
		"customer_description": "d", "estimated_impact_ms": 8000, "affected_count": 1,
		"action_ref": "0", "technical_detail_json": json.dumps(detail), **kw,
	}


def _node(function, filename, cumulative, children=None, lineno=10):
	return {"function": function, "filename": filename, "lineno": lineno, "kind": "python",
	        "cumulative_ms": cumulative, "self_ms": 0, "children": children or []}


class TestEmitter:
	def test_fallback_finding_names_the_job_method(self):
		tree = _node("<root>", "", 10000, [_node("sync_rows", _USER_FILE, 8000, lineno=12)])
		with patch.object(call_tree, "_walk_for_findings"):
			out = call_tree._emit_per_action_findings(tree, 3, "RQ Job: sync_all", 10000)
		assert [f["finding_type"] for f in out] == ["Slow Background Job"]
		assert json.loads(out[0]["technical_detail_json"])["job_method"] == "sync_all"


class TestGate:
	def test_eligible_type(self):
		assert "Slow Background Job" in ai_fix.AI_ELIGIBLE_FINDING_TYPES

	def test_user_frame_passes(self):
		with patch("optimus.settings.get_config", return_value=_cfg()):
			assert ai_fix.llm_gate_note(_job()) is None

	def test_no_frame_is_refused(self):
		for detail in ({}, {"function": "f"}, {"filename": _USER_FILE}, {"filename": _USER_FILE, "lineno": 0},
		               {"filename": _USER_FILE, "lineno": "12"}, {"filename": "", "lineno": 3}):
			with patch("optimus.settings.get_config", return_value=_cfg()):
				assert ai_fix.llm_gate_note(_job(detail)) == ai_grounding.JOB_NO_FRAME_NOTE

	def test_framework_frame_is_refused(self):
		detail = {"filename": "/home/b/apps/frappe/frappe/model/document.py", "lineno": 5}
		with patch("optimus.settings.get_config", return_value=_cfg()):
			note = ai_fix.llm_gate_note(_job(detail))
		assert "framework or library code" in note

	def test_render_dict_shape_is_gated_too(self):
		finding = {"finding_type": "Slow Background Job", "technical_detail": {}}
		assert ai_fix.llm_gate_note(finding) == ai_grounding.JOB_NO_FRAME_NOTE

	def test_gate_that_raises_fails_closed(self):
		lines = []
		with patch("optimus.settings.get_config", return_value=_cfg()), \
			patch.object(ai_grounding, "slow_job_gate", side_effect=RuntimeError("boom")), \
			patch("optimus.safe_call.log_error_line", side_effect=lines.append):
			note = ai_fix.llm_gate_note(_job())
		assert note == ai_grounding.JOB_GATE_CHECK_FAILED_NOTE
		assert lines == ["optimus: slow-job gate failed: RuntimeError"]

	def test_suggest_fix_refuses_a_gated_job_before_any_request(self):
		with patch("optimus.settings.get_config", return_value=_cfg()), \
			patch.object(ai_fix, "_provider_config") as provider:
			try:
				ai_fix.suggest_fix({"finding_type": "Slow Background Job", "technical_detail": {}})
			except ai_fix.AiFixError as e:
				assert e.kind == "not_eligible"
			else:
				raise AssertionError("expected AiFixError")
		provider.assert_not_called()


class TestSelection:
	def _rows(self):
		return [
			SimpleNamespace(name="a", llm_fix_json=None, **_job()),
			SimpleNamespace(name="b", llm_fix_json=None, **_job({})),
			SimpleNamespace(name="c", llm_fix_json=None, **_job({"filename": _USER_FILE, "lineno": 3}, severity="Low")),
		]

	def test_ordered_with_other_types_and_gated_counted(self):
		n1 = SimpleNamespace(
			name="n", llm_fix_json=None, **{**_job(), "finding_type": "N+1 Query", "severity": "Low", "estimated_impact_ms": 1},
		)
		with patch("optimus.settings.get_config", return_value=_cfg()):
			picked = analyze.eligible_findings([n1, *self._rows()], _cfg())
		assert [r.name for r in picked] == ["a", "c", "n"]  # High first, then Low by larger impact
		assert picked.gated == 1

	def test_exclusion_list_skips_the_type(self):
		cfg = _cfg(ai_excluded_finding_types=("Slow Background Job",))
		with patch("optimus.settings.get_config", return_value=cfg):
			picked = analyze.eligible_findings(self._rows(), cfg)
		assert list(picked) == [] and picked.excluded == 3


class TestPrompt:
	def _payload(self, tmp_path):
		src = tmp_path / "jobs.py"
		src.write_text("def sync_rows(rows):\n\tfor r in rows:\n\t\tupdate(r)\n")
		detail = {"function": "sync_rows", "filename": str(src), "lineno": 3, "cumulative_ms": 8000,
		          "action_wall_time_ms": 10000, "job_method": "myapp.jobs.sync_all"}
		row = SimpleNamespace(**_job(detail), llm_fix_json=None)
		return analyze.ai_payload_for_finding(
			row, {},
			recordings_by_uuid={"u": {"calls": [
				{"query": "SELECT * FROM `tabItem` WHERE name = 'secret-1'", "duration": 900},
			]}},
			actions_by_idx={0: {"recording_uuid": "u"}},
		)

	def test_prompt_carries_job_hot_frame_source_and_slow_sql(self, tmp_path):
		payload = self._payload(tmp_path)
		_, messages, shown = ai_fix._build_fix_request(payload, threshold_ms=1000.0, context_tokens=200000)
		content = messages[0]["content"]
		assert "Finding type: Slow Background Job" in content
		assert "myapp.jobs.sync_all" in content
		assert "sync_rows" in content and "80%" in content
		assert "update(r)" in content and shown
		assert "SELECT * FROM `tabItem`" in content
		assert "secret-1" not in content  # literals redacted unless raw values are enabled
		assert "background job is slow" in content

	def test_without_job_method_the_line_is_omitted(self, tmp_path):
		payload = self._payload(tmp_path)
		payload["technical_detail"].pop("job_method")
		_, messages, _ = ai_fix._build_fix_request(payload, threshold_ms=1000.0, context_tokens=200000)
		assert "Background job (the method" not in messages[0]["content"]

	def test_other_types_never_get_the_job_line(self, tmp_path):
		payload = self._payload(tmp_path)
		payload["finding_type"] = "N+1 Query"
		_, messages, _ = ai_fix._build_fix_request(payload, threshold_ms=1000.0, context_tokens=200000)
		assert "Background job (the method" not in messages[0]["content"]

	def test_job_method_is_clipped(self, tmp_path):
		payload = self._payload(tmp_path)
		payload["technical_detail"]["job_method"] = "m" * 5000
		_, messages, _ = ai_fix._build_fix_request(payload, threshold_ms=1000.0, context_tokens=200000)
		assert "m" * 400 not in messages[0]["content"]


class TestReport:
	def _doc(self, llm_fix):
		detail = {
			"function": "sync_rows", "filename": _USER_FILE, "lineno": 12, "job_method": "sync_all",
			"callsite": {"filename": _USER_FILE, "lineno": 12, "function": "sync_rows",
			             "source_snippet": [{"lineno": 12, "content": "update(r)"}]},
		}
		finding = SimpleNamespace(
			finding_type="Slow Background Job", severity="High", title="In job sync_all, sync_rows is hot",
			customer_description="A job is slow.", estimated_impact_ms=8000.0, affected_count=1, action_ref="0",
			technical_detail_json=json.dumps(detail),
			llm_fix_json=json.dumps(llm_fix) if llm_fix else None,
		)
		from optimus.tests.test_finding_card_ai_fix import _doc
		return _doc([finding])

	def test_stored_suggestion_renders_on_the_job_card(self):
		doc = self._doc({
			"suggestion": "**Diagnosis**\n\nThe job updates row by row.\n\n**Fix**\n\nBatch the updates.\n\n"
			"**Why it works**\n\nFewer round-trips.",
			"model": "m", "provider": "Anthropic", "generated_at": "2026-10-27T00:00:00+00:00",
		})
		with patch("optimus.settings.get_config", return_value=settings.OptimusConfig(ai_suggest_findings=True)):
			html = renderer.render_raw(doc, recordings=[])
		assert 'class="fix-box"' in html and "Batch the updates" in html

	def test_no_block_without_a_stored_suggestion(self):
		with patch("optimus.settings.get_config", return_value=settings.OptimusConfig(ai_suggest_findings=True)):
			html = renderer.render_raw(self._doc(None), recordings=[])
		assert 'class="fix-box"' not in html
