# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The v0.5.0 analyzers in the registry, and what ``analyze._persist`` stores for them.

The wiring of ``run`` (infra and frontend data before the analyzers) is asserted by
``test_analyze_delivery_guards.py``.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from optimus import analyze


def test_builtin_analyzers_list_includes_v5():
	# _BUILTIN_ANALYZERS is the list consumed by _get_analyzers which
	# drives the run loop. If this list loses the v0.5.0 analyzers,
	# they never fire.
	assert any(
		a.__module__.endswith("infra_pressure")
		for a in analyze._BUILTIN_ANALYZERS
	), "infra_pressure.analyze missing from _BUILTIN_ANALYZERS"
	assert any(
		a.__module__.endswith("frontend_timings")
		for a in analyze._BUILTIN_ANALYZERS
	), "frontend_timings.analyze missing from _BUILTIN_ANALYZERS"


def _persisted(monkeypatch, aggregate):
	doc = MagicMock(notes="written by the user", title="fake title")
	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(
		get_doc=lambda *a: doc, db=SimpleNamespace(get_value=lambda *a: doc.notes, set_value=lambda *a, **kw: None)))
	monkeypatch.setattr(analyze, "safe_commit", lambda: None)
	monkeypatch.setattr(analyze.session, "get_jobs", lambda *a: [])
	context = analyze.AnalyzeContext("fake-session", "fake-doc")
	context.aggregate.update(aggregate)
	analyze._persist("fake-doc", context, [])
	return doc


def test_persist_keeps_the_tail_of_each_v5_aggregate_list_and_warns(monkeypatch):
	# Uncapped, v5_aggregate_json grows past 1 MB on a large session and slows the form load.
	doc = _persisted(monkeypatch, {
		"infra_timeline": [{"i": i} for i in range(201)],
		"frontend_xhr_matched": [{"i": i} for i in range(501)],
		"frontend_orphans": [{"i": i} for i in range(101)],
	})

	stored = json.loads(doc.v5_aggregate_json)
	assert stored["infra_timeline"] == [{"i": i} for i in range(1, 201)]
	assert stored["frontend_xhr_matched"] == [{"i": i} for i in range(1, 501)]
	assert stored["frontend_orphans"] == [{"i": i} for i in range(1, 101)]
	# The warnings must already be in the field the session form shows.
	warnings = doc.analyzer_warnings.splitlines()
	assert len(warnings) == 3
	for name, before, cap in (("infra_timeline", 201, 200), ("frontend_xhr_matched", 501, 500), ("frontend_orphans", 101, 100)):
		assert any(f"{name} truncated from {before} to {cap} entries" in line for line in warnings)



def test_persist_leaves_the_notes_a_user_wrote_unchanged(monkeypatch):
	# The steps are only auto-filled into empty notes; text typed on the form between start and stop stays.
	monkeypatch.setattr(analyze, "_build_auto_notes_html", lambda *a: "generated steps")
	doc = _persisted(monkeypatch, {})
	assert doc.notes == "written by the user"


def test_truncate_finding_titles_clamps_overlong():
	"""_persist clamps any finding.title over the 140-char Optimus Finding.title
	limit, preventing CharacterLengthExceededError from breaking the analyze
	pipeline on a pathological title."""
	findings = [
		# Under the limit untouched.
		{"title": "Short title"},
		# Exactly at the limit untouched.
		{"title": "A" * 140},
		# One over the limit must be clamped to 140 and end with "...".
		{"title": "B" * 141},
		# Far over the limit must be clamped to exactly 140.
		{"title": "C" * 500},
		# The production payload that started this bug.
		{
			"title": (
				"Same query ran 65× at "
				"jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/"
				"doctype/parent_manufacturing_order/"
				"parent_manufacturing_order.py:503"
			)
		},
	]
	analyze._truncate_finding_titles(findings)

	assert findings[0]["title"] == "Short title"
	assert findings[1]["title"] == "A" * 140
	assert len(findings[1]["title"]) == 140

	assert len(findings[2]["title"]) == 140
	assert findings[2]["title"].endswith("...")

	assert len(findings[3]["title"]) == 140
	assert findings[3]["title"].endswith("...")

	# Production payload was 144 chars, must now be <= 140.
	assert len(findings[4]["title"]) <= 140


def test_truncate_finding_titles_ignores_missing_title_key():
	"""A finding dict missing the title key must not crash the
	truncator. It should treat None/empty as untouched."""
	findings = [{}, {"title": None}, {"title": ""}]
	# Must not raise.
	analyze._truncate_finding_titles(findings)
	# Missing / None / empty → unchanged.
	assert findings[0] == {}
	assert findings[1]["title"] is None
	assert findings[2]["title"] == ""
