# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the renderer's root-cause grouping pass, which collapses findings
whose deepest user-code anchor matches into one primary card with the others
attached as ``sub_findings`` (so a single bug reads as one card, not five)."""

from optimus.renderer import (
	_group_findings_by_root_cause,
	_root_cause_key,
)


def _shp(*, function="looped_validate", filename="apps/myapp/common.py",
         lineno=6, impact_ms=700.0, severity="Medium",
         title=None, drilldown_leaf_function=None,
         drilldown_leaf_filename=None):
	"""Build a Slow Hot Path-shaped finding dict (post-_finding_to_dict
	flattening) with an optional drilldown_chain."""
	detail = {
		"function": function,
		"filename": filename,
		"lineno": lineno,
		"callsite": {"filename": filename, "lineno": lineno, "function": function},
	}
	if drilldown_leaf_function:
		detail["drilldown_chain"] = [{
			"filename": drilldown_leaf_filename or filename,
			"lineno": 20,
			"function": drilldown_leaf_function,
			"cumulative_ms": 500.0,
			"pct_of_origin": 70,
		}]
	return {
		"finding_type": "Slow Hot Path",
		"severity": severity,
		"title": title or f"In foo, {function} consumed time",
		"customer_description": "walltime",
		"estimated_impact_ms": impact_ms,
		"affected_count": 1,
		"action_ref": "0",
		"technical_detail": detail,
	}


def _hot_line(*, function="_check_user_exists", filename="apps/myapp/common.py",
              lineno=20, impact_ms=300.0, severity="High"):
	return {
		"finding_type": "Hot Line",
		"severity": severity,
		"title": f"{function}:{lineno} consumed {impact_ms}ms (100 hits) single hottest line",
		"customer_description": "leaf hot line",
		"estimated_impact_ms": impact_ms,
		"affected_count": 100,
		"action_ref": None,
		"technical_detail": {
			"file": filename,
			"function": function,
			"lineno": lineno,
			"callsite": {"filename": filename, "lineno": lineno, "function": function},
			"line_content": "user = frappe.get_doc(...)",
		},
	}


def _redundant_call(*, function="_check_user_exists", filename="apps/myapp/common.py",
                    lineno=20, impact_ms=80.0, severity="Medium"):
	return {
		"finding_type": "Redundant Call",
		"severity": severity,
		"title": "Redundant doc fetch: User abc (155 times)",
		"customer_description": "repeated User get_doc",
		"estimated_impact_ms": impact_ms,
		"affected_count": 155,
		"action_ref": "0",
		"technical_detail": {
			"function": function,
			"filename": filename,
			"lineno": lineno,
			"callsite": {"filename": filename, "lineno": lineno, "function": function},
		},
	}


def _infra(*, severity="Low", impact_ms=0.0):
	"""An infra observation: no callsite at all."""
	return {
		"finding_type": "System CPU Hot",
		"severity": severity,
		"title": "System CPU > 85% on 2 of 20 actions",
		"customer_description": "infra signal",
		"estimated_impact_ms": impact_ms,
		"affected_count": 0,
		"action_ref": None,
		"technical_detail": {},
	}


class TestRootCauseKey:
	def test_drilldown_leaf_wins_over_callsite(self):
		"""When drilldown_chain is non-empty, its leaf's (file, function) is the
		key, not the callsite, so a Slow Hot Path groups with a Hot Line on the
		chain's leaf function."""
		shp = _shp(
			function="_run_validations",  # retargeted callsite
			lineno=13,
			drilldown_leaf_function="_check_user_exists",
			drilldown_leaf_filename="apps/myapp/common.py",
		)
		assert _root_cause_key(shp) == ("common.py", "_check_user_exists")

	def test_falls_back_to_callsite_when_no_chain(self):
		"""With no drilldown_chain, the callsite's own function is the key."""
		shp = _shp(function="bg_recheck_users")
		# no drilldown_chain
		assert _root_cause_key(shp) == ("common.py", "bg_recheck_users")

	def test_hot_line_uses_own_callsite(self):
		hl = _hot_line(function="_check_user_exists")
		assert _root_cause_key(hl) == ("common.py", "_check_user_exists")

	def test_redundant_call_uses_own_callsite(self):
		rc = _redundant_call(function="_check_user_exists")
		assert _root_cause_key(rc) == ("common.py", "_check_user_exists")

	def test_infra_finding_has_no_key(self):
		"""Findings without a callsite (infra observations) skip grouping."""
		assert _root_cause_key(_infra()) is None


