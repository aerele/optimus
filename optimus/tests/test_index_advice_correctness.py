# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Cycle-2 corrections to the index advisor (T12): which existing-index checks apply to
which recipe (C1, C6), Search Index is no proof of an index (C2), JSON fields (C4), the
lead's named cause (C7), reserved words on Postgres (E3), tables Optimus knows nothing
about (E6), UNION queries (E7), the empty Tracked Apps wording (E1), the existing-index
tail text (U4), the card verdicts (U2) and the branches T3 found untested."""

import dataclasses

import pytest

from optimus.analyzers.base import QUERY_TEXT_LIMIT
from optimus.renderer import index_recipes as ir
from optimus.renderer.recipe_enrichment import IndexEvidence, TableEvidence
from optimus.tests.test_index_recipes import _ALL, _SI, F, _ev, _explain, _lookup, _missing

_GENERIC_HEDGE = "a LIKE pattern that starts with a wildcard, a function or IFNULL wrapped around the column"


def _with_index(ev, *indexes):
	"""``ev`` plus ``(name, columns, unique)`` indexes."""
	return dataclasses.replace(
		ev, indexes=tuple(ev.indexes) + tuple(IndexEvidence(n, tuple(c), u) for n, c, u in indexes),
	)


def _text(advice):
	return ir.finding_text(advice)


# --- C1: the lead-column checks apply only to a single-column recipe ----------------------


class TestLeadChecksOnlyForOneColumn:
	_SI_IX = _with_index(
		_ev(fields={**_ALL, "customer": F("Link", search_index=True), "posting_date": F("Date", search_index=True)}),
		("customer_index", ["customer"], False), ("posting_date_index", ["posting_date"], False),
	)

	def _advise(self, ftype, query, explain_row=None, ev=None):
		return ir.advise_finding(_explain(ftype, query, explain_row=explain_row), evidence_lookup=_lookup(ev or self._SI_IX))

	def test_a_list_view_sort_on_an_indexed_filter_gets_its_composite(self):
		"""c2corr/p1_lead_indexed q1: customer leads customer_index and EXPLAIN uses it, yet
		(customer, creation) is what removes the filesort."""
		q = (
			"SELECT `tabSales Invoice`.`name` FROM `tabSales Invoice` WHERE `tabSales Invoice`.`customer` = ? "
			"AND `tabSales Invoice`.`docstatus` = ? ORDER BY `tabSales Invoice`.`creation` DESC LIMIT ?"
		)
		row = {"key": "customer_index", "possible_keys": "customer_index", "Extra": "Using where; Using filesort"}
		advice = self._advise("Filesort", q, row)
		assert advice.route == ir.ROUTE_ENSURE_INDEXES, _text(advice)
		assert advice.entry["columns"] == ["customer", "creation"]

	@pytest.mark.parametrize("ftype,query,columns", [
		("Low Filter Ratio", "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ?", ["customer", "status"]),
		(
			"Temporary Table", "SELECT customer, sum(po_no) FROM `tabSales Invoice` WHERE customer = ? GROUP BY status",
			["customer", "status"],
		),
		(
			"Filesort", "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND company = ? ORDER BY posting_date",
			["company", "customer", "posting_date"],
		),
	])
	def test_common_composite_shapes_are_advised(self, ftype, query, columns):
		advice = self._advise(ftype, query, {"key": "customer_index"})
		assert advice.route == ir.ROUTE_ENSURE_INDEXES, _text(advice)
		assert advice.entry["columns"] == columns

	@pytest.mark.parametrize("ftype", ["Full Table Scan", "Low Filter Ratio", "Slow Query"])
	@pytest.mark.parametrize("existing,route", [
		((), ir.ROUTE_ENSURE_INDEXES),
		((("status_index", ["status"], False),), ir.ROUTE_ENSURE_INDEXES),
		((("idx_status_customer", ["status", "customer"], False),), ir.ROUTE_NO_CODE),
		((("idx_customer_status", ["customer", "status"], False),), ir.ROUTE_NO_CODE),
	])
	def test_the_verdict_does_not_depend_on_predicate_order(self, ftype, existing, route):
		"""c2corr/p1c and review-t12 item 1: both predicate orders give one verdict, one
		column order and one index name, with or without an existing composite in either
		order."""
		ev = _with_index(_ev(fields={**_ALL, "status": F("Select", search_index=True)}), *existing)
		advices = [
			self._advise(ftype, q, {"key": None, "possible_keys": "status_index"}, ev)
			for q in (
				"SELECT name FROM `tabSales Invoice` WHERE status = ? AND customer = ?",
				"SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ?",
			)
		]
		assert {a.route for a in advices} == {route}, [_text(a) for a in advices]
		assert advices[0].columns == advices[1].columns
		assert advices[0].entry == advices[1].entry

	def test_an_index_that_starts_with_the_whole_recipe_still_gives_no_code(self):
		ev = _with_index(_SI, ("idx_cust_status_date", ["customer", "status", "posting_date"], False))
		advice = self._advise("Full Table Scan", "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ?", ev=ev)
		assert advice.route == ir.ROUTE_NO_CODE
		assert 'The index "idx_cust_status_date" on table "tabSales Invoice" already starts with (customer, status)' in _text(advice)

	def test_a_single_column_recipe_keeps_the_lead_checks(self):
		advice = ir.advise_finding(_missing("customer"), evidence_lookup=_lookup(self._SI_IX))
		assert advice.route == ir.ROUTE_NO_CODE
		assert 'Column "customer" already leads the index "customer_index"' in _text(advice)
		row = {"type": "ALL", "possible_keys": "customer_index", "key": None}
		advice = self._advise("Full Table Scan", "SELECT name FROM `tabSales Invoice` WHERE customer = ?", row, ev=_SI)
		assert advice.route == ir.ROUTE_NO_CODE
		assert 'EXPLAIN shows the database can use the index "customer_index" on column "customer"' in _text(advice)

	def test_a_composite_that_leads_with_the_column_blocks_only_a_single_column_recipe(self):
		ev = _with_index(_SI, ("idx_si_customer_custom", ["customer", "company"], False))
		single = ir.advise_finding(_missing("customer"), evidence_lookup=_lookup(ev))
		assert single.route == ir.ROUTE_NO_CODE and 'already leads the index "idx_si_customer_custom"' in _text(single)
		pair = self._advise("Full Table Scan", "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ?", ev=ev)
		assert pair.route == ir.ROUTE_ENSURE_INDEXES and pair.entry["columns"] == ["customer", "status"]

	def test_the_final_recipe_decides_a_dropped_column_makes_it_single(self):
		"""The lead checks follow the FINAL recipe: a JSON second column is left out, so the
		recipe is one column and its own index gives no code."""
		ev = _with_index(_ev(fields={**_ALL, "payload": F("JSON")}), ("customer_index", ["customer"], False))
		advice = self._advise("Full Table Scan", "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND payload = ?", ev=ev)
		assert advice.route == ir.ROUTE_NO_CODE and 'already leads the index "customer_index"' in _text(advice)


# --- C2: Search Index is not proof that an index exists -------------------------------------


class TestSearchIndexIsNoProof:
	def _pg_queue(self):
		"""c2corr/p4: live optimus-pg.local, status has Search Index but no index (the bare
		name "status" belongs to tabPrepared Report)."""
		return TableEvidence(
			table="tabEmail Queue Recipient", doctype="Email Queue Recipient", app="frappe", is_custom_doctype=False,
			dialect="postgres", fields={"recipient": F("Data"), "status": F("Select", search_index=True)},
			column_types={"name": "character varying", "recipient": "character varying", "status": "character varying"},
			text_columns=frozenset(), unindexable_columns=frozenset(),
			indexes=(IndexEvidence("tabEmail Queue Recipient_pkey", ("name",), True),),
		)

	def test_postgres_search_index_without_an_index_gets_code(self):
		lookup = _lookup(self._pg_queue())
		advice = ir.advise_finding(_missing("status", table="tabEmail Queue Recipient"), evidence_lookup=lookup)
		assert advice.route == ir.ROUTE_ENSURE_INDEXES, _text(advice)
		assert advice.entry["columns"] == ["status"] and advice.entry["db"] == "postgres"
		card = ir.advise_table("tabEmail Queue Recipient", ["status"], evidence_lookup=lookup)
		assert card.route == ir.ROUTE_ENSURE_INDEXES

	@pytest.mark.parametrize("dialect", ["mariadb", "postgres"])
	def test_search_index_alone_never_gives_no_code(self, dialect):
		ev = _ev(dialect=dialect, fields={**_ALL, "customer": F("Link", search_index=True)})
		advice = ir.advise_finding(_missing("customer"), evidence_lookup=_lookup(ev))
		assert advice.route != ir.ROUTE_NO_CODE, _text(advice)
		assert "Search Index ticked" not in _text(advice) + ir.card_note(advice)

	def test_a_real_index_still_gives_no_code(self):
		ev = _with_index(_ev(dialect="postgres", fields={**_ALL, "customer": F("Link", search_index=True)}), ("customer", ["customer"], False))
		advice = ir.advise_finding(_missing("customer"), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE and 'already leads the index "customer"' in _text(advice)


# --- C4: a JSON field is unindexable whatever information_schema calls it ------------------


class TestJsonFields:
	def _ev(self):
		# MariaDB reports a JSON column as longtext in information_schema
		return _ev(fields={**_ALL, "meta": F("JSON")}, extra_types={"meta": "longtext"})

	def test_a_leading_json_field_reported_as_longtext_gives_no_code(self):
		ev = self._ev()
		assert "meta" in ev.text_columns and "meta" not in ev.unindexable_columns
		advice = ir.advise_finding(_missing("meta"), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE and advice.code is None
		assert 'Column "meta" is a JSON field, which a plain index cannot cover' in _text(advice)

	def test_a_later_json_field_is_left_out_never_prefixed(self):
		advice = ir.advise_table("tabSales Invoice", ["customer", "meta"], evidence_lookup=_lookup(self._ev()))
		assert advice.columns == ("customer",)
		assert "meta(255)" not in (advice.code or "")
		assert "Optimus left out meta (a type a plain index cannot cover)" in ir.card_note(advice)


# --- C6: "already unique" only for a single-column unique index -----------------------------


class TestUnique:
	def test_a_composite_unique_lead_is_an_index_lead_not_unique(self):
		ev = _with_index(_SI, ("uniq_po_company", ["po_no", "company"], True))
		advice = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE
		assert "already unique" not in _text(advice)
		assert 'Column "po_no" already leads the index "uniq_po_company"' in _text(advice)

	def test_a_single_column_unique_index_is_unique(self):
		ev = _with_index(_SI, ("po_no", ["po_no"], True))
		advice = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(ev))
		assert 'Column "po_no" is already unique' in _text(advice)

	def test_a_unique_range_column_does_not_block_a_composite(self):
		"""review-t12 item 2: a unique EQUALITY column refuses the composite, a range or sort
		column never does."""
		ev = _ev(fields={**_ALL, "status": F("Select", unique=True)})
		advice = ir.advise_finding(
			_explain("Full Table Scan", "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status > ?"),
			evidence_lookup=_lookup(ev),
		)
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.entry["columns"] == ["customer", "status"]


# --- C7: the lead names the right cause ---------------------------------------------------


_C7 = _ev(fields={**_ALL, "grand_total": F("Int")})


def _c7(ftype, query):
	return ir.advise_finding(_explain(ftype, query), evidence_lookup=_lookup(_C7))


class TestLeadCause:
	def test_a_filesort_on_an_aggregate_names_the_aggregate(self):
		for order in ("total DESC", "sum(grand_total) DESC"):
			q = (
				"SELECT customer, sum(grand_total) AS total FROM `tabSales Invoice` WHERE company = ? "
				f"GROUP BY customer ORDER BY {order}"
			)
			lead = _c7("Filesort", q).lead
			assert ir._NARROWS + "the query sorts by an aggregate, which no index can return in order, so the sort stays." in lead, lead
			assert "metadata" not in lead

	def test_a_grouping_sorted_by_an_aggregate_names_the_aggregate(self):
		for order in ("total DESC", "SUM(grand_total) DESC"):
			q = (
				"SELECT customer, sum(grand_total) AS total FROM `tabSales Invoice` WHERE company = ? "
				f"GROUP BY customer ORDER BY {order}"
			)
			lead = _c7("Temporary Table", q).lead
			assert ir._NARROWS + "the query sorts its groups by an aggregate" in lead and "so the temporary table stays" in lead, lead
			assert "groups by an expression" not in lead

	def test_an_implicit_aggregate_alias_counts_as_an_aggregate(self):
		q = (
			"SELECT customer, sum(grand_total) total, count(name) FROM `tabSales Invoice` WHERE company = ? "
			"GROUP BY customer ORDER BY total DESC"
		)
		assert ir._NARROWS + "the query sorts by an aggregate" in _c7("Filesort", q).lead
		assert ir._aggregate_aliases(ir._SQL_TOKEN_RE.findall(q)) == {"total"}

	def test_a_grouping_with_a_range_sorted_by_an_aggregate_names_the_aggregate(self):
		q = (
			"SELECT `name` FROM `tabSales Invoice` WHERE `company`=? AND `posting_date` > ? GROUP BY `customer` "
			"ORDER BY SUM(`grand_total`) DESC"
		)
		advice = _c7("Temporary Table", q)
		assert advice.columns == ("company", "posting_date")
		assert ir._NARROWS + "the query sorts its groups by an aggregate" in advice.lead

	def test_a_grouping_sorted_by_another_column_says_so(self):
		q = "SELECT customer FROM `tabSales Invoice` WHERE company = ? GROUP BY customer ORDER BY posting_date"
		lead = _c7("Temporary Table", q).lead
		assert ir._NARROWS + "the query sorts by other columns than it groups by, so the temporary table stays." in lead, lead

	def test_distinct_without_group_by_names_the_distinct(self):
		lead = _c7("Temporary Table", "SELECT DISTINCT customer FROM `tabSales Invoice` WHERE company = ?").lead
		assert ir._NARROWS + "the temporary table comes from the query's DISTINCT, which this index does not cover" in lead, lead
		assert "metadata" not in lead

	def test_a_metadata_sort_column_is_named_as_one(self):
		lead = _c7("Filesort", "SELECT name FROM `tabSales Invoice` WHERE company = ? ORDER BY idx DESC").lead
		assert ir._NARROWS + "the sort column is a Frappe metadata column, which Optimus never indexes, so the sort stays." in lead
		assert "aggregate" not in lead
		lead = _c7("Temporary Table", "SELECT count(*) FROM `tabSales Invoice` WHERE company = ? GROUP BY docstatus").lead
		assert ir._NARROWS + "the grouping column is a Frappe metadata column, which Optimus never indexes" in lead


# --- E3: the MariaDB reserved-word check is MariaDB's -------------------------------------


class TestReservedWords:
	def test_a_reserved_word_column_on_postgres_gets_a_postgres_only_entry(self):
		ev = _ev(dialect="postgres", fields={**_ALL, "order": F("Data")})
		single = ir.advise_finding(_missing("order"), evidence_lookup=_lookup(ev))
		assert single.route == ir.ROUTE_ENSURE_INDEXES and single.entry["db"] == "postgres"
		pair = ir.advise_table("tabSales Invoice", ["order", "customer"], evidence_lookup=_lookup(ev))
		assert pair.route == ir.ROUTE_ENSURE_INDEXES, ir.card_note(pair)
		# frappe.db.add_index writes MariaDB column names unquoted, so the entry never runs there
		assert pair.entry["db"] == "postgres"

	def test_a_plain_postgres_composite_stays_unstamped(self):
		ev = _ev(dialect="postgres", fields=_ALL)
		assert "db" not in ir.advise_table("tabSales Invoice", ["status", "customer"], evidence_lookup=_lookup(ev)).entry

	def test_a_reserved_word_column_on_mariadb_still_gives_no_code(self):
		ev = _ev(fields={**_ALL, "order": F("Data")})
		advice = ir.advise_finding(_missing("order"), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE and "is a reserved word in MariaDB" in _text(advice)


# --- E6: a table Optimus has no information about ------------------------------------------


class TestUnknownTable:
	@pytest.mark.parametrize("table", ["tabSessions", "tabSeries", "tabSingles", "tabGone DocType"])
	def test_no_evidence_says_optimus_has_no_information(self, table):
		finding = _explain("Full Table Scan", f"SELECT name FROM `{table}` WHERE customer = ?", table=table)
		advice = ir.advise_finding(finding, evidence_lookup=lambda t: None)
		assert advice.route == ir.ROUTE_NO_CODE and advice.unknown
		assert f'Optimus has no information about table "{table}"' in _text(advice)
		assert "the DocType may not exist" not in _text(advice)
		card = ir.advise_table(table, ["customer"], evidence_lookup=lambda t: None)
		note = ir.card_note(card)
		assert "Do not add this index." not in note
		assert note.startswith(ir.NO_VERDICT)

	def test_a_definite_no_code_card_keeps_do_not_add(self):
		ev = _with_index(_SI, ("customer_index", ["customer"], False))
		card = ir.advise_table("tabSales Invoice", ["customer"], evidence_lookup=_lookup(ev))
		assert not card.unknown and ir.card_note(card).startswith("Do not add this index.")


# --- E7: a UNION that filters the table in more than one branch -----------------------------


class TestUnion:
	def test_two_filtered_branches_give_the_could_not_read_no_code(self):
		q = (
			"SELECT name FROM `tabSales Invoice` WHERE customer = ? UNION "
			"SELECT name FROM `tabSales Invoice` WHERE status = ?"
		)
		for ftype in ("Full Table Scan", "Slow Query", "Low Filter Ratio"):
			advice = ir.advise_finding(_explain(ftype, q), evidence_lookup=_lookup(_SI))
			assert advice.route == ir.ROUTE_NO_CODE and advice.unknown, ftype
			assert advice.code is None
			assert "Optimus could not read how this query filters" in _text(advice)
			assert "UNION" in _text(advice)

	def test_union_all_with_aliases_counts_too(self):
		q = (
			"SELECT si.name FROM `tabSales Invoice` si WHERE si.customer = ? UNION ALL "
			"SELECT s2.name FROM `tabSales Invoice` s2 WHERE s2.status = ?"
		)
		assert ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI)).route == ir.ROUTE_NO_CODE

	def test_one_filtered_branch_is_still_advised(self):
		q = (
			"SELECT name FROM `tabSales Invoice` WHERE `tabSales Invoice`.customer = ? AND `tabSales Invoice`.status = ? "
			"UNION SELECT name FROM `tabSales Order` WHERE `tabSales Order`.company = ?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES

	def test_a_union_all_never_makes_where_an_alias(self):
		"""sql_metadata reports {"WHERE": table} for a UNION ALL; the target filtered in one
		branch only is one branch."""
		q = (
			"SELECT name FROM `tabPurchase Invoice` WHERE supplier = ? UNION ALL "
			"SELECT name FROM `tabSales Invoice` WHERE customer = ?"
		)
		qualifiers = ir._target_qualifiers(q, "tabSales Invoice")
		assert qualifiers == frozenset({"tabSales Invoice"})
		assert ir._union_branches(q, qualifiers) == 1

	def test_a_branch_without_a_where_is_no_filtered_branch(self):
		q = (
			"SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ? UNION "
			"SELECT name FROM `tabSales Invoice`"
		)
		assert ir._union_branches(q, frozenset({"tabSales Invoice"})) == 1
		assert ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI)).route == ir.ROUTE_ENSURE_INDEXES

	def test_a_table_named_only_inside_a_subquery_is_no_filtered_branch(self):
		q = (
			"SELECT name FROM `tabSales Order` WHERE x IN (SELECT parent FROM `tabSales Invoice` WHERE a = ?) "
			"UNION SELECT name FROM `tabSales Invoice` WHERE status = ?"
		)
		assert ir._union_branches(q, frozenset({"tabSales Invoice"})) == 1

	def test_no_evidence_wins_over_the_union_text(self):
		q = (
			"SELECT name FROM `tabSales Invoice` WHERE customer = ? UNION "
			"SELECT name FROM `tabSales Invoice` WHERE status = ?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=lambda table: None)
		assert advice.unknown and "Optimus has no information about table" in _text(advice)

	def test_a_union_inside_a_subquery_is_not_top_level(self):
		q = (
			"SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ? AND name IN "
			"(SELECT parent FROM `tabSales Invoice` WHERE company = ? UNION SELECT parent FROM `tabSales Invoice` WHERE po_no = ?)"
		)
		assert ir._union_branches(q, frozenset({"tabSales Invoice"})) == 1


# --- E1: empty Tracked Apps never states "do not edit it" as a fact ----------------------


class TestEmptyTrackedAppsWording:
	"""RULING E1: with Tracked Apps empty no app counts as the developer's own, so a
	non-framework app's DocType gets the Property Setter route with a conditional note."""

	def test_empty_tracked_apps_says_if_it_is_your_app(self):
		ev = _ev(app="myapp", fields=_ALL)
		advice = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(ev))
		assert advice.entry["search_index_field"] == "po_no"
		text = _text(advice)
		assert "do not edit it" not in text
		assert (
			'If "myapp" is your app, tick Search Index on the field instead; set Tracked Apps in Optimus '
			"Settings so Optimus can tell." in text
		)

	def test_an_unknown_app_gets_the_conditional_too(self):
		ev = _ev(app="", fields=_ALL)
		text = _text(ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(ev)))
		assert "do not edit it" not in text and "If the DocType belongs to your app, tick Search Index" in text

	def test_a_framework_app_is_never_yours(self):
		text = _text(ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(_SI)))
		assert 'belongs to the "erpnext" app, so do not edit it' in text and "set Tracked Apps" not in text

	def test_an_untracked_app_with_tracked_apps_set_is_not_yours(self):
		advice = ir.advise_finding(_missing("po_no"), evidence_lookup=_lookup(_SI), tracked_apps=("myapp",))
		assert 'belongs to the "erpnext" app, so do not edit it' in _text(advice)
		assert "set Tracked Apps" not in _text(advice)


