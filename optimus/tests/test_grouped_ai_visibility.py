# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Saved answers survive grouping and remain reachable from their own jobs."""

import json
from collections import Counter
from copy import deepcopy
from html.parser import HTMLParser

import pytest

from optimus import renderer
from optimus.ai_prompts import PROMPT_VERSION
from optimus.tests.test_finding_card_ai_fix import _doc, _finding
from optimus.tests.test_report_a11y import _action


class Node:

	def __init__(self, tag="root", attrs=(), parent=None):
		self.tag, self.attrs, self.parent = tag, dict(attrs), parent
		self.children, self.parts = [], []

	def all(self, tag=None, cls=None):
		return [n for c in self.children for n in [c, *c.all()]
		        if (tag is None or n.tag == tag)
		        and (cls is None or cls in n.attrs.get("class", "").split())]

	@property
	def text(self):
		return " ".join(self.parts + [c.text for c in self.children])


class Report(HTMLParser):

	def __init__(self, html):
		super().__init__()
		self.root = self.current = Node()
		self.feed(html)

	def handle_starttag(self, tag, attrs):
		node = Node(tag, attrs, self.current)
		self.current.children.append(node)
		if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
			self.current = node

	def handle_startendtag(self, tag, attrs):
		self.current.children.append(Node(tag, attrs, self.current))

	def handle_endtag(self, tag):
		node = self.current
		while node.parent is not None:
			if node.tag == tag:
				self.current = node.parent
				return
			node = node.parent

	def handle_data(self, data):
		self.current.parts.append(data)

	def section(self, section_id):
		return next(n for n in self.root.all() if n.attrs.get("id") == section_id)


def saved_answer(label="Remove the unused query", **kw):
	return {
		"suggestion": f"**Diagnosis**\n\n{label}.\n\n**Fix**\n\n```diff\n-unused_read()\n```\n",
		"prompt_version": PROMPT_VERSION, "completion_status": "complete", "finish_reason": "stop",
		"source_available": True, "model": "saved-model", "tokens": {"total_tokens": 123},
		**kw,
	}


def grouped_doc(*, child_ref="0", answer=None):
	parent = _finding(finding_type="Slow Hot Path")
	parent.name, parent.title = "private-parent-row", "bg_long_running_job took 63.03s"
	parent.estimated_impact_ms, parent.affected_count = 63032.67, 1
	child = _finding(llm_fix=answer if answer is not None else saved_answer())
	child.name, child.title, child.action_ref = "private-child-row", "Same query ran 65× at foo.py:371", child_ref
	child.estimated_impact_ms, child.affected_count = 110.18, 65
	for row, lineno in ((parent, 330), (child, 371)):
		detail = json.loads(row.technical_detail_json)
		detail["callsite"].update(lineno=lineno, function="bg_long_running_job",
		                          source_snippet=[{"lineno": lineno, "content": "unused_read()"}])
		if row is child:
			detail.update(total_time_ms=113.68, loop_count=65, run_count=1, impact_scope_label="recoverable")
		row.technical_detail_json = json.dumps(detail)
	doc = _doc([parent, child])
	doc.actions = [_action(i, event_type="RQ Job", action_label=f"RQ Job: job_{i}",
	                       path=f"myapp.jobs.job_{i}", duration_ms=64000) for i in range(2)]
	return doc


def test_saved_child_answer_is_visible_once_and_linked_from_job():
	doc = grouped_doc()
	before = [f.llm_fix_json for f in doc.findings]
	html = renderer.render_raw(doc, recordings=[])
	report = Report(html)
	boxes = report.root.all(cls="fix-box")
	assert len(boxes) == 1, "the paid, saved child answer was lost in grouping"
	assert "Remove the unused query" in boxes[0].text
	assert "Related findings in this function" in report.section("findings").text
	assert "one fix resolves all" not in html
	links = report.section("background-jobs").all(cls="ai-suggestion-link")
	assert len(links) == 1
	assert links[0].attrs["href"] == "#" + boxes[0].attrs["id"]
	assert "Same query ran 65" in links[0].text
	assert not report.section("background-jobs").all(cls="fix-box")
	assert not report.section("per-action").all(cls="fix-box")
	assert [f.llm_fix_json for f in doc.findings] == before
	assert "private-child-row" not in html and "private-parent-row" not in html


