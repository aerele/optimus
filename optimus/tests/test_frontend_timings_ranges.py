# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Browser timings are validated per row: no empty buckets, no overwrites, sane ranges, drops counted."""

import pytest

from optimus import renderer
from optimus.tests import test_renderer_structure_snapshot as snapshot
from optimus.tests.test_frontend_timings_hardening import (
	GOOD,
	_analyze,
	_build,
	_lcp,
	_nav,
	_xhr,
)


ALL_BAD_NAV = dict.fromkeys(("ttfb_ms", "dom_content_loaded_ms", "load_ms", "dns_ms", "tcp_ms"), "x")


def _vitals(result):
	return result.aggregate["frontend_vitals_by_page"]


def test_an_unknown_or_empty_vital_row_makes_no_page_bucket():
	bogus = {"name": "bogus", "page_url": "/a", "value_ms": 5}
	result = _analyze(vitals=[bogus, _nav(page_url="/b", **ALL_BAD_NAV)])
	assert _vitals(result) == {}


def test_a_later_bad_row_does_not_overwrite_a_valid_value():
	fcp = {"name": "fcp", "page_url": "/a", "value_ms": 300}
	empty_nav = _nav(page_url="/a", ttfb_ms=None, load_ms="x", dns_ms=[1])
	result = _analyze(vitals=[fcp, {**fcp, "value_ms": "x"}, _nav(page_url="/a"), empty_nav])
	page = _vitals(result)["/a"]
	assert page["fcp_ms"] == 300
	assert page["ttfb_ms"] == 90 and page["load_ms"] == 600 and page["dns_ms"] == 1


def test_a_malformed_lcp_on_a_page_with_a_good_lcp_keeps_the_good_one():
	result = _analyze(vitals=[_lcp(900, timestamp=1), _lcp("x", timestamp=9), _lcp(-5, timestamp=10)])
	assert _vitals(result)["/app"]["lcp_ms"] == 900


@pytest.mark.parametrize("bad", [1e300, -5, 1e13, 700_000])
def test_out_of_range_timings_never_become_findings_or_cells(bad):
	result = _analyze(vitals=[_lcp(bad), _nav(**{key: bad for key in ALL_BAD_NAV})], xhr=[_xhr(duration_ms=bad)])
	assert result.findings == []
	assert _vitals(result) == {}
	assert result.aggregate["frontend_xhr_matched"] == []


def test_a_cls_out_of_range_is_dropped():
	result = _analyze(vitals=[{"name": "cls", "page_url": "/a", "value": 1e300}])
	assert _vitals(result) == {}


def test_no_estimated_impact_can_exceed_the_float_column():
	result = _analyze(vitals=[_lcp(590_000)], xhr=[_xhr(duration_ms=599_000)])
	assert result.findings
	assert all(f["estimated_impact_ms"] <= 600_000 for f in result.findings)


def test_an_absurd_response_size_is_not_a_heavy_response():
	result = _analyze(xhr=[_xhr(response_size_bytes=1e300)])
	assert [f for f in result.findings if f["finding_type"] == "Heavy Response"] == []


def test_a_negative_lcp_timestamp_still_records_the_lcp():
	result = _analyze(vitals=[_lcp(900, timestamp=-5)])
	assert _vitals(result)["/app"]["lcp_ms"] == 900


def test_a_malformed_duration_is_dropped_not_counted_as_zero():
	result = _analyze(xhr=[_xhr(duration_ms="slow"), _xhr(duration_ms=50)])
	assert result.aggregate["frontend_summary"]["total_xhrs"] == 1


def test_a_non_string_page_url_is_dropped_not_shown_as_a_root_page():
	result = _analyze(vitals=[{"name": "fcp", "page_url": 888.89, "value_ms": 10}, {"name": "fcp", "value_ms": 10}])
	assert _vitals(result) == {}


def test_dropped_rows_are_counted_in_the_summary():
	result = _analyze(xhr=[_xhr(duration_ms="x"), "junk"], vitals=[_lcp("x"), 5, *GOOD])
	assert result.aggregate["frontend_summary"]["dropped_rows"] == 4


def test_the_context_has_no_vitals_rows_when_only_xhrs_arrived():
	assert _build(_analyze(xhr=[_xhr()]))["web_vitals"] == []


def test_the_report_says_when_no_page_load_was_captured(monkeypatch):
	base = snapshot._v5_aggregate()
	base.update(_analyze(xhr=[_xhr()]).aggregate)
	monkeypatch.setattr(snapshot, "_v5_aggregate", lambda: base)
	html_text = renderer.render_raw(snapshot._snapshot_doc(), recordings=[])
	assert "No page-load timings were captured in this session." in html_text