# --- U4: the existing-index tail names only what the scan found -----------------------------


_INDEXED = _with_index(_SI, ("customer_index", ["customer"], False))


class TestExistingIndexTail:
	def test_no_query_never_says_rewrite_the_filter(self):
		for advice in (
			ir.advise_finding(_missing("customer"), evidence_lookup=_lookup(_INDEXED)),
			ir.advise_table("tabSales Invoice", ["customer"], evidence_lookup=_lookup(_INDEXED)),
		):
			blob = _text(advice) + ir.card_note(advice)
			assert "Rewrite the filter" not in blob and _GENERIC_HEDGE not in blob
			assert "Check the slow queries on this column with EXPLAIN" in blob

	def test_a_plain_filter_is_called_index_friendly(self):
		advice = ir.advise_finding(
			_explain("Full Table Scan", "SELECT name FROM `tabSales Invoice` WHERE customer = ?"), evidence_lookup=_lookup(_INDEXED),
		)
		text = _text(advice)
		assert advice.route == ir.ROUTE_NO_CODE
		assert "The query's filter looks index-friendly, so check the query with EXPLAIN" in text
		assert "Rewrite the filter" not in text and _GENERIC_HEDGE not in text

	@pytest.mark.parametrize("where,phrase", [
		("customer = ? AND po_no LIKE ?", "a LIKE on po_no, which cannot use an index when its pattern starts with a wildcard"),
		("customer = ? AND ifnull(status, ?) = ?", "the function IFNULL() wrapped around status"),
		("customer = ? AND (status = ? OR po_no = ?)", "an OR between conditions on status, po_no"),
	])
	def test_a_shape_the_scan_found_is_named(self, where, phrase):
		q = f"SELECT name FROM `tabSales Invoice` WHERE {where}"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_INDEXED))
		text = _text(advice)
		assert advice.route == ir.ROUTE_NO_CODE and advice.columns == ("customer",)
		assert f"The cost comes from how the query filters: {phrase}." in text, text
		assert "Rewrite the filter" in text and _GENERIC_HEDGE not in text

	def test_an_unsure_column_is_named_as_unread(self):
		advice = ir.advise(
			"tabSales Invoice", ["customer", "status"], evidence=_INDEXED, query="SELECT 1",
			unusable={"status": {"unsure"}},
		)
		assert advice.route == ir.ROUTE_NO_CODE
		assert "Optimus could not read how the query filters on status, so check the query with EXPLAIN" in _text(advice)


# --- T3: feature branches that had no test ------------------------------------------------


class TestUncoveredBranches:
	def test_an_unsure_column_is_left_out_with_its_reason(self):
		advice = ir.advise("tabSales Invoice", ["customer", "status"], evidence=_SI, unusable={"status": {"unsure"}})
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.columns == ("customer",)
		assert "Optimus left out status (a filter Optimus could not place with certainty)" in _text(advice)

	def test_a_check_field_filter_alone_gives_no_code(self):
		ev = _ev(fields={**_ALL, "is_return": F("Check"), "disabled": F("Check")})
		one = ir.advise_finding(
			_explain("Full Table Scan", "SELECT name FROM `tabSales Invoice` WHERE is_return = ?"), evidence_lookup=_lookup(ev),
		)
		text = _text(one)
		assert one.route == ir.ROUTE_NO_CODE and not one.unknown
		assert (  # bounded corrective R1: hedged, Optimus cannot see the value distribution
			"is_return is a Check field, which usually matches most of the table's rows; if this query looks for "
			"the rare value, an index on (is_return) can help."
		) in text
		assert "would not help" not in text
		assert "Filter on a more selective field as well, and check the result with EXPLAIN." in text
		two = ir.advise_finding(
			_explain("Full Table Scan", "SELECT name FROM `tabSales Invoice` WHERE is_return = ? AND disabled = ?"),
			evidence_lookup=_lookup(ev),
		)
		assert (
			"disabled, is_return are Check fields, which usually match most of the table's rows; if this query looks "
			"for the rare values, an index on (disabled, is_return) can help."
		) in _text(two)

	@pytest.mark.parametrize("where,phrase", [
		("customer LIKE ?", "a LIKE on customer, which cannot use an index when its pattern starts with a wildcard"),
		("upper(customer) = ?", "the function UPPER() wrapped around customer"),
		("customer = ? OR status = ?", "an OR between conditions on customer, status"),
		("grand_total + 1 > ?", "arithmetic on grand_total"),
	])
	def test_each_filter_shape_has_its_text(self, where, phrase):
		q = f"SELECT name FROM `tabSales Invoice` WHERE {where}"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_C7))
		assert advice.route == ir.ROUTE_NO_CODE and not advice.unknown
		assert phrase in _text(advice), _text(advice)

	def test_a_first_column_of_the_wrong_type_gives_no_code(self):
		"""A card keeps the analyzer's order (M6), so its leading JSON column gives no code; a
		later one is left out."""
		ev = _ev(fields={**_ALL, "payload": F("JSON")})
		for cols in (["payload"], ["payload", "customer"]):
			advice = ir.advise_table("tabSales Invoice", cols, evidence_lookup=_lookup(ev))
			assert advice.route == ir.ROUTE_NO_CODE and advice.code is None
			assert 'Column "payload" is a JSON field, which a plain index cannot cover' in ir.card_note(advice)
		advice = ir.advise_table("tabSales Invoice", ["customer", "payload"], evidence_lookup=_lookup(ev))
		assert advice.columns == ("customer",)
		assert "Optimus left out payload (a type a plain index cannot cover)" in ir.card_note(advice)
		pg = _ev(dialect="postgres", fields={**_ALL, "geo": F("Data")}, extra_types={"geo": "jsonb"})
		advice = ir.advise_table("tabSales Invoice", ["geo"], evidence_lookup=_lookup(pg))
		assert 'Column "geo" has the type jsonb, which a plain index cannot cover' in ir.card_note(advice)


# --- the card verdict --------------------------------------------------------------------


def test_an_unread_filter_is_no_definite_verdict():
	"""A Slow Query cut at QUERY_TEXT_LIMIT before its WHERE clause ends cannot be read."""
	q = "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ?"
	while len(q) < QUERY_TEXT_LIMIT:
		q += " AND company = ?"
	advice = ir.advise_finding(_explain("Slow Query", q), evidence_lookup=_lookup(_SI))
	assert advice.route == ir.ROUTE_NO_CODE and advice.unknown
	assert "Do not add this index." not in ir.card_note(advice)


def test_a_query_too_long_to_parse_is_no_definite_verdict():
	q = "SELECT name FROM `tabSales Invoice` WHERE " + " AND ".join(f"c{i} = ?" for i in range(600))
	advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI))
	assert advice.route == ir.ROUTE_NO_CODE and advice.unknown


def test_a_shape_with_an_unread_column_is_no_definite_verdict():
	advice = ir.advise(
		"tabSales Invoice", ["customer", "status"], evidence=_SI, query="SELECT 1",
		unusable={"customer": {"like"}, "status": {"unsure"}},
	)
	assert advice.route == ir.ROUTE_NO_CODE and advice.unknown
	assert "Optimus could not read how the query filters on status" in _text(advice)
	known = ir.advise("tabSales Invoice", ["customer"], evidence=_SI, query="SELECT 1", unusable={"customer": {"like"}})
	assert known.route == ir.ROUTE_NO_CODE and not known.unknown


def test_the_docs_and_changelog_carry_the_t12_texts():
	"""The docs quote the texts the report shows, so they stay in step with the code."""
	from pathlib import Path

	from optimus.renderer import recipe_enrichment

	root = Path(ir.__file__).resolve().parents[2]
	doc = " ".join((root / "docs" / "AI-FIXING.md").read_text(encoding="utf-8").split())
	log = " ".join((root / "CHANGELOG.md").read_text(encoding="utf-8").split())
	title = recipe_enrichment.NO_INDEX_TITLE.format(table="<table>", column="<column>")
	for text in (doc, log):
		assert title in text and ir.NO_VERDICT in text
		assert '"optimus: index advice failed"' in text
	assert recipe_enrichment.NO_INDEX_ACTION_TITLE in doc and "if <app> is your app" in doc
	# fix round 1: the equality block's order, coverage in any order, the parser-dropped columns
	assert "`(posting_date, company)` serves `company = ? AND posting_date = ?`" in doc
	assert "one fixed order" in doc and "one fixed order" in log
	assert "did not report" in doc and "did not report" in log
	assert "`unknown` flag" in doc and "`unknown` flag" in log
	# fix round 2: cards keep their order, the unique subset, the Check rule's tail
	assert "most-used-first" in doc and "most-used-first" in log
	assert "UNIQUE `(po_no, customer)`" in doc and "creation > ? AND is_return = ?" in doc
	# fix round 3: the served verdict needs evidence, IN (?), the evidence cap, keys
	assert "keeps that \"already serves\" verdict" in doc and "normalize_query" in doc
	assert "an existing index already finds these rows" in doc and "by `name` (the primary key)" in doc
	assert "`IN (?)`" in log and "rejected by the optimizer" in log
	# fix round 4: keys need a value, the parent index must exist, EXPLAIN is no evidence, the cap
	assert "is a join condition, never a lookup" in doc and "`mariadb/schema.py`" in doc
	assert "join condition such as `pr_item.parent = pr.name`" in log and "Postgres none" in log
	assert "`possible_keys` never lists an index that only serves the ORDER BY" in doc
	assert "possible_keys never lists an index that only serves ORDER BY" in log
	# fix round 5: a left-out index refuses when unique or ranked as well as the weakest kept column
	assert "at least as well as the weakest column the new index would keep" in doc
	assert "at least as well as the weakest kept column" in log
	# bounded corrective: the hedged Check wording, the partial sort, the sort-only recipe
	assert "if this query looks for the rare value, an index on (is_return, posting_date) can help" in doc
	assert "usually matches most rows" in log and "keeps only some sort columns" in log
	assert "rarely walks a whole index instead of sorting when the query has no LIMIT" in doc
	assert "rarely walks a whole index instead of sorting" in log
	assert "A Filesort query with no `LIMIT` is the exception" in doc and "except a Filesort query with no LIMIT" in log
	assert "the sort columns keep the clause's order" in doc and "in the clause's order" in log



# --- fix round 1, item 1: an existing index covers a recipe in any equality order ----------


