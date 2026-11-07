# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""A background job recording is labelled as a job, never as a GET request.

Frappe's recorder writes ``event_type = "Background Job"`` (v15 and v16); the
dotted method sits in ``path`` while ``cmd``, ``method`` and ``form_dict`` are
None.
"""

import json
import types
from pathlib import Path

import pytest

from optimus import ai_fix, ai_privacy, analyze
from optimus.analyzers import call_tree, per_action
from optimus.analyzers.base import AnalyzeContext
from optimus.renderer import source_resolution
from optimus.report_context import build_report_context
from optimus.tests.test_report_context import _ctx, _doc

JOB_PATH = "erpnext.accounts.utils.repost_gle"
GET_LABEL = f"GET {JOB_PATH}"
JOB_LABEL = "RQ Job: repost_gle"
ACTION_JSON = Path(per_action.__file__).parents[1] / "optimus/doctype/optimus_action/optimus_action.json"


def _job_v16(event_type="Background Job"):
	return {
		"uuid": "j1",
		"event_type": event_type,
		"path": JOB_PATH,
		"cmd": None,
		"method": None,
		"headers": None,
		"form_dict": None,
		"duration": 5000.0,
		"calls": [],
		"queries": 0,
		"number_of_events": 0,
		"apps_involved": [],
	}


def _job_v15(event_type="Background Job"):
	return {
		"uuid": "j1",
		"event_type": event_type,
		"path": JOB_PATH,
		"cmd": None,
		"method": None,
		"headers": None,
		"form_dict": None,
		"duration": 5000.0,
		"calls": [],
	}


JOBS = [
	pytest.param(make(et), id=f"{name}-{et}")
	for name, make in (("v15", _job_v15), ("v16", _job_v16))
	for et in ("Background Job", "RQ Job")
]

HTTP_V16 = {
	"uuid": "h1",
	"event_type": "HTTP Request",
	"path": "/api/method/frappe.client.save",
	"cmd": "frappe.client.save",
	"method": "POST",
	"form_dict": {},
	"duration": 10.0,
	"calls": [],
}


@pytest.mark.parametrize("job", JOBS)
def test_job_label_and_http_method(job):
	assert per_action._label(job) == JOB_LABEL
	assert per_action.humanized_label(job) == JOB_LABEL
	action = per_action._build_action(job)
	assert action["action_label"] == JOB_LABEL
	assert action["http_method"] == ""


def test_emitted_event_type_is_a_select_option():
	field = next(f for f in json.loads(ACTION_JSON.read_text())["fields"] if f["fieldname"] == "event_type")
	options = field["options"].split("\n")
	for rec in (HTTP_V16, _job_v15(), _job_v16()):
		assert per_action._build_action(rec)["event_type"] in options


@pytest.mark.parametrize("hostile", [["Background Job"], {"a": 1}, None, 5, "x" * 5000, "Background Job "])
def test_hostile_event_type_never_raises_and_is_not_a_job(hostile):
	rec = {**_job_v16(), "event_type": hostile}
	assert not per_action.is_background_job(hostile)
	assert per_action._label(rec).startswith("GET ")
	per_action.humanized_label(rec)


def test_predicate_accepts_both_values():
	assert per_action.is_background_job("Background Job")
	assert per_action.is_background_job("RQ Job")
	assert not per_action.is_background_job("HTTP Request")


# ---- call tree ---------------------------------------------------------------------------


def _node(function, filename, cumulative, self_ms, children=None):
	return {
		"function": function,
		"filename": filename,
		"lineno": 10,
		"kind": "python",
		"cumulative_ms": cumulative,
		"self_ms": self_ms,
		"children": children or [],
	}


def _root(body):
	return _node(
		"<root>", "", 5000, 0, [_node("execute_job", "site-packages/rq/worker.py", 5000, 200, [body])]
	)


def _findings(tree, job):
	label = per_action._build_action(job)["action_label"]
	return call_tree._emit_per_action_findings(
		tree, action_idx=0, action_label=label, action_wall_time_ms=5000
	)


@pytest.mark.parametrize("job", [_job_v16(), _job_v15()])
def test_job_body_that_is_tracked_user_code_names_its_callee(job):
	hot = _node("process_invoice_line", "apps/my_app/invoice.py", 4500, 4500)
	body = _node("repost_gle", "apps/erpnext/erpnext/accounts/utils.py", 4800, 300, [hot])
	findings = _findings(_root(body), job)
	assert findings
	detail = json.loads(findings[0]["technical_detail_json"])
	assert detail["function"] == "process_invoice_line"
	assert findings[0]["title"].startswith("In job repost_gle")


def test_job_body_with_only_self_time_is_a_hot_path_not_a_tautology():
	body = _node("repost_gle", "apps/erpnext/erpnext/accounts/utils.py", 4800, 4800)
	findings = _findings(_root(body), _job_v16())
	assert findings
	assert "spent in repost_gle" not in findings[0]["customer_description"]
	assert "self-time hot path" in findings[0]["title"]


def test_sql_heavy_job_gets_slow_background_job_finding():
	user_frame = _node("trickle", "apps/my_app/work.py", 2400, 2400)
	body = _node("long_running_job", "apps/my_app/jobs.py", 2600, 200, [user_frame])
	tree = _node(
		"<root>", "", 12000, 0, [_node("execute_job", "site-packages/rq/worker.py", 12000, 9400, [body])]
	)
	label = per_action._build_action(_job_v16())["action_label"]
	findings = call_tree._emit_per_action_findings(
		tree, action_idx=0, action_label=label, action_wall_time_ms=12000
	)
	assert [f["finding_type"] for f in findings] == ["Slow Background Job"]
	assert findings[0]["title"].startswith("In job repost_gle")


# ---- Steps and AI input ----------------------------------------------------------------


def test_auto_steps_name_the_job():
	html = analyze._build_auto_notes_list_html([HTTP_V16, _job_v16()])
	assert JOB_LABEL in html
	assert GET_LABEL not in html


@pytest.mark.parametrize("send_raw", [True, False])
def test_humanizer_actions_label_the_job(send_raw):
	actions = analyze.actions_for_humanizer([_job_v16()], send_raw=send_raw)
	assert actions[0]["label"] == JOB_LABEL
	assert ai_privacy.private_action(actions[0])["label"] == JOB_LABEL


def test_steps_prompt_names_the_job_in_private_mode():
	actions = analyze.actions_for_humanizer([_job_v16()], send_raw=False)
	_, messages = ai_fix._build_steps_messages(actions, session_title="")
	text = json.dumps(messages)
	assert JOB_LABEL in text
	assert "GET erpnext" not in text


def test_private_action_is_idempotent_and_keeps_event_type():
	once = ai_privacy.private_action(
		{"path": JOB_PATH, "event_type": "Background Job", "label": "x", "duration_ms": 5}
	)
	assert once["event_type"] == "Background Job"
	assert ai_privacy.private_action(once) == once
	assert ai_privacy.private_action(once)["label"] == JOB_LABEL


@pytest.mark.parametrize(
	"hostile", ["<b>SECRET-CANARY</b>", "x" * 5000, ["SECRET-CANARY"], {"k": "SECRET-CANARY"}]
)
def test_private_action_drops_hostile_event_type(hostile):
	out = ai_privacy.private_action({"path": JOB_PATH, "event_type": hostile})
	assert out["event_type"] == ""
	assert "SECRET-CANARY" not in json.dumps(out)
	assert len(json.dumps(out)) < 1000


# ---- summary ---------------------------------------------------------------------------


def _summary(rec, stored_title, stored_label):
	ctx = AnalyzeContext(session_uuid="s", docname="d")
	action = per_action._build_action(rec)
	action["action_label"] = stored_label
	ctx.actions = [action]
	ctx.findings = [
		{
			"finding_type": "Slow Hot Path",
			"severity": "High",
			"title": stored_title,
			"estimated_impact_ms": 4800,
			"action_ref": "0",
		}
	]
	return analyze._build_summary_html(ctx, 0, [rec])


def test_summary_drops_the_job_prefix_of_a_new_finding_title():
	out = _summary(
		_job_v16(), "In job repost_gle, 96% of its wall time was spent in process_invoice_line", JOB_LABEL
	)
	assert "went into <strong>96% of its wall time" in out
	assert "In job" not in out


def test_summary_drops_the_prefix_of_an_old_session_title():
	old_title = f"In {GET_LABEL}, 96% of its wall time was spent in process_invoice_line"
	out = _summary(_job_v16(), old_title, GET_LABEL)
	assert "went into <strong>96% of its wall time" in out
	assert "In RQ Job" not in out


# ---- render side -----------------------------------------------------------------------


def test_report_actions_mark_a_background_job_event_as_bg():
	actions = [{"action_label": JOB_LABEL, "event_type": "Background Job", "duration_ms": 100}]
	out = build_report_context(_doc(), _ctx(actions=actions))
	assert out["actions"][0]["kind"] == "bg"


def test_source_resolution_reads_the_job_path():
	action = {"event_type": "Background Job", "path": "erpnext.x.y"}
	assert source_resolution._action_dotted_entry(action) == "erpnext.x.y"


def test_source_resolution_keeps_a_job_path_as_recorded():
	action = {"event_type": "Background Job", "path": "erpnext.x.y?z"}
	assert source_resolution._action_dotted_entry(action) == "erpnext.x.y?z"


def test_private_action_keeps_http_request_event_type():
	out = ai_privacy.private_action({"path": "/api/method/x", "event_type": "HTTP Request"})
	assert out["event_type"] == "HTTP Request"


@pytest.mark.parametrize("path", [None, "", "mod.fn."])
def test_job_label_without_a_usable_method_tail(path):
	label = per_action._label({**_job_v16(), "path": path})
	assert label == ("RQ Job" if not path else "RQ Job: mod.fn.")


def test_job_with_a_cmd_still_gets_the_job_label():
	rec = {**_job_v16(), "cmd": "frappe.client.get_list", "form_dict": {"doctype": "User"}}
	assert per_action.humanized_label(rec) == JOB_LABEL


def test_actions_carry_no_inline_finding_field():
	findings = [{"severity": "High", "title": "Slow loop", "action_ref": "1"}]
	actions = [{"action_label": "save", "duration_ms": 1}]
	out = build_report_context(_doc(), _ctx(actions=actions, findings=findings))
	assert all("finding_inline_html" not in a for a in out["actions"] + out["actions_framework"])


def _action_row(event_type, label, path=JOB_PATH):
	return types.SimpleNamespace(
		action_label=label, event_type=event_type, http_method="", path=path,
		recording_uuid="", duration_ms=5000, queries_count=0, query_time_ms=0,
		slowest_query_ms=0,
	)


def _render_actions(actions):
	from optimus import renderer
	from optimus.tests.test_render_duration_threshold import _doc as render_doc

	html = renderer.render_raw(render_doc(actions), recordings=[])
	start = html.index('id="per-action"')
	return html[start : html.index("</table>", start)]


@pytest.mark.parametrize("event_type", ["Background Job", "RQ Job"])
def test_rendered_report_shows_a_job_row_with_label_pill_and_bg_class(event_type):
	section = _render_actions([_action_row(event_type, GET_LABEL)])
	assert f'<div class="action-name">{JOB_LABEL}</div>' in section
	assert GET_LABEL not in section
	assert '<span class="type-tag bg">RQ Job</span>' in section


def test_rendered_report_keeps_a_page_request_row_blue():
	section = _render_actions([_action_row("HTTP Request", "GET /app/todo", path="/app/todo")])
	assert '<span class="type-tag">HTTP Request</span>' in section


def test_action_rows_carry_one_job_flag_from_the_shared_predicate():
	from optimus.report_context import _build_actions

	rows = _build_actions([
		{"event_type": "Background Job"}, {"event_type": "RQ Job"}, {"event_type": "HTTP Request"},
	])
	assert [r["is_background_job"] for r in rows] == [True, True, False]
	assert [r["kind"] for r in rows] == ["bg", "bg", "http"]
