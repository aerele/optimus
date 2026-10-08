# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""index_recipes: the one index advisor for findings and table cards (owner decisions
A1 and A2). Evidence decides: an already indexed, unique, missing or reserved column
gives no code; a single column on a field the developer controls gets Search Index;
everything else gets one explicitly named, idempotent ensure_indexes() entry."""

import json
import re

from optimus.renderer import index_recipes as ir
from optimus.renderer.recipe_enrichment import FieldEvidence, IndexEvidence, TableEvidence

_MARIADB_TYPES = {
	"Data": "varchar", "Link": "varchar", "Dynamic Link": "varchar", "Select": "varchar",
	"Small Text": "text", "Text": "text", "Text Editor": "longtext", "Long Text": "longtext",
	"Attach": "text", "Date": "date", "Check": "tinyint", "Int": "int", "JSON": "json",
}
_POSTGRES_TYPES = {
	"Data": "character varying", "Link": "character varying", "Dynamic Link": "character varying",
	"Select": "character varying", "Small Text": "text", "Text": "text", "Text Editor": "text",
	"Long Text": "text", "Attach": "text", "Date": "date", "Check": "smallint", "Int": "integer",
	"JSON": "json",
}
_FORBIDDEN = ("ALTER TABLE", "CREATE INDEX", "Customize Form", "\u2014", "\u2013")
_HOOKS = "after_install, after_sync and after_migrate"


def F(fieldtype="Data", *, length=0, search_index=False, unique=False, custom=False):
	return FieldEvidence(fieldtype, length, search_index, unique, custom)


def _ev(
	doctype="Sales Invoice", *, app="erpnext", custom_doctype=False, dialect="mariadb",
	fields=None, extra_types=None, indexes=(),
):
	fields = dict(fields or {})
	mapping = _POSTGRES_TYPES if dialect == "postgres" else _MARIADB_TYPES
	types = {name: mapping[field.fieldtype] for name, field in fields.items()}
	types["name"] = mapping["Data"]
	types["creation"] = "datetime" if dialect == "mariadb" else "timestamp without time zone"
	types.update(extra_types or {})
	text_types = {"text"} if dialect == "postgres" else {"text", "longtext", "mediumtext", "tinytext"}
	return TableEvidence(
		table=f"tab{doctype}", doctype=doctype, app=app, is_custom_doctype=custom_doctype,
		dialect=dialect, fields=fields, column_types=types,
		text_columns=frozenset(c for c, t in types.items() if t in text_types),
		unindexable_columns=frozenset(c for c, t in types.items() if t in ("json", "jsonb")),
		indexes=tuple(IndexEvidence(name, tuple(cols), unique) for name, cols, unique in indexes),
	)


def _lookup(*evidences):
	by_table = {ev.table: ev for ev in evidences}
	return lambda table: by_table.get(table)


def _missing(column, *, table="tabSales Invoice"):
	return {"finding_type": "Missing Index", "technical_detail": {"table": table, "column": column}}


def _explain(ftype, query, *, table="tabSales Invoice", explain_row=None):
	detail = {"table": table, "normalized_query": query}
	if explain_row is not None:
		detail["explain_row"] = explain_row
	return {"finding_type": ftype, "technical_detail": detail}


_ALL = {
	"po_no": F("Data"), "remarks": F("Small Text"), "customer": F("Link"),
	"status": F("Select"), "posting_date": F("Date"), "company": F("Link"),
}
_SI = _ev(fields=_ALL)
_TWO = "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ?"


class TestMetadataRule:
	def test_metadata_column_never_alone_or_first(self):
		assert ir.apply_metadata_rule(["creation"]) == []
		assert ir.apply_metadata_rule(["modified", "customer"]) == ["customer"]
		assert ir.apply_metadata_rule(["parent", "idx"]) == []

	def test_trailing_creation_and_modified_are_kept(self):
		assert ir.apply_metadata_rule(["customer", "Creation", "modified"]) == ["customer", "Creation", "modified"]

	def test_other_metadata_is_dropped_even_when_trailing(self):
		assert ir.apply_metadata_rule(["customer", "docstatus"]) == ["customer"]
		assert ir.apply_metadata_rule(["customer", "creation", "status"]) == ["customer", "status"]


class TestRoutes:
	def test_own_app_single_column_ticks_search_index(self):
		ev = _ev(app="myapp", fields={"po_no": F("Data")})
		advice = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(ev), tracked_apps=("myapp",))
		assert advice.route == ir.ROUTE_SEARCH_INDEX and advice.code is None
		assert 'Tick "Search Index" on the "po_no" field of DocType "Sales Invoice"' in ir.finding_text(advice)

	def test_custom_field_ticks_its_own_search_index(self):
		ev = _ev(fields={"po_no": F("Data", custom=True)})
		advice = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_SEARCH_INDEX
		assert 'The "po_no" field of "Sales Invoice" is a Custom Field' in ir.finding_text(advice)

	def test_ui_created_doctype_ticks_search_index(self):
		ev = _ev("My Notes", app="frappe", custom_doctype=True, fields={"po_no": F("Data")})
		advice = ir.advise_finding(_missing("po_no", table="tabMy Notes"), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_SEARCH_INDEX
		assert 'DocType "My Notes" was created in the UI' in ir.finding_text(advice)

	def test_another_apps_single_column_gets_a_property_setter_entry(self):
		advice = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(_SI))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES
		assert advice.entry == {"doctype": "Sales Invoice", "search_index_field": "po_no", "db": "mariadb"}
		assert '{"doctype": "Sales Invoice", "search_index_field": "po_no", "db": "mariadb"}' in advice.code
		assert 'belongs to the "erpnext" app, so do not edit it' in ir.finding_text(advice)

	def test_empty_tracked_apps_never_makes_a_third_party_app_own(self):
		ev = _ev(app="india_compliance", fields={"po_no": F("Data")})
		advice = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES
		assert advice.entry["search_index_field"] == "po_no"

	def test_composite_gets_an_explicitly_named_entry(self):
		advice = ir.advise_finding(_explain("Full Table Scan", _TWO), evidence_lookup=_lookup(_SI))
		name = ir.optimus_index_name("Sales Invoice", ("customer", "status"))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES
		assert advice.entry == {"doctype": "Sales Invoice", "columns": ["customer", "status"], "index_name": name}
		assert "frappe.db.add_index(doctype, columns, index_name=entry[\"index_name\"])" in advice.code
		text = ir.finding_text(advice)
		assert text.startswith("Index the columns this query filters on so it stops reading the whole table.")
		assert "never drops an index that spans several columns" in text
		assert _HOOKS in text

	def test_own_app_composite_is_an_entry_in_the_own_app(self):
		ev = _ev(app="myapp", fields=_ALL)
		advice = ir.advise_finding(
			_explain("Full Table Scan", _TWO), evidence_lookup=_lookup(ev), tracked_apps=("myapp",),
		)
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.app_name == "myapp"
		assert advice.code.startswith("# myapp/myapp/optimus_indexes.py\n")

	def test_text_column_gets_a_prefix_on_mariadb(self):
		ev = _ev(app="myapp", fields={"remarks": F("Small Text")})
		advice = ir.advise_finding(_missing("remarks"), evidence_lookup=_lookup(ev), tracked_apps=("myapp",))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES
		assert advice.entry["columns"] == ["remarks(255)"]
		assert "never adds or drops an index on a text column" in ir.finding_text(advice)

	def test_attach_column_is_text_by_its_real_type(self):
		ev = _ev(fields={"customer": F("Link"), "file_url": F("Attach")})
		advice = ir.advise_table("tabSales Invoice", ["customer", "file_url"], evidence_lookup=_lookup(ev))
		assert advice.entry["columns"] == ["customer", "file_url(255)"]

	def test_postgres_single_column_of_an_own_app_is_still_an_entry(self):
		ev = _ev(app="myapp", dialect="postgres", fields={"po_no": F("Data")})
		advice = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(ev), tracked_apps=("myapp",))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES
		assert advice.entry["columns"] == ["po_no"] and advice.entry["index_name"].startswith("idx_sales_invoice_")
		assert "drops only indexes named after a bare field name" in ir.finding_text(advice)
		assert "DROP INDEX IF EXISTS" in ir.finding_text(advice)

	def test_postgres_drops_a_text_column_after_the_first(self):
		ev = _ev(dialect="postgres", fields=_ALL)
		q = "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND remarks = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev))
		assert advice.entry["columns"] == ["customer"]
		assert "Optimus left out remarks" in ir.finding_text(advice)

	def test_one_tracked_app_hosts_the_entry_for_another_apps_doctype(self):
		advice = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(_SI), tracked_apps=("myapp",))
		assert advice.app_name == "myapp"
		assert '"myapp.optimus_indexes.ensure_indexes"' in ir.finding_text(advice)

	def test_unknown_developer_app_is_named_your_app_and_explained(self):
		advice = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(_SI), tracked_apps=("a", "b"))
		assert advice.app_name == ir.UNKNOWN_APP
		assert "Replace your_app with the name of your app" in ir.finding_text(advice)

	def test_a_doctype_with_no_known_app_says_another_app(self):
		ev = _ev(app="", fields={"po_no": F("Data")})
		advice = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(ev))
		assert 'belongs to another app, so do not edit it' in ir.finding_text(advice)


class TestIndexNames:
	def test_names_are_short_stable_and_table_unique(self):
		name = ir.optimus_index_name("A" * 140, ("customer",))
		assert re.fullmatch(r"idx_[a-z0-9_]{1,40}_[0-9a-f]{8}", name) and len(name) <= 53
		cols = ("customer", "status")
		assert ir.optimus_index_name("Sales Invoice", cols) == ir.optimus_index_name("Sales Invoice", cols)
		assert ir.optimus_index_name("Sales Invoice", cols) != ir.optimus_index_name("Sales Invoice", cols[::-1])
		assert ir.optimus_index_name("Sales Invoice", cols) != ir.optimus_index_name("Sales-Invoice", cols)

	def test_two_doctypes_with_the_same_columns_on_postgres_get_different_names(self):
		"""Review Focus 2 (C-I1, P1): Postgres index names are schema-wide."""
		fields = {"company": F("Link"), "posting_date": F("Date")}
		lookup = _lookup(
			_ev("Sales Invoice", dialect="postgres", fields=fields),
			_ev("Purchase Invoice", dialect="postgres", fields=fields),
		)
		a = ir.advise_table("tabSales Invoice", ["company", "posting_date"], evidence_lookup=lookup)
		b = ir.advise_table("tabPurchase Invoice", ["company", "posting_date"], evidence_lookup=lookup)
		assert a.route == b.route == ir.ROUTE_ENSURE_INDEXES
		assert a.entry["index_name"] != b.entry["index_name"]
		assert max(len(a.entry["index_name"]), len(b.entry["index_name"])) <= 53
		assert "company_posting_date_index" not in a.code + b.code

	def test_a_long_four_column_mariadb_index_stays_under_64_characters(self):
		"""P1: Frappe's own name for these four columns is 65 characters."""
		fields = {c: F("Link") for c in ("serial_and_batch_bundle", "warehouse", "posting_time")}
		fields["posting_date"] = F("Date")
		ev = _ev("Stock Ledger Entry", fields=fields)
		cols = ["serial_and_batch_bundle", "warehouse", "posting_date", "posting_time"]
		advice = ir.advise_table("tabStock Ledger Entry", cols, evidence_lookup=_lookup(ev))
		assert len("_".join(cols) + "_index") == 65
		assert len(advice.entry["index_name"]) <= 53