_SLE = _ev("Stock Ledger Entry", fields={
	"item_code": F("Link"), "warehouse": F("Link"), "voucher_type": F("Link", search_index=True),
	"voucher_no": F("Dynamic Link"), "is_cancelled": F("Check"), "posting_date": F("Date"), "company": F("Link"),
}, indexes=[
	("voucher_type_index", ["voucher_type"], False), ("voucher_no_voucher_type_index", ["voucher_no", "voucher_type"], False),
])
_GL = _ev("GL Entry", fields={
	"account": F("Link", search_index=True), "party_type": F("Link", search_index=True),
	"party": F("Dynamic Link", search_index=True), "voucher_type": F("Link"), "voucher_no": F("Dynamic Link", search_index=True),
	"posting_date": F("Date", search_index=True), "is_cancelled": F("Check"), "company": F("Link", search_index=True),
}, indexes=[
	("account_index", ["account"], False), ("party_type_index", ["party_type"], False), ("party_index", ["party"], False),
	("voucher_no_index", ["voucher_no"], False), ("posting_date_index", ["posting_date"], False),
	("company_index", ["company"], False), ("voucher_type_voucher_no_index", ["voucher_type", "voucher_no"], False),
	("posting_date_company_index", ["posting_date", "company"], False), ("party_type_party_index", ["party_type", "party"], False),
])


class TestPermutedCoverage:
	"""review-t12 item 1: real ERPNext ledger shapes (review-t12/ledger_real.py)."""

	@pytest.mark.parametrize("ftype", ["Full Table Scan", "Low Filter Ratio", "Slow Query"])
	@pytest.mark.parametrize("table,where", [
		("tabStock Ledger Entry", "`voucher_type` = ? AND `voucher_no` = ?"),
		("tabStock Ledger Entry", "`voucher_no` = ? AND `voucher_type` = ?"),
		("tabStock Ledger Entry", "`voucher_no` = ? AND `voucher_type` = ? AND `is_cancelled` = ?"),
		("tabGL Entry", "`voucher_no` = ? AND `voucher_type` = ? AND `is_cancelled` = ?"),
		("tabGL Entry", "`voucher_type` = ? AND `voucher_no` = ?"),
		("tabGL Entry", "`party` = ? AND `party_type` = ?"),
		("tabGL Entry", "`company` = ? AND `posting_date` = ?"),
	])
	def test_a_ledger_lookup_an_existing_index_serves_gets_no_code(self, ftype, table, where):
		q = f"SELECT name FROM `{table}` WHERE {where}"
		finding = _explain(ftype, q, table=table if ftype != "Slow Query" else "")
		advice = ir.advise_finding(finding, evidence_lookup=_lookup(_SLE, _GL))
		assert advice.route == ir.ROUTE_NO_CODE and not advice.unknown, _text(advice)
		assert "already starts with" in _text(advice)

	def test_a_check_field_beyond_an_existing_equality_prefix_is_named(self):
		q = "SELECT name FROM `tabStock Ledger Entry` WHERE `voucher_no` = ? AND `voucher_type` = ? AND `is_cancelled` = ?"
		text = _text(ir.advise_finding(_explain("Full Table Scan", q, table="tabStock Ledger Entry"), evidence_lookup=_lookup(_SLE)))
		assert 'The index "voucher_no_voucher_type_index" on table "tabStock Ledger Entry" already starts with (voucher_no, voucher_type)' in text
		assert "is_cancelled is a Check field, which usually matches most of the table's rows, so Optimus gives no" in text

	def test_a_check_field_with_a_sort_after_it_still_gets_code(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND is_return = ? ORDER BY posting_date"
		ev = _with_index(_ev(fields={**_ALL, "is_return": F("Check")}), ("customer_index", ["customer"], False))
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.entry["columns"] == ["customer", "is_return", "posting_date"]

	def test_a_range_after_a_permuted_equality_prefix_must_follow_in_order(self):
		"""The equality block is a set, the tail follows in order: (posting_date, company)
		does not serve company = ? with a sort on posting_date."""
		q = "SELECT name FROM `tabGL Entry` WHERE company = ? AND posting_date BETWEEN ? AND ? ORDER BY posting_date"
		advice = ir.advise_finding(_explain("Filesort", q, table="tabGL Entry"), evidence_lookup=_lookup(_GL))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.entry["columns"] == ["company", "posting_date"]

	def test_the_sort_columns_after_the_equality_block_must_follow_in_order(self):
		ev = _with_index(_ev(fields={**_ALL, "due_date": F("Date")}), ("idx_cpd", ["company", "posting_date", "due_date"], False))
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? ORDER BY {}"
		same = ir.advise_finding(_explain("Filesort", q.format("posting_date, due_date")), evidence_lookup=_lookup(ev))
		assert same.route == ir.ROUTE_NO_CODE
		swapped = ir.advise_finding(_explain("Filesort", q.format("due_date, posting_date")), evidence_lookup=_lookup(ev))
		assert swapped.route == ir.ROUTE_ENSURE_INDEXES and swapped.entry["columns"] == ["company", "due_date", "posting_date"]

	def test_a_unique_composite_in_another_order_covers_the_recipe(self):
		ev = _with_index(_SI, ("uniq_po_customer", ["po_no", "customer"], True))
		for where in ("customer = ? AND po_no = ?", "po_no = ? AND customer = ?"):
			q = f"SELECT name FROM `tabSales Invoice` WHERE {where}"
			advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev))
			assert advice.route == ir.ROUTE_NO_CODE
			assert 'The unique index "uniq_po_customer" on table "tabSales Invoice" covers (po_no, customer)' in _text(advice)

	@pytest.mark.parametrize("where", ["status = ? AND company = ?", "company = ? AND status = ?"])
	def test_a_filesort_equality_permutation_with_its_sort_is_covered(self, where):
		ev = _with_index(_ev(fields={**_ALL, "due_date": F("Date")}), ("idx_csd", ["company", "status", "due_date"], False))
		q = f"SELECT name FROM `tabSales Invoice` WHERE {where} ORDER BY due_date"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE, _text(advice)
		q = f"SELECT name FROM `tabSales Invoice` WHERE {where} ORDER BY posting_date"
		other = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(ev))
		assert other.route == ir.ROUTE_ENSURE_INDEXES and other.entry["columns"] == ["company", "status", "posting_date"]

	def test_a_card_is_one_unordered_equality_set_for_coverage(self):
		"""Coverage reads a card's columns as one equality set; its own order stays (M6)."""
		ev = _with_index(_SI, ("idx_cs", ["company", "status", "posting_date"], False))
		for cols in (["status", "company"], ["company", "status"]):
			card = ir.advise_table("tabSales Invoice", cols, evidence_lookup=_lookup(ev))
			assert card.route == ir.ROUTE_NO_CODE
			assert "Check the slow queries on these columns with EXPLAIN" in ir.card_note(card)

	@pytest.mark.parametrize("dialect,field,why", [
		("mariadb", F("JSON"), "a type a plain index cannot cover"),
		("postgres", F("Small Text"), "a text column, which a Postgres index cannot hold safely"),
		("mariadb", F("Data", length=1000), "the index would pass the 3072-byte MariaDB key limit"),
	])
	def test_a_column_that_cannot_lead_goes_last_whatever_its_name(self, dialect, field, why):
		""""aaa" sorts before "customer" by name, yet it cannot lead, so a finding's equality
		block puts it last and leaves it out; by name alone it would lead and give no code."""
		ev = _ev(dialect=dialect, fields={**_ALL, "aaa": field})
		for where in ("aaa = ? AND customer = ?", "customer = ? AND aaa = ?"):
			q = f"SELECT name FROM `tabSales Invoice` WHERE {where}"
			advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev))
			assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.columns == ("customer",), _text(advice)
			assert f"Optimus left out aaa ({why})" in _text(advice)

	def test_the_equality_block_puts_check_fields_last_and_keeps_creation_last(self):
		"""The canonical order: business columns by name, then Check fields by name, then
		creation / modified (which never lead)."""
		ev = _ev(fields={**_ALL, "is_return": F("Check")})
		q = "SELECT name FROM `tabSales Invoice` WHERE is_return = ? AND status = ? AND creation = ? AND company = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev))
		assert advice.entry["columns"] == ["company", "status", "is_return", "creation"]


# --- fix round 1, item 2: a unique equality column makes a composite pointless --------------


_USER = _ev("User", app="frappe", fields={
	"email": F("Data", unique=True), "enabled": F("Check"), "user_type": F("Select"), "username": F("Data", unique=True),
}, indexes=[("email", ["email"], True), ("username", ["username"], True)])


class TestUniqueEqualityColumn:
	@pytest.mark.parametrize("where", ["email = ? AND user_type = ?", "user_type = ? AND email = ?"])
	def test_a_unique_equality_column_gives_no_code(self, where):
		q = f"SELECT name FROM `tabUser` WHERE {where}"
		for ftype, table in (("Slow Query", ""), ("Full Table Scan", "tabUser")):
			advice = ir.advise_finding(_explain(ftype, q, table=table), evidence_lookup=_lookup(_USER))
			assert advice.route == ir.ROUTE_NO_CODE and 'Column "email" is already unique' in _text(advice)

	def test_a_unique_card_column_gives_no_code(self):
		card = ir.advise_table("tabUser", ["email", "enabled"], evidence_lookup=_lookup(_USER))
		assert card.route == ir.ROUTE_NO_CODE and 'Column "email" is already unique' in ir.card_note(card)

	def test_a_unique_field_without_a_listed_index_counts(self):
		ev = _ev(fields={**_ALL, "irn": F("Data", unique=True)})
		q = "SELECT name FROM `tabSales Invoice` WHERE irn = ? AND company = ?"
		assert ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev)).route == ir.ROUTE_NO_CODE

	def test_a_unique_sort_column_is_no_reason_to_refuse(self):
		q = "SELECT name FROM `tabUser` WHERE user_type = ? ORDER BY username"
		advice = ir.advise_finding(_explain("Filesort", q, table="tabUser"), evidence_lookup=_lookup(_USER))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.entry["columns"] == ["user_type", "username"]


# --- fix round 1, item 3: a column the SQL parser drops is never silently left out ---------


_DROPPED = ("account", "user", "role", "date", "type", "comment", "language", "source", "data", "level")


class TestParserDroppedColumns:
	def _ev(self):
		fields = {**_ALL, "party": F("Link")} | {name: F("Data") for name in _DROPPED}
		return _with_index(_ev(fields=fields), ("party_index", ["party"], False))

	@pytest.mark.parametrize("name", _DROPPED)
	def test_a_dropped_column_of_the_only_table_is_advised(self, name):
		"""M4: the only table of the main FROM owns every unqualified column."""
		q = f"SELECT name FROM `tabSales Invoice` WHERE {name} = ? AND party = ?"
		for ftype in ("Full Table Scan", "Slow Query"):
			advice = ir.advise_finding(_explain(ftype, q), evidence_lookup=_lookup(self._ev()))
			assert advice.route == ir.ROUTE_ENSURE_INDEXES and set(advice.entry["columns"]) == {name, "party"}, _text(advice)

	@pytest.mark.parametrize("name", _DROPPED)
	def test_a_dropped_column_of_a_join_gives_the_could_not_read_no_code(self, name):
		q = (
			"SELECT si.name FROM `tabSales Invoice` si JOIN `tabSales Order` so ON so.name = si.po_no "
			f"WHERE {name} = ? AND si.party = ?"
		)
		for ftype in ("Full Table Scan", "Slow Query"):
			advice = ir.advise_finding(_explain(ftype, q), evidence_lookup=_lookup(self._ev()))
			text = _text(advice)
			assert advice.route == ir.ROUTE_NO_CODE and advice.unknown, text
			assert f"Optimus could not read the filter on {name}" in text
			assert "would not help" not in text

	@pytest.mark.parametrize("name", ["account", "date", "type"])
	def test_a_quoted_column_is_read(self, name):
		q = f"SELECT name FROM `tabSales Invoice` WHERE `{name}` = ? AND `party` = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(self._ev()))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and set(advice.entry["columns"]) == {name, "party"}

	@pytest.mark.parametrize("value", ["DATE(?)", "DATE ?"])
	def test_a_keyword_that_is_no_comparison_is_no_column(self, value):
		"""date is also a value keyword: DATE(?) is a call and DATE ? a typed literal, never
		the date column."""
		q = f"SELECT name FROM `tabSales Invoice` WHERE `party` = ? AND `posting_date` = {value}"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(self._ev()))
		assert not advice.unknown, _text(advice)

	def test_a_column_inside_a_subquery_is_no_dropped_column(self):
		q = (
			"SELECT name FROM `tabSales Invoice` WHERE `tabSales Invoice`.`party` = ? AND `tabSales Invoice`.`customer` = ? "
			"AND name IN (SELECT parent FROM `tabSales Invoice Item` WHERE account = ?)"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(self._ev()))
		assert not advice.unknown, _text(advice)

	def test_an_unqualified_column_of_a_query_on_several_tables_is_unread(self):
		q = (
			"SELECT si.name FROM `tabSales Invoice` si JOIN `tabSales Order` so ON so.name = si.po_no "
			"WHERE customer = ? AND si.party = ?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(self._ev()))
		assert advice.unknown and "an unqualified column of a query on several tables" in _text(advice)

	def test_the_where_scan_reads_only_the_target_tables_comparable_columns(self):
		"""The scan's own guards, read directly: a keyword that is no comparison, another
		table's qualified column, a column inside a subquery, a call."""
		names = {n: n for n in ("date", "account", "party", "customer", "year")}
		quals = frozenset({"tabSales Invoice", "si"})
		scan = ir._where_columns
		assert scan("SELECT 1 FROM `tabSales Invoice` WHERE `party` = ? AND `posting_date` = DATE ?", quals, names) == {"party"}
		assert scan("SELECT 1 FROM `tabSales Invoice` WHERE date = ? AND party = ?", quals, names) == {"date", "party"}
		assert scan("SELECT 1 FROM `tabSales Invoice` si WHERE si.party = ? AND so.account = ?", quals, names) == {"party"}
		assert scan(
			"SELECT 1 FROM `tabSales Invoice` WHERE party = ? AND name IN (SELECT parent FROM `tabX` WHERE account = ?)",
			quals, names,
		) == {"party"}
		assert scan("SELECT 1 FROM `tabSales Invoice` WHERE year(posting_date) = ? AND party = ?", quals, names) == {"party"}

	def test_a_dropped_metadata_column_of_a_join_never_fires(self):
		q = (
			"SELECT si.name FROM `tabSales Invoice` si JOIN `tabSales Order` so ON so.name = si.po_no "
			"WHERE owner = ? AND si.party = ? AND si.customer = ?"
		)
		ev = _ev(fields={**_ALL, "party": F("Link")}, extra_types={"owner": "varchar"})
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev))
		assert not advice.unknown, _text(advice)

	def test_a_dropped_metadata_column_is_never_missed(self):
		"""owner is dropped by the parser too, but a metadata column is never indexed."""
		q = "SELECT name FROM `tabSales Invoice` WHERE owner = ? AND `party` = ? AND `customer` = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(self._ev()))
		assert not advice.unknown and advice.route == ir.ROUTE_ENSURE_INDEXES, _text(advice)

	def test_another_tables_column_is_no_dropped_column(self):
		q = (
			"SELECT si.name FROM `tabSales Invoice` si JOIN `tabSales Order` so ON so.name = si.po_no "
			"WHERE si.party = ? AND si.customer = ? AND so.account = ?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(self._ev()))
		assert not advice.unknown, _text(advice)


# --- fix round 1, items 4-7 ----------------------------------------------------------------


def test_a_filesort_grouped_by_other_columns_says_so():
	q = "SELECT customer FROM `tabSales Invoice` WHERE company = ? GROUP BY customer ORDER BY posting_date"
	lead = _c7("Filesort", q).lead
	assert ir._NARROWS + "the query groups by other columns than it sorts by, so the sort stays." in lead, lead


def test_a_union_without_labelled_columns_is_unread():
	q = "SELECT name FROM `tabSales Invoice` WHERE 1 = 1 UNION SELECT name FROM `tabSales Invoice` WHERE 2 = 2"
	advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI))
	assert advice is not None and advice.route == ir.ROUTE_NO_CODE and advice.unknown


