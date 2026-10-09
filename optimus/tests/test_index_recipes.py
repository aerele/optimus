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
		"""With Tracked Apps set, an app outside it is another app's (E1: with Tracked Apps
		empty the note is conditional, test_index_advice_correctness)."""
		ev = _ev(app="", fields={"po_no": F("Data")})
		advice = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(ev), tracked_apps=("myapp",))
		assert 'belongs to another app, so do not edit it' in ir.finding_text(advice)

	def test_the_property_setter_route_says_it_builds_the_index_first(self):
		"""D1: the entry builds po_no_index itself, then declares Search Index on the
		field; it no longer syncs the table."""
		advice = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(_SI))
		text = ir.finding_text(advice)
		assert 'creates the index "po_no_index" on "po_no" once' in text
		assert "then sets Search Index on the field with a Property Setter" in text
		assert "syncs the table" not in text and "updatedb" not in advice.code

	def test_a_custom_field_created_in_code_gets_search_index_in_its_dict(self):
		"""D5: a Custom Field your app creates with create_custom_fields()."""
		ev = _ev(fields={"po_no": F("Data", custom=True)})
		advice = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(ev))
		assert (
			'If your app creates it in code, add "search_index": 1 to the field\'s dict in your '
			"create_custom_fields() call."
		) in ir.finding_text(advice)

	def test_a_trailing_newline_never_passes_as_an_app_name(self):
		"""S1: ``$`` matches before a trailing newline, so ``.match`` let "myapp\\n"
		through and the generated header split into a broken comment line."""
		advice = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(_SI), tracked_apps=("myapp\n",))
		assert advice.app_name == ir.UNKNOWN_APP
		code = ir.ensure_indexes_code([advice.entry], app_name="myapp\n")
		assert code.startswith("# your_app/your_app/optimus_indexes.py\n")
		compile(code, "optimus_indexes.py", "exec")
		own = _ev(app="myapp\n", fields=_ALL)
		advice = ir.advise_finding(_explain("Full Table Scan", _TWO), evidence_lookup=_lookup(own), tracked_apps=("myapp\n",))
		assert advice.app_name == ir.UNKNOWN_APP

	def test_a_trailing_newline_never_passes_as_a_table_name(self):
		"""S1: the outer strip() leaves a newline that sits inside the backticks."""
		assert ir.doctype_of("`tabSales Invoice`") == "Sales Invoice"
		assert ir.doctype_of("`tabSales Invoice\n`") is None