class TestNoCode:
	def test_a_column_that_leads_an_index_with_another_name_gives_no_code(self):
		"""Review Focus 3 (E-I1, P9a)."""
		ev = _ev(fields=_ALL, indexes=[("idx_si_customer_custom", ["customer", "company"], False)])
		advice = ir.advise_finding(_explain("Full Table Scan", _TWO), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE and advice.code is None
		text = ir.finding_text(advice)
		assert 'already leads the index "idx_si_customer_custom"' in text
		assert "The cost comes from how the query filters" in text
		assert not text.startswith("Index the")

	def test_search_index_ticked_gives_no_code(self):
		ev = _ev(fields={**_ALL, "customer": F("Link", search_index=True)})
		advice = ir.advise_finding(_missing("customer"), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE
		assert "already has Search Index ticked" in ir.finding_text(advice)

	def test_explain_possible_keys_names_an_index_on_the_column(self):
		row = {"type": "ALL", "possible_keys": "customer_index", "key": None}
		q = "select `name` from `tabSales Invoice` where `customer` = ? and `docstatus` = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q, explain_row=row), evidence_lookup=_lookup(_SI))
		assert advice.route == ir.ROUTE_NO_CODE
		assert 'EXPLAIN shows the database can use the index "customer_index"' in ir.finding_text(advice)

	def test_unique_field_gives_no_code(self):
		ev = _ev(fields={**_ALL, "po_no": F("Data", unique=True)})
		advice = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE
		assert 'Column "po_no" is already unique' in ir.finding_text(advice)

	def test_a_ghost_column_gives_no_code(self):
		advice = ir.advise_finding(_missing("po_number"), evidence_lookup=_lookup(_SI))
		assert advice.route == ir.ROUTE_NO_CODE
		assert 'Table "tabSales Invoice" has no column "po_number"' in ir.finding_text(advice)

	def test_a_case_mismatch_gives_no_code_and_names_the_real_column(self):
		advice = ir.advise_finding(_missing("Customer"), evidence_lookup=_lookup(_SI))
		assert advice.route == ir.ROUTE_NO_CODE
		assert 'the column on table "tabSales Invoice" is "customer"' in ir.finding_text(advice)

	def test_a_column_that_is_not_a_field_gives_no_code(self):
		ev = _ev(fields=_ALL, extra_types={"legacy_col": "varchar"})
		advice = ir.advise_finding(_missing("legacy_col"), evidence_lookup=_lookup(ev))
		assert 'is not a field of DocType "Sales Invoice"' in ir.finding_text(advice)

	def test_a_reserved_word_column_gives_no_code(self):
		ev = _ev(fields={"key": F("Data")})
		advice = ir.advise_finding(_missing("key"), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE
		assert "is a reserved word in MariaDB" in ir.finding_text(advice)

	def test_a_leading_text_column_on_postgres_gives_no_code(self):
		ev = _ev(dialect="postgres", fields={"remarks": F("Small Text")})
		advice = ir.advise_finding(_missing("remarks"), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE
		assert "On Postgres an index stores the whole value" in ir.finding_text(advice)

	def test_an_unindexable_leading_column_gives_no_code(self):
		ev = _ev(fields={"payload": F("JSON")})
		advice = ir.advise_finding(_missing("payload"), evidence_lookup=_lookup(ev))
		assert 'has the type json, which a plain index cannot cover' in ir.finding_text(advice)

	def test_a_key_over_3072_bytes_gives_no_code(self):
		ev = _ev(fields={"long_code": F("Data", length=1000)})
		advice = ir.advise_finding(_missing("long_code"), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE
		assert "would be 4000 bytes wide, over the 3072-byte MariaDB key limit" in ir.finding_text(advice)

	def test_trailing_columns_are_trimmed_to_fit_and_the_trim_is_said(self):
		ev = _ev(fields={c: F("Small Text") for c in ("a", "b", "c")} | {"d": F("Link")})
		advice = ir.advise_table("tabSales Invoice", ["a", "b", "c", "d"], evidence_lookup=_lookup(ev))
		assert advice.entry["columns"] == ["a(255)", "b(255)", "c(255)"]
		assert "Optimus left out d (the index would pass the 3072-byte MariaDB key limit)" in ir.finding_text(advice)

	def test_no_evidence_gives_no_code(self):
		advice = ir.advise_finding(_missing("po_no"), evidence_lookup=lambda table: None)
		assert advice.route == ir.ROUTE_NO_CODE
		assert 'Optimus could not read DocType "Sales Invoice"' in ir.finding_text(advice)

	def test_link_search_with_or_and_like_names_the_shapes(self):
		"""Fix round 2: the real link-search shape. Every column but disabled sits inside an
		OR group and is compared by LIKE ?, which no composite index can use; disabled is a
		Check field, so an index would not help."""
		ev = _ev("Item", fields={
			"disabled": F("Check"), "item_name": F("Data", search_index=True), "description": F("Text Editor"),
			"item_group": F("Link"), "customer_code": F("Small Text"),
		})
		q = (
			"select `tabItem`.`name` from `tabItem` where `tabItem`.`disabled` = ? and (`tabItem`.`item_name` like ? "
			"or `tabItem`.`description` like ? or `tabItem`.`item_group` like ? or `tabItem`.`customer_code` like ?) "
			"order by `tabItem`.`idx` desc limit ?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabItem"), evidence_lookup=_lookup(ev))
		text = ir.finding_text(advice)
		assert advice.route == ir.ROUTE_NO_CODE and advice.code is None and "item_name" in text
		assert "a LIKE on item_name" in text and "an OR between conditions" in text
		assert "disabled is a Check field" in text and "an index would not help" in text

	def test_ifnull_around_an_indexed_column_names_the_function(self):
		"""Fix round 3: IFNULL(status, ?) cannot use any index on status, even its Search
		Index, so status is left out with the function named and customer is indexed."""
		ev = _ev(fields={**_ALL, "status": F("Select", search_index=True)})
		q = "SELECT name FROM `tabSales Invoice` WHERE ifnull(status, ?) != ? and customer = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.columns == ("customer",)
		assert (
			"Optimus left out status (compared through the function IFNULL(), which an index on the column "
			"cannot use)" in ir.finding_text(advice)
		)

	def test_a_trailing_creation_never_counts_as_already_indexed(self):
		"""D5: Frappe indexes creation on every non-child table; a trailing creation is the
		sort column, not an equality filter, so even a unique index on it is no reason to
		refuse the recipe."""
		q = "SELECT name FROM `tabSales Invoice` WHERE customer = ? ORDER BY creation DESC"
		for unique in (False, True):
			ev = _ev(fields=_ALL, indexes=[("creation", ["creation"], unique)])
			advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(ev))
			assert advice.route == ir.ROUTE_ENSURE_INDEXES, ir.finding_text(advice)
			assert advice.entry["columns"] == ["customer", "creation"]

	def test_a_later_column_with_its_own_index_does_not_block_the_composite(self):
		"""Fix round 1 I3: only the LEADING column's index decides; real ERPNext Sales
		Invoice has Search Index on customer and posting_date, and (company, posting_date)
		still serves WHERE company = ? ORDER BY posting_date."""
		ev = _ev(
			fields={**_ALL, "customer": F("Link", search_index=True), "posting_date": F("Date", search_index=True)},
			indexes=[("customer", ["customer"], False), ("posting_date", ["posting_date"], False)],
		)
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? ORDER BY posting_date DESC"
		row = {"type": "ALL", "possible_keys": "posting_date", "key": "posting_date"}
		for explain_row in (None, row):
			advice = ir.advise_finding(_explain("Filesort", q, explain_row=explain_row), evidence_lookup=_lookup(ev))
			assert advice.route == ir.ROUTE_ENSURE_INDEXES, ir.finding_text(advice)
			assert advice.entry == {
				"doctype": "Sales Invoice", "columns": ["company", "posting_date"],
				"index_name": ir.optimus_index_name("Sales Invoice", ("company", "posting_date")),
			}

	def test_a_recipe_an_existing_index_already_starts_with_gives_no_code(self):
		"""Fix round 1 I3: an existing composite that starts with the whole recipe list."""
		ev = _ev(fields=_ALL, indexes=[("idx_company_date_status", ["company", "posting_date", "status"], False)])
		advice = ir.advise_table("tabSales Invoice", ["company", "posting_date"], evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE and advice.code is None
		assert (
			'The index "idx_company_date_status" on table "tabSales Invoice" already starts with '
			"(company, posting_date)" in ir.card_note(advice)
		)


class TestPredicateShape:
	"""Fix round 2: a column compared only inside an OR group, or only by a LIKE whose
	pattern may start with a wildcard (a normalized LIKE ? hides it), cannot be used by a
	composite index, so it is left out and named."""

	def test_an_or_group_keeps_only_the_and_columns(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? AND (status = ? OR docstatus = ?)"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.columns == ("company",)
		assert "Optimus left out status (compared only inside an OR between conditions)" in ir.finding_text(advice)

	def test_a_like_parameter_is_left_out(self):
		for like in ("LIKE ?", "NOT LIKE ?", "like %(txt)s", "LIKE '%voice'"):
			q = f"SELECT name FROM `tabSales Invoice` WHERE customer = ? AND po_no {like} AND status = ?"
			advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI))
			assert advice.columns == ("customer", "status"), like
			assert "Optimus left out po_no (compared by a LIKE whose pattern can start with a wildcard)" in (
				ir.finding_text(advice)
			)

	def test_a_sort_on_the_like_column_is_no_plain_use_of_it(self):
		"""Fix round 3: the ORDER BY after the WHERE clause is not part of the filter."""
		q = "SELECT `name` FROM `tabSales Invoice` WHERE `customer_name` LIKE ? ORDER BY `customer_name`"
		assert ir._unusable_where_columns(q, [("WHERE", "customer_name")]) == {"customer_name": {"like"}}

	def test_comments_are_not_part_of_the_filter(self):
		for q in (
			"SELECT `name` FROM `tabSales Invoice` WHERE `company`=? /* or `customer`=? */ AND `status`=?",
			"SELECT `name` FROM `tabSales Invoice` WHERE `company`=? -- don't or this\n AND `status`=?",
		):
			advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI))
			assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.columns == ("company", "status"), q

	def test_double_quoted_postgres_identifiers_are_names(self):
		q = (
			'select "name" from "tabSales Invoice" where "company" = %s and ("status" = %s or "customer" = %s) '
			'order by "posting_date"'
		)
		shapes = ir._unusable_where_columns(q, [("WHERE", "company"), ("WHERE", "status"), ("WHERE", "customer")])
		assert shapes == {"status": {"or"}, "customer": {"or"}}

	def test_a_visible_prefix_like_can_use_the_index(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND po_no LIKE 'PO-%'"
		assert ir._unusable_where_columns(q, [("WHERE", "customer"), ("WHERE", "po_no")]) == {}

	def test_a_top_level_or_leaves_nothing_to_index(self):
		for q in (
			"SELECT name FROM `tabSales Invoice` WHERE customer = ? OR status = ?",
			"SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ? OR company = ?",
		):
			advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI))
			assert advice.route == ir.ROUTE_NO_CODE, q
			text = ir.finding_text(advice)
			assert "an OR between conditions" in text and "an index would not help" in text

	def test_between_and_plain_conditions_are_kept(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? AND posting_date BETWEEN ? AND ? AND status = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI))
		assert advice.columns == ("company", "posting_date", "status")

	def test_a_sort_column_is_not_a_filter(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND (status = ? OR company = ?) ORDER BY posting_date"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI))
		assert advice.columns == ("customer", "posting_date")

	def test_a_filter_the_scan_cannot_place_is_left_out(self):
		"""Fail closed: a WHERE column the token scan cannot find in the main WHERE clause."""
		shapes = ir._unusable_where_columns(
			"SELECT name FROM `tabSales Invoice` WHERE customer = ?", [("WHERE", "customer"), ("WHERE", "status")],
		)
		assert shapes == {"status": {"unsure"}}
		assert ir._unusable_where_columns("SELECT name FROM `tabX` WHERE (a = ?", [("WHERE", "a")]) == {"a": {"unsure"}}
		# a name used only as a function is no use of that column
		q = "SELECT name FROM `tabX` WHERE company = ? AND year(posting_date) = ?"
		assert ir._unusable_where_columns(q, [("WHERE", "company"), ("WHERE", "year")]) == {"year": {"unsure"}}


class TestFunctionWrapped:
	"""Fix round 3: a column compared only inside a function call (IFNULL(), YEAR(),
	DATE(), LOWER(), NOT (...)) cannot use an index on it."""

	_EV = _ev(fields={**_ALL, "customer_name": F("Data")})

	def _advise(self, ftype, where):
		q = f"SELECT `name` FROM `tabSales Invoice` WHERE {where}"
		return ir.advise_finding(_explain(ftype, q), evidence_lookup=_lookup(self._EV))

	def test_an_ifnull_filter_is_left_out_and_the_rest_is_kept(self):
		advice = self._advise("Filesort", "IFNULL(`status`,?)<>? AND `customer`=? ORDER BY `posting_date` DESC")
		assert advice.columns == ("customer", "posting_date")
		assert "Optimus left out status (compared through the function IFNULL()" in ir.finding_text(advice)

	def test_a_lone_ifnull_filter_gives_no_code(self):
		advice = self._advise("Full Table Scan", "IFNULL(`status`,?)<>?")
		assert advice.route == ir.ROUTE_NO_CODE
		text = ir.finding_text(advice)
		assert "the function IFNULL() wrapped around status" in text and "an index would not help" in text

	def test_a_not_like_through_ifnull_keeps_the_other_filter(self):
		advice = self._advise("Full Table Scan", "IFNULL(`customer_name`,?) NOT LIKE ? AND `company`=?")
		assert advice.columns == ("company",)
		assert "compared through the function IFNULL()" in ir.finding_text(advice)

	def test_year_and_date_get_no_exemption(self):
		advice = self._advise("Full Table Scan", "YEAR(`posting_date`)=? AND `company`=?")
		assert advice.columns == ("company",) and "the function YEAR()" in ir.finding_text(advice)
		advice = self._advise("Full Table Scan", "DATE(`posting_date`)=? AND `customer`=?")
		assert advice.columns == ("customer",) and "the function DATE()" in ir.finding_text(advice)

	def test_a_like_inside_a_call_is_named_by_the_call(self):
		advice = self._advise("Full Table Scan", "IF(`customer_name` LIKE ?, ?, ?)=? AND `company`=?")
		assert advice.columns == ("company",)
		assert "Optimus left out customer_name (compared through the function IF()" in ir.finding_text(advice)

	def test_a_function_on_the_value_side_keeps_the_column(self):
		advice = self._advise("Full Table Scan", "`posting_date` BETWEEN ? AND DATE_ADD(?, INTERVAL ? DAY) AND `company`=?")
		assert "posting_date" in advice.columns and "company" in advice.columns

	def test_not_brackets_and_the_regexp_family(self):
		advice = self._advise("Full Table Scan", "`company`=? AND NOT (`status`=? OR `status`=?)")
		assert advice.columns == ("company",) and "the function NOT()" in ir.finding_text(advice)
		for op in ("ILIKE ?", "RLIKE ?", "REGEXP ?", "NOT REGEXP ?", "REGEXP '^A'"):
			advice = self._advise("Full Table Scan", f"`company`=? AND `customer` {op}")
			assert advice.columns == ("company",), op

	def test_the_innermost_call_is_named(self):
		advice = self._advise("Full Table Scan", "IFNULL(LOWER(`customer_name`), ?)=? AND `company`=?")
		assert advice.columns == ("company",)
		assert "Optimus left out customer_name (compared through the function LOWER()" in ir.finding_text(advice)


class TestUnreadableQueries:
	"""Fix round 3: a query the scan cannot read gives honest text, never parser fragments."""

	_EV = _ev(fields=_ALL)

	@staticmethod
	def _cut_query(at: str, offset: int) -> str:
		"""A Slow Query whose 500-character cut lands ``offset`` characters into ``at``."""
		def query(pad):
			return (
				f"SELECT `tabSales Invoice`.`name` AS `{'x' * pad}` FROM `tabSales Invoice` WHERE "
				"`tabSales Invoice`.`company`=? AND `tabSales Invoice`.`customer`=? AND "
				"`tabSales Invoice`.`status` IN (?) ORDER BY `tabSales Invoice`.`posting_date` DESC"
			)
		pad = 500 - query(0).index(at) - offset
		return query(pad)[:500]

	def test_a_slow_query_cut_inside_its_where_gives_honest_no_code(self):
		for at, offset in (("`tabSales Invoice`.`customer`", 12), ("`tabSales Invoice`.`customer`", 0)):
			cut = self._cut_query(at, offset)
			assert len(cut) == 500 and "ORDER BY" not in cut
			finding = {"finding_type": "Slow Query", "technical_detail": {"normalized_query": cut}}
			advice = ir.advise_finding(finding, evidence_lookup=_lookup(self._EV))
			assert advice.route == ir.ROUTE_NO_CODE, cut[-40:]
			text = ir.finding_text(advice, install=False)
			assert "Optimus could not read how this query combines its filters" in text
			assert "EXPLAIN" in text and "tabSales" not in text

	def test_a_union_of_bracketed_selects_gives_honest_no_code(self):
		q = (
			"(SELECT `name` FROM `tabSales Invoice` WHERE `company`=? AND `customer`=?) UNION "
			"(SELECT `name` FROM `tabSales Invoice` WHERE `company`=? AND `status`=?)"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(self._EV))
		assert advice.route == ir.ROUTE_NO_CODE
		assert ir.finding_text(advice).startswith("Optimus could not read how this query combines its filters")

	def test_unsure_columns_stay_out_of_the_cannot_use_sentence(self):
		advice = ir.advise(
			"tabSales Invoice", ["customer"], evidence=self._EV,
			unusable={"customer": {"or"}, "status": {"unsure"}, "tabSales": {"unsure"}, "I": {"unsure"}},
		)
		assert advice.route == ir.ROUTE_NO_CODE
		first, rest = advice.reason.split("A composite index cannot use those columns.", 1)
		assert "an OR between conditions on customer" in first and "status" not in first
		assert "Optimus could not read how the query filters on status." in rest
		assert "tabSales" not in advice.reason and " I," not in advice.reason and " I." not in advice.reason


class TestQualifiersAndSubqueries:
	"""Fix round 3: a dotted reference counts only for the target table or its aliases,
	and a (SELECT ...) group neither uses nor taints the outer columns."""

	_GL = _ev("GL Entry", fields={
		"company": F("Link"), "party": F("Dynamic Link"), "account": F("Link"), "voucher_no": F("Dynamic Link"),
	})

	def test_another_tables_column_of_the_same_name_does_not_count(self):
		for q in (
			"select gle.name from `tabGL Entry` gle inner join `tabAccount` acc on acc.name = gle.account "
			"where (gle.company = ? or gle.party = ?) and acc.company = ?",
			"select `tabGL Entry`.name from `tabGL Entry` inner join `tabAccount` on `tabAccount`.name = "
			"`tabGL Entry`.account where (`tabGL Entry`.company = ? or `tabGL Entry`.party = ?) and "
			"`tabAccount`.company = ?",
		):
			advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabGL Entry"), evidence_lookup=_lookup(self._GL))
			assert advice.columns == ("account",), q
			assert "Optimus left out company (compared only inside an OR between conditions)" in ir.finding_text(advice)

	def test_a_subquery_neither_uses_nor_taints_the_outer_columns(self):
		q = (
			"SELECT `tabGL Entry`.`name` FROM `tabGL Entry` WHERE (`tabGL Entry`.`company`=? OR `tabGL Entry`.`party`=?) "
			"AND `tabGL Entry`.`voucher_no` IN (SELECT `name` FROM `tabSales Invoice` WHERE `company`=?)"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabGL Entry"), evidence_lookup=_lookup(self._GL))
		assert "company" not in advice.columns and "voucher_no" in advice.columns
		q = (
			"SELECT `tabSales Invoice`.`name` FROM `tabSales Invoice` WHERE `tabSales Invoice`.`company`=? AND "
			"`tabSales Invoice`.`customer` IN (SELECT `tabCustomer`.`name` FROM `tabCustomer` "
			"WHERE `tabCustomer`.`disabled`=? OR `tabCustomer`.`is_frozen`=?)"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI))
		assert advice.columns == ("company", "customer")


class TestSingleColumnOr:
	"""Fix round 3: an OR whose every branch compares the same one column is a plain use."""

	def test_an_or_on_one_column_is_a_plain_use(self):
		for where, cols in (
			("(`po_no` IS NULL OR `po_no`=?) AND `customer`=?", ("po_no", "customer")),
			("(`status`=? OR `status`=?) AND `company`=?", ("status", "company")),
			("(`status` IN (?) OR `status` IS NULL) AND `company`=?", ("status", "company")),
			("(`status`=? /* a comment */ OR `status`=?) AND `company`=?", ("status", "company")),
		):
			q = f"SELECT `name` FROM `tabSales Invoice` WHERE {where}"
			advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI))
			assert advice.columns == cols, where

	def test_an_or_across_two_columns_still_taints_both(self):
		for where in (
			"(`po_no` IS NULL OR `customer`=?) AND `company`=?",
			"((`po_no`=? AND `customer`=?) OR `status`=?) AND `company`=?",
		):
			q = f"SELECT `name` FROM `tabSales Invoice` WHERE {where}"
			advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI))
			assert advice.columns == ("company",), where