_JOINED = (
	"SELECT si.name FROM `tabSales Invoice` si JOIN `tabSales Invoice Item` sii ON sii.parent = si.name "
	"WHERE si.company = ? {tail}"
)


def test_a_temporary_table_without_a_group_by_never_blames_the_grouping():
	lead = _c7("Temporary Table", _JOINED.format(tail="ORDER BY sii.item_code")).lead
	assert "grouping" not in lead and "groups by" not in lead, lead
	assert ir._NARROWS + "the query has no GROUP BY or DISTINCT on this table" in lead


def test_a_sort_on_another_tables_column_says_so():
	lead = _c7("Filesort", _JOINED.format(tail="ORDER BY sii.item_code")).lead
	assert ir._NARROWS + "the query sorts by a column of another table, which an index on this table cannot return in order" in lead
	assert "expression" not in lead
	lead = _c7("Temporary Table", _JOINED.format(tail="GROUP BY sii.item_code")).lead
	assert ir._NARROWS + "the query groups by a column of another table" in lead, lead


def test_the_real_explain_flags_fix_sentences_are_removed():
	"""review-t12 item 4: the descriptions explain_flags really writes (not copies) lose the
	sentence that promises an index fixes them."""
	from types import SimpleNamespace

	from optimus.analyzers import explain_flags
	from optimus.renderer import recipe_enrichment

	plan = SimpleNamespace(
		table="tabSales Invoice", rows_examined=100000, full_scan=True, sort_without_index=True, temp_used=True,
		selectivity_pct=1.0, raw={},
	)
	buckets = {}
	explain_flags._inspect_table(plan, "SELECT 1", 0, 10.0, buckets)
	by_type = {ftype: row for (ftype, _table), row in buckets.items()}
	no_code = {"route": ir.ROUTE_NO_CODE, "unknown": False}
	removed = 0
	for ftype in ("Full Table Scan", "Filesort", "Low Filter Ratio", "Temporary Table"):
		row = by_type[ftype]
		before = row["customer_description"]
		after = recipe_enrichment.finding_display(dict(row), no_code)["customer_description"]
		removed += sum(sentence in before for sentence in recipe_enrichment._INDEX_FIX_SENTENCES)
		assert not any(sentence in after for sentence in recipe_enrichment._INDEX_FIX_SENTENCES)
		assert after.endswith(recipe_enrichment.NO_INDEX_NOTE)
	assert removed == len(recipe_enrichment._INDEX_FIX_SENTENCES) == 3


def test_failed_and_unknown_advice_get_a_neutral_description():
	from optimus.renderer import recipe_enrichment

	finding = {"finding_type": "Missing Index", "customer_description": "x",
		"technical_detail": {"table": "tabSales Invoice", "column": "customer"}}
	certain = recipe_enrichment.finding_display(finding, {"route": ir.ROUTE_NO_CODE, "unknown": False})
	unknown = recipe_enrichment.finding_display(finding, {"route": ir.ROUTE_NO_CODE, "unknown": True})
	assert "Optimus does not recommend a new index on it" in certain["customer_description"]
	assert "Optimus cannot say whether a new index on it would help" in unknown["customer_description"]
	assert "does not recommend" not in unknown["customer_description"]
	fts = {"finding_type": "Full Table Scan", "customer_description": "Adding an appropriate index is usually the fix."}
	note = recipe_enrichment.finding_display(fts, {"route": ir.ROUTE_NO_CODE, "unknown": True})["customer_description"]
	assert note == recipe_enrichment.NO_INDEX_UNKNOWN_NOTE
	failed, is_failed = recipe_enrichment.export_advice(
		{"finding_type": "Missing Index", "technical_detail": {"table": "tabSales Invoice", "column": "customer"}},
		evidence_lookup=lambda table: (_ for _ in ()).throw(RuntimeError("boom")),
	)
	assert is_failed and failed["unknown"] is True
	gone, _ = recipe_enrichment.export_advice(
		{"finding_type": "Missing Index", "technical_detail": {"table": "tabGone", "column": "customer"}},
		evidence_lookup=lambda table: None,
	)
	assert gone["unknown"] is True


# --- fix round 2 (review-t12 battery3): never emit code that does not help ---------------


def _with_creation(ev):
	return _with_index(ev, ("creation", ["creation"], False))


class TestCheckRuleWithATail:
	"""M1: the Check rule also counts the range or sort tail the existing index serves."""

	def test_a_creation_range_with_a_check_field_gives_no_code(self):
		ev = _with_creation(_ev(fields={**_ALL, "is_return": F("Check")}))
		q = "SELECT name FROM `tabSales Invoice` WHERE creation > ? AND is_return = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev))
		text = _text(advice)
		assert advice.route == ir.ROUTE_NO_CODE and not advice.unknown, text
		assert 'The index "creation" on table "tabSales Invoice" already starts with (creation)' in text
		assert "is_return is a Check field" in text

	def test_the_sle_cancelled_filter_gives_no_code(self):
		q = "SELECT name FROM `tabStock Ledger Entry` WHERE creation > ? and is_cancelled = ?"
		advice = ir.advise_finding(
			_explain("Full Table Scan", q, table="tabStock Ledger Entry"), evidence_lookup=_lookup(_with_creation(_SLE)),
		)
		assert advice.route == ir.ROUTE_NO_CODE and "is_cancelled is a Check field" in _text(advice)

	def test_a_sort_the_existing_index_serves_after_a_check_field_gives_no_code(self):
		ev = _with_index(_ev(fields={**_ALL, "is_return": F("Check")}), ("idx_cp", ["customer", "posting_date"], False))
		q = "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND is_return = ? ORDER BY posting_date"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE and "is_return is a Check field" in _text(advice)
		q = "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND is_return = ? AND posting_date > ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE and 'already starts with (customer, posting_date)' in _text(advice)

	def test_without_the_creation_index_the_rule_does_not_fire(self):
		ev = _ev(fields={**_ALL, "is_return": F("Check")})
		q = "SELECT name FROM `tabSales Invoice` WHERE creation > ? AND is_return = ?"
		assert ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev)).route == ir.ROUTE_ENSURE_INDEXES


_ITEM = _ev("Item", fields={"sales_uom": F("Link"), "disabled": F("Check"), "has_variants": F("Check"), "item_group": F("Link")})


class TestDrivingTableJoinColumns:
	"""M2: the FROM table of a LEFT JOIN chain is read first, so its ON columns are probe
	values, never compared with a known value."""

	def test_a_left_join_driving_table_with_check_filters_gives_no_code(self):
		q = (
			"SELECT i.name FROM `tabItem` i LEFT JOIN `tabUOM Conversion Detail` c ON i.sales_uom = c.uom "
			"WHERE i.disabled = ? AND i.has_variants = ?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabItem"), evidence_lookup=_lookup(_ITEM))
		assert advice.route == ir.ROUTE_NO_CODE and "sales_uom" not in advice.columns, _text(advice)
		assert "are Check fields" in _text(advice)

	def test_a_left_join_driving_table_keeps_its_where_columns(self):
		q = (
			"SELECT i.name FROM `tabItem` i LEFT JOIN `tabUOM Conversion Detail` c ON i.sales_uom = c.uom "
			"WHERE i.item_group = ? AND i.disabled = ?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabItem"), evidence_lookup=_lookup(_ITEM))
		assert advice.entry["columns"] == ["item_group", "disabled"]

	def test_the_joined_side_of_a_left_join_keeps_its_lookup_column(self):
		q = (
			"SELECT c.name FROM `tabUOM` c LEFT JOIN `tabItem` i ON i.sales_uom = c.name "
			"WHERE i.disabled = ?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabItem"), evidence_lookup=_lookup(_ITEM))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and "sales_uom" in advice.columns

	def test_battery3_324(self):
		ev = _ev(fields={**_ALL, "is_return": F("Check"), "is_pos": F("Check"), "zzz": F("Link")})
		q = (
			"SELECT si.name FROM `tabSales Invoice` si LEFT JOIN `tabCustomer` c ON c.name = si.zzz "
			"WHERE si.is_return = 0 AND si.is_pos = 0"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE


class TestUniqueSubset:
	"""M3: a unique index whose columns the equality filter all fixes returns at most one row."""

	@pytest.mark.parametrize("where", ["po_no = ? AND customer = ? AND company = ?", "company = ? AND customer = ? AND po_no = ?"])
	def test_a_unique_composite_inside_the_equality_set_gives_no_code(self, where):
		ev = _with_index(_SI, ("u4", ["po_no", "customer"], True))
		advice = ir.advise_finding(_explain("Full Table Scan", f"SELECT name FROM `tabSales Invoice` WHERE {where}"), evidence_lookup=_lookup(ev))
		text = _text(advice)
		assert advice.route == ir.ROUTE_NO_CODE, text
		assert 'The unique index "u4" on table "tabSales Invoice" covers (po_no, customer)' in text

	def test_the_bin_unique_pair_gives_no_code(self):
		ev = _ev("Bin", fields={"item_code": F("Link"), "warehouse": F("Link"), "projected_qty": F("Int")},
			indexes=[("unique_item_warehouse", ["item_code", "warehouse"], True)])
		q = "SELECT name FROM `tabBin` WHERE warehouse = ? AND item_code = ? AND projected_qty < ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabBin"), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE and 'The unique index "unique_item_warehouse"' in _text(advice)

	def test_a_unique_composite_only_partly_fixed_does_not_block(self):
		ev = _with_index(_SI, ("u4", ["po_no", "customer"], True))
		q = "SELECT name FROM `tabSales Invoice` WHERE po_no = ? AND company = ?"
		assert ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev)).route == ir.ROUTE_ENSURE_INDEXES


_GL_PLAIN = _ev("GL Entry", fields={"account": F("Link"), "company": F("Link"), "party": F("Link")})


