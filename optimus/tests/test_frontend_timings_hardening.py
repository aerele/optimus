# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""A malformed browser timing must be dropped, never take the Frontend section or the report down."""

import json

import pytest

from optimus import renderer, report_context
from optimus.analyzers import frontend_timings
from optimus.analyzers.base import AnalyzeContext
from optimus.tests import test_renderer_structure_snapshot as snapshot
from optimus.tests.test_renderer_structure_snapshot import _index_evidence  # noqa: F401

REC = [{"uuid": "rec1", "method": "POST", "path": "/api/method/x"}]
HUGE = 10**400


def _analyze(xhr=(), vitals=()):
	ctx = AnalyzeContext(session_uuid="s", docname="d")
	ctx.frontend_data = {"xhr": list(xhr), "vitals": list(vitals)}
	return frontend_timings.analyze(REC, ctx)


def _xhr(**kw):
	row = {
		"recording_id": "rec1",
		"url": "/x",
		"duration_ms": 120,
		"status": 200,
		"response_size_bytes": 900,
		"transport": "xhr",
		"timestamp": 5,
	}
	row.update(kw)
	return row


def _nav(**kw):
	row = {
		"name": "navigation",
		"page_url": "/app",
		"ttfb_ms": 90,
		"dom_content_loaded_ms": 400,
		"load_ms": 600,
		"dns_ms": 1,
		"tcp_ms": 2,
		"timestamp": 5,
	}
	row.update(kw)
	return row


def _lcp(value="x", **kw):
	row = {"name": "lcp", "page_url": "/app", "value_ms": value, "timestamp": 6}
	row.update(kw)
	return row


GOOD = [
	{"name": "fcp", "page_url": "/ok", "value_ms": 300, "timestamp": 1},
	{"name": "lcp", "page_url": "/ok", "value_ms": 900, "timestamp": 2},
	{"name": "cls", "page_url": "/ok", "value": 0.05, "timestamp": 3},
]

BAD_VITALS = {
	"string ttfb": [_nav(ttfb_ms="9")],
	"text ttfb": [_nav(ttfb_ms="slow")],
	"list ttfb": [_nav(ttfb_ms=[1])],
	"text timestamp": [_lcp(500, timestamp="x")],
	"huge timestamp": [_lcp(500, timestamp=HUGE)],
	"huge value": [_lcp(HUGE)],
	"text lcp": [_lcp("fast")],
	"list lcp": [_lcp([1])],
	"text cls": [{"name": "cls", "page_url": "/app", "value": "bad"}],
	"list page": [{"name": "fcp", "page_url": ["/a"], "value_ms": 1}],
	"dict page": [{"name": "fcp", "page_url": {"a": 1}, "value_ms": 1}],
	"non-dict row": ["fcp", 7, None, [1]],
	"nan text": [_lcp("nan"), _nav(load_ms="inf")],
}

BAD_XHR = {
	"huge duration": [_xhr(duration_ms=HUGE)],
	"huge orphan duration": [_xhr(recording_id="other", duration_ms=HUGE)],
	"text duration": [_xhr(duration_ms="slow")],
	"list duration": [_xhr(duration_ms=[1])],
	"text size": [_xhr(response_size_bytes="big")],
	"huge size": [_xhr(response_size_bytes=HUGE)],
	"list recording id": [_xhr(recording_id=["rec1"])],
	"dict recording id": [_xhr(recording_id={"a": 1})],
	"text orphan timestamp": [_xhr(recording_id="other", timestamp="x")],
	"non-dict row": ["x", 3, None],
	"dict url": [_xhr(url={"a": "<b>"})],
	"list url": [_xhr(url=["/x"])],
	"dict orphan url": [_xhr(recording_id="other", url={"a": 1})],
	"dict status": [_xhr(status={"a": 1})],
	"text status": [_xhr(status="oops")],
	"list transport": [_xhr(transport=["xhr"])],
}


def _build(result):
	return report_context._build_frontend(dict(result.aggregate))


def _assert_numbers_only(value):
	"""Every number the report formats must be a real number, or absent."""
	if isinstance(value, dict):
		for v in value.values():
			_assert_numbers_only(v)
	elif isinstance(value, list):
		for v in value:
			_assert_numbers_only(v)
	if isinstance(value, float):
		assert value == value and abs(value) != float("inf")