def assert_link_targets(report):
	ids = Counter(n.attrs["id"] for n in report.root.all() if n.attrs.get("id"))
	assert all(count == 1 for count in ids.values())
	for link in report.root.all(cls="ai-suggestion-link"):
		assert ids[link.attrs["href"][1:]] == 1
	for section in ("per-action", "background-jobs"):
		assert not [n for n in report.section(section).all() if n.attrs.get("id", "").startswith("ai-finding-")]
	assert not report.root.all("base") and not report.root.all("script")


def job_links(report):
	"""Associate link-bearing detail rows with the preceding job's main row."""
	result, name = {}, None
	for row in report.section("background-jobs").all("tr"):
		names = row.all(cls="action-name")
		if names:
			name = names[0].text.strip()
			result[name] = []
		if name:
			result[name].extend(row.all(cls="ai-suggestion-link"))
	return result


def test_child_uses_own_action_reference_even_without_a_primary_card():
	report = Report(renderer.render_raw(grouped_doc(child_ref=" 1 "), []))
	links = job_links(report)
	assert not links["job_0"]
	assert len(links["job_1"]) == 1
	assert_link_targets(report)


def test_parent_and_multiple_children_render_once_with_stable_unique_ids():
	doc = grouped_doc(child_ref="1")
	doc.findings[0].llm_fix_json = json.dumps(saved_answer("Parent diagnosis"))
	third = deepcopy(doc.findings[1])
	third.name, third.title, third.action_ref = "third-row", "Another repeated query", "0"
	third.llm_fix_json = json.dumps(saved_answer("Third diagnosis"))
	doc.findings.append(third)
	report = Report(renderer.render_raw(doc, []))
	assert_link_targets(report)
	assert len(report.root.all(cls="fix-box")) == 3
	assert len(job_links(report)["job_0"]) == 2
	assert len(job_links(report)["job_1"]) == 1
	before = {n.text: n.attrs["id"] for n in report.root.all(cls="fix-box")}
	doc.findings.reverse()
	after = Report(renderer.render_raw(doc, []))
	assert {n.text: n.attrs["id"] for n in after.root.all(cls="fix-box")} == before


@pytest.mark.parametrize("names", [(None, None), ("same-private-name", "same-private-name")])
def test_legacy_and_duplicate_row_names_have_unique_deterministic_targets(names):
	doc = grouped_doc()
	for row, name in zip(doc.findings, names, strict=True):
		row.name = name
		row.llm_fix_json = json.dumps(saved_answer(row.title))
	first = Report(renderer.render_raw(doc, []))
	second = Report(renderer.render_raw(doc, []))
	assert_link_targets(first)
	assert [n.attrs["id"] for n in first.root.all(cls="fix-box")] == [n.attrs["id"] for n in second.root.all(cls="fix-box")]


@pytest.mark.parametrize("status,badge", [
	("complete", "Unverified: review before applying"),
	("incomplete", "Incomplete response"),
	("unknown", "Completion unconfirmed"),
	("blocked", "Provider blocked response"),
	("processing_limited", "Response needs local review"),
])
def test_grouped_status_sanitization_and_retained_response_privacy(status, badge):
	answer = saved_answer(completion_status=status, source_available=False,
	                      suggestion="**Diagnosis**\n\nSaved diagnosis.<script>evil_canary()</script>",
	                      received_responses=[{"content": "PRIVATE_PARTIAL_CANARY"}],
	                      provider_payload={"api_key": "PRIVATE_PROVIDER_CANARY"})
	report_html = renderer.render_raw(grouped_doc(answer=answer), [])
	report = Report(report_html)
	assert badge in report.root.all(cls="fix-box")[0].text
	assert "directional guidance" in report.root.all(cls="fix-box")[0].text
	assert "PRIVATE_PARTIAL_CANARY" not in report_html and "PRIVATE_PROVIDER_CANARY" not in report_html
	assert "evil_canary" not in report_html
	assert ("View AI suggestion" if status == "complete" else "View AI response") in report.root.all(cls="ai-suggestion-link")[0].text
	assert_link_targets(report)


def test_grouped_sql_policy_withholds_code_and_keeps_diagnosis():
	answer = saved_answer(suggestion='**Diagnosis**\n\nAvoid this repeated read.\n\n**Fix**\n\n```diff\n+frappe.db.sql("select SQL_CANARY")\n```')
	html = renderer.render_raw(grouped_doc(answer=answer), [])
	report = Report(html)
	box = report.root.all(cls="fix-box")[0]
	assert "Frappe API update needed" in box.text
	assert "Avoid this repeated read" in box.text
	assert "SQL_CANARY" not in html
	assert "View AI response" in report.root.all(cls="ai-suggestion-link")[0].text