class TestDroppedColumnScope:
	"""M4, M5: the trigger runs only for the main FROM; the only table there owns its
	unqualified columns; a column the filter shape rules out never fires it."""

	def test_the_qb_subquery_shape_is_advised(self):
		q = (
			"SELECT `name` FROM `tabGL Entry` WHERE `account` IN (SELECT `name` FROM `tabAccount` WHERE `root_type`=?) "
			"AND `company`=?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabGL Entry"), evidence_lookup=_lookup(_GL_PLAIN))
		assert not advice.unknown and advice.route == ir.ROUTE_ENSURE_INDEXES, _text(advice)
		assert set(advice.entry["columns"]) == {"account", "company"}

	def test_an_employee_subquery_shape_is_advised(self):
		ev = _ev("Employee", fields={"company": F("Link"), "status": F("Select")})
		q = (
			"SELECT `name` FROM `tabEmployee` WHERE `company`=? AND `name` IN "
			"(SELECT `parent` FROM `tabEmployee Skill Map` WHERE `skill`=?)"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabEmployee"), evidence_lookup=_lookup(ev))
		assert not advice.unknown and advice.columns == ("company",), _text(advice)

	def test_a_discounted_invoice_subquery_shape_is_advised(self):
		ev = _ev("Discounted Invoice", fields={"sales_invoice": F("Link"), "outstanding_amount": F("Int")},
			extra_types={"parent": "varchar"})
		q = (
			"SELECT `sales_invoice` FROM `tabDiscounted Invoice` WHERE `parent` IN "
			"(SELECT `name` FROM `tabInvoice Discounting` WHERE `status`=?) AND `outstanding_amount`>?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabDiscounted Invoice"), evidence_lookup=_lookup(ev))
		assert not advice.unknown and advice.columns == ("outstanding_amount",), _text(advice)

	def test_a_scalar_subquery_before_the_main_from_is_not_the_main_from(self):
		ev = _ev(fields={**_ALL, "party": F("Link"), "account": F("Link")})
		q = (
			"SELECT si.name, (SELECT COUNT(*) FROM `tabSales Invoice Item` WHERE parent = si.name) AS n "
			"FROM `tabSales Invoice` si WHERE account = ? AND si.party = ?"
		)
		assert ir._from_clause(q) == ["tabsales invoice"]
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev))
		assert set(advice.entry["columns"]) == {"account", "party"}, _text(advice)

	def test_an_outer_column_is_never_given_to_a_table_only_in_a_subquery(self):
		ev = _ev(fields={**_ALL, "account": F("Link")})
		q = (
			"SELECT name FROM `tabSales Order` WHERE account = ? AND name IN "
			"(SELECT po_no FROM `tabSales Invoice` WHERE customer = ?)"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev))
		assert advice is None or ("account" not in advice.columns and "filter on account" not in _text(advice))

	def test_the_only_table_owns_a_dropped_keyword_column(self):
		ev = _with_index(_ev(fields={**_ALL, "party": F("Link"), "account": F("Link")}), ("party_index", ["party"], False))
		q = "SELECT name FROM `tabSales Invoice` WHERE account = ? AND party = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev))
		assert not advice.unknown and advice.entry["columns"] == ["account", "party"]

	def test_a_tax_rule_date_or_keeps_its_shape_verdict(self):
		ev = _ev("Tax Rule", fields={"tax_type": F("Select"), "to_date": F("Date"), "company": F("Link")})
		q = "SELECT name FROM `tabTax Rule` WHERE `tax_type` = ? AND (TO_DATE IS NULL OR TO_DATE >= ?)"
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabTax Rule"), evidence_lookup=_lookup(ev))
		assert not advice.unknown and advice.columns == ("tax_type",), _text(advice)
		assert "Optimus left out to_date (compared only inside an OR between conditions)" in _text(advice)

	def test_a_quotation_or_shape_keeps_its_verdict(self):
		ev = _ev("Quotation", fields={"opportunity": F("Link"), "quotation_to": F("Link"), "company": F("Link")})
		q = 'SELECT name FROM `tabQuotation` WHERE (opportunity!="" or quotation_to="Lead") AND company = ?'
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabQuotation"), evidence_lookup=_lookup(ev))
		assert not advice.unknown and advice.columns == ("company",), _text(advice)

	def test_an_unusable_dropped_column_of_a_join_never_fires(self):
		ev = _ev(fields={**_ALL, "party": F("Link")})
		q = (
			"SELECT si.name FROM `tabSales Invoice` si JOIN `tabSales Order` so ON so.name = si.po_no "
			"WHERE si.party = ? AND (customer = ? OR so.status = ?)"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev))
		assert not advice.unknown, _text(advice)

	def test_a_function_named_like_a_column_is_no_column(self):
		"""M7: YEAR(...) on tabFiscal Year, which has a year column, is a call."""
		ev = _ev("Fiscal Year", fields={"year": F("Data"), "year_start_date": F("Date"), "disabled": F("Check"),
			"company": F("Link")})
		q = (
			"SELECT `name` FROM `tabFiscal Year` WHERE YEAR(`year_start_date`) = ? AND `company` = ? AND `name` IN "
			"(SELECT `parent` FROM `tabFiscal Year Company` WHERE `company` = ?)"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabFiscal Year"), evidence_lookup=_lookup(ev))
		assert "year" not in advice.columns and not advice.unknown, _text(advice)
		assert ir._where_columns(q, frozenset({"tabFiscal Year"}), {"year": "year", "company": "company"}) == {"company"}


class TestUnservableSortTail:
	"""M8: a sort column the index cannot return in order is left out."""

	def test_battery3_301(self):
		ev = _with_index(_SI, ("idx_cc", ["company", "customer"], False))
		q = "SELECT name FROM `tabSales Invoice` WHERE customer IN (?, ?) AND company = ? ORDER BY posting_date"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE, _text(advice)
		assert 'The index "idx_cc" on table "tabSales Invoice" already starts with (company, customer)' in _text(advice)

	def test_without_an_index_the_sort_column_is_left_out_and_named(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE customer IN (?, ?) AND company = ? ORDER BY posting_date"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI))
		text = _text(advice)
		assert advice.entry["columns"] == ["company", "customer"]
		assert "Optimus left out posting_date (an index cannot return these rows in order" in text
		assert ir._NARROWS + "the filter on customer matches more than one value, so it cannot return the rows in order" in advice.lead

	def test_a_refused_sort_recipe_is_kept_when_the_retry_has_nothing_left(self):
		"""posting_date is both the range and the sort: the sort-first recipe (posting_date) is
		refused (its index exists), and the retry without the sort has no column left."""
		ev = _with_index(_SI, ("posting_date_index", ["posting_date"], False))
		q = "SELECT name FROM `tabSales Invoice` WHERE docstatus = ? AND posting_date <= ? ORDER BY posting_date"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(ev))
		assert advice is not None and advice.route == ir.ROUTE_NO_CODE
		assert 'Column "posting_date" already leads the index "posting_date_index"' in _text(advice)

	def test_a_range_filtered_sort_column_keeps_its_range_when_the_sort_goes(self):
		"""SLE: creation is the range filter and a sort column after an expression; the sort
		cannot be served, so creation stays as the range and the creation index serves it."""
		q = (
			"SELECT name FROM `tabStock Ledger Entry` WHERE creation > ? and is_cancelled = ? "
			"ORDER BY timestamp(posting_date, posting_time) asc, creation asc"
		)
		ev = _with_creation(_SLE)
		advice = ir.advise_finding(_explain("Filesort", q, table="tabStock Ledger Entry"), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE and 'The index "creation"' in _text(advice), _text(advice)
		advice = ir.advise_finding(_explain("Filesort", q, table="tabStock Ledger Entry"), evidence_lookup=_lookup(_SLE))
		assert advice.entry["columns"] == ["is_cancelled", "creation"], _text(advice)

	def test_a_sort_recipe_an_existing_index_serves_needs_evidence_to_stand(self):
		"""battery3 [303]: the existing (company, due_date) serves the sort, yet the Filesort
		says the optimizer chose another plan, so the range recipe is the advice; with LIMIT the
		served verdict stands and names that range recipe (round 3, item 1)."""
		ev = _with_index(_ev(fields={**_ALL, "due_date": F("Date")}), ("idx_cd", ["company", "due_date"], False))
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? AND posting_date > ? ORDER BY due_date"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.entry["columns"] == ["company", "posting_date"]
		advice = ir.advise_finding(_explain("Filesort", q + " LIMIT ?"), evidence_lookup=_lookup(ev))
		text = _text(advice)
		assert advice.route == ir.ROUTE_NO_CODE and advice.served_by == "idx_cd"
		assert "an index on (company, posting_date) for the filter may help instead" in text
		assert "would not help" not in text

	def test_a_refused_sort_column_keeps_its_verdict_when_nothing_else_is_left(self):
		"""ORDER BY `order` (a MariaDB reserved word): the sort-first recipe is refused for the
		name, and the retry without the sort has no column, so the refusal stands."""
		ev = _ev(fields={**_ALL, "order": F("Data")})
		q = "SELECT name FROM `tabSales Invoice` WHERE docstatus = ? ORDER BY `order`"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(ev))
		assert advice is not None and "is a reserved word in MariaDB" in _text(advice)

	def test_an_expression_sort_column_is_left_out(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? ORDER BY FIELD(status, ?, ?)"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI))
		assert "status" not in advice.columns


# --- fix round 2, M6: cards keep the analyzer's order; the LFR lead makes no order claim ----


def test_a_card_keeps_the_analyzers_most_used_first_order():
	for cols in (["status", "customer"], ["customer", "status"]):
		card = ir.advise_table("tabSales Invoice", cols, evidence_lookup=_lookup(_SI))
		assert list(card.columns) == cols and card.entry["columns"] == cols


def test_the_low_filter_ratio_lead_makes_no_selectivity_claim():
	q = "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ?"
	lead = ir.advise_finding(_explain("Low Filter Ratio", q), evidence_lookup=_lookup(_SI)).lead
	assert "most selective" not in lead and "first" not in lead
	assert lead.startswith("An index on these filter columns lets the database skip most of the rows it now reads")


# --- fix round 3 (review-t12 battery4, c1_diff23) ------------------------------------------


_FS = "Using where; Using filesort"
_SI4 = _ev(fields={**_ALL, "due_date": F("Date"), "is_return": F("Check"), "grand_total": F("Int")})


class TestServedNeedsEvidence:
	"""Item 1: an existing sort-serving index on a Filesort finding was rejected by the
	optimizer unless the query has a LIMIT (round 4, item 3: a capture-time EXPLAIN is no
	evidence)."""

	def test_battery4_401_the_rejected_composite_gives_the_range_recipe(self):
		ev = _with_index(_SI4, ("idx_cd", ["company", "due_date"], False), ("company_index", ["company"], False))
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? AND posting_date BETWEEN ? AND ? ORDER BY due_date"
		row = {"key": "company_index", "possible_keys": "company_index,idx_cd", "Extra": _FS, "rows": 90000}
		advice = ir.advise_finding(_explain("Filesort", q, explain_row=row), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.entry["columns"] == ["company", "posting_date"], _text(advice)

	def test_battery4_402_a_listed_single_sort_index_gives_the_range_recipe(self):
		ev = _with_index(_SI4, ("due_date_index", ["due_date"], False))
		q = "SELECT name FROM `tabSales Invoice` WHERE posting_date BETWEEN ? AND ? ORDER BY due_date"
		row = {"key": None, "possible_keys": "due_date_index", "Extra": _FS, "type": "ALL"}
		advice = ir.advise_finding(_explain("Filesort", q, explain_row=row), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.columns == ("posting_date",), _text(advice)

	@pytest.mark.parametrize("q,row", [
		("SELECT name FROM `tabSales Invoice` WHERE posting_date BETWEEN ? AND ? ORDER BY due_date LIMIT ?", None),
		(
			"SELECT name FROM `tabSales Invoice` WHERE posting_date BETWEEN ? AND ? ORDER BY due_date LIMIT ?",
			{"key": None, "possible_keys": None, "Extra": _FS, "type": "ALL"},
		),
	])
	def test_battery4_403_a_kept_served_verdict_names_the_range_alternative(self, q, row):
		"""Round 4, item 3: only LIMIT keeps the served verdict. A capture-time EXPLAIN that
		does not list the index is no evidence: MariaDB's possible_keys never lists an index
		that only serves the ORDER BY (battery5 [501]), so [403] without LIMIT now gets the
		range recipe (TestServedNeedsLimit)."""
		ev = _with_index(_SI4, ("due_date_index", ["due_date"], False))
		advice = ir.advise_finding(_explain("Filesort", q, explain_row=row), evidence_lookup=_lookup(ev))
		text = _text(advice)
		assert advice.route == ir.ROUTE_NO_CODE, text
		assert "would not help" not in text
		assert 'The index "due_date_index" on table "tabSales Invoice" already serves this filter and sort' in text
		assert "an index on (posting_date) for the filter may help instead" in text

	def test_only_the_main_querys_limit_counts(self):
		assert ir._has_limit("SELECT name FROM `tabX` WHERE a = ? ORDER BY b LIMIT ?")
		assert not ir._has_limit("SELECT name, (SELECT c FROM `tabY` LIMIT 1) AS c FROM `tabX` WHERE a = ? ORDER BY b")

	def test_without_evidence_the_served_verdict_is_not_kept(self):
		ev = _with_index(_SI4, ("idx_cd", ["company", "due_date"], False))
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? AND posting_date > ? ORDER BY due_date"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.entry["columns"] == ["company", "posting_date"]


class TestNotEqualIsNoRange:
	"""Item 1a: !=, <> and NOT never narrow an index."""

	@pytest.mark.parametrize("op", ["!= ?", "<> ?", "NOT IN (?)", "NOT BETWEEN ? AND ?"])
	def test_a_not_comparison_is_left_out(self, op):
		q = f"SELECT name FROM `tabSales Invoice` WHERE company = ? AND status {op}"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI4))
		assert advice.columns == ("company",), _text(advice)
		assert "Optimus left out status (compared only by !=, <> or NOT, which usually matches most of the table's rows)" in _text(advice)

	def test_a_value_side_not_equal_is_a_not_comparison(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? AND ? != status"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI4))
		assert advice.columns == ("company",) and "Optimus left out status (compared only by !=" in _text(advice)

	def test_a_not_comparison_alone_gives_its_shape_no_code(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE status != ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI4))
		assert advice.route == ir.ROUTE_NO_CODE and "A !=, <> or NOT comparison on status" in _text(advice)

	def test_a_not_comparison_on_a_missing_column_names_the_column(self):
		"""c1: i.is_exempt != 1 on a table without that column is a column problem, never
		"could not read"."""
		q = (
			"select sum(i.base_net_amount) from `tabSales Invoice Item` i inner join `tabSales Invoice` s on "
			"i.parent = s.name where s.docstatus = 1 and i.is_exempt != 1 and i.is_zero_rated != 1"
		)
		ev = _ev("Sales Invoice Item", fields={"item_code": F("Link")})
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabSales Invoice Item"), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE and not advice.unknown, _text(advice)
		assert 'has no column "is_exempt"' in _text(advice)

	def test_is_not_null_stays_a_range(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? AND po_no IS NOT NULL"
		assert ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI4)).columns == ("company", "po_no")

	def test_the_sle_not_equal_lookup_gives_no_code(self):
		sle = _with_index(_ev("Stock Ledger Entry", fields={
			"item_code": F("Link"), "warehouse": F("Link"), "voucher_no": F("Dynamic Link"), "is_cancelled": F("Check"),
			"posting_datetime": F("Date"),
		}), ("iwpc", ["item_code", "warehouse", "posting_datetime", "creation"], False))
		q = (
			"SELECT name FROM `tabStock Ledger Entry` WHERE item_code = ? AND warehouse = ? AND voucher_no != ? "
			"AND is_cancelled = ? ORDER BY posting_datetime DESC"
		)
		advice = ir.advise_finding(_explain("Filesort", q, table="tabStock Ledger Entry"), evidence_lookup=_lookup(sle))
		assert advice.route == ir.ROUTE_NO_CODE, _text(advice)


class TestCollapsedIn:
	"""Item 2: Frappe's recorder rewrites IN (?, ?, ?) to IN (?), so IN (?) may be one value."""

	def test_battery4_404_a_collapsed_in_keeps_the_sort_with_a_hedge(self):
		ev = _with_index(_SI4, ("company_index", ["company"], False))
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? AND customer IN (?) ORDER BY posting_date"
		advice = ir.advise_finding(_explain("Filesort", q, explain_row={"type": "ref", "key": "company_index"}), evidence_lookup=_lookup(ev))
		assert advice.entry["columns"] == ["company", "customer", "posting_date"], _text(advice)
		assert "if the IN list on customer has more than one value, the sort stays" in advice.lead

	def test_the_list_view_keeps_creation(self):
		q = (
			"SELECT `tabSales Invoice`.`name` FROM `tabSales Invoice` WHERE `tabSales Invoice`.`company` in (?) "
			"ORDER BY `tabSales Invoice`.`creation` DESC LIMIT ?"
		)
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI4))
		assert advice.entry["columns"] == ["company", "creation"], _text(advice)
		assert "which Optimus never indexes" not in advice.lead

	def test_battery4_407_an_in_on_the_recipe_column_keeps_the_sort(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE status IN (?) ORDER BY posting_date"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI4))
		assert advice.entry["columns"] == ["status", "posting_date"], _text(advice)

	@pytest.mark.parametrize("where", ["docstatus IN (?, ?)", "ifnull(status, ?) IN (?, ?)"])
	def test_battery4_405_406_an_in_outside_the_recipe_never_blocks_the_sort(self, where):
		q = f"SELECT name FROM `tabSales Invoice` WHERE company = ? AND {where} ORDER BY posting_date DESC"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI4))
		assert advice.entry["columns"] == ["company", "posting_date"], _text(advice)
		assert "already sorted" in advice.lead

	def test_a_metadata_sort_column_that_cannot_follow_a_filter_is_named_honestly(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE posting_date > ? ORDER BY creation DESC"
		lead = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI4)).lead
		assert "which Optimus never indexes" not in lead
		assert ir._NARROWS + "the sort column creation can only follow an equality filter column in an index" in lead


_GL4 = _ev("GL Entry", fields={
	"account": F("Link"), "party_type": F("Link"), "party": F("Dynamic Link"), "voucher_type": F("Link"),
	"voucher_no": F("Dynamic Link"), "posting_date": F("Date"), "is_cancelled": F("Check"), "company": F("Link"),
	"cost_center": F("Link"), "debit": F("Int"), "credit": F("Int"), "voucher_detail_no": F("Data"),
}, indexes=[
	("voucher_no_index", ["voucher_no"], False), ("account_index", ["account"], False),
	("voucher_type_voucher_no_index", ["voucher_type", "voucher_no"], False), ("company_index", ["company"], False),
	("cost_center_index", ["cost_center"], False), ("voucher_detail_no_index", ["voucher_detail_no"], False),
])