@pytest.mark.parametrize("case", BAD_VITALS)
def test_a_bad_vital_never_crashes_analyze_or_the_report_context(case):
	result = _analyze(vitals=BAD_VITALS[case] + GOOD)
	frontend = _build(result)
	rows = {r["url"]: r for r in frontend["web_vitals"]}
	assert rows["/ok"]["fcp_display"] == "300ms"
	assert rows["/ok"]["lcp_display"] == "900ms"
	assert rows["/ok"]["cls_display"] == "0.050"
	_assert_numbers_only(result.aggregate)


@pytest.mark.parametrize("case", BAD_XHR)
def test_a_bad_xhr_never_crashes_analyze_or_the_report_context(case):
	result = _analyze(xhr=BAD_XHR[case] + [_xhr(url="/good", duration_ms=77)])
	_build(result)
	assert any(m["url"] == "/good" and m["xhr_ms"] == 77 for m in result.aggregate["frontend_xhr_matched"])
	_assert_numbers_only(result.aggregate)


def test_a_numeric_string_is_read_as_the_number_it_spells():
	result = _analyze(vitals=[_nav(ttfb_ms="9")])
	assert result.aggregate["frontend_vitals_by_page"]["/app"]["ttfb_ms"] == 9.0


def test_a_huge_timestamp_does_not_decide_which_lcp_wins():
	result = _analyze(vitals=[_lcp(500, timestamp=1), _lcp(900, timestamp=HUGE), _lcp(700, timestamp=2)])
	assert result.aggregate["frontend_vitals_by_page"]["/app"]["lcp_ms"] == 700


def test_a_bad_row_does_not_hide_good_rows_in_the_same_payload():
	result = _analyze(
		xhr=[_xhr(duration_ms="slow"), _xhr(url="/good", duration_ms=40)],
		vitals=[_lcp([1]), *GOOD, "junk"],
	)
	assert result.aggregate["frontend_summary"]["total_xhrs"] == 2
	assert set(result.aggregate["frontend_vitals_by_page"]) >= {"/ok"}
	assert result.aggregate["frontend_orphans"] == []


def test_non_list_payload_sections_are_ignored():
	ctx = AnalyzeContext(session_uuid="s", docname="d")
	ctx.frontend_data = {"xhr": "oops", "vitals": {"a": 1}}
	result = frontend_timings.analyze(REC, ctx)
	assert result.aggregate["frontend_summary"]["total_xhrs"] == 0


def test_the_whole_report_renders_with_every_bad_row_present(monkeypatch):
	bad_vitals = [row for rows in BAD_VITALS.values() for row in rows]
	bad_xhr = [row for rows in BAD_XHR.values() for row in rows]
	result = _analyze(xhr=bad_xhr + [_xhr()], vitals=bad_vitals + GOOD)
	aggregate = json.loads(json.dumps(result.aggregate, default=str))
	base = snapshot._v5_aggregate()
	base.update(aggregate)
	monkeypatch.setattr(snapshot, "_v5_aggregate", lambda: base)
	html_text = renderer.render_raw(snapshot._snapshot_doc(), recordings=[])
	assert 'id="frontend"' in html_text
	assert "/ok" in html_text


def test_helpers_reject_non_finite_and_oversized_numbers():
	assert frontend_timings._num(HUGE) is None
	assert frontend_timings._num(float("nan")) is None
	assert frontend_timings._num("inf") is None
	assert frontend_timings._num(True) is None
	assert frontend_timings._num("3000") == 3000.0


def test_the_frontend_section_tells_the_user_how_to_get_a_page_load():
	html_text = renderer.render_raw(snapshot._snapshot_doc(), recordings=[])
	text = " ".join(html_text.split())
	assert (
		"Page-load timings (FCP, LCP, CLS, TTFB, DCL) are captured only for pages loaded after you press "
		"Record. To include a page's load, reload it while recording." in text.replace("&#39;", "'")
	)


def test_xhr_text_fields_are_type_checked_before_the_report_sees_them():
	rows = [
		_xhr(url={"a": "<b>"}, status="oops", transport=["xhr"]),
		_xhr(recording_id="other", url={"a": 1}),
		_xhr(url="/ok", status="200", transport="fetch"),
	]
	result = _analyze(xhr=rows)
	frontend = _build(result)
	matched = result.aggregate["frontend_xhr_matched"]
	assert matched[0]["url"] is None and matched[0]["status"] is None and matched[0]["transport"] is None
	assert matched[1]["url"] == "/ok" and matched[1]["status"] == 200 and matched[1]["transport"] == "fetch"
	assert result.aggregate["frontend_orphans"][0]["url"] is None
	assert all(isinstance(x["meta"], str) for x in frontend["xhrs"])