class TestPostgresCaveatOnEveryEntryThatRunsThere:
	"""D4: an entry without a db stamp runs on both databases, so it carries the
	Postgres DROP INDEX caveat and a "why it stays" text for both."""

	def test_an_unstamped_mariadb_composite_carries_the_postgres_caveat(self):
		advice = ir.advise_finding(_explain("Full Table Scan", _TWO), evidence_lookup=_lookup(_SI))
		assert "db" not in advice.entry
		text = ir.finding_text(advice)
		assert "DROP INDEX IF EXISTS" in text
		assert "never drops an index that spans several columns" in text
		assert "drops only indexes named after a bare field name" in text

	def test_an_unstamped_postgres_composite_also_explains_mariadb(self):
		ev = _ev(dialect="postgres", fields=_ALL)
		advice = ir.advise_finding(_explain("Full Table Scan", _TWO), evidence_lookup=_lookup(ev))
		assert "db" not in advice.entry
		text = ir.finding_text(advice)
		assert "DROP INDEX IF EXISTS" in text and "never drops an index that spans several columns" in text

	def test_a_mariadb_only_entry_has_no_postgres_text(self):
		ev = _ev(app="myapp", fields={"remarks": F("Small Text")})
		prefix = ir.advise_finding(_missing("remarks"), evidence_lookup=_lookup(ev), tracked_apps=("myapp",))
		setter = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(_SI))
		for advice in (prefix, setter):
			assert advice.entry["db"] == "mariadb"
			text = ir.finding_text(advice)
			assert "DROP INDEX" not in text and "bare field name" not in text

	def test_a_postgres_only_entry_has_no_mariadb_text(self):
		ev = _ev(app="myapp", dialect="postgres", fields={"po_no": F("Data")})
		advice = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(ev), tracked_apps=("myapp",))
		assert advice.entry["db"] == "postgres"
		text = ir.finding_text(advice)
		assert "DROP INDEX IF EXISTS" in text and "spans several columns" not in text


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
		"""Review Focus 3 (E-I1, P9a), for a single-column recipe: a recipe of several
		columns is refused only by an index that starts with all of them (C1)."""
		ev = _ev(fields=_ALL, indexes=[("idx_si_customer_custom", ["customer", "company"], False)])
		q = "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND docstatus = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE and advice.code is None
		text = ir.finding_text(advice)
		assert 'already leads the index "idx_si_customer_custom"' in text
		assert "The query's filter looks index-friendly" in text
		assert not text.startswith("Index the")

	def test_search_index_ticked_is_no_proof_of_an_index(self):
		"""C2: the table's real indexes decide, never the Search Index flag."""
		ev = _ev(fields={**_ALL, "customer": F("Link", search_index=True)})
		advice = ir.advise_finding(_missing("customer"), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES
		assert "Search Index ticked" not in ir.finding_text(advice)

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
		assert 'Column "payload" is a JSON field, which a plain index cannot cover' in ir.finding_text(advice)

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
		assert advice.unknown
		assert 'Optimus has no information about table "tabSales Invoice"' in ir.finding_text(advice)

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
		# bounded corrective R1: with a Check field in the mix the verdict is hedged, never "would not help"
		assert "disabled is a Check field, which usually matches most of the table's rows" in text
		assert "So Optimus gives no index code." in text and "would not help" not in text

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
		"""Fix round 4: equality columns first, the BETWEEN range column last."""
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? AND posting_date BETWEEN ? AND ? AND status = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI))
		assert advice.columns == ("company", "status", "posting_date")

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
		# a clause that ends on an operator was cut short, whatever cut it
		assert ir._unusable_where_columns("SELECT name FROM `tabX` WHERE a = ? AND", [("WHERE", "a")]) == {"a": {"unsure"}}
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
		assert advice.columns == ("company",)
		assert "Optimus left out status (compared inside a NOT (...), which an index" in ir.finding_text(advice)
		assert "NOT()" not in ir.finding_text(advice)
		advice = self._advise("Full Table Scan", "NOT (`status`=? OR `status`=?)")
		assert "a NOT (...) around status" in advice.reason
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
		# fix round 4: Optimus cannot say an index would not help when it could not read a filter
		assert "an index would not help" not in advice.reason
		assert advice.reason.endswith(
			"So Optimus gives no index code. Check the query with EXPLAIN to see which index it needs."
		)


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
			assert "Optimus left out company, party (compared only inside an OR between conditions)" in ir.finding_text(advice)

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
			("(`po_no` IS NULL OR `po_no`=?) AND `customer`=?", ("customer", "po_no")),  # canonical order
			("(`status`=? OR `status`=?) AND `company`=?", ("company", "status")),
			("(`status` IN (?) OR `status` IS NULL) AND `company`=?", ("company", "status")),
			("(`status`=? /* a comment */ OR `status`=?) AND `company`=?", ("company", "status")),
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
			# bounded corrective R1: the rare value may still use an index, so the text says so
			assert "is_return is a Check field, which usually matches most of the table's rows" in advice.reason
			assert "an index on (is_return) can help" in advice.reason and "would not help" not in advice.reason


_ROUND4_FIELDS = {
	**_ALL, "grand_total": F("Int"), "outstanding_amount": F("Int"), "due_date": F("Date"),
	"is_return": F("Check"), "customer_name": F("Data"), "meta": F("JSON"), "order": F("Data"),
	"territory": F("Link"),
}
_SII = _ev("Sales Invoice Item", fields={
	"item_code": F("Link"), "qty": F("Int"), "delivered_qty": F("Int"), "against_sales_order": F("Link"),
})
_R4 = _lookup(_ev(fields=_ROUND4_FIELDS, extra_types={"modified": "datetime"}), _SII)


def _r4(ftype, where, *, table="tabSales Invoice"):
	q = f"SELECT `name` FROM `{table}` WHERE {where}"
	return ir.advise_finding(_explain(ftype, q, table=table), evidence_lookup=_R4)