class TestCapByEvidence:
	"""Item 3: the four kept columns are chosen by evidence, not by name."""

	def test_battery4_412_the_cap_keeps_the_indexed_columns(self):
		q = (
			"SELECT name FROM `tabGL Entry` WHERE `voucher_type`=? AND `voucher_no`=? AND `account`=? AND `cost_center`=? "
			"AND `debit`=? AND `credit`=?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabGL Entry"), evidence_lookup=_lookup(_GL4))
		assert advice.entry["columns"] == ["account", "cost_center", "voucher_no", "voucher_type"], _text(advice)
		assert "Optimus left out credit, debit (an index here holds at most 4 columns)" in _text(advice)

	def test_battery4_413_a_left_out_index_as_selective_as_the_kept_gives_no_code(self):
		"""Round 5: voucher_detail_no (Data) ranks with the kept Link columns, so its own index
		already narrows the rows as well as the capped recipe could, which cannot help."""
		q = (
			"SELECT name FROM `tabGL Entry` WHERE `company`=? AND `account`=? AND `voucher_type`=? AND `voucher_no`=? "
			"AND `voucher_detail_no`=?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabGL Entry"), evidence_lookup=_lookup(_GL4))
		assert advice.route == ir.ROUTE_NO_CODE and not advice.unknown, _text(advice)
		assert (
			'The index "voucher_detail_no_index" on table "tabGL Entry" covers (voucher_detail_no), which this query '
			"compares with known values"
		) in _text(advice)

	def test_without_indexes_check_fields_go_first_out(self):
		ev = _ev(fields={**_ALL, "is_return": F("Check"), "due_date": F("Date"), "territory": F("Link")})
		q = (
			"SELECT name FROM `tabSales Invoice` WHERE is_return = ? AND status = ? AND company = ? AND customer = ? "
			"AND territory = ?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev))
		assert "is_return" not in advice.columns and len(advice.columns) == 4, _text(advice)
		assert "Optimus left out is_return (an index here holds at most 4 columns)" in _text(advice)


class TestJoinProbes:
	"""Item 4: a column the target only feeds into a fixed LEFT JOIN is no index key."""

	def test_battery4_416_a_null_rejecting_where_keeps_the_join_column(self):
		ev = _with_index(_GL4, ("is_cancelled_dummy", ["party"], False))
		q = (
			"SELECT gle.name FROM `tabGL Entry` gle LEFT JOIN `tabAccount` ac ON ac.name = gle.account "
			"WHERE ac.account_type = ? AND gle.is_cancelled = 0"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabGL Entry"), evidence_lookup=_lookup(ev))
		assert "account" in advice.columns, _text(advice)

	def test_the_joined_tables_own_lookup_key_stays(self):
		"""The WHERE names only the FROM table, so the LEFT JOIN stays one; the joined table's
		ON column is the key it is looked up by, never a probe."""
		q = (
			"SELECT c.name FROM `tabUOM` c LEFT JOIN `tabItem` i ON i.sales_uom = c.name "
			"WHERE c.enabled = ? AND c.must_be_whole_number = ?"
		)
		assert ir._join_probes(q, frozenset({"tabItem", "i"}), "tabitem") == set()
		q2 = (
			"SELECT i.name FROM `tabItem` i LEFT JOIN `tabUOM` c ON c.name = i.sales_uom "
			"WHERE i.disabled = ?"
		)
		assert ir._join_probes(q2, frozenset({"tabItem", "i"}), "tabitem") == {"sales_uom"}

	def test_c1_31_a_middle_tables_probe_column_is_no_key(self):
		ev = _ev("Bank Transaction", fields={"bank_account": F("Link"), "date": F("Date")})
		q = (
			"SELECT btp.name FROM `tabBank Transaction Payments` btp LEFT JOIN `tabBank Transaction` bt ON bt.name=btp.parent "
			"LEFT JOIN `tabBank Account` ba ON ba.name=bt.bank_account WHERE btp.payment_document = ? AND bt.docstatus = 1"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabBank Transaction"), evidence_lookup=_lookup(ev))
		assert advice is None or "bank_account" not in advice.columns, _text(advice)

	def test_a_derived_table_left_chain_is_read_too(self):
		ev = _ev("Bank Transaction", fields={"bank_account": F("Link"), "date": F("Date")})
		q = (
			"SELECT total FROM ( SELECT btp.name AS total FROM `tabBank Transaction Payments` btp LEFT JOIN "
			"`tabBank Transaction` bt ON bt.name=btp.parent LEFT JOIN `tabBank Account` ba ON ba.name=bt.bank_account "
			"WHERE btp.payment_document = ? AND bt.docstatus = 1 ) temp WHERE total = ?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabBank Transaction"), evidence_lookup=_lookup(ev))
		assert advice is None or "bank_account" not in advice.columns, _text(advice)


def test_battery4_410_the_temporary_table_caveat_is_honest():
	q = (
		"SELECT customer, SUM(grand_total) FROM `tabSales Invoice` WHERE company = ? GROUP BY customer "
		"ORDER BY SUM(grand_total) DESC"
	)
	text = _text(ir.advise_finding(_explain("Temporary Table", q), evidence_lookup=_lookup(_SI4)))
	assert "would not remove the temporary table" in text and "would add nothing" not in text


class TestPrimaryKeyAndParent:
	"""Item 6: a lookup by name or by a child row's parent already has its index."""

	def test_battery4_415_a_name_lookup_gives_no_code(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE `name`=? AND `company`=?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI4))
		assert advice.route == ir.ROUTE_NO_CODE and not advice.unknown
		assert "The query finds its row by name, the primary key, so no other index can help." in _text(advice)

	def test_battery4_414_a_child_row_lookup_by_parent_gives_no_code(self):
		ev = _ev("Sales Invoice Item", fields={"item_code": F("Link"), "warehouse": F("Link")},
			extra_types={"parent": "varchar", "parenttype": "varchar", "parentfield": "varchar"},
			indexes=[("parent", ["parent"], False)])
		q = "SELECT name FROM `tabSales Invoice Item` WHERE `parent`=? AND `item_code`=?"
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabSales Invoice Item"), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE, _text(advice)
		# round 4, item 2: the text names the real index, not a claim about every child table
		assert 'finds its rows by parent, which the index "parent" on table "tabSales Invoice Item" serves' in _text(advice)

	@pytest.mark.parametrize("q", [
		"select sum(debit_in_account_currency) - sum(credit_in_account_currency) from `tabJournal Entry Account` "
		"where parent=? and account=? and (reference_type is null or reference_type = '')",
		"select * from `tabJournal Entry Account` where account = ? and docstatus = 1 and parent = ? and "
		"(reference_type is null or reference_type in ('', 'Sales Order', 'Purchase Order'))",
		"select debit, credit from `tabJournal Entry Account` where account = ? and party=? and docstatus = 1 and parent = ? "
		'and (reference_type is null or reference_type in ("", "Sales Order", "Purchase Order"))',
	])
	def test_c1_41_42_45_journal_entry_account_by_parent(self, q):
		"""Round 4, item 2: the evidence carries the parent index MariaDB gives every child
		table; without it the parent rule no longer fires (TestParentIndexMustExist)."""
		ev = _ev("Journal Entry Account", fields={"account": F("Link"), "party": F("Dynamic Link"), "reference_type": F("Link")},
			extra_types={"parent": "varchar", "parenttype": "varchar", "parentfield": "varchar"},
			indexes=[("parent", ["parent"], False)])
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabJournal Entry Account"), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE and "by parent" in _text(advice), _text(advice)

	def test_a_name_not_in_subquery_is_no_lookup(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE name NOT IN (SELECT parent FROM `tabX`) AND company = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI4))
		assert "primary key" not in _text(advice)


# --- fix round 4: keys need a value, the parent index must exist, served evidence, the cap ---


_CHILD_TYPES = {"parent": "varchar", "parenttype": "varchar", "parentfield": "varchar"}
_PR = _ev("Purchase Receipt", fields={"supplier": F("Link"), "posting_date": F("Date")})
_PRI = _ev("Purchase Receipt Item", fields={"item_code": F("Link"), "base_amount": F("Int")},
	extra_types=_CHILD_TYPES, indexes=[("parent", ["parent"], False)])
_MRI = _ev("Material Request Item", fields={
	"item_code": F("Link"), "warehouse": F("Link"), "stock_qty": F("Int"), "ordered_qty": F("Int"),
}, extra_types=_CHILD_TYPES, indexes=[("parent", ["parent"], False)])
_PR_JOIN = (
	"SELECT COUNT(pr_item.base_amount) FROM `tabPurchase Receipt Item` pr_item, `tabPurchase Receipt` pr "
	"WHERE pr.supplier = ? AND pr.posting_date BETWEEN ? AND ? AND pr_item.docstatus = 1 AND pr_item.parent = pr.name"
)


class TestKeyLookupNeedsAValue:
	"""Round 4, item 1: name or parent compared with another table's column is a join
	condition, never a key lookup; only a value (?, a literal, an IN list or subquery) is."""

	def test_the_purchase_receipt_comma_join_gets_its_filter_index(self):
		advice = ir.advise_finding(
			_explain("Full Table Scan", _PR_JOIN, table="tabPurchase Receipt"), evidence_lookup=_lookup(_PR, _PRI),
		)
		assert advice.route == ir.ROUTE_ENSURE_INDEXES, _text(advice)
		assert advice.entry["columns"] == ["supplier", "posting_date"]

	def test_the_material_request_item_comma_join_gets_its_filter_index(self):
		q = (
			"select sum(mr_item.stock_qty - mr_item.ordered_qty) from `tabMaterial Request Item` mr_item, "
			"`tabMaterial Request` mr where mr_item.item_code=? and mr_item.warehouse=? and "
			"mr.material_request_type = 'Material Issue' and mr_item.stock_qty > mr_item.ordered_qty and "
			"mr_item.parent=mr.name and mr.status!='Stopped' and mr.docstatus=1"
		)
		advice = ir.advise_finding(
			_explain("Full Table Scan", q, table="tabMaterial Request Item"), evidence_lookup=_lookup(_MRI),
		)
		assert advice.route == ir.ROUTE_ENSURE_INDEXES, _text(advice)
		assert advice.entry["columns"] == ["item_code", "warehouse"]

	def test_name_on_the_left_of_a_join_condition_is_no_lookup(self):
		q = (
			"SELECT pr.name FROM `tabPurchase Receipt` pr, `tabPurchase Receipt Item` pr_item "
			"WHERE pr.name = pr_item.parent AND pr.supplier = ?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabPurchase Receipt"), evidence_lookup=_lookup(_PR))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.columns == ("supplier",), _text(advice)

	def test_a_join_condition_next_to_a_value_lookup_still_finds_the_key(self):
		q = (
			"SELECT pr.name FROM `tabPurchase Receipt` pr, `tabPurchase Receipt Item` pr_item "
			"WHERE pr.name = pr_item.parent AND pr.name = ? AND pr.supplier = ?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabPurchase Receipt"), evidence_lookup=_lookup(_PR))
		assert "by name, the primary key" in _text(advice)

	@pytest.mark.parametrize("where,what", [
		("`name`=? AND `company`=?", "its row"),
		("? = `name` AND `company`=?", "its row"),
		("`name` = 'SINV-0001' AND `company`=?", "its row"),
		("`name` = 7 AND `company`=?", "its row"),
		("`name` IN (?, ?) AND `company`=?", "its rows"),
		("`name` IN ('a', 'b') AND `company`=?", "its rows"),
		("`name` IN (?) AND `company`=?", "its rows"),
		("`name` IN %(names)s AND `company`=?", "its rows"),
	])
	def test_a_value_lookup_by_name_stays_a_lookup(self, where, what):
		q = f"SELECT name FROM `tabSales Invoice` WHERE {where}"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI4))
		assert f"The query finds {what} by name, the primary key" in _text(advice)

	@pytest.mark.parametrize("compare,what", [("IN", "its rows"), ("=", "its row")])
	def test_a_name_compared_with_a_subquery_stays_a_lookup(self, compare, what):
		q = (
			f"SELECT si.name FROM `tabSales Invoice` si WHERE si.name {compare} (SELECT sii.parent FROM "
			"`tabSales Invoice Item` sii WHERE sii.item_code = ?) AND si.company = ?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI4))
		assert f"The query finds {what} by name, the primary key" in _text(advice)

	def test_the_most_selective_value_comparison_names_the_rows(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE `name` IN (?, ?) AND `name` = ? AND `company` = ?"
		assert "The query finds its row by name" in _text(ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI4)))

	@pytest.mark.parametrize("where", [
		"`name` IN (?, `customer`) AND `company`=?",
		"`name` = `po_no` AND `company`=?",
		"`name` IS NULL AND `company`=?",
		"`name` = concat(?, ?) AND `company`=?",
		# a double-quoted token reads as a name (a Postgres identifier), as the scan reads it everywhere
		'`name` IN ("a", "b") AND `company`=?',
	])
	def test_name_compared_with_no_plain_value_is_no_lookup(self, where):
		q = f"SELECT name FROM `tabSales Invoice` WHERE {where}"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI4))
		assert "primary key" not in _text(advice)

	def test_a_value_side_is_read_on_either_side_and_in_a_list(self):
		quals = frozenset({"tabSales Invoice"})
		q = "SELECT name FROM `tabSales Invoice` WHERE ? = x.parent AND ? = name AND parent IN (?, 12)"
		assert ir._scan_where(q, [("WHERE", "name"), ("WHERE", "parent")], quals)[2] == {"name": "eq", "parent": "in"}
		q2 = 'SELECT "name" FROM "tabSales Invoice" WHERE "name" IN ("a", "b") AND "parent" = "x"."parent"'
		assert ir._scan_where(q2, [("WHERE", "name"), ("WHERE", "parent")], quals)[2] == {}

	@pytest.mark.parametrize("pin", ["mp.name = ?", "? = mp.name", "mp.name in ?", "mp.name IN (?, ?)"])
	def test_a_join_column_pinned_to_a_value_carries_the_value_over(self, pin):
		"""c1 Mode of Payment Account: mp.name = ? fixes mpa.parent = mp.name too (equality
		propagation), so mpa is found by parent and an index on company cannot help."""
		ev = _ev("Mode of Payment Account", fields={"company": F("Link"), "default_account": F("Link")},
			extra_types=_CHILD_TYPES, indexes=[("parent", ["parent"], False)])
		q = (
			"select mpa.default_account, mpa.parent, mp.type as type from `tabMode of Payment Account` mpa,"
			f"`tabMode of Payment` mp where mpa.parent = mp.name and mpa.company = ? and mp.enabled = 1 and {pin}"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabMode of Payment Account"), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE and "by parent" in _text(advice), _text(advice)

	@pytest.mark.parametrize("pin", [
		"(mp.name = ? OR mp.enabled = 0)", "mp.name > ?", "mp.name = ? + 1", "? = mp.name + 1", "mp.name = mpa.default_account",
		"ifnull(mp.name, ?) = ?",
	])
	def test_a_join_column_that_no_piece_pins_is_no_lookup(self, pin):
		ev = _ev("Mode of Payment Account", fields={"company": F("Link"), "default_account": F("Link")},
			extra_types=_CHILD_TYPES, indexes=[("parent", ["parent"], False)])
		q = (
			"select mpa.default_account from `tabMode of Payment Account` mpa, `tabMode of Payment` mp "
			f"where mpa.parent = mp.name and mpa.company = ? and {pin}"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabMode of Payment Account"), evidence_lookup=_lookup(ev))
		assert "by parent" not in _text(advice), _text(advice)

	def test_only_an_equality_sign_joins_a_value_to_the_reference(self):
		"""Direct _valued checks: a value before the reference counts only across = or <=>."""
		assert ir._valued(["?", "=", "name"], 2, 2, {})
		assert ir._valued(["?", "<=>", "name"], 2, 2, {})
		assert not ir._valued(["?", ",", "name", "is", "null"], 2, 2, {})
		assert not ir._valued(["?", ">", "name"], 2, 2, {})

	def test_the_child_side_of_the_join_is_no_parent_lookup(self):
		advice = ir.advise_finding(
			_explain("Full Table Scan", _PR_JOIN, table="tabPurchase Receipt Item"), evidence_lookup=_lookup(_PRI),
		)
		assert advice is None or "by parent" not in _text(advice)


class TestParentIndexMustExist:
	"""Round 4, item 2: Frappe adds index parent(parent) to a child table on MariaDB only
	(mariadb/schema.py), never on Postgres (postgres/schema.py); the real index list decides."""

	_Q = "SELECT name FROM `tabSales Invoice Item` WHERE `parent`=? AND `item_code`=?"

	def _child(self, dialect="mariadb", indexes=()):
		return _ev("Sales Invoice Item", fields={"item_code": F("Link"), "warehouse": F("Link")},
			extra_types=_CHILD_TYPES, dialect=dialect, indexes=indexes)

	@pytest.mark.parametrize("dialect", ["mariadb", "postgres"])
	def test_without_a_parent_index_the_filter_gets_code(self, dialect):
		"""battery5 [505]: a Postgres child table has no parent index."""
		q = self._Q if dialect == "mariadb" else 'SELECT "name" FROM "tabSales Invoice Item" WHERE "parent" = ? AND "item_code" = ?'
		advice = ir.advise_finding(
			_explain("Full Table Scan", q, table="tabSales Invoice Item"), evidence_lookup=_lookup(self._child(dialect)),
		)
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.columns == ("item_code",), _text(advice)
		assert "by parent" not in _text(advice)

	@pytest.mark.parametrize("dialect,index", [
		("mariadb", ("parent", ["parent"], False)),
		("postgres", ("idx_sii_parent", ["parent"], False)),
		("mariadb", ("parent_item", ["parent", "item_code"], False)),
	])
	def test_a_real_parent_index_gives_no_code_and_is_named(self, dialect, index):
		advice = ir.advise_finding(
			_explain("Full Table Scan", self._Q, table="tabSales Invoice Item"),
			evidence_lookup=_lookup(self._child(dialect, [index])),
		)
		assert advice.route == ir.ROUTE_NO_CODE and not advice.unknown, _text(advice)
		assert (
			f'The query finds its rows by parent, which the index "{index[0]}" on table "tabSales Invoice Item" '
			"serves, and one parent has only a few rows, so no other index can help."
		) in _text(advice)

	def test_a_table_without_parenttype_is_no_child_table(self):
		"""Custom DocPerm has a parent column (the DocType) and no parenttype: one parent may
		have many rows, so even a parent index makes no key lookup."""
		ev = _ev("Custom DocPerm", fields={"role": F("Link")}, extra_types={"parent": "varchar"},
			indexes=[("parent", ["parent"], False)])
		q = "SELECT name FROM `tabCustom DocPerm` WHERE `parent`=? AND `role`=?"
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabCustom DocPerm"), evidence_lookup=_lookup(ev))
		assert "by parent" not in _text(advice), _text(advice)

	def test_an_index_that_only_contains_parent_is_no_parent_index(self):
		advice = ir.advise_finding(
			_explain("Full Table Scan", self._Q, table="tabSales Invoice Item"),
			evidence_lookup=_lookup(self._child("mariadb", [("item_parent", ["item_code", "parent"], False)])),
		)
		assert "by parent" not in _text(advice)


def test_a_served_verdict_needs_the_sort_in_the_refused_recipe():
	"""Round 4, item 5 (c1): the JE join drops the unqualified ORDER BY posting_date, so the
	refused recipe is (company) alone, and company_index never serves the sort."""
	ev = _with_index(_ev("Journal Entry", fields={"company": F("Link"), "posting_date": F("Date")}),
		("company_index", ["company"], False))
	q = (
		"SELECT p.name, p.posting_date FROM `tabJournal Entry` p, `tabJournal Entry Account` c WHERE p.name = c.parent "
		"and p.company=? and c.account=? and c.docstatus < 2 order by posting_date desc limit 1"
	)
	advice = ir.advise_finding(_explain("Filesort", q, table="tabJournal Entry"), evidence_lookup=_lookup(ev))
	assert "already serves this filter and sort" not in _text(advice) and "remove the sort" not in _text(advice)


def test_a_served_verdict_stands_for_an_index_that_serves_the_whole_sort():
	"""c1 SLE: Optimus's recipe holds four columns and leaves out creation, yet ERPNext's
	(item_code, warehouse, posting_datetime, creation) returns the rows in the order of the
	whole ORDER BY, so with LIMIT the served verdict stands (round 4, item 5)."""
	sle = _with_index(_ev("Stock Ledger Entry", fields={
		"item_code": F("Link"), "warehouse": F("Link"), "is_cancelled": F("Check"), "posting_datetime": F("Date"),
	}), ("iwpc", ["item_code", "warehouse", "posting_datetime", "creation"], False))
	q = (
		"select qty_after_transaction from `tabStock Ledger Entry` where item_code=? and warehouse=? and is_cancelled=0 "
		"order by posting_datetime desc, creation desc limit 1"
	)
	advice = ir.advise_finding(_explain("Filesort", q, table="tabStock Ledger Entry"), evidence_lookup=_lookup(sle))
	assert 'The index "iwpc" on table "tabStock Ledger Entry" already serves this filter and sort' in _text(advice)


def test_a_unique_index_the_filter_fixes_serves_any_sort():
	"""At most one row comes back, so it is in any order."""
	ev = _with_index(_SI4, ("po_customer", ["po_no", "customer"], True))
	q = "SELECT name FROM `tabSales Invoice` WHERE po_no = ? AND customer = ? AND company = ? ORDER BY due_date LIMIT 1"
	advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(ev))
	assert 'The index "po_customer" on table "tabSales Invoice" already serves this filter and sort' in _text(advice)


class TestServedNeedsLimit:
	"""Round 4, item 3: MariaDB possible_keys never lists an index that only serves ORDER BY,
	and a Postgres plan node names only the index it used, so a capture-time EXPLAIN is no
	evidence; only LIMIT keeps the served verdict, and otherwise the range recipe names the
	sort-serving index."""

	def test_battery5_501_a_mariadb_plan_without_the_sort_index_gives_the_range_recipe(self):
		ev = _with_index(_SI4, ("due_date_index", ["due_date"], False))
		q = "SELECT name FROM `tabSales Invoice` WHERE posting_date BETWEEN ? AND ? ORDER BY due_date"
		row = {"type": "ALL", "possible_keys": None, "key": None, "Extra": _FS}
		advice = ir.advise_finding(_explain("Filesort", q, explain_row=row), evidence_lookup=_lookup(ev))
		text = _text(advice)
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.columns == ("posting_date",), text
		assert (
			'The index "due_date_index" on table "tabSales Invoice" can already remove the sort for this filter, '
			"but the captured query did not use it that way"
		) in text
		assert "may have been added since" not in text

	def test_battery5_502_a_postgres_plan_node_gives_the_range_recipe(self):
		ev = _with_index(dataclasses.replace(_SI4, dialect="postgres"), ("idx_cd", ["company", "due_date"], False))
		q = (
			'SELECT "name" FROM "tabSales Invoice" WHERE "company" = ? AND "posting_date" BETWEEN ? AND ? '
			'ORDER BY "due_date"'
		)
		advice = ir.advise_finding(_explain("Filesort", q, explain_row={"Node Type": "Seq Scan"}), evidence_lookup=_lookup(ev))
		assert advice.entry["columns"] == ["company", "posting_date"], _text(advice)
		assert 'The index "idx_cd" on table "tabSales Invoice" can already remove the sort' in _text(advice)

	def test_a_capture_time_explain_is_no_evidence(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE posting_date > ? ORDER BY due_date"
		assert not ir._served_evidence(q)
		assert ir._served_evidence(q + " LIMIT ?")

	def test_a_temporary_table_names_its_grouping_index(self):
		ev = _with_index(_SI4, ("idx_cc", ["company", "customer"], False))
		q = "SELECT customer, COUNT(name) FROM `tabSales Invoice` WHERE company = ? AND posting_date > ? GROUP BY customer"
		advice = ir.advise_finding(_explain("Temporary Table", q), evidence_lookup=_lookup(ev))
		assert advice.entry["columns"] == ["company", "posting_date"], _text(advice)
		assert 'The index "idx_cc" on table "tabSales Invoice" can already remove the temporary table' in _text(advice)

	def test_a_retry_refused_by_the_same_index_names_it_once(self):
		ev = _with_index(_SI, ("posting_date_index", ["posting_date"], False))
		q = "SELECT name FROM `tabSales Invoice` WHERE docstatus = ? AND posting_date <= ? ORDER BY posting_date"
		text = _text(ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(ev)))
		assert text.count('"posting_date_index"') == 1, text


_SI_CAP = _ev(fields={
	**_ALL, "project": F("Link"), "cost_center": F("Link"), "territory": F("Link"), "voucher_ref": F("Link"),
	"due_date": F("Date"),
})


class TestCapRefusesOnlyForAUniqueIndex:
	"""Round 4, items 4 and 5, refined in round 5: leaving out every column of an index the
	query fixes gives no code when that index is unique, or when its lead column ranks by
	field type at least as well as the weakest kept column (the cap's own rank); a Select
	index left out behind Link columns keeps the capped recipe. The verdict never claims a
	sort."""

	def test_battery5_507_a_left_out_select_index_keeps_the_capped_recipe(self):
		ev = _with_index(_SI_CAP, *((f"{c}_index", [c], False) for c in ("status", "customer", "company", "project", "cost_center")))
		q = "SELECT name FROM `tabSales Invoice` WHERE status = ? AND customer = ? AND company = ? AND project = ? AND cost_center = ?"
		advice = ir.advise_finding(_explain("Low Filter Ratio", q), evidence_lookup=_lookup(ev))
		assert advice.entry["columns"] == ["company", "cost_center", "customer", "project"], _text(advice)
		assert "Optimus left out status (an index here holds at most 4 columns)" in _text(advice)
		assert "already finds these rows" not in _text(advice)

	def _unique_cut(self):
		return _with_index(
			_SI_CAP, ("idx_cc", ["company", "customer"], False), ("project_index", ["project"], False),
			("territory_index", ["territory"], False), ("voucher_ref", ["voucher_ref"], True),
		)

	_Q = "SELECT name FROM `tabSales Invoice` WHERE company = ? AND customer = ? AND project = ? AND territory = ? AND voucher_ref = ?"

	def test_a_left_out_unique_index_gives_no_code(self):
		advice = ir.advise_finding(_explain("Full Table Scan", self._Q), evidence_lookup=_lookup(self._unique_cut()))
		assert advice.route == ir.ROUTE_NO_CODE and not advice.unknown, _text(advice)
		assert (
			'The unique index "voucher_ref" on table "tabSales Invoice" already finds these rows by (voucher_ref)'
		) in _text(advice)

	def test_a_left_out_unique_index_refuses_whatever_its_type(self):
		"""(a): a unique Select index ranks below the kept Link columns, yet returns one row."""
		ev = _with_index(
			_SI_CAP, ("idx_cc", ["company", "customer"], False), ("idx_pc", ["project", "cost_center"], False),
			("status_unique", ["status"], True),
		)
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? AND customer = ? AND project = ? AND cost_center = ? AND status = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE, _text(advice)
		assert 'The unique index "status_unique"' in _text(advice)

	def test_a_left_out_index_as_selective_as_the_weakest_kept_column_refuses(self):
		"""(b): due_date and posting_date (Date) tie; posting_date is left out, and the kept
		due_date ranks no better, so posting_date_index narrows the rows as well."""
		ev = _with_index(
			_SI_CAP, ("idx_cc", ["company", "customer"], False), ("project_index", ["project"], False),
			("posting_date_index", ["posting_date"], False), ("due_date_index", ["due_date"], False),
		)
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? AND customer = ? AND project = ? AND posting_date = ? AND due_date = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE, _text(advice)
		assert 'The index "posting_date_index" on table "tabSales Invoice" covers (posting_date)' in _text(advice)

	def test_the_left_out_indexs_lead_column_decides(self):
		"""(status, territory) is left out whole; its lead status is a Select field, which ranks
		below the kept Link columns, so the capped recipe stays."""
		ev = _with_index(
			_SI_CAP, ("idx_cc", ["company", "customer"], False), ("idx_pc", ["project", "cost_center"], False),
			("idx_st", ["status", "territory"], False),
		)
		q = (
			"SELECT name FROM `tabSales Invoice` WHERE company = ? AND customer = ? AND project = ? AND cost_center = ? "
			"AND status = ? AND territory = ?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev))
		assert advice.entry["columns"] == ["company", "cost_center", "customer", "project"], _text(advice)
		assert "Optimus left out status, territory (an index here holds at most 4 columns)" in _text(advice)

	def test_a_left_out_column_that_is_no_field_ranks_last(self):
		"""creation has no DocField, so the cap's rank puts it after every typed column, and
		Frappe's creation index left out behind Link columns keeps the capped recipe."""
		ev = _with_index(
			_SI_CAP, ("idx_cc", ["company", "customer"], False), ("idx_pc", ["project", "cost_center"], False),
			("creation", ["creation"], False),
		)
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? AND customer = ? AND project = ? AND cost_center = ? AND creation = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(ev))
		assert advice.entry["columns"] == ["company", "cost_center", "customer", "project"], _text(advice)

	def test_the_real_gl_voucher_detail_no_lookup_gives_no_code_and_no_sort_claim(self):
		"""c1: the left-out Data column voucher_detail_no has its own index, which narrows the rows
		as well as the Link columns a new index would keep."""
		q = (
			"select name, posting_date from `tabGL Entry` where company=? and account=? and voucher_type=? and "
			"voucher_no=? and voucher_detail_no=? and is_cancelled = 0 order by posting_date desc limit 1"
		)
		for ftype in ("Full Table Scan", "Filesort"):
			advice = ir.advise_finding(_explain(ftype, q, table="tabGL Entry"), evidence_lookup=_lookup(_GL4))
			text = _text(advice)
			assert advice.route == ir.ROUTE_NO_CODE and advice.served_by == "", text
			assert '"voucher_detail_no_index"' in text and "already serves this filter and sort" not in text

	def test_a_cap_found_verdict_never_claims_the_sort(self):
		"""c1: GL voucher_detail_no = ? ... ORDER BY posting_date DESC LIMIT 1 read "already
		serves this filter and sort" from the cap's index."""
		q = self._Q + " ORDER BY due_date DESC LIMIT 1"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(self._unique_cut()))
		text = _text(advice)
		assert advice.route == ir.ROUTE_NO_CODE and advice.served_by == "", text
		assert "already serves this filter and sort" not in text and "in order" not in text
		assert 'The unique index "voucher_ref"' in text
		# the first verdict gave no code, so no sort-first recipe was cut (bounded corrective R2)
		assert "would need every sort column" not in text


