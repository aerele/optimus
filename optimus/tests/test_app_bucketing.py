# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for per-app sub-grouping inside Findings and Observations.

The renderer buckets findings by their callsite's top-level app
(apps/myapp/... -> myapp), puts tracked apps first, then remaining apps by
total impact, with "Other (no callsite)" as a tail bucket.
"""

from optimus.renderer import (
	_OTHER_APP_LABEL,
	_app_from_finding,
	_bucket_findings_by_app,
)


def _finding(app_filename=None, impact=10.0, severity="Medium", title="x"):
	"""Build a minimal finding dict matching what _finding_to_dict returns."""
	detail = {}
	if app_filename:
		detail["callsite"] = {"filename": app_filename, "lineno": 1}
	return {
		"finding_type": "N+1 Query",
		"severity": severity,
		"title": title,
		"customer_description": "",
		"estimated_impact_ms": impact,
		"affected_count": 1,
		"action_ref": "",
		"technical_detail": detail,
	}


class TestAppFromFinding:
	def test_bench_relative_path(self):
		f = _finding("apps/myapp/myapp/controllers/foo.py")
		assert _app_from_finding(f) == "myapp"

	def test_absolute_path(self):
		f = _finding("/home/frappe/bench/apps/myapp/myapp/foo.py")
		assert _app_from_finding(f) == "myapp"

	def test_short_form(self):
		f = _finding("myapp/foo.py")
		assert _app_from_finding(f) == "myapp"

	def test_framework_path(self):
		f = _finding("frappe/model/document.py")
		assert _app_from_finding(f) == "frappe"

	def test_no_callsite_falls_back_to_other(self):
		f = _finding(app_filename=None)
		assert _app_from_finding(f) == _OTHER_APP_LABEL

	def test_empty_callsite_dict_falls_back_to_other(self):
		f = _finding()
		f["technical_detail"] = {"callsite": {}}
		assert _app_from_finding(f) == _OTHER_APP_LABEL


class TestBucketing:
	def test_single_app_one_bucket(self):
		findings = [
			_finding("apps/myapp/foo.py", impact=10.0),
			_finding("apps/myapp/bar.py", impact=20.0),
		]
		buckets = _bucket_findings_by_app(findings)
		assert len(buckets) == 1
		assert buckets[0]["app"] == "myapp"
		assert buckets[0]["count"] == 2
		assert buckets[0]["total_impact_ms"] == 30.0

	def test_empty_input_returns_empty_list(self):
		assert _bucket_findings_by_app([]) == []

	def test_multiple_apps_sorted_by_total_impact_desc(self):
		findings = [
			_finding("apps/apple/foo.py", impact=5.0),
			_finding("apps/apple/bar.py", impact=5.0),
			# Banana has higher total should come first.
			_finding("apps/banana/x.py", impact=50.0),
			_finding("apps/carrot/y.py", impact=20.0),
		]
		buckets = _bucket_findings_by_app(findings)
		apps = [b["app"] for b in buckets]
		assert apps == ["banana", "carrot", "apple"]

	def test_alphabetic_tiebreak_on_equal_impact(self):
		findings = [
			_finding("apps/zebra/foo.py", impact=10.0),
			_finding("apps/alpha/foo.py", impact=10.0),
		]
		buckets = _bucket_findings_by_app(findings)
		assert [b["app"] for b in buckets] == ["alpha", "zebra"]


class TestTrackedAppsOrdering:
	def test_tracked_apps_come_first_in_admin_order(self):
		"""Tracked apps listed by the admin become the first buckets regardless
		of impact; the remaining apps follow in impact-desc order."""
		findings = [
			_finding("apps/third_party/foo.py", impact=1000.0),  # huge impact
			_finding("apps/my_secondary/foo.py", impact=5.0),
			_finding("apps/my_primary/foo.py", impact=10.0),
		]
		buckets = _bucket_findings_by_app(
			findings, tracked_apps=("my_primary", "my_secondary")
		)
		assert [b["app"] for b in buckets] == [
			"my_primary", "my_secondary", "third_party",
		]

	def test_tracked_app_not_present_is_skipped(self):
		"""Tracking 'ghost' when no findings come from ghost must NOT
		insert an empty ghost bucket."""
		findings = [_finding("apps/myapp/foo.py")]
		buckets = _bucket_findings_by_app(findings, tracked_apps=("ghost", "myapp"))
		apps = [b["app"] for b in buckets]
		assert apps == ["myapp"], (
			"Ghost app with no findings must not produce an empty bucket"
		)


class TestHotPathBucketRename:
	"""When every finding in the no-callsite bucket is a hot-path / hook /
	frontend-render type (which legitimately have no code-location callsite),
	the bucket is renamed from "Other (no callsite)" to "Request hotspots"."""

	def _hotpath(self, title, impact=400.0):
		return {
			"finding_type": "Slow Hot Path",
			"severity": "Medium",
			"title": title,
			"customer_description": "",
			"estimated_impact_ms": impact,
			"affected_count": 1,
			"action_ref": "",
			"technical_detail": {},  # no callsite
		}

	def _hook(self, title, impact=200.0):
		return {
			"finding_type": "Hook Bottleneck",
			"severity": "Medium",
			"title": title,
			"customer_description": "",
			"estimated_impact_ms": impact,
			"affected_count": 1,
			"action_ref": "",
			"technical_detail": {},
		}

	def test_all_hotpath_findings_renames_bucket_to_request_hotspots(self):
		buckets = _bucket_findings_by_app([
			self._hotpath("In savedocs:Submit, 57% in on_submit"),
			self._hook("In apply_pricing_rule, 97% in apply_pricing_rule"),
		])
		apps = [b["app"] for b in buckets]
		assert "Request hotspots" in apps, (
			"All-hotpath no-callsite bucket must rename to 'Request "
			f"hotspots'. Got: {apps}"
		)
		assert _OTHER_APP_LABEL not in apps, (
			"'Other (no callsite)' label must NOT appear when the "
			"bucket is entirely hot-path findings"
		)

	def test_mixed_bucket_is_suppressed(self):
		"""A no-callsite bucket with mixed finding types (hot-path plus
		non-hot-path) is dropped entirely; only the all-hotpath case survives
		(renamed to 'Request hotspots')."""
		buckets = _bucket_findings_by_app([
			self._hotpath("In savedocs:Save, 66% in validate"),
			{
				"finding_type": "Resource Contention",  # NOT a hot-path type
				"severity": "High",
				"title": "CPU saturated",
				"customer_description": "",
				"estimated_impact_ms": 0,
				"affected_count": 1,
				"action_ref": "",
				"technical_detail": {},
			},
		])
		apps = [b["app"] for b in buckets]
		assert _OTHER_APP_LABEL not in apps, (
			"'Other (no callsite)' bucket must be suppressed in v0.7.x. "
			f"Got: {apps}"
		)

	def test_hotpath_bucket_ordered_last(self):
		"""'Request hotspots' is ordered last (secondary to user-app findings)
		since the reader can't jump straight to a line of code from it."""
		buckets = _bucket_findings_by_app([
			{"finding_type": "N+1 Query", "severity": "High",
			 "title": "myapp N+1", "customer_description": "",
			 "estimated_impact_ms": 50.0, "affected_count": 1,
			 "action_ref": "",
			 "technical_detail": {
				 "callsite": {"filename": "apps/myapp/foo.py", "lineno": 1}
			 }},
			self._hotpath("Slow submit"),
		])
		apps = [b["app"] for b in buckets]
		assert apps == ["myapp", "Request hotspots"]