class TestTopLevelSameColumnOr:
	"""Fix round 4 (A): Frappe writes a lone "is not set" filter as a bare top-level OR."""

	def test_a_top_level_or_on_one_column_is_a_plain_use(self):
		where = "`po_no` IS NULL OR `po_no`=? ORDER BY `posting_date` DESC"
		assert _r4("Full Table Scan", where).columns == ("po_no",)
		advice = _r4("Filesort", where)
		# po_no matches two values (NULL and ?), so the rows do not come back sorted and the sort
		# column would only widen the index (review-t12 M8)
		assert advice.columns == ("po_no",)
		text = ir.finding_text(advice)
		assert "already sorted" not in text and "removes the sort" not in text
		assert "The filter on po_no matches more than one value, so this index cannot return the rows in order" in text
		assert "Optimus left out posting_date (an index cannot return these rows in order" in text

	def test_other_top_level_ors_still_count_as_or(self):
		labelled = [("WHERE", "status"), ("WHERE", "customer"), ("WHERE", "company")]
		for where in ("`status`=? OR `customer`=?", "`status`=? AND `company`=? OR `status`=?", "`status`=? OR `status` LIKE ?"):
			shapes = ir._unusable_where_columns(f"SELECT `name` FROM `tabSales Invoice` WHERE {where}", labelled)
			assert shapes["status"] == {"or"}, where


class TestSlowQueryCut:
	"""Fix round 4 (B): a Slow Query keeps only its first 500 characters (top_queries), so its
	WHERE clause counts only when the clause reached its end keyword."""

	@staticmethod
	def _cut(tail: str, at: str, offset: int) -> str:
		def query(pad):
			return f"select `name` as `{'x' * pad}` {tail}"
		pad = ir.QUERY_TEXT_LIMIT - query(0).index(at) - offset
		cut = query(pad)[: ir.QUERY_TEXT_LIMIT]
		assert len(cut) == ir.QUERY_TEXT_LIMIT
		return cut

	def _advise(self, cut):
		finding = {"finding_type": "Slow Query", "technical_detail": {"normalized_query": cut}}
		return ir.advise_finding(finding, evidence_lookup=_R4)

	def test_a_cut_inside_the_order_by_keeps_the_whole_where_clause(self):
		tail = (
			"FROM `tabSales Invoice` WHERE `tabSales Invoice`.`company`=? AND `tabSales Invoice`.`customer`=? "
			"ORDER BY `tabSales Invoice`.`posting_date` DESC LIMIT ?"
		)
		advice = self._advise(self._cut(tail, "ORDER BY `tabSales Invoice`", 15))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.columns == ("company", "customer")

	def test_a_cut_inside_a_column_name_gives_honest_no_code(self):
		tail = "from `tabSales Invoice` where company = ? and customer_name = ? order by posting_date desc limit ?"
		advice = self._advise(self._cut(tail, "customer_name", 8))
		assert advice.route == ir.ROUTE_NO_CODE
		text = ir.finding_text(advice, install=False)
		assert text.startswith("Optimus could not read how this query combines its filters")
		assert "has no column" not in text

	def test_a_cut_just_before_a_top_level_or_gives_honest_no_code(self):
		tail = "from `tabSales Invoice` where company = ? and customer = ? or status = ? order by posting_date"
		advice = self._advise(self._cut(tail, "or status", 0))
		assert advice.route == ir.ROUTE_NO_CODE
		assert ir.finding_text(advice, install=False).startswith("Optimus could not read")

	def test_a_short_slow_query_needs_no_end_keyword(self):
		finding = {"finding_type": "Slow Query", "technical_detail": {
			"normalized_query": "SELECT `name` FROM `tabSales Invoice` WHERE `company`=? AND `customer`=?",
		}}
		assert ir.advise_finding(finding, evidence_lookup=_R4).columns == ("company", "customer")


