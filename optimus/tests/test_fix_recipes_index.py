# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""fix_recipes.index_recipe: the durable Frappe index recipe (spec 4.4).

Own-app DocType: single column -> DocType editor Search Index, composite ->
on_doctype_update + add_index. Another app's DocType: single column ->
Property Setter search_index=1, composite -> add_index in a patch. Custom
Field: its own Search Index, composite -> patch. Never raw DDL, never
Customize Form, metadata column never alone or first.
"""

from optimus.renderer import fix_recipes as fr


def _meta(app="erpnext", *, own=False, custom_doctype=False, fields=None):
	fields = fields or {}
	return lambda doctype: {
		"module_app": app,
		"is_own_app": own,
		"is_custom_doctype": custom_doctype,
		"fields": {
			name: {"fieldtype": fieldtype, "is_custom_field": bool(custom)}
			for name, (fieldtype, custom) in fields.items()
		},
	}


def _missing_index(column, *, table="tabSales Invoice", ddl=None):
	return {
		"finding_type": "Missing Index",
		"technical_detail": {
			"table": table,
			"column": column,
			"suggested_ddl": ddl if ddl is not None else (
				f"ALTER TABLE `{table}` ADD INDEX IF NOT EXISTS `{column}_index` (`{column}`);"
			),
		},
	}


def _explain(ftype, query, *, table="tabSales Invoice"):
	return {"finding_type": ftype, "technical_detail": {"table": table, "normalized_query": query}}


_ERP = _meta("erpnext", fields={"customer": ("Link", 0), "status": ("Select", 0)})
_FORBIDDEN = ("ALTER TABLE", "CREATE INDEX", "Customize Form", "\u2014")


class TestMetadataRule:
	def test_metadata_column_never_alone(self):
		assert fr.apply_metadata_rule(["creation"]) == []
		assert fr.apply_metadata_rule(["modified"]) == []

	def test_metadata_column_never_first(self):
		assert fr.apply_metadata_rule(["modified", "customer"]) == ["customer"]
		assert fr.apply_metadata_rule(["parent", "idx"]) == []

	def test_trailing_creation_allowed(self):
		assert fr.apply_metadata_rule(["customer", "creation"]) == ["customer", "creation"]
		assert fr.apply_metadata_rule(["customer", "Creation", "modified"]) == [
			"customer", "Creation", "modified",
		]

	def test_other_metadata_dropped_even_when_trailing(self):
		assert fr.apply_metadata_rule(["customer", "docstatus"]) == ["customer"]

	def test_non_trailing_creation_dropped(self):
		assert fr.apply_metadata_rule(["customer", "creation", "status"]) == ["customer", "status"]

	def test_recipe_for_a_metadata_column_is_none(self):
		assert fr.index_recipe(_missing_index("modified"), meta_lookup=_ERP) is None


class TestIsOwnApp:
	def test_tracked_apps_is_an_allowlist(self):
		assert fr.is_own_app("myapp", tracked_apps=("myapp",)) is True
		assert fr.is_own_app("otherapp", tracked_apps=("myapp",)) is False
		# An ERPNext contributor who tracks erpnext owns its DocTypes.
		assert fr.is_own_app("erpnext", tracked_apps=("erpnext",)) is True

	def test_exclusion_mode_uses_framework_and_installed_apps(self):
		assert fr.is_own_app("erpnext") is False
		assert fr.is_own_app("myapp", installed_apps=frozenset({"frappe", "myapp"})) is True
		assert fr.is_own_app("ghost", installed_apps=frozenset({"frappe"})) is False
		assert fr.is_own_app("myapp") is True  # off-bench: a non-framework app is the developer's

	def test_empty_app_is_not_own(self):
		assert fr.is_own_app("") is False


class TestThreeRowTable:
	def test_own_app_single_column_uses_search_index(self):
		r = fr.index_recipe(
			_missing_index("po_no"),
			meta_lookup=_meta("myapp", own=True, fields={"po_no": ("Data", 0)}),
		)
		assert r["kind"] == "index"
		assert 'Tick "Search Index" on the "po_no" field of DocType "Sales Invoice"' in r["text"]
		assert r["code"] is None

	def test_other_app_single_column_uses_property_setter(self):
		r = fr.index_recipe(_missing_index("po_no"), meta_lookup=_meta("erpnext", fields={"po_no": ("Data", 0)}))
		assert 'belongs to the "erpnext" app, so do not edit it' in r["text"]
		assert (
			'make_property_setter("Sales Invoice", "po_no", "search_index", "1", "Check", for_doctype=False)'
			in r["code"]
		)
		assert 'frappe.db.add_index("Sales Invoice", ["po_no"])' in r["code"]

	def test_custom_field_uses_its_own_search_index(self):
		r = fr.index_recipe(_missing_index("po_no"), meta_lookup=_meta("erpnext", fields={"po_no": ("Data", 1)}))
		assert '"po_no" field of "Sales Invoice" is a Custom Field' in r["text"]
		assert r["code"] is None

	def test_custom_doctype_single_column(self):
		r = fr.index_recipe(
			_missing_index("po_no", table="tabMy Notes"),
			meta_lookup=_meta("frappe", custom_doctype=True, fields={"po_no": ("Data", 0)}),
		)
		assert 'DocType "My Notes" was created in the UI' in r["text"]
		assert r["code"] is None

	def test_own_app_text_column_uses_on_doctype_update_with_prefix(self):
		r = fr.index_recipe(
			_missing_index("remarks"),
			meta_lookup=_meta("myapp", own=True, fields={"remarks": ("Small Text", 0)}),
		)
		assert r["code"] == 'import frappe\n\n\ndef on_doctype_update():\n\tfrappe.db.add_index("Sales Invoice", ["remarks(255)"])\n'
		assert "Search Index" not in r["text"]  # DocType validation refuses it on text fields
		assert "only after its JSON changes" in r["text"]

	def test_other_app_text_column_uses_a_patch(self):
		r = fr.index_recipe(_missing_index("remarks"), meta_lookup=_meta("erpnext", fields={"remarks": ("Text", 0)}))
		assert r["code"] == (
			'import frappe\n\n\ndef execute():\n\tfrappe.db.add_index("Sales Invoice", ["remarks(255)"])\n'
		)
		assert "never drops an index on a text column" in r["text"]

	def test_write_hot_table_gets_the_write_cost_note(self):
		r = fr.index_recipe(
			_missing_index("against_voucher", table="tabGL Entry"),
			meta_lookup=_meta("erpnext", fields={"against_voucher": ("Dynamic Link", 0)}),
		)
		assert 'Note: "tabGL Entry" takes many writes' in r["text"]


class TestUnreadableMeta:
	def test_unreadable_doctype_gets_both_options_and_no_code(self):
		r = fr.index_recipe(_missing_index("po_no"), meta_lookup=lambda dt: None)
		assert 'Optimus could not read DocType "Sales Invoice"' in r["text"]
		assert "Property Setter" in r["text"] and "Search Index" in r["text"]
		assert r["code"] is None

	def test_lookup_error_is_treated_as_unreadable(self):
		def boom(dt):
			raise RuntimeError("db gone")

		r = fr.index_recipe(_missing_index("po_no"), meta_lookup=boom)
		assert r is not None and r["code"] is None

	def test_mariadb_prefix_ddl_marks_a_text_column_when_meta_is_unknown(self):
		ddl = "ALTER TABLE `tabSales Invoice` ADD INDEX IF NOT EXISTS `remarks_index` (`remarks`(255));"
		r = fr.index_recipe(_missing_index("remarks", ddl=ddl), meta_lookup=lambda dt: None)
		assert '["remarks(255)"]' in r["code"]

	def test_postgres_ddl_is_never_echoed(self):
		ddl = 'CREATE INDEX IF NOT EXISTS "tabSales Invoice_po_no_index" ON "public"."tabSales Invoice" ("po_no");'
		r = fr.index_recipe(_missing_index("po_no", ddl=ddl), meta_lookup=lambda dt: None)
		assert "CREATE INDEX" not in r["text"] and r["code"] is None

	def test_non_doctype_table_has_no_recipe(self):
		assert fr.index_recipe(_missing_index("x", table="__Auth"), meta_lookup=_ERP) is None

	def test_bad_identifier_has_no_recipe(self):
		assert fr.index_recipe(_missing_index("po_no`; DROP"), meta_lookup=_ERP) is None


class TestExplainFindings:
	def test_filesort_keeps_trailing_creation(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE customer = ? ORDER BY creation DESC"
		r = fr.index_recipe(_explain("Filesort", q), meta_lookup=_ERP)
		assert 'frappe.db.add_index("Sales Invoice", ["customer", "creation"])' in r["code"]
		assert r["text"].startswith("Index the filter columns followed by the sort column")

	def test_filesort_on_creation_alone_has_no_recipe(self):
		q = "SELECT name FROM `tabSales Invoice` ORDER BY creation DESC"
		assert fr.index_recipe(_explain("Filesort", q), meta_lookup=_ERP) is None

	def test_full_table_scan_uses_where_columns_in_order(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ?"
		r = fr.index_recipe(_explain("Full Table Scan", q), meta_lookup=_ERP)
		assert '["customer", "status"]' in r["code"]

	def test_temporary_table_drops_leading_metadata(self):
		q = "SELECT customer, count(*) FROM `tabSales Invoice` WHERE docstatus = ? GROUP BY customer"
		r = fr.index_recipe(_explain("Temporary Table", q), meta_lookup=_ERP)
		assert 'make_property_setter("Sales Invoice", "customer"' in r["code"]

	def test_alias_table_resolves_to_the_single_doctype_table(self):
		q = "SELECT si.name FROM `tabSales Invoice` si WHERE si.customer = ?"
		r = fr.index_recipe(_explain("Low Filter Ratio", q, table="si"), meta_lookup=_ERP)
		assert r["text"].startswith("Index the most selective filter column first")
		assert '"Sales Invoice", "customer"' in r["code"]

	def test_child_table_parent_filter_has_no_recipe(self):
		q = "SELECT * FROM `tabSales Invoice Item` WHERE parent = ? ORDER BY idx"
		assert fr.index_recipe(_explain("Full Table Scan", q, table="tabSales Invoice Item"), meta_lookup=_ERP) is None

	def test_empty_query_has_no_recipe(self):
		assert fr.index_recipe(_explain("Full Table Scan", ""), meta_lookup=_ERP) is None


def _every_recipe():
	metas = [
		_meta("myapp", own=True, fields={"po_no": ("Data", 0), "remarks": ("Text", 0)}),
		_meta("erpnext", fields={"po_no": ("Data", 0), "remarks": ("Text", 0)}),
		_meta("erpnext", fields={"po_no": ("Data", 1), "remarks": ("Text", 0)}),
		_meta("frappe", custom_doctype=True, fields={"po_no": ("Data", 0)}),
		lambda dt: None,
	]
	q = "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ? GROUP BY customer ORDER BY creation"
	for m in metas:
		for col in ("po_no", "remarks"):
			for table in ("tabSales Invoice", "tabGL Entry"):
				yield fr.index_recipe(_missing_index(col, table=table), meta_lookup=m)
		for ftype in ("Full Table Scan", "Filesort", "Temporary Table", "Low Filter Ratio"):
			yield fr.index_recipe(_explain(ftype, q), meta_lookup=m)


def test_no_raw_ddl_customize_form_or_em_dash_in_any_recipe():
	recipes = [r for r in _every_recipe() if r]
	assert len(recipes) >= 30
	for r in recipes:
		blob = r["text"] + (r["code"] or "")
		for bad in _FORBIDDEN:
			assert bad not in blob, (bad, blob)


_TWO_COLS = "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ?"


class TestCompositeCells:
	"""The composite column of the spec 4.4 table, one named test per cell
	(plan-check cycle 1: composite cells were untested by name)."""

	def test_own_app_composite_uses_on_doctype_update(self):
		m = _meta("myapp", own=True, fields={"customer": ("Link", 0), "status": ("Select", 0)})
		r = fr.index_recipe(_explain("Full Table Scan", _TWO_COLS), meta_lookup=m)
		assert r["code"] == (
			'import frappe\n\n\ndef on_doctype_update():\n\tfrappe.db.add_index("Sales Invoice", ["customer", "status"])\n'
		)
		assert 'Add this function to the Python module of DocType "Sales Invoice" in your app "myapp"' in r["text"]
		assert "also call the same frappe.db.add_index once from a patch" in r["text"]

	def test_other_app_composite_uses_a_patch(self):
		r = fr.index_recipe(_explain("Full Table Scan", _TWO_COLS), meta_lookup=_ERP)
		assert r["code"] == (
			'import frappe\n\n\ndef execute():\n\tfrappe.db.add_index("Sales Invoice", ["customer", "status"])\n'
		)
		assert r["text"].startswith(
			"Index the columns this query filters on so it stops reading the whole table. "
			'DocType "Sales Invoice" belongs to the "erpnext" app, so do not edit it.'
		)
		assert "never drops an index that spans several columns" in r["text"]

	def test_custom_field_composite_uses_a_patch(self):
		m = _meta("erpnext", fields={"customer": ("Link", 0), "status": ("Data", 1)})
		r = fr.index_recipe(_explain("Full Table Scan", _TWO_COLS), meta_lookup=m)
		assert "This index includes a Custom Field." in r["text"]
		assert r["code"].startswith("import frappe\n\n\ndef execute():\n")

	def test_own_app_composite_with_a_custom_field_uses_a_patch(self):
		m = _meta("myapp", own=True, fields={"customer": ("Link", 0), "status": ("Data", 1)})
		r = fr.index_recipe(_explain("Full Table Scan", _TWO_COLS), meta_lookup=m)
		assert "This index includes a Custom Field." in r["text"]
		assert "on_doctype_update" not in r["code"]

	def test_custom_doctype_composite_uses_a_patch(self):
		m = _meta("frappe", custom_doctype=True, fields={"customer": ("Link", 0), "status": ("Select", 0)})
		r = fr.index_recipe(_explain("Full Table Scan", _TWO_COLS), meta_lookup=m)
		assert 'DocType "Sales Invoice" was created in the UI and has no module file.' in r["text"]
		assert r["code"].startswith("import frappe\n\n\ndef execute():\n")

	def test_custom_doctype_in_an_own_app_module_composite_uses_a_patch(self):
		m = _meta(
			"myapp", own=True, custom_doctype=True,
			fields={"customer": ("Link", 0), "status": ("Select", 0)},
		)
		r = fr.index_recipe(_explain("Full Table Scan", _TWO_COLS), meta_lookup=m)
		assert "was created in the UI and has no module file." in r["text"]
		assert "on_doctype_update" not in r["code"]


class TestTableCardColumns:
	def test_composite_is_kept(self):
		assert fr.table_card_columns(
			"tabSales Invoice", ["customer", "posting_date"], meta_lookup=_ERP,
		) == ["customer", "posting_date"]

	def test_single_non_text_column_is_not_durable_from_a_patch(self):
		assert fr.table_card_columns("tabSales Invoice", ["customer"], meta_lookup=_ERP) is None

	def test_single_text_column_gets_the_prefix(self):
		m = _meta("erpnext", fields={"remarks": ("Text", 0)})
		assert fr.table_card_columns("tabSales Invoice", ["remarks"], meta_lookup=m) == ["remarks(255)"]

	def test_only_one_text_column_per_index(self):
		m = _meta("myapp", own=True, fields={"a": ("Text", 0), "b": ("Long Text", 0), "c": ("Data", 0)})
		assert fr.table_card_columns("tabX", ["a", "b", "c"], meta_lookup=m) == ["a(255)", "c"]

	def test_metadata_rule_applies_to_cards(self):
		assert fr.table_card_columns(
			"tabSales Invoice", ["modified", "customer", "status"], meta_lookup=_ERP,
		) == ["customer", "status"]

	def test_non_doctype_table(self):
		assert fr.table_card_columns("__Auth", ["a", "b"], meta_lookup=_ERP) is None

	def test_capped_at_four_columns(self):
		assert fr.table_card_columns(
			"tabX", ["a", "b", "c", "d", "e"], meta_lookup=lambda dt: None,
		) == ["a", "b", "c", "d"]
