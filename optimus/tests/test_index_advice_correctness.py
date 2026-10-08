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
			["customer", "company", "posting_date"],
		),
	])
	def test_common_composite_shapes_are_advised(self, ftype, query, columns):
		advice = self._advise(ftype, query, {"key": "customer_index"})
		assert advice.route == ir.ROUTE_ENSURE_INDEXES, _text(advice)
		assert advice.entry["columns"] == columns

	@pytest.mark.parametrize("ftype", ["Full Table Scan", "Low Filter Ratio", "Slow Query"])
	def test_the_verdict_does_not_depend_on_predicate_order(self, ftype):
		"""c2corr/p1c: status = ? AND customer = ? used to give no code, the other order code."""
		ev = _with_index(_ev(fields={**_ALL, "status": F("Select", search_index=True)}), ("status_index", ["status"], False))
		routes = set()
		for q in (
			"SELECT name FROM `tabSales Invoice` WHERE status = ? AND customer = ?",
			"SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ?",
		):
			advice = self._advise(ftype, q, {"key": None, "possible_keys": "status_index"}, ev)
			routes.add(advice.route)
			assert sorted(advice.columns) == ["customer", "status"]
		assert routes == {ir.ROUTE_ENSURE_INDEXES}

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

	def test_a_later_unique_column_does_not_block_a_composite(self):
		ev = _ev(fields={**_ALL, "status": F("Select", unique=True)})
		advice = ir.advise_finding(
			_explain("Full Table Scan", "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ?"),
			evidence_lookup=_lookup(ev),
		)
		assert advice.route == ir.ROUTE_ENSURE_INDEXES


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
		assert "is_return, disabled are Check fields, which match too many rows for an index to narrow." in _text(two)

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
		ev = _ev(fields={**_ALL, "payload": F("JSON")})
		advice = ir.advise_table("tabSales Invoice", ["payload", "customer"], evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE and advice.code is None
		assert 'Column "payload" is a JSON field, which a plain index cannot cover' in ir.card_note(advice)
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