class TestExpressions:
	"""Fix round 4 (C): arithmetic on a column, or a comparison with another column of the
	same row, cannot use an index on it."""

	def test_arithmetic_on_a_column_is_left_out(self):
		advice = _r4("Full Table Scan", "`grand_total` - `outstanding_amount` > ? AND `company`=?")
		assert advice.columns == ("company",)
		assert (
			"Optimus left out grand_total, outstanding_amount (compared with another column or through arithmetic, "
			"which an index on the column cannot use)" in ir.finding_text(advice)
		)
		advice = _r4("Full Table Scan", "`grand_total` - `outstanding_amount` > ?")
		assert advice.route == ir.ROUTE_NO_CODE and "arithmetic on grand_total" in advice.reason

	def test_a_column_compared_with_another_column_is_left_out(self):
		advice = _r4("Full Table Scan", "`qty` > `delivered_qty` AND `item_code`=?", table="tabSales Invoice Item")
		assert advice.columns == ("item_code",)
		assert "Optimus left out qty, delivered_qty (compared with another column or through arithmetic" in ir.finding_text(advice)

	def test_joins_value_arithmetic_and_literal_words_stay_usable(self):
		q = (
			"select si.name from `tabSales Invoice` si, `tabSales Invoice Item` sii "
			"where sii.against_sales_order = si.name and sii.item_code = ?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabSales Invoice Item"), evidence_lookup=_R4)
		assert advice.columns == ("against_sales_order", "item_code")
		assert _r4("Full Table Scan", "`posting_date` > ? - INTERVAL ? DAY AND `company`=?").columns == ("company", "posting_date")
		assert _r4("Full Table Scan", "`posting_date` <= CURRENT_DATE AND `company`=?").columns == ("company", "posting_date")
		assert _r4("Full Table Scan", "`posting_date` <= CURDATE() AND `company`=?").columns == ("company", "posting_date")
		assert _r4("Full Table Scan", "`status` = NULL AND `company`=?").columns == ("company", "status")


class TestCaseFrame:
	"""Fix round 4 (D): a column inside CASE ... END cannot use an index on it."""

	def test_every_case_form_is_a_frame(self):
		for where, cols, left_out in (
			("`company`=? AND (CASE WHEN `customer`=? THEN ? ELSE ? END)=?", ("company",), "customer"),
			("`company`=? AND CASE WHEN `status`=? AND `customer`=? THEN ? ELSE ? END = ?", ("company",), "status"),
			("`company`=? AND `customer`=? AND CASE WHEN `status`=? OR `po_no`=? THEN ? ELSE ? END = ?", ("company", "customer"), "status"),
			("`company`=? AND CASE `status` WHEN ? THEN ? ELSE ? END = ?", ("company",), "status"),
			("`company`=? AND CASE WHEN `status`=? THEN CASE WHEN `customer`=? THEN ? END END = ?", ("company",), "customer"),
		):
			advice = _r4("Full Table Scan", where)
			assert advice.columns == cols, where
			text = ir.finding_text(advice)
			assert "Optimus left out " in text, where
			assert left_out in text.split("Optimus left out ", 1)[1].split(" (compared inside a CASE", 1)[0], where
			assert "(compared inside a CASE expression, which an index on the column cannot use)" in text, where

	def test_a_value_side_case_leaves_the_column_plain(self):
		assert _r4("Full Table Scan", "`company`=? AND `status` = CASE WHEN ? THEN ? ELSE ? END").columns == ("company", "status")
		where = "`company`=? AND CASE WHEN `status`=? THEN CASE WHEN `customer`=? THEN ? END END = `posting_date`"
		assert _r4("Full Table Scan", where).columns == ("company", "posting_date")

	def test_a_lone_case_filter_gives_no_code(self):
		advice = _r4("Full Table Scan", "CASE WHEN `status`=? THEN ? ELSE ? END = ?")
		assert advice.route == ir.ROUTE_NO_CODE and "a CASE expression around status" in advice.reason


class TestIndexDesign:
	"""Fix round 4: equality columns first, then at most one range or <> column, then the
	sort or group column; a column after a range column cannot use the index."""

	def test_equality_columns_lead(self):
		assert _r4("Full Table Scan", "`posting_date` BETWEEN ? AND ? AND `company`=?").columns == ("company", "posting_date")
		assert _r4("Full Table Scan", "`po_no` IS NOT NULL AND `company`=?").columns == ("company", "po_no")
		# the equality block (=, IS NULL, IN) is in one canonical order, by name (review-t12 item 1)
		assert _r4("Full Table Scan", "`po_no` IS NULL AND `company`=?").columns == ("company", "po_no")
		assert _r4("Full Table Scan", "`status` IN (?) AND `company`=?").columns == ("company", "status")
		assert _r4("Full Table Scan", "`posting_date` > ? AND ? = `company`").columns == ("company", "posting_date")
		item = _lookup(_ev("Item", fields={"disabled": F("Check"), "item_group": F("Link")}))
		q = "SELECT `name` FROM `tabItem` WHERE `disabled`<>? AND `item_group`=?"
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabItem"), evidence_lookup=item)
		# <> never narrows an index (review-t12 round 3, item 1a)
		assert advice.columns == ("item_group",)
		assert "Optimus left out disabled (compared only by !=, <> or NOT" in ir.finding_text(advice)

	def test_a_join_column_counts_as_equality(self):
		q = (
			"select sii.name from `tabSales Invoice Item` sii join `tabSales Invoice` si "
			"on si.name = sii.against_sales_order where sii.qty > ?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabSales Invoice Item"), evidence_lookup=_R4)
		assert advice.columns == ("against_sales_order", "qty")

	def test_only_one_range_column_is_kept(self):
		advice = _r4("Full Table Scan", "`posting_date` > ? AND `due_date` < ? AND `company`=?")
		assert advice.columns == ("company", "posting_date")
		assert "Optimus left out due_date (it comes after the range condition on posting_date" in ir.finding_text(advice)