class TestCheckOnly:
	def test_a_check_only_recipe_gives_no_code(self):
		"""Fix round 3: every recipe of Check fields only, not just after the shape scan."""
		ev = _ev(fields={**_ALL, "is_return": F("Check")})
		q = "SELECT `name` FROM `tabSales Invoice` WHERE `is_return`=?"
		for advice in (
			ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev)),
			ir.advise_table("tabSales Invoice", ["is_return"], evidence_lookup=_lookup(ev)),
		):
			assert advice.route == ir.ROUTE_NO_CODE
			assert "is_return is a Check field" in advice.reason and "an index would not help" in advice.reason


class TestPostgresRowWidth:
	"""Fix round 1 item 9: a Postgres btree row holds at most about 2704 bytes."""

	def test_a_postgres_index_is_trimmed_to_the_row_limit(self):
		"""Fix round 2: trailing columns are left out until the row fits, as on MariaDB."""
		ev = _ev(dialect="postgres", fields={"code_a": F("Data", length=400), "code_b": F("Data", length=400)})
		advice = ir.advise_table("tabSales Invoice", ["code_a", "code_b"], evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.entry["columns"] == ["code_a"]
		assert "Optimus left out code_b (the index would pass the 2704-byte Postgres index row limit)" in (
			ir.card_note(advice)
		)

	def test_a_postgres_leading_column_over_the_row_limit_gives_no_code(self):
		ev = _ev(dialect="postgres", fields={"long_code": F("Data", length=700), "customer": F("Link")})
		advice = ir.advise_table("tabSales Invoice", ["long_code", "customer"], evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE
		assert "could be 2800 bytes wide, over the 2704-byte Postgres index row limit" in ir.card_note(advice)

	def test_a_postgres_index_under_the_row_limit_is_an_entry(self):
		ev = _ev(dialect="postgres", fields={"code_a": F("Data", length=300), "customer": F("Link")})
		advice = ir.advise_table("tabSales Invoice", ["code_a", "customer"], evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.entry["columns"] == ["code_a", "customer"]


class TestDatabaseStamp:
	"""Fix round 1 I2: an entry that is only right on one database says which one."""

	def test_entries_tied_to_one_database_name_it(self):
		ev = _ev(app="myapp", fields={"remarks": F("Small Text"), "customer": F("Link")})
		prefixed = ir.advise_table("tabSales Invoice", ["customer", "remarks"], evidence_lookup=_lookup(ev))
		assert prefixed.entry["columns"] == ["customer", "remarks(255)"] and prefixed.entry["db"] == "mariadb"
		setter = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(_SI))
		assert setter.entry["db"] == "mariadb"
		pg = _ev(app="myapp", dialect="postgres", fields={"po_no": F("Data")})
		single = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(pg), tracked_apps=("myapp",))
		assert single.entry["db"] == "postgres"
		for advice in (prefixed, setter, single):
			assert json.dumps(advice.entry) in advice.code

	def test_a_mariadb_composite_too_wide_for_a_postgres_row_is_mariadb_only(self):
		"""Fix round 2: 2800 bytes fits the 3072-byte MariaDB key but not a Postgres row."""
		ev = _ev(fields={"code_a": F("Data", length=350), "code_b": F("Data", length=350)})
		advice = ir.advise_table("tabSales Invoice", ["code_a", "code_b"], evidence_lookup=_lookup(ev))
		assert advice.entry["columns"] == ["code_a", "code_b"] and advice.entry["db"] == "mariadb"

	def test_a_plain_composite_runs_on_any_database(self):
		for dialect in ("mariadb", "postgres"):
			ev = _ev(dialect=dialect, fields=_ALL)
			advice = ir.advise_finding(_explain("Full Table Scan", _TWO), evidence_lookup=_lookup(ev))
			assert advice.route == ir.ROUTE_ENSURE_INDEXES and "db" not in advice.entry


class TestCaveats:
	def test_write_hot_table_gets_the_write_cost_note(self):
		ev = _ev("GL Entry", fields={"against_voucher": F("Dynamic Link")})
		advice = ir.advise_finding(_missing("against_voucher", table="tabGL Entry"), evidence_lookup=_lookup(ev))
		assert 'Note: "tabGL Entry" takes many writes' in ir.finding_text(advice)
		assert "maintenance window" not in ir.finding_text(advice)

	def test_write_hot_table_on_postgres_gets_the_build_cost_note(self):
		ev = _ev("GL Entry", dialect="postgres", fields={"against_voucher": F("Dynamic Link")})
		advice = ir.advise_finding(_missing("against_voucher", table="tabGL Entry"), evidence_lookup=_lookup(ev))
		text = ir.finding_text(advice)
		assert "building an index blocks writes to the table until it finishes" in text
		assert "maintenance window" in text

	def test_a_custom_field_composite_is_indexed_after_fixtures_sync(self):
		"""D4: after_sync runs right after the install's fixture sync, so a fixture-shipped
		Custom Field is indexed on a fresh install too; no residual is left to explain."""
		ev = _ev(fields={**_ALL, "status": F("Data", custom=True)})
		advice = ir.advise_finding(_explain("Full Table Scan", _TWO), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES
		text = ir.finding_text(advice)
		assert "a fixture-shipped Custom Field is indexed right after fixtures sync on install (after_sync)" in text
		assert "after_install runs before fixtures are synced" not in text
		assert "next bench migrate" not in text


class TestExplainColumns:
	def test_filesort_keeps_trailing_creation(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE customer = ? ORDER BY creation DESC"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI))
		assert advice.entry["columns"] == ["customer", "creation"]
		assert ir.finding_text(advice).startswith("Index the filter columns followed by the sort column")

	def test_filesort_on_creation_alone_has_no_advice(self):
		q = "SELECT name FROM `tabSales Invoice` ORDER BY creation DESC"
		assert ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI)) is None

	def test_temporary_table_drops_leading_metadata(self):
		q = "SELECT customer, count(*) FROM `tabSales Invoice` WHERE docstatus = ? GROUP BY customer"
		advice = ir.advise_finding(_explain("Temporary Table", q), evidence_lookup=_lookup(_SI))
		assert advice.entry == {"doctype": "Sales Invoice", "search_index_field": "customer", "db": "mariadb"}
		assert ir.finding_text(advice).startswith("Index the filter and GROUP BY columns")

	def test_alias_table_resolves_to_the_single_doctype_table(self):
		q = "SELECT si.name FROM `tabSales Invoice` si WHERE si.customer = ?"
		advice = ir.advise_finding(_explain("Low Filter Ratio", q, table="si"), evidence_lookup=_lookup(_SI))
		assert advice.entry == {"doctype": "Sales Invoice", "search_index_field": "customer", "db": "mariadb"}

	def test_order_by_an_aggregate_alias_is_not_indexed(self):
		"""P9b: ORDER BY total, total = sum(amount), parses as ORDER BY amount."""
		ev = _ev(fields={**_ALL, "amount": F("Data")})
		q = "SELECT customer, sum(amount) as total FROM `tabSales Invoice` WHERE company = ? GROUP BY customer ORDER BY total DESC"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(ev))
		assert advice.entry == {"doctype": "Sales Invoice", "search_index_field": "company", "db": "mariadb"}
		assert "The sort column is a Frappe metadata column or an aggregate" in ir.finding_text(advice)

	def test_dropped_sort_column_is_not_claimed_by_the_lead(self):
		"""P9c: idx is a metadata column, so the lead must not promise a sort index."""
		ev = _ev("Sales Invoice Item", fields={"item_code": F("Link")})
		q = "SELECT * FROM `tabSales Invoice Item` WHERE item_code = ? ORDER BY idx"
		advice = ir.advise_finding(_explain("Filesort", q, table="tabSales Invoice Item"), evidence_lookup=_lookup(ev))
		assert not ir.finding_text(advice).startswith("Index the filter columns followed by the sort column")

	def test_child_table_parent_filter_has_no_advice(self):
		ev = _ev("Sales Invoice Item", fields={"item_code": F("Link")})
		q = "SELECT * FROM `tabSales Invoice Item` WHERE parent = ? ORDER BY idx"
		finding = _explain("Full Table Scan", q, table="tabSales Invoice Item")
		assert ir.advise_finding(finding, evidence_lookup=_lookup(ev)) is None

	def test_empty_query_and_non_doctype_table_have_no_advice(self):
		assert ir.advise_finding(_explain("Full Table Scan", ""), evidence_lookup=_lookup(_SI)) is None
		assert ir.advise_finding(_missing("x", table="__Auth"), evidence_lookup=_lookup(_SI)) is None

	def test_slow_query_uses_its_single_doctype_table(self):
		finding = {"finding_type": "Slow Query", "technical_detail": {"normalized_query": _TWO}}
		advice = ir.advise_finding(finding, evidence_lookup=_lookup(_SI))
		assert advice.entry["columns"] == ["customer", "status"]
		assert ir.finding_text(advice).startswith("Your app's ensure_indexes() function creates the index")