class TestGrouping:
	def test_four_findings_one_leaf_collapse_to_primary(self):
		"""Four findings that all resolve to _check_user_exists collapse into one
		primary with three sub_findings."""
		shp = _shp(
			function="looped_validate",
			lineno=6,
			drilldown_leaf_function="_check_user_exists",
			impact_ms=700.0,
			severity="High",
		)
		hot = _hot_line(impact_ms=300.0, severity="High")
		rc = _redundant_call(severity="High", impact_ms=80.0,
		                     function="_check_user_exists")
		# Two redundant calls same root cause, different subjects.
		rc2 = {
			**_redundant_call(severity="Medium", impact_ms=40.0),
			"finding_type": "Redundant Permission Check",
			"title": "Redundant permission check: User abc read (80 times)",
		}

		result = _group_findings_by_root_cause([shp, hot, rc, rc2])

		assert len(result) == 1
		primary = result[0]
		# Primary picked by (severity, impact_ms desc). All three Highs;
		# the highest impact_ms is shp (700) → primary is the Slow Hot Path.
		assert primary["finding_type"] == "Slow Hot Path"
		assert primary["estimated_impact_ms"] == 700.0
		# Three sub-findings attached.
		subs = primary.get("sub_findings") or []
		assert len(subs) == 3
		sub_types = {s["finding_type"] for s in subs}
		assert sub_types == {"Hot Line", "Redundant Call", "Redundant Permission Check"}
		# Sub-findings are ordered by severity then impact_ms desc.
		assert subs[0]["estimated_impact_ms"] >= subs[1]["estimated_impact_ms"]

	def test_different_leaves_do_not_group(self):
		"""Two findings on different functions stay as two."""
		a = _hot_line(function="alpha", filename="apps/myapp/a.py")
		b = _hot_line(function="beta", filename="apps/myapp/b.py")
		result = _group_findings_by_root_cause([a, b])
		assert len(result) == 2
		# Each is its own primary with no sub_findings.
		for f in result:
			assert "sub_findings" not in f

	def test_singleton_finding_unchanged(self):
		"""A finding with a unique root cause passes through unchanged (no
		sub_findings)."""
		hl = _hot_line()
		result = _group_findings_by_root_cause([hl])
		assert len(result) == 1
		assert "sub_findings" not in result[0]

	def test_infra_findings_pass_through_ungrouped(self):
		"""Findings with no resolvable root cause (no callsite) skip grouping and
		pass through as singletons, after the grouped findings."""
		shp = _shp(impact_ms=500.0, severity="High",
		           drilldown_leaf_function="_leaf")
		infra = _infra()
		result = _group_findings_by_root_cause([shp, infra])
		assert len(result) == 2
		# Infra is at the tail (passthrough).
		assert result[-1]["finding_type"] == "System CPU Hot"

	def test_primary_pick_uses_severity_first(self):
		"""Within a group, severity beats impact_ms when picking the primary (a
		High 100ms finding beats a Medium 1000ms one)."""
		low_impact_high_sev = _hot_line(severity="High", impact_ms=100.0)
		high_impact_med_sev = _redundant_call(severity="Medium", impact_ms=1000.0,
		                                       function="_check_user_exists")
		result = _group_findings_by_root_cause([
			low_impact_high_sev, high_impact_med_sev,
		])
		assert len(result) == 1
		primary = result[0]
		# High severity wins → Hot Line.
		assert primary["finding_type"] == "Hot Line"
		# Redundant Call is the sub.
		subs = primary.get("sub_findings") or []
		assert len(subs) == 1
		assert subs[0]["finding_type"] == "Redundant Call"

	def test_empty_input_returns_empty(self):
		assert _group_findings_by_root_cause([]) == []

	def test_subs_carry_compact_payload_only(self):
		"""sub_findings entries are a compact dict (type, severity, title,
		description, impact, count), not the full technical_detail or llm_fix."""
		shp = _shp(
			drilldown_leaf_function="_check_user_exists",
			impact_ms=500.0, severity="High",
		)
		hot = _hot_line(impact_ms=300.0, severity="High")
		result = _group_findings_by_root_cause([shp, hot])
		primary = result[0]
		sub = primary["sub_findings"][0]
		# Compact keys only.
		assert set(sub.keys()) == {
			"finding_type", "severity", "title",
			"customer_description", "estimated_impact_ms", "affected_count",
		}