class TestRangeAndSort:
	"""Fix round 5: a Filesort or Temporary Table finding is about the sort or the
	grouping, so with a range filter on another column the index takes the equality
	columns and the sort or group column, and the range filter is left out. A range on
	the sort column itself is one index for both. A Full Table Scan keeps the range."""

	_WHERE = "`company`=? AND `posting_date` BETWEEN ? AND ?"

	def test_a_filesort_with_a_range_filter_indexes_the_sort(self):
		advice = _r4("Filesort", f"{self._WHERE} ORDER BY `modified` DESC")
		assert advice.columns == ("company", "modified")
		text = ir.finding_text(advice)
		assert text.startswith("Index the filter columns followed by the sort column")
		assert (
			"Optimus left out posting_date (the range filter on posting_date cannot also use this index, which "
			"removes the sort instead)" in text
		)

	def test_a_range_on_the_sort_column_is_one_index_for_both(self):
		advice = _r4("Filesort", f"{self._WHERE} ORDER BY `posting_date` DESC")
		assert advice.columns == ("company", "posting_date")
		text = ir.finding_text(advice)
		assert text.startswith("Index the filter columns followed by the sort column") and "left out" not in text

	def test_a_sort_column_the_index_cannot_hold_keeps_the_range(self):
		"""ORDER BY idx (Frappe metadata) gives no sort column, so the range filter stays."""
		advice = _r4("Filesort", f"{self._WHERE} ORDER BY `idx`")
		assert advice.columns == ("company", "posting_date")
		assert "The sort column is a Frappe metadata column, which Optimus never indexes" in ir.finding_text(advice)

	def test_a_metadata_sort_column_never_leads_the_index(self):
		"""creation may only trail a business column; with no equality column before it the
		range filter stays and the sort is left to Frappe's own creation index."""
		advice = _r4("Filesort", "`po_no` <> ? ORDER BY `creation` DESC")
		assert advice.columns == ("po_no",)
		advice = _r4("Filesort", "`po_no` <> ? AND `company`=? ORDER BY `creation` DESC")
		assert advice.columns == ("company", "creation")

	def test_a_full_table_scan_keeps_the_range_column(self):
		assert _r4("Full Table Scan", self._WHERE).columns == ("company", "posting_date")
		# an index that serves no sort keeps the range column and leaves the sort column out
		order = {"company": "eq", "posting_date": "range", "modified": "sort"}
		cols, dropped = ir._index_order(["company", "posting_date", "modified"], order)
		assert cols == ["company", "posting_date"]
		assert dropped == [("modified", "it comes after the range condition on posting_date, so the index cannot use it")]

	def test_a_temporary_table_with_a_range_filter_indexes_the_group(self):
		advice = _r4("Temporary Table", "`company`=? AND `posting_date` > ? GROUP BY `customer`")
		assert advice.columns == ("company", "customer")
		assert (
			"Optimus left out posting_date (the range filter on posting_date cannot also use this index, which "
			"removes the temporary table instead)" in ir.finding_text(advice)
		)

	def test_an_equality_on_the_sort_column_stays_first(self):
		advice = _r4("Filesort", "`customer`=? AND `posting_date` > ? ORDER BY `customer`, `modified`")
		assert advice.columns == ("customer", "modified")
		assert _r4("Filesort", "`customer`=? AND `company`=? ORDER BY `customer`").columns == ("company", "customer")


