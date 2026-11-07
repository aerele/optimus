# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Real-bench integration test for the index-suggestions statement count.

``index_suggestions`` runs Frappe's own ``_optimize_query`` once per query shape, and
that function re-reads ``DESCRIBE``, ``SHOW INDEX``, the row count and
``COUNT(DISTINCT col)`` every time. Optimus puts ``optimizer_db_cache`` in front of
``frappe.db`` so a table is read once per run. The cache recognises Frappe's four
statements by their text, so a Frappe release that rewords one would turn the cache
into a silent passthrough. The unit suite runs against a pinned copy of Frappe's
optimizer and cannot see that, so this test runs the installed one and counts what
reaches the database.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from optimus.analyzers import index_suggestions
from optimus.analyzers.base import AnalyzeContext

_TABLE = "tabToDo"
_SHAPES = 15


class _CountingDb:
	"""Stands in for ``frappe.local.db`` and records the text of every ``sql`` call."""

	def __init__(self, real):
		self._real = real
		self.statements = []

	def __getattr__(self, name):
		return getattr(self._real, name)

	def sql(self, query, *args, **kwargs):
		self.statements.append(" ".join(str(query).split()).lower())
		return self._real.sql(query, *args, **kwargs)

	def count(self, prefix):
		return sum(1 for text in self.statements if text.startswith(prefix))


def _recording():
	calls = []
	for i in range(_SHAPES):
		normalized = f"SELECT name, {i} AS n{i} FROM `{_TABLE}` WHERE status = ? AND allocated_to = ?"
		calls.append(
			{
				"query": normalized.replace("?", "'x'"),
				"normalized_query": normalized,
				"duration": 50.0,
				"stack": [],
			}
		)
	return [{"uuid": "statement-count", "calls": calls}]


class TestIndexSuggestionsStatementCount(FrappeTestCase):
	def test_the_installed_optimizer_reads_the_table_once_for_many_shapes(self):
		if frappe.db.db_type != "mariadb":
			self.skipTest("Frappe's optimizer introspection is MariaDB only")

		real = frappe.local.db
		counting = _CountingDb(real)
		frappe.local.db = counting
		try:
			index_suggestions.analyze(_recording(), AnalyzeContext(session_uuid="x", docname="x"))
		finally:
			frappe.local.db = real

		describes = counting.count("describe ")
		self.assertEqual(
			describes,
			1,
			f"expected one DESCRIBE of {_TABLE} for {_SHAPES} shapes, saw {describes}; "
			"Frappe may have reworded an optimizer statement (see optimizer_db_cache._STATEMENTS)",
		)