class TestOtherBucketSuppressed:
	def test_other_bucket_not_rendered(self):
		"""The 'Other (no callsite)' bucket is suppressed from the render output
		even when no-callsite findings exist; they still count toward severity
		totals. Named-app buckets render unchanged."""
		findings = [
			_finding(app_filename=None, impact=9999.0),  # no callsite
			_finding("apps/myapp/foo.py", impact=10.0),
		]
		buckets = _bucket_findings_by_app(findings)
		apps = [b["app"] for b in buckets]
		assert _OTHER_APP_LABEL not in apps
		assert apps == ["myapp"]

	def test_other_bucket_absent_when_no_uncallsite_findings(self):
		findings = [_finding("apps/myapp/foo.py")]
		buckets = _bucket_findings_by_app(findings)
		assert _OTHER_APP_LABEL not in [b["app"] for b in buckets]


class TestBucketContents:
	def test_bucket_preserves_input_order_within_app(self):
		"""Within each app the bucketer preserves the caller's global
		severity+impact order (it must not re-sort)."""
		findings = [
			_finding("apps/myapp/a.py", impact=100.0, severity="High"),
			_finding("apps/myapp/b.py", impact=10.0, severity="High"),
			_finding("apps/myapp/c.py", impact=50.0, severity="Medium"),
		]
		buckets = _bucket_findings_by_app(findings)
		bucket_impacts = [f["estimated_impact_ms"] for f in buckets[0]["findings"]]
		assert bucket_impacts == [100.0, 10.0, 50.0]

	def test_bucket_count_and_impact_totals_correct(self):
		findings = [
			_finding("apps/myapp/foo.py", impact=12.0),
			_finding("apps/myapp/bar.py", impact=8.0),
			_finding("apps/myapp/baz.py", impact=30.0),
		]
		buckets = _bucket_findings_by_app(findings)
		assert buckets[0]["count"] == 3
		assert buckets[0]["total_impact_ms"] == 50.0