class TestSortGate:
	"""Bounded corrective to round 5: the sort-over-range recipe applies only when every
	ORDER BY / GROUP BY item is a bare column of the table, indexable and sortable, in one
	direction, with no multi-value equality filter; otherwise the round-4 recipe stays
	(equality columns, then one range column) and the text never claims the sort goes."""

	@staticmethod
	def _no_sort_claim(advice):
		text = ir.finding_text(advice)
		assert "removes the sort" not in text and "removes the temporary table" not in text, text
		assert "already sorted" not in text and "instead of a temporary table" not in text, text
		return text

	def test_an_expression_sort_keeps_the_range(self):
		advice = _r4("Filesort", "`due_date` < ? ORDER BY FIELD(`status`, ?, ?)")
		assert advice.columns == ("due_date",)
		assert "The query sorts by an expression" in self._no_sort_claim(advice)
		advice = _r4("Filesort", "`company`=? AND `due_date` < ? ORDER BY IFNULL(`grand_total`, ?) DESC")
		assert advice.columns == ("company", "due_date")
		self._no_sort_claim(advice)
		advice = _r4("Filesort", "`posting_date` > ? ORDER BY `grand_total` + `outstanding_amount`")
		assert advice.columns == ("posting_date",)
		self._no_sort_claim(advice)

	def test_an_expression_sort_without_a_range_never_claims_the_sort_goes(self):
		advice = _r4("Filesort", "`company`=? ORDER BY FIELD(`status`, ?, ?)")
		assert "The query sorts by an expression" in self._no_sort_claim(advice)

	def test_an_expression_grouping_keeps_the_range(self):
		advice = _r4("Temporary Table", "`due_date` < ? GROUP BY DATE(`posting_date`)")
		assert advice.columns == ("due_date",)
		self._no_sort_claim(advice)
		q = (
			"SELECT DATE(`creation`) AS d, COUNT(*) FROM `tabSales Invoice` WHERE `company`=? "
			"AND `posting_date` BETWEEN ? AND ? GROUP BY DATE(`creation`)"
		)
		advice = ir.advise_finding(_explain("Temporary Table", q), evidence_lookup=_R4)
		assert advice.columns == ("company", "posting_date")
		self._no_sort_claim(advice)

	def test_a_text_sort_column_keeps_the_range(self):
		advice = _r4("Filesort", "`posting_date` > ? ORDER BY `remarks`")
		assert advice.columns == ("posting_date",)
		assert "a text column" in self._no_sort_claim(advice)
		pg = _lookup(_ev(dialect="postgres", fields=_ROUND4_FIELDS, extra_types={"modified": "timestamp"}))
		q = "SELECT `name` FROM `tabSales Invoice` WHERE `company`=? AND `posting_date` > ? ORDER BY `remarks`"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=pg)
		assert advice.columns == ("company", "posting_date")
		self._no_sort_claim(advice)

	def test_a_select_alias_sort_keeps_the_range(self):
		q = (
			"select name, grand_total - outstanding_amount as paid from `tabSales Invoice` "
			"where company = ? and posting_date > ? order by paid desc"
		)
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_R4)
		assert advice.columns == ("company", "posting_date")
		self._no_sort_claim(advice)
		# an implicit alias (no AS) is no column of the table either
		q = (
			"select name, grand_total - outstanding_amount paid from `tabSales Invoice` "
			"where company = ? and posting_date > ? order by paid desc"
		)
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_R4)
		assert advice.columns == ("company", "posting_date")
		self._no_sort_claim(advice)
		# an alias that shadows a real column: ORDER BY names the alias, not the column
		q = (
			"select name, grand_total - outstanding_amount as customer from `tabSales Invoice` "
			"where company = ? and posting_date > ? order by customer desc"
		)
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_R4)
		assert "customer" not in advice.columns
		self._no_sort_claim(advice)

	def test_a_sort_column_the_recipe_cannot_hold_keeps_the_range(self):
		for sort in ("`is_return`", "`meta`", "`order`"):
			advice = _r4("Filesort", f"`posting_date` > ? ORDER BY {sort}")
			assert advice.columns == ("posting_date",), sort
			self._no_sort_claim(advice)
		assert "The query sorts by an expression" in ir.finding_text(_r4("Filesort", "`posting_date` > ? ORDER BY `meta`"))

	def test_an_order_by_only_inside_a_subquery_keeps_the_range(self):
		q = (
			"SELECT `name` FROM `tabSales Invoice` WHERE `company`=? AND `due_date` > ? AND EXISTS "
			"(SELECT `name` FROM `tabSales Invoice` ORDER BY `customer`)"
		)
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_R4)
		assert "due_date" in advice.columns and "customer" not in advice.columns
		self._no_sort_claim(advice)

	def test_no_evidence_gives_no_code_for_a_sort_finding(self):
		q = "SELECT `name` FROM `tabSales Invoice` WHERE `company`=? AND `posting_date` > ? ORDER BY `customer`"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=lambda table: None)
		assert advice.route == ir.ROUTE_NO_CODE

	def test_mixed_directions_and_a_different_order_keep_the_range(self):
		advice = _r4("Filesort", "`company`=? AND `posting_date` > ? ORDER BY `customer` ASC, `po_no` DESC")
		assert advice.columns == ("company", "posting_date")
		self._no_sort_claim(advice)
		advice = _r4("Temporary Table", "`company`=? AND `posting_date` > ? GROUP BY `customer` ORDER BY SUM(`grand_total`) DESC")
		assert advice.columns == ("company", "posting_date")
		self._no_sort_claim(advice)

	def test_the_column_cap_never_contradicts_the_text(self):
		where = "`company`=? AND `customer`=? AND `status`=? AND `territory`=? AND `posting_date` > ? ORDER BY `po_no`"
		advice = _r4("Filesort", where)
		assert advice.columns == ("company", "customer", "status", "territory")
		text = self._no_sort_claim(advice)
		assert "The sort column comes after the range condition on posting_date" in text

	def test_a_bare_qualified_sort_still_wins_over_the_range(self):
		q = "select si.name from `tabSales Invoice` si where si.company = ? and si.posting_date > ? order by si.customer desc"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_R4)
		assert advice.columns == ("company", "customer")
		assert "which removes the sort instead" in ir.finding_text(advice)