class TestSortColumnsOfTheOnlyTable:
	"""Round 4, item 6: a WHERE subquery makes sql_metadata drop every unqualified column, the
	outer ORDER BY or GROUP BY too; the only table of the main FROM owns them."""

	def test_battery5_516_the_qb_subquery_keeps_its_sort(self):
		q = (
			"SELECT name FROM `tabSales Invoice` WHERE name IN (SELECT parent FROM `tabSales Invoice Item` "
			"WHERE item_code = ?) AND customer = ? ORDER BY posting_date"
		)
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI4))
		assert advice.entry["columns"] == ["customer", "posting_date"], _text(advice)

	def test_a_scalar_subquery_keeps_the_sort_and_its_range_note(self):
		q = (
			"SELECT `name` FROM `tabSales Invoice` WHERE `customer`=? AND `grand_total` > (SELECT AVG(`amount`) "
			"FROM `tabSales Invoice Item`) ORDER BY `posting_date` DESC LIMIT ?"
		)
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI4))
		assert advice.entry["columns"] == ["customer", "posting_date"], _text(advice)
		assert "the range filter on grand_total cannot also use this index" in _text(advice)

	def test_a_merged_sort_column_that_is_also_the_range_keeps_its_range(self):
		"""The merged sort columns come after the merged filters, so posting_date reads as the
		range it also is; the IN list of several values cannot serve the sort, and the range
		stays in the recipe."""
		q = (
			"SELECT `name` FROM `tabSales Invoice` WHERE `company` IN (?, ?) AND `posting_date` > ? AND EXISTS "
			"(SELECT `name` FROM `tabSales Invoice Item`) ORDER BY `posting_date` DESC"
		)
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI4))
		assert advice.entry["columns"] == ["company", "posting_date"], _text(advice)

	@pytest.mark.parametrize("order,columns", [
		("account, cost_center", ["voucher_no", "account", "cost_center"]),
		("cost_center, account", ["voucher_no", "cost_center", "account"]),
	])
	def test_a_sort_column_named_like_an_sql_word_keeps_the_clause_order(self, order, columns):
		"""c1: sql_metadata drops a bare account in ORDER BY too; merged, the sort columns follow
		the clause's order, so the index returns the rows in that order."""
		ev = _ev("GL Entry", fields={"voucher_no": F("Link"), "account": F("Link"), "cost_center": F("Link")})
		q = f"select account, debit, credit, cost_center from `tabGL Entry` where voucher_no=? order by {order}"
		advice = ir.advise_finding(_explain("Filesort", q, table="tabGL Entry"), evidence_lookup=_lookup(ev))
		assert advice.entry["columns"] == columns, _text(advice)

	def test_a_group_by_of_the_only_table_is_merged(self):
		q = (
			"SELECT `customer`, COUNT(`name`) FROM `tabSales Invoice` WHERE `company`=? AND EXISTS (SELECT `name` "
			"FROM `tabSales Invoice Item` WHERE `item_code`=?) GROUP BY `customer`"
		)
		advice = ir.advise_finding(_explain("Temporary Table", q), evidence_lookup=_lookup(_SI4))
		assert advice.entry["columns"] == ["company", "customer"], _text(advice)

	@pytest.mark.parametrize("q", [
		# another table in the main FROM could own the unqualified sort column
		"SELECT si.name FROM `tabSales Invoice` si JOIN `tabCustomer` c ON c.name = si.customer WHERE si.company = ? "
		"ORDER BY posting_date",
		# a UNION's ORDER BY sorts the union's rows
		"SELECT name FROM `tabSales Invoice` WHERE company = ? AND EXISTS (SELECT name FROM `tabCustomer`) "
		"UNION ALL SELECT name FROM `tabCustomer` ORDER BY posting_date",
		# the target only appears in a subquery
		"SELECT name FROM `tabCustomer` WHERE name IN (SELECT customer FROM `tabSales Invoice` WHERE company = ?) "
		"ORDER BY posting_date",
	])
	def test_a_sort_column_another_table_could_own_is_not_merged(self, q):
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI4))
		assert advice is None or "posting_date" not in advice.columns, _text(advice)

	def test_only_whole_bare_items_of_this_table_are_merged(self):
		names = {"posting_date": "posting_date", "due_date": "due_date", "status": "status", "company": "company"}
		quals = frozenset({"tabSales Invoice", "si"})
		q = (
			"SELECT si.name FROM `tabSales Invoice` si WHERE si.company IN (SELECT name FROM `tabCompany`) ORDER BY "
			"si.posting_date DESC, x.due_date, lower(status), status + 1, `company` ASC, nosuch"
		)
		assert ir._clause_columns(q, "order", quals, names) == ["posting_date", "company"]
		assert ir._clause_columns(q, "group", quals, names) == []

	def test_an_aggregated_sort_column_is_not_merged(self):
		"""As the parser's own ORDER BY columns (P9b): the query sorts by the aggregate, so the
		column is no sort column at all, not one left out."""
		q = (
			"SELECT customer, SUM(grand_total) AS grand_total FROM `tabSales Invoice` WHERE company = ? AND EXISTS "
			"(SELECT name FROM `tabCompany`) GROUP BY customer ORDER BY grand_total DESC"
		)
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI4))
		assert advice.columns == ("company",) and "left out grand_total" not in _text(advice), _text(advice)


# --- bounded corrective (after the final T12 review): R1 Check wording, R2 partial sort, R3 sort only ---


