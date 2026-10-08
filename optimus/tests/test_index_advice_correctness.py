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
			assert "The query sorts by an aggregate, which no index can return in order, so the sort stays." in lead, lead
			assert "metadata" not in lead

	def test_a_grouping_sorted_by_an_aggregate_names_the_aggregate(self):
		for order in ("total DESC", "SUM(grand_total) DESC"):
			q = (
				"SELECT customer, sum(grand_total) AS total FROM `tabSales Invoice` WHERE company = ? "
				f"GROUP BY customer ORDER BY {order}"
			)
			lead = _c7("Temporary Table", q).lead
			assert "The query sorts its groups by an aggregate" in lead and "so the temporary table stays" in lead, lead
			assert "groups by an expression" not in lead

	def test_an_implicit_aggregate_alias_counts_as_an_aggregate(self):
		q = (
			"SELECT customer, sum(grand_total) total, count(name) FROM `tabSales Invoice` WHERE company = ? "
			"GROUP BY customer ORDER BY total DESC"
		)
		assert "The query sorts by an aggregate" in _c7("Filesort", q).lead
		assert ir._aggregate_aliases(ir._SQL_TOKEN_RE.findall(q)) == {"total"}

	def test_a_grouping_with_a_range_sorted_by_an_aggregate_names_the_aggregate(self):
		q = (
			"SELECT `name` FROM `tabSales Invoice` WHERE `company`=? AND `posting_date` > ? GROUP BY `customer` "
			"ORDER BY SUM(`grand_total`) DESC"
		)
		advice = _c7("Temporary Table", q)
		assert advice.columns == ("company", "posting_date")
		assert "The query sorts its groups by an aggregate" in advice.lead

	def test_a_grouping_sorted_by_another_column_says_so(self):
		q = "SELECT customer FROM `tabSales Invoice` WHERE company = ? GROUP BY customer ORDER BY posting_date"
		lead = _c7("Temporary Table", q).lead
		assert "The query sorts by other columns than it groups by, so the temporary table stays." in lead, lead

	def test_distinct_without_group_by_names_the_distinct(self):
		lead = _c7("Temporary Table", "SELECT DISTINCT customer FROM `tabSales Invoice` WHERE company = ?").lead
		assert "The temporary table comes from the query's DISTINCT, which this index does not cover" in lead, lead
		assert "metadata" not in lead

	def test_a_metadata_sort_column_is_named_as_one(self):
		lead = _c7("Filesort", "SELECT name FROM `tabSales Invoice` WHERE company = ? ORDER BY idx DESC").lead
		assert "The sort column is a Frappe metadata column, which Optimus never indexes, so the sort stays." in lead
		assert "aggregate" not in lead
		lead = _c7("Temporary Table", "SELECT count(*) FROM `tabSales Invoice` WHERE company = ? GROUP BY docstatus").lead
		assert "The grouping column is a Frappe metadata column, which Optimus never indexes" in lead


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
		assert "is_return is a Check field, which matches too many rows for an index to narrow." in text
		assert "Filter on a more selective field as well, and check the result with EXPLAIN." in text
		two = ir.advise_finding(
			_explain("Full Table Scan", "SELECT name FROM `tabSales Invoice` WHERE is_return = ? AND disabled = ?"),
			evidence_lookup=_lookup(ev),
		)
		assert "disabled, is_return are Check fields, which match too many rows for an index to narrow." in _text(two)

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
	assert "never trades it for a weaker recipe" in doc



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
		assert "is_cancelled is a Check field, which matches too many rows for an index to narrow" in text

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
	assert "The query groups by other columns than it sorts by, so the sort stays." in lead, lead


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
	assert "The query has no GROUP BY or DISTINCT on this table" in lead


def test_a_sort_on_another_tables_column_says_so():
	lead = _c7("Filesort", _JOINED.format(tail="ORDER BY sii.item_code")).lead
	assert "The query sorts by a column of another table, which an index on this table cannot return in order" in lead
	assert "expression" not in lead
	lead = _c7("Temporary Table", _JOINED.format(tail="GROUP BY sii.item_code")).lead
	assert "The query groups by a column of another table" in lead, lead


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
		assert ir._from_clause(q) == [("tabsales invoice", "from")]
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
		assert "The filter on customer matches more than one value, so this index cannot return the rows in order" in advice.lead

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

	def test_a_sort_recipe_an_existing_index_serves_is_never_traded_for_a_range_recipe(self):
		"""battery3 [303] and the real SLE stock query: the existing index serves the equality
		columns and the sort, so the recipe without the sort (with the range) would not help."""
		ev = _with_index(_ev(fields={**_ALL, "due_date": F("Date")}), ("idx_cd", ["company", "due_date"], False))
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? AND posting_date > ? ORDER BY due_date"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE and advice.served
		assert 'The index "idx_cd" on table "tabSales Invoice" already starts with (company, due_date)' in _text(advice)
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