class TestMultiValueEquality:
	"""Bounded corrective: IN (...) and a same-column IS NULL OR = keep their equality
	place, but rows matching several values do not come back sorted."""

	def test_an_in_filter_keeps_the_range_and_the_sort_claim_goes(self):
		# a collapsed IN (?) may be one value: the sort is kept and the lead hedges (round 3)
		advice = _r4("Filesort", "`status` IN (?) AND `posting_date` > ? ORDER BY `modified`")
		assert advice.columns == ("status", "modified")
		assert "if the IN list on status has more than one value, the sort stays" in advice.lead
		advice = _r4("Filesort", "`status` IN (?, ?) AND `posting_date` > ? ORDER BY `modified`")
		assert advice.columns == ("status", "posting_date")
		text = ir.finding_text(advice)
		assert "removes the sort" not in text and "already sorted" not in text
		advice = _r4("Filesort", "(`po_no` IS NULL OR `po_no`=?) AND `posting_date` > ? ORDER BY `modified` DESC")
		assert advice.columns == ("po_no", "posting_date")
		advice = _r4("Filesort", "`po_no` IS NULL OR `po_no`=? ORDER BY `creation` DESC")
		assert advice.columns == ("po_no",)  # the sort column cannot be served (review-t12 M8)
		assert "already sorted" not in ir.finding_text(advice)


def test_the_most_selective_plain_use_wins():
	q = "SELECT `name` FROM `tabSales Invoice` WHERE `status` = ? AND `status` IN (?) AND `company` > ?"
	labelled = [("WHERE", "status"), ("WHERE", "company")]
	assert ir._scan_where(q, labelled)[1] == {"status": "eq", "company": "range"}