def test_grouped_outdated_answer_preserves_status():
	report = Report(renderer.render_raw(grouped_doc(answer=saved_answer(prompt_version=1)), []))
	assert "earlier version" in report.root.all(cls="fix-box")[0].text
	assert "Outdated AI suggestion" in report.section("findings").text
	assert "View AI response" in report.root.all(cls="ai-suggestion-link")[0].text


@pytest.mark.parametrize("mode", ["off", "empty", "malformed", "filtered", "retired"])
def test_hidden_answers_create_no_boxes_indicators_or_links(monkeypatch, mode):
	from optimus import settings

	doc = grouped_doc()
	if mode == "off":
		monkeypatch.setattr(settings, "get_config", lambda: settings.OptimusConfig(ai_suggest_findings=False))
	elif mode == "empty":
		doc.findings[1].llm_fix_json = json.dumps({"suggestion": "   "})
	elif mode == "malformed":
		doc.findings[1].llm_fix_json = "[malformed"
	elif mode == "filtered":
		doc.findings[1].finding_type = "Function Not Invoked"
	else:
		doc.findings[1].finding_type = "Framework N+1"
	report = Report(renderer.render_raw(doc, []))
	assert not report.root.all(cls="fix-box")
	assert not report.root.all(cls="ai-suggestion-link")
	assert "AI suggestion available" not in report.section("findings").text


@pytest.mark.parametrize("ref", [None, "", "missing-action", {}, False])
def test_unmatched_references_keep_canonical_answer_without_guessed_job_link(ref):
	report = Report(renderer.render_raw(grouped_doc(child_ref=ref), []))
	assert len(report.root.all(cls="fix-box")) == 1
	assert not report.root.all(cls="ai-suggestion-link")


def test_observational_saved_answer_can_link_without_adding_actionable_card():
	doc = grouped_doc(child_ref="1")
	for row in doc.findings:
		row.finding_type = "Repeated Hot Frame"
	report = Report(renderer.render_raw(doc, []))
	assert len(report.root.all(cls="fix-box")) == 1
	assert len(job_links(report)["job_1"]) == 1
	assert not report.section("background-jobs").all(cls="finding")
	assert_link_targets(report)


def test_observed_query_timing_and_estimate_scope_are_distinct_from_function_time():
	report = Report(renderer.render_raw(grouped_doc(), []))
	findings = report.section("findings")
	assert "63.03s" in findings.text
	assert "Observed query time: 113.68ms total." in findings.text
	assert "recoverable" in findings.all(cls="sub-impact")[0].text
	assert "related line:" in findings.all(cls="finding-meta")[0].text
	assert "function time is not a measurement of this line" in findings.text
	assert "pinned here to the actual hot line" not in findings.text


def test_legacy_scope_fallback_and_missing_measurements_are_not_fabricated():
	doc = grouped_doc()
	detail = json.loads(doc.findings[1].technical_detail_json)
	for key in ("impact_scope_label", "total_time_ms", "loop_count", "run_count"):
		detail.pop(key)
	doc.findings[1].technical_detail_json = json.dumps(detail)
	report = Report(renderer.render_raw(doc, []))
	assert "consolidated" in report.section("findings").all(cls="sub-impact")[0].text
	assert "Observed query time" not in report.root.text


def test_render_never_calls_provider_or_changes_token_accounting(monkeypatch):
	from optimus import ai_fix

	def forbidden(*args, **kwargs):
		pytest.fail("A render must reuse saved answers without calling a provider")

	monkeypatch.setattr(ai_fix, "_dispatch_call", forbidden)
	monkeypatch.setattr(ai_fix.requests, "post", forbidden)
	doc = grouped_doc()
	before = deepcopy(vars(doc))
	report = Report(renderer.render_raw(doc, []))
	assert vars(doc) == before
	assert "123" in report.section("findings").text
	assert "246" not in report.section("findings").text


def test_pdf_expansion_includes_canonical_child_answer_once():
	from optimus.pdf_export import _expand_collapsible_sections

	html = renderer.render_raw(grouped_doc(), [])
	expanded = _expand_collapsible_sections(html)
	report = Report(expanded)
	assert all("open" in d.attrs for d in report.root.all("details"))
	assert len(report.root.all(cls="fix-box")) == 1
	assert "Remove the unused query" in report.root.all(cls="fix-box")[0].text
	assert_link_targets(report)