class TestColumns:
	def test_duplicates_collapse_and_non_identifiers_are_dropped(self):
		lookup = _lookup(_SI)
		assert ir.advise_table("tabSales Invoice", ["customer", "customer"], evidence_lookup=lookup).columns == ("customer",)
		assert ir.advise_table("tabSales Invoice", ["customer\n", "status"], evidence_lookup=lookup).columns == ("status",)
		assert ir.advise_finding(_missing("po_no`; DROP"), evidence_lookup=lookup) is None
		assert ir.advise_finding(_missing("café"), evidence_lookup=lookup) is None

	def test_capped_at_four_columns(self):
		ev = _ev(fields={c: F("Link") for c in "abcde"})
		advice = ir.advise_table("tabSales Invoice", list("abcde"), evidence_lookup=_lookup(ev))
		assert advice.entry["columns"] == ["a", "b", "c", "d"]


class TestCardNote:
	"""D3: the card shows the advice's own code (``rec.code``) above this note, or no
	code line at all, so the note never refers to a hard-coded add_index call."""

	def test_card_and_finding_come_from_the_same_advice(self):
		advice = ir.advise_table("tabSales Invoice", ["customer", "status"], evidence_lookup=_lookup(_SI))
		name = ir.optimus_index_name("Sales Invoice", ("customer", "status"))
		note = ir.card_note(advice)
		assert name in note and name in ir.finding_text(advice) and name in advice.code
		assert _HOOKS in note
		assert note.startswith("Your app's ensure_indexes() function creates the index")

	def test_card_note_for_a_single_column_says_it_belongs_on_the_field(self):
		ev = _ev(app="myapp", fields={"po_no": F("Data")})
		advice = ir.advise_table("tabSales Invoice", ["po_no"], evidence_lookup=_lookup(ev), tracked_apps=("myapp",))
		assert advice.code is None
		assert ir.card_note(advice).startswith("One column: bench migrate drops a single-column index")

	def test_card_note_for_no_code(self):
		advice = ir.advise_table("tabSales Invoice", ["po_number"], evidence_lookup=_lookup(_SI))
		assert ir.card_note(advice).startswith("Do not add this index.")

	def test_an_existing_module_gets_only_the_new_entry(self):
		"""Fix round 1 I4: a developer who already has optimus_indexes.py adds one entry."""
		for advice in (
			ir.advise_finding(_explain("Full Table Scan", _TWO), evidence_lookup=_lookup(_SI)),
			ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(_SI)),
		):
			line = f"If that file already exists, add only this entry to its INDEXES list: {json.dumps(advice.entry)}"
			assert line in ir.finding_text(advice) and line in ir.card_note(advice)

	def test_the_finding_and_the_card_point_at_the_code_above_them(self):
		"""Fix round 1 item 7: the report shows the code block before the prose."""
		advice = ir.advise_finding(_explain("Full Table Scan", _TWO), evidence_lookup=_lookup(_SI))
		for text in (ir.finding_text(advice), ir.card_note(advice)):
			assert "Save the code above as your_app/your_app/optimus_indexes.py" in text and "below" not in text

	def test_the_prompt_text_has_no_save_instruction(self):
		"""Fix round 1 item 7: the Slow Query prompt carries no code, so nothing to save."""
		advice = ir.advise_finding(_explain("Full Table Scan", _TWO), evidence_lookup=_lookup(_SI))
		text = ir.finding_text(advice, install=False)
		assert advice.entry["index_name"] in text
		for gone in ("Save the code", "If that file already exists", "Replace your_app"):
			assert gone not in text

	def test_no_card_note_points_at_a_call_above(self):
		for advice in _every_advice():
			if advice is not None:
				note = ir.card_note(advice)
				assert "call above" not in note and "Run that call" not in note, note