class TestMetadataNeverLeads:
	"""Bounded corrective: creation / modified never lead, after index ordering too."""

	def test_creation_equality_never_leads(self):
		advice = _r4("Full Table Scan", "`company` > ? AND `creation` = ?")
		assert advice.columns and advice.columns[0] not in ("creation", "modified")
		advice = _r4("Filesort", "`po_no` <> ? AND `creation` = ? ORDER BY `modified`")
		assert advice.columns and advice.columns[0] not in ("creation", "modified")
		assert advice.columns == ("po_no",)


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
		# a card keeps the analyzer's most-used-first order (review-t12 M6)
		advice = ir.advise_table("tabSales Invoice", ["long_code", "customer"], evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE
		assert "could be 2800 bytes wide, over the 2704-byte Postgres index row limit" in ir.card_note(advice)
		advice = ir.advise_table("tabSales Invoice", ["customer", "long_code"], evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.entry["columns"] == ["customer"]
		assert "Optimus left out long_code (the index would pass the 2704-byte Postgres index row limit)" in (
			ir.card_note(advice)
		)

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
		assert "The query sorts by an aggregate, which no index can return in order" in ir.finding_text(advice)

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

	def test_every_commit_sits_inside_a_try_statement(self):
		"""The pinned Frappe Semgrep rule frappe-manual-commit allows a commit only inside a
		try/except statement; this mirrors it where semgrep is not installed."""
		import ast

		tree = ast.parse(ir.ensure_indexes_code([{"doctype": "X", "search_index_field": "a", "db": "mariadb"}]))
		parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
		commits = [
			node for node in ast.walk(tree)
			if isinstance(node, ast.Call) and ast.unparse(node.func) == "frappe.db.commit"
		]
		assert len(commits) >= 3
		for node in commits:
			while node in parents and not isinstance(node, ast.Try):
				node = parents[node]
			assert isinstance(node, ast.Try) and node.handlers

	def test_a_hook_set_as_a_string_becomes_a_list_with_ensure_indexes_last(self):
		"""D2: most apps set after_install or after_migrate as a string; pasting a list
		under it replaces it (or is replaced). The comment and the install text show the
		two-item form, the existing string first."""
		code = ir.ensure_indexes_code([{"doctype": "X", "search_index_field": "a"}], app_name="myapp")
		pair = '["<the string already there>", "myapp.optimus_indexes.ensure_indexes"]'
		assert f"#   after_migrate = {pair}" in code.splitlines()
		assert "as the last item of each list" in code
		advice = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(_SI), tracked_apps=("myapp",))
		for text in (ir.finding_text(advice), ir.card_note(advice)):
			assert 'as the last item of the after_install, after_sync and after_migrate lists' in text
			assert f"after_migrate = {pair}" in text


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


def test_the_docs_show_the_generated_module_verbatim():
	"""docs/AI-FIXING.md section 2.3 shows the module the real advisor generates: a plain
	MariaDB composite without a db stamp and a Property Setter entry with one."""
	from pathlib import Path

	composite = ir.advise_finding(_explain("Full Table Scan", _TWO), evidence_lookup=_lookup(_SI))
	single = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(_SI))
	entries = [composite.entry, single.entry]
	assert "db" not in composite.entry and single.entry["db"] == "mariadb"
	doc = (Path(ir.__file__).resolve().parents[2] / "docs" / "AI-FIXING.md").read_text(encoding="utf-8")
	assert ir.ensure_indexes_code(entries, app_name="your_app") in doc


def test_the_docs_say_how_to_remove_an_entry_and_that_the_guards_are_silent():
	"""D6, O3d: removing an index or entry (the idx_* index and the
	"<DocType>-<field>-search_index" Property Setter), and the guards that skip
	without an Error Log row."""
	from pathlib import Path

	doc = (Path(ir.__file__).resolve().parents[2] / "docs" / "AI-FIXING.md").read_text(encoding="utf-8")
	assert '"Sales Invoice-po_no-search_index"' in doc and "bench remove-app" in doc
	assert "skips it without an Error Log row" in doc


def test_the_docs_show_the_title_order_and_limit_the_indexed_lookup_to_mariadb():
	"""T11 fix round 1: the title example has the key and the error type before the
	DocType; the "never reads the whole Error Log" claim is MariaDB's, because Postgres
	names a Search Index after the bare field, schema-wide."""
	from pathlib import Path

	root = Path(ir.__file__).resolve().parents[2]
	doc = " ".join((root / "docs" / "AI-FIXING.md").read_text(encoding="utf-8").split())
	log = " ".join((root / "CHANGELOG.md").read_text(encoding="utf-8").split())
	assert "`ensure_indexes: idx_sales_invoice_04c198b9 was not created (OperationalError) on Sales Invoice`" in doc
	assert "so on MariaDB it never reads the whole Error Log table on every migrate" in doc
	assert "Frappe names a Search Index after the bare field name and index names are schema-wide" in doc
	assert "which MariaDB indexes" in log and "schema-wide" in log