_SI_R = _with_index(
	_ev(fields={**_ALL, "is_return": F("Check"), "disabled": F("Check"), "due_date": F("Date")}),
	("posting_date_index", ["posting_date"], False),
)


class TestCheckWordingIsHedged:
	"""R1: Optimus cannot see how a Check field's values are spread, so the Check texts say
	"usually" and name the index that helps a query for the rare value. The verdict stays."""

	def test_the_check_rule_names_the_index_for_the_rare_value(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE is_return = ? AND posting_date BETWEEN ? AND ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI_R))
		text = _text(advice)
		assert advice.route == ir.ROUTE_NO_CODE and not advice.unknown, text
		assert (
			'The index "posting_date_index" on table "tabSales Invoice" already starts with (posting_date), and '
			"is_return is a Check field, which usually matches most of the table's rows, so Optimus gives no index "
			"code; if this query looks for the rare value, an index on (is_return, posting_date) can help."
		) in text
		assert "would not help" not in text and "too many rows" not in text

	def test_the_plural_check_rule(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE is_return = ? AND disabled = ? AND posting_date > ?"
		text = _text(ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI_R)))
		assert (
			"disabled, is_return are Check fields, which usually match most of the table's rows, so Optimus gives no "
			"index code; if this query looks for the rare values, an index on (disabled, is_return, posting_date) can help."
		) in text

	def test_the_purchase_invoice_on_hold_lookup(self):
		ev = _with_index(_ev("Purchase Invoice", fields={"on_hold": F("Check"), "release_date": F("Date")}),
			("release_date_index", ["release_date"], False))
		q = "select name from `tabPurchase Invoice` where on_hold = 1 and release_date IS NOT NULL and release_date > CURDATE()"
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabPurchase Invoice"), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE, _text(advice)
		assert "if this query looks for the rare value, an index on (on_hold, release_date) can help" in _text(advice)

	def test_a_shape_with_a_check_field_never_says_would_not_help(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE is_return = ? AND customer LIKE ?"
		text = _text(ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI_R)))
		assert "The cost comes from the shape of the filter: a LIKE on customer" in text
		assert "an index on (is_return) can help. So Optimus gives no index code." in text
		assert "would not help" not in text

	def test_a_shape_alone_keeps_its_verdict(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE customer LIKE ?"
		text = _text(ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI_R)))
		assert "So an index would not help, and Optimus gives no index code." in text

	def test_a_not_comparison_is_hedged_too(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE status != ?"
		text = _text(ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI_R)))
		assert (
			"A !=, <> or NOT comparison on status usually matches most of the table's rows; if the rows this query "
			"looks for are rare, an index on (status) can help."
		) in text
		assert "would not help" not in text


_GL_R = _ev("GL Entry", fields={
	"voucher_type": F("Link"), "voucher_no": F("Dynamic Link"), "account": F("Link"), "debit": F("Int"), "credit": F("Int"),
}, indexes=[("voucher_type_voucher_no_index", ["voucher_type", "voucher_no"], False)])
_SLE_R = _ev("Stock Ledger Entry", fields={
	"voucher_type": F("Link"), "voucher_no": F("Dynamic Link"), "item_code": F("Link"), "warehouse": F("Link"),
	"actual_qty": F("Int"),
}, indexes=[("voucher_no_voucher_type_index", ["voucher_no", "voucher_type"], False)])


class TestPartialSortRecipe:
	"""R2: a recipe that keeps only some sort or group columns never returns the rows in the
	query's order, so the recipe without the sort is the advice, and no lead says the rows
	come back sorted or the grouping reads the index."""

	@pytest.mark.parametrize("ev,q,index", [
		(_GL_R, "select account, debit, credit from `tabGL Entry` where voucher_type=? and voucher_no=? "
			"order by account asc, debit asc, credit asc", "voucher_type_voucher_no_index"),
		(_SLE_R, "select item_code, warehouse, actual_qty from `tabStock Ledger Entry` where voucher_type = ? and "
			"voucher_no = ? order by item_code, warehouse, actual_qty", "voucher_no_voucher_type_index"),
	])
	def test_a_capped_sort_gives_the_filter_verdict(self, ev, q, index):
		advice = ir.advise_finding(_explain("Filesort", q, table=ev.table), evidence_lookup=_lookup(ev))
		text = _text(advice)
		assert advice.route == ir.ROUTE_NO_CODE, text
		assert f'The index "{index}" on table "{ev.table}" already starts with' in text
		assert "already sorted" not in text
		assert "An index that returns these rows in the query's order would need every sort column (" in text
		assert ") here, so the sort stays." in text or "here, so the sort stays." in text

	def test_the_note_names_the_sort_and_the_left_out_columns(self):
		q = (
			"select account, debit, credit from `tabGL Entry` where voucher_type=? and voucher_no=? "
			"order by account asc, debit asc, credit asc"
		)
		text = _text(ir.advise_finding(_explain("Filesort", q, table="tabGL Entry"), evidence_lookup=_lookup(_GL_R)))
		assert (
			"An index that returns these rows in the query's order would need every sort column (account, debit, "
			"credit), and Optimus leaves out credit here, so the sort stays."
		) in text

	def test_a_kept_range_sort_column_never_claims_the_order(self):
		ev = _ev(fields={**_ALL, "due_date": F("Date")})
		q = (
			"SELECT name FROM `tabSales Invoice` WHERE company = ? AND customer = ? AND status = ? AND posting_date > ? "
			"ORDER BY posting_date, due_date"
		)
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(ev))
		assert advice.entry["columns"] == ["company", "customer", "status", "posting_date"], _text(advice)
		assert "already sorted" not in advice.lead
		assert ir._NARROWS + "it does not cover every sort column (due_date), so the sort stays." in advice.lead
		# the lead says it; the no-code note is only for a retry that gives no code
		assert "in the query's order would need" not in _text(advice)

	def test_a_capped_grouping_names_the_temporary_table(self):
		q = (
			"select account, sum(debit) from `tabGL Entry` where voucher_type=? and voucher_no=? "
			"group by account, debit, credit"
		)
		text = _text(ir.advise_finding(_explain("Temporary Table", q, table="tabGL Entry"), evidence_lookup=_lookup(_GL_R)))
		assert (
			"would need every grouping column (account, debit, credit), and Optimus leaves out credit here, so the "
			"temporary table stays."
		) in text, text

	def test_a_refused_sort_recipe_gets_no_cut_note(self):
		"""The sort-first recipe gave no code (its index exists), so nothing was cut."""
		ev = _with_index(_SI, ("posting_date_index", ["posting_date"], False))
		q = "SELECT name FROM `tabSales Invoice` WHERE docstatus = ? AND posting_date <= ? ORDER BY posting_date"
		assert "in the query's order would need" not in _text(ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(ev)))

	def test_a_grouping_with_a_metadata_column_never_claims_the_index(self):
		ev = _ev("Stock Reconciliation Item", fields={"item_code": F("Link"), "warehouse": F("Link")},
			extra_types=_CHILD_TYPES, indexes=[("parent", ["parent"], False)])
		q = (
			"SELECT parent, COUNT(*) as records FROM `tabStock Reconciliation Item` WHERE item_code = ? and docstatus = 1 "
			"GROUP By item_code, warehouse, parent HAVING records > 1"
		)
		advice = ir.advise_finding(_explain("Temporary Table", q, table="tabStock Reconciliation Item"), evidence_lookup=_lookup(ev))
		assert "reads the index" not in advice.lead, advice.lead
		assert (
			ir._NARROWS + "it does not cover every grouping column (warehouse, parent), so the temporary table stays."
		) in advice.lead

	def test_a_whole_sort_still_claims_the_order(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? ORDER BY posting_date, customer"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI))
		assert advice.entry["columns"] == ["company", "posting_date", "customer"]
		assert advice.lead == ir._TYPE_LEADS["Filesort"]

	def test_an_equality_fixed_sort_column_needs_no_index_column(self):
		"""docstatus is fixed by the filter and never indexed (a metadata column), so it does
		not change the order the index returns."""
		ev = _ev(fields=_ALL, extra_types={"docstatus": "int"})
		q = "SELECT name FROM `tabSales Invoice` WHERE docstatus = ? AND company = ? ORDER BY docstatus, posting_date"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(ev))
		assert advice.entry["columns"] == ["company", "posting_date"], _text(advice)
		assert advice.lead == ir._TYPE_LEADS["Filesort"], advice.lead


class TestSortOnlyRecipeNeedsLimit:
	"""R3: an index that holds only the sort columns helps a query with no LIMIT and no filter
	it narrows only if the database walks the whole index, which it rarely does."""

	_ST = _ev("Share Transfer", fields={"date": F("Date"), "from_shareholder": F("Link"), "to_shareholder": F("Link")})
	_Q = (
		"SELECT * FROM `tabShare Transfer` WHERE ((DATE(date) <= ? AND from_shareholder = ? ) OR (DATE(date) <= ? AND "
		"to_shareholder = ? )) AND docstatus = 1 ORDER BY date"
	)
	_VIDEO = _ev("Video", fields={"view_count": F("Int"), "publish_date": F("Date")})

	def _advise(self, q, ev, table):
		return ir.advise_finding(_explain("Filesort", q, table=table), evidence_lookup=_lookup(ev))

	def test_share_transfer_gives_no_code(self):
		advice = self._advise(self._Q, self._ST, "tabShare Transfer")
		text = _text(advice)
		assert advice.route == ir.ROUTE_NO_CODE and not advice.unknown, text
		assert (
			"MariaDB rarely walks a whole index instead of sorting when the query has no LIMIT; add a LIMIT or a "
			"narrowing filter first."
		) in text

	def test_video_gives_no_code(self):
		q = (
			"SELECT publish_date, title, view_count FROM `tabVideo` WHERE view_count is not null and publish_date "
			"between ? and ? ORDER BY view_count desc"
		)
		advice = self._advise(q, self._VIDEO, "tabVideo")
		assert advice.route == ir.ROUTE_NO_CODE and "rarely walks a whole index" in _text(advice), _text(advice)

	def test_postgres_names_postgres(self):
		ev = dataclasses.replace(self._ST, dialect="postgres", column_types={**self._ST.column_types, "date": "date"})
		advice = self._advise(self._Q, ev, "tabShare Transfer")
		assert "Postgres rarely walks a whole index" in _text(advice), _text(advice)

	@pytest.mark.parametrize("q,table,columns", [
		(_Q + " LIMIT ?", "tabShare Transfer", ("date",)),
		("SELECT name FROM `tabVideo` WHERE view_count > ? ORDER BY view_count desc", "tabVideo", ("view_count",)),
		("SELECT name FROM `tabVideo` WHERE view_count IS NOT NULL AND view_count < ? ORDER BY view_count", "tabVideo",
			("view_count",)),
	])
	def test_a_limit_or_a_narrowing_range_keeps_the_code(self, q, table, columns):
		ev = self._ST if table == "tabShare Transfer" else self._VIDEO
		advice = self._advise(q, ev, table)
		assert advice.route != ir.ROUTE_NO_CODE and advice.columns == columns, _text(advice)

	def test_a_temporary_table_is_not_this_rule(self):
		q = "SELECT customer, COUNT(name) FROM `tabSales Invoice` GROUP BY customer"
		advice = ir.advise_finding(_explain("Temporary Table", q), evidence_lookup=_lookup(_SI))
		assert advice is None or "rarely walks a whole index" not in _text(advice)

	def test_only_a_bare_is_not_null_piece_is_no_narrowing(self):
		quals = frozenset({"tabVideo"})
		assert ir._only_not_null("SELECT name FROM `tabVideo` WHERE view_count IS NOT NULL AND a = ?", quals, "view_count")
		assert not ir._only_not_null("SELECT name FROM `tabVideo` WHERE view_count IS NOT NULL AND view_count > ?", quals, "view_count")
		assert not ir._only_not_null("SELECT name FROM `tabVideo` WHERE a = ?", quals, "view_count")
		assert not ir._only_not_null("SELECT name FROM `tabVideo` WHERE (view_count IS NOT NULL OR a = ?)", quals, "view_count")
		assert not ir._only_not_null(
			"SELECT name FROM `tabVideo` WHERE view_count IS NOT NULL AND (view_count > ? OR a = ?)", quals, "view_count",
		)


class TestRangeBeatsSortWithoutLimit:
	"""Bounded corrective follow-up: without a LIMIT the query reads every matching row, so a
	usable range filter on another column beats an index that only returns the rows in
	order; the range recipe is the advice and the caveat says the sort stays. R3's no-code
	stays for a query with no usable filter (Share Transfer, Video)."""

	_STAYS = "it comes after the range condition on posting_date, so the index cannot return the rows in order and the sort stays"

	def test_battery_41_gives_the_range_recipe(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE posting_date > ? ORDER BY due_date"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI4))
		text = _text(advice)
		assert advice.route != ir.ROUTE_NO_CODE and advice.columns == ("posting_date",), text
		assert f"Optimus left out due_date ({self._STAYS})" in text
		assert "the sort stays" in advice.lead and "rarely walks a whole index" not in text

	def test_an_equality_column_keeps_the_range_too(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? AND posting_date > ? ORDER BY due_date"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI4))
		assert advice.entry["columns"] == ["company", "posting_date"], _text(advice)

	def test_with_a_limit_the_sort_still_wins(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? AND posting_date > ? ORDER BY due_date LIMIT ?"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI4))
		assert advice.entry["columns"] == ["company", "due_date"], _text(advice)

	def test_an_existing_range_index_names_the_sort_that_stays(self):
		ev = _with_index(_SI4, ("posting_date_index", ["posting_date"], False))
		q = "SELECT name FROM `tabSales Invoice` WHERE posting_date > ? ORDER BY due_date"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(ev))
		text = _text(advice)
		assert advice.route == ir.ROUTE_NO_CODE and 'already leads the index "posting_date_index"' in text, text
		assert (
			"The query has no LIMIT, so it reads every row the range filter on posting_date matches, and the database "
			"usually reads fewer rows through that filter than through an index that returns them in order, so the "
			"sort stays."
		) in text

	def test_an_unusable_range_is_no_range(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE DATE(posting_date) > ? ORDER BY due_date"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI4))
		assert advice.route == ir.ROUTE_NO_CODE and "rarely walks a whole index" in _text(advice), _text(advice)

	def test_a_temporary_table_still_indexes_the_group(self):
		q = "SELECT customer, COUNT(name) FROM `tabSales Invoice` WHERE company = ? AND posting_date > ? GROUP BY customer"
		advice = ir.advise_finding(_explain("Temporary Table", q), evidence_lookup=_lookup(_SI4))
		assert advice.entry["columns"] == ["company", "customer"], _text(advice)

	def test_a_left_out_group_column_says_the_temporary_table_stays(self):
		q = "SELECT customer, COUNT(name) FROM `tabSales Invoice` WHERE company IN (?, ?) AND posting_date > ? GROUP BY customer"
		text = _text(ir.advise_finding(_explain("Temporary Table", q), evidence_lookup=_lookup(_SI4)))
		assert (
			"Optimus left out customer (it comes after the range condition on posting_date, so the index cannot return "
			"the rows in order and the temporary table stays)"
		) in text, text

	def test_a_metadata_range_is_no_range_optimus_indexes(self):
		"""c1 Supplier Quotation: docstatus < 2 is a filter Optimus never indexes, so the sort
		recipe (supplier, creation) stands."""
		ev = _with_index(_ev("Supplier Quotation", fields={"supplier": F("Link")}, extra_types={"docstatus": "int"}),
			("supplier_index", ["supplier"], False))
		q = (
			"select `tabSupplier Quotation`.name from `tabSupplier Quotation` where `tabSupplier Quotation`.docstatus < 2 "
			"and `tabSupplier Quotation`.supplier = ? order by `tabSupplier Quotation`.creation desc"
		)
		advice = ir.advise_finding(_explain("Filesort", q, table="tabSupplier Quotation"), evidence_lookup=_lookup(ev))
		assert advice.entry["columns"] == ["supplier", "creation"], _text(advice)
		assert "range filter on docstatus" not in _text(advice)

	def test_a_creation_range_after_an_equality_column_still_wins(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? AND creation > ? ORDER BY due_date"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI4))
		assert advice.entry["columns"] == ["company", "creation"], _text(advice)