class TestGeneratedCode:
	def test_every_generated_module_compiles(self):
		for advice in _every_advice():
			if advice is not None and advice.code:
				compile(advice.code, "optimus_indexes.py", "exec")

	def test_an_unsafe_app_name_falls_back_to_your_app(self):
		code = ir.ensure_indexes_code([{"doctype": "X", "search_index_field": "a"}], app_name="bad app\nimport os")
		assert code.startswith("# your_app/your_app/optimus_indexes.py\n")
		assert "import os" not in code

	def test_hooks_lines_name_all_three_hooks(self):
		"""D4: after_install, after_sync (right after the install's fixture sync) and
		after_migrate, in that order."""
		code = ir.ensure_indexes_code([{"doctype": "X", "search_index_field": "a"}], app_name="myapp")
		lines = [
			'#   after_install = ["myapp.optimus_indexes.ensure_indexes"]',
			'#   after_sync = ["myapp.optimus_indexes.ensure_indexes"]',
			'#   after_migrate = ["myapp.optimus_indexes.ensure_indexes"]',
		]
		for line in lines:
			assert line in code.splitlines(), line
		assert [code.index(line) for line in lines] == sorted(code.index(line) for line in lines)


def _every_advice():
	evidences = [
		_ev(app="myapp", fields=_ALL),
		_ev(fields=_ALL),
		_ev(fields={**_ALL, "po_no": F("Data", custom=True)}),
		_ev("My Notes", app="frappe", custom_doctype=True, fields=_ALL),
		_ev(dialect="postgres", fields=_ALL),
		_ev("GL Entry", fields=_ALL),
		_ev("GL Entry", dialect="postgres", fields=_ALL),
	]
	query = "SELECT name FROM `{t}` WHERE customer = ? AND status = ? GROUP BY customer ORDER BY creation"
	for ev in evidences:
		lookup = _lookup(ev)
		for col in ("po_no", "remarks", "customer"):
			yield ir.advise_finding(_missing(col, table=ev.table), evidence_lookup=lookup, tracked_apps=("myapp",))
		for ftype in ("Full Table Scan", "Filesort", "Temporary Table", "Low Filter Ratio"):
			yield ir.advise_finding(_explain(ftype, query.format(t=ev.table), table=ev.table), evidence_lookup=lookup)
		yield ir.advise_table(ev.table, ["customer", "remarks", "posting_date"], evidence_lookup=lookup)


def test_no_raw_ddl_customize_form_or_dash_in_any_advice():
	advices = [a for a in _every_advice() if a is not None]
	assert len(advices) >= 40
	for advice in advices:
		blob = ir.finding_text(advice) + ir.card_note(advice) + (advice.code or "")
		for bad in _FORBIDDEN:
			assert bad not in blob, (bad, blob)
