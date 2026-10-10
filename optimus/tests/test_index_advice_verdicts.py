# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Index advisor verdicts and texts: the FROM-clause reader on JOIN ... ON, the list-view
Check filter with a sorted LIMIT, self-joins, != filters, anti-joins, and the wording of
Check, shape and sort-cause texts on findings, cards and Missing Index findings."""

import dataclasses

import pytest

from optimus.renderer import index_recipes as ir
from optimus.renderer.recipe_enrichment import IndexEvidence
from optimus.tests.test_index_recipes import _ALL, F, _ev, _explain, _lookup, _missing


def _with_index(ev, *indexes):
	return dataclasses.replace(
		ev, indexes=tuple(ev.indexes) + tuple(IndexEvidence(n, tuple(c), u) for n, c, u in indexes),
	)


def _text(advice):
	return ir.finding_text(advice)


# ERPNext v16 GL Entry: its Search Index fields and on_doctype_update composites.
_GL_SEARCH = ("account", "party_type", "party", "voucher_no", "posting_date", "company", "cost_center")
_GL = _with_index(
	_ev("GL Entry", fields={
		"account": F("Link", search_index=True), "party_type": F("Link", search_index=True),
		"party": F("Dynamic Link", search_index=True), "voucher_type": F("Link"),
		"voucher_no": F("Dynamic Link", search_index=True), "posting_date": F("Date", search_index=True),
		"is_cancelled": F("Check"), "company": F("Link", search_index=True), "cost_center": F("Link", search_index=True),
	}),
	("PRIMARY", ["name"], True),
	*((f"{name}_index", [name], False) for name in _GL_SEARCH),
	("voucher_type_voucher_no_index", ["voucher_type", "voucher_no"], False),
	("posting_date_company_index", ["posting_date", "company"], False),
	("party_type_party_index", ["party_type", "party"], False),
)
_PI = _ev("Purchase Invoice", fields={"supplier": F("Link"), "company": F("Link"), "posting_date": F("Date")})


# --- the FROM clause: a qualified ON column is no table name ---------------------------


class TestFromClause:
	@pytest.mark.parametrize("query,tables", [
		(
			"SELECT gl.name FROM `tabPurchase Invoice` p JOIN `tabGL Entry` gl ON gl.voucher_type = ? "
			"AND gl.voucher_no = p.name WHERE p.supplier = ?",
			["tabpurchase invoice", "tabgl entry"],
		),
		(
			"SELECT a.x FROM `tabA` a LEFT JOIN `tabB` b ON b.parent = a.name JOIN `tabC` c ON c.name = b.link WHERE a.y = ?",
			["taba", "tabb", "tabc"],
		),
		("SELECT name FROM db1.`tabSales Invoice` si WHERE si.customer = ?", ["tabsales invoice"]),
		("SELECT x FROM db1.tabA a, db2.tabB WHERE a.y = ?", ["taba", "tabb"]),
		('SELECT x FROM t17.public."tabA" a JOIN `tabB` b ON b.x = a.y WHERE 1', ["taba", "tabb"]),
	])
	def test_only_a_name_right_after_the_table_is_qualified(self, query, tables):
		assert ir._from_clause(query) == tables

	def test_a_joined_target_is_checked_for_columns_the_parser_dropped(self):
		"""An unqualified account on a query of two tables could be either table's, so the
		filter is not read and there is no verdict; the joined GL Entry used to read as a table
		outside the main FROM, which skipped that check and gave a false "already exists"."""
		q = (
			"SELECT gl.name FROM `tabPurchase Invoice` p JOIN `tabGL Entry` gl ON gl.voucher_type = ? "
			"AND gl.voucher_no = p.name WHERE p.supplier = ? AND gl.party = ? AND account = ?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabGL Entry"), evidence_lookup=_lookup(_GL, _PI))
		assert advice.route == ir.ROUTE_NO_CODE and advice.unknown, _text(advice)
		assert "Optimus could not read the filter on account" in _text(advice)

	def test_the_comma_join_gives_the_same_verdict(self):
		q = (
			"SELECT gl.name FROM `tabPurchase Invoice` p, `tabGL Entry` gl WHERE gl.voucher_type = ? "
			"AND gl.voucher_no = p.name AND p.supplier = ? AND gl.party = ? AND account = ?"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabGL Entry"), evidence_lookup=_lookup(_GL, _PI))
		assert advice.route == ir.ROUTE_NO_CODE and advice.unknown, _text(advice)


# --- a Check filter whose sort an index serves only with a LIMIT ------------------------

_SI_CHECK = _ev(fields={**_ALL, "is_return": F("Check"), "is_pos": F("Check")}, indexes=(
	("PRIMARY", ["name"], True), ("creation", ["creation"], False), ("customer_index", ["customer"], False),
	("company_index", ["company"], False),
))
_LIST_VIEW = (
	"SELECT `tabSales Invoice`.`name` FROM `tabSales Invoice` WHERE `tabSales Invoice`.`is_return` = ? "
	"ORDER BY `tabSales Invoice`.`creation` DESC LIMIT ? OFFSET ?"
)


class TestCheckFilterWithASortedLimit:
	def test_the_list_view_query_gets_the_check_then_sort_index(self):
		"""The creation index serves the sort, not the filter: the database still sorted, and
		(is_return, creation) returns the first rows directly for either value."""
		advice = ir.advise_finding(_explain("Filesort", _LIST_VIEW), evidence_lookup=_lookup(_SI_CHECK))
		text = _text(advice)
		assert advice.route == ir.ROUTE_ENSURE_INDEXES, text
		assert advice.entry["columns"] == ["is_return", "creation"]
		assert "already serves this filter and sort" not in text
		assert (
			"is_return is a Check field, which usually matches most of the table's rows; this index still returns "
			"the query's first rows in order for any of its values."
		) in text

	def test_two_check_fields(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE is_return = ? AND is_pos = ? ORDER BY creation DESC LIMIT ?"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI_CHECK))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.entry["columns"] == ["is_pos", "is_return", "creation"]
		assert "is_pos, is_return are Check fields, which usually match most of the table's rows;" in _text(advice)

	def test_without_a_limit_the_check_hedge_stays_and_names_no_index(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE is_return = ? ORDER BY creation DESC"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI_CHECK))
		text = _text(advice)
		assert advice.route == ir.ROUTE_NO_CODE, text
		assert "if this query looks for the rare value" in text
		assert '"creation"' not in text and "already serves" not in text

	def test_a_range_on_the_served_column_keeps_the_check_verdict(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE creation > ? AND is_return = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI_CHECK))
		assert advice.route == ir.ROUTE_NO_CODE and advice.served_by == ""
		assert 'The index "creation" on table "tabSales Invoice" already starts with (creation)' in _text(advice)

	def test_a_range_tail_with_a_limit_keeps_the_check_verdict(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE creation > ? AND is_return = ? LIMIT ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI_CHECK))
		assert advice.route == ir.ROUTE_NO_CODE, _text(advice)

	def test_the_check_note_needs_a_limit(self):
		"""No index serves posting_date, so the recipe stands either way, but only a LIMIT
		query reads just its first rows."""
		with_limit, without = (
			ir.advise_finding(_explain("Filesort", f"SELECT name FROM `tabSales Invoice` WHERE is_return = ? ORDER BY posting_date{tail}"),
				evidence_lookup=_lookup(_SI_CHECK))
			for tail in (" LIMIT ?", "")
		)
		assert with_limit.route == without.route == ir.ROUTE_ENSURE_INDEXES
		assert any("first rows in order" in c for c in with_limit.caveats)
		assert not any("first rows in order" in c for c in without.caveats)

	def test_the_check_note_is_only_for_a_recipe_led_by_check_fields(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? AND is_return = ? ORDER BY creation DESC LIMIT ?"
		ev = dataclasses.replace(_SI_CHECK, indexes=tuple(ix for ix in _SI_CHECK.indexes if ix.name != "company_index"))
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.entry["columns"] == ["company", "is_return", "creation"]
		assert not any("first rows in order" in c for c in advice.caveats)

	def test_a_plain_filter_column_keeps_the_served_verdict(self):
		ev = _with_index(_SI_CHECK, ("idx_company_creation", ["company", "creation"], False))
		q = "SELECT name FROM `tabSales Invoice` WHERE company = ? AND is_return = ? ORDER BY creation DESC LIMIT ?"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(ev))
		assert advice.route == ir.ROUTE_NO_CODE and "idx_company_creation" in _text(advice)


# --- a self-join: a key lookup on one alias pins only that alias -------------------------

_ACC = _ev("Account", app="erpnext", fields={
	"account_type": F("Select"), "parent_account": F("Link"), "lft": F("Int"), "rgt": F("Int"),
	"account_currency": F("Link"), "company": F("Link"),
}, indexes=(
	("PRIMARY", ["name"], True), ("parent_account_index", ["parent_account"], False),
	("lft_index", ["lft"], False), ("rgt_index", ["rgt"], False),
))


class TestSelfJoin:
	"""A query that reads the target table more than once gives no verdict: a filter or a key
	on one reference says nothing about the other, and Optimus cannot tell which reference
	the finding is about."""

	_NO_PARENT_INDEX = dataclasses.replace(_ACC, indexes=(IndexEvidence("PRIMARY", ("name",), True),))

	@pytest.mark.parametrize("query", [
		"SELECT a.name, p.account_type FROM `tabAccount` a JOIN `tabAccount` p ON a.parent_account = p.name WHERE a.name = ?",
		"SELECT a.name FROM `tabAccount` a JOIN `tabAccount` p ON a.parent_account = p.name WHERE a.name = ? AND p.company = ?",
		"SELECT a.name FROM `tabAccount` a JOIN `tabAccount` p ON p.name = a.parent_account WHERE a.name = ? AND p.name = ?",
		"SELECT a.name FROM `tabAccount` a JOIN `tabAccount` p ON a.parent_account = p.name WHERE p.name = ? "
		"AND a.account_type = ?",
		"SELECT a.name FROM `tabAccount` a, `tabAccount` p WHERE a.lft >= p.lft AND a.rgt <= p.rgt AND p.name = ?",
		"SELECT a.name FROM `tabAccount` a, `tabAccount` p WHERE a.lft >= p.lft AND a.rgt <= p.rgt AND p.name = ? "
		"AND a.account_currency = ?",
	])
	@pytest.mark.parametrize("ftype", ["Full Table Scan", "Filesort"])
	def test_a_self_join_is_no_verdict(self, query, ftype):
		advice = ir.advise_finding(_explain(ftype, query, table="tabAccount"), evidence_lookup=_lookup(self._NO_PARENT_INDEX))
		assert advice.route == ir.ROUTE_NO_CODE and advice.unknown and advice.code is None, _text(advice)
		assert _text(advice) == (
			'Optimus could not read how this query filters: it reads "tabAccount" more than once (a self-join). '
			"So it gives no index code. Check the query with EXPLAIN."
		)

	def test_a_name_lookup_without_a_self_join_stays(self):
		q = "SELECT a.name FROM `tabAccount` a WHERE a.name = ? AND a.company = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabAccount"), evidence_lookup=_lookup(_ACC))
		assert "The query finds its row by name, the primary key" in _text(advice) and not advice.unknown

	def test_a_second_alias_in_a_subquery_is_no_self_join(self):
		q = (
			"SELECT a.name FROM `tabAccount` a WHERE a.name = ? AND a.account_type = ? AND EXISTS "
			"(SELECT 1 FROM `tabAccount` p WHERE p.parent_account = a.name)"
		)
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabAccount"), evidence_lookup=_lookup(_ACC))
		assert "The query finds its row by name, the primary key" in _text(advice)


# --- a != filter: the rows it keeps may be the rare ones --------------------------------

_SE = _ev("Stock Entry", fields={"project": F("Link"), "company": F("Link"), "purpose": F("Select")})


class TestNotEqualIsHedged:
	def test_a_not_equal_filter_alone_names_the_index_for_rare_rows(self):
		q = "SELECT name FROM `tabStock Entry` WHERE project != ? AND docstatus = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q, table="tabStock Entry"), evidence_lookup=_lookup(_SE))
		text = _text(advice)
		assert advice.route == ir.ROUTE_NO_CODE and not advice.unknown, text
		assert (
			"A !=, <> or NOT comparison on project usually matches most of the table's rows; if the rows this query "
			"looks for are rare, an index on (project) can help."
		) in text
		assert "composite" not in text and "would not help" not in text and "Rewrite the filter" not in text
		assert "shape of the filter" not in text and "cannot serve that filter" not in text

	def test_two_not_equal_columns(self):
		q = "SELECT name FROM `tabStock Entry` WHERE project <> ? AND purpose != ?"
		text = _text(ir.advise_finding(_explain("Full Table Scan", q, table="tabStock Entry"), evidence_lookup=_lookup(_SE)))
		assert "!=, <> or NOT comparisons on project, purpose usually match most of the table's rows;" in text
		assert "an index on (project, purpose) can help" in text

	def test_a_not_equal_beside_a_like_keeps_the_like_verdict_for_the_like_only(self):
		q = "SELECT name FROM `tabStock Entry` WHERE project != ? AND purpose LIKE ?"
		text = _text(ir.advise_finding(_explain("Full Table Scan", q, table="tabStock Entry"), evidence_lookup=_lookup(_SE)))
		assert "a LIKE on purpose" in text and "an index on (project) can help" in text
		assert "would not help" not in text


# --- an anti-join keeps its LEFT JOIN ---------------------------------------------------

_SI_C = _ev(fields={"customer": F("Link"), "company": F("Link"), "status": F("Select")})
_CU = _ev("Customer", fields={"customer_group": F("Link"), "territory": F("Link")})
_ANTI = "SELECT si.name FROM `tabSales Invoice` si LEFT JOIN `tabCustomer` c ON c.name = si.customer WHERE "


class TestAntiJoin:
	@pytest.mark.parametrize("where", ["c.name IS NULL", "c.name IS NULL AND si.docstatus = ?"])
	def test_an_anti_join_with_no_filter_left_is_no_verdict(self, where):
		"""Leaving out the probe leaves nothing to index: no verdict, never the analyzer's own
		"Add an index on the WHERE/JOIN columns" hint."""
		advice = ir.advise_finding(_explain("Full Table Scan", _ANTI + where), evidence_lookup=_lookup(_SI_C, _CU))
		assert advice.route == ir.ROUTE_NO_CODE and advice.unknown and advice.code is None, _text(advice)
		assert _text(advice) == (
			'Optimus could not find a filter on "tabSales Invoice" that an index could use: the query uses customer '
			"only to look up rows of another table through a LEFT JOIN. So it gives no index code. Check the query "
			"with EXPLAIN."
		)

	def test_an_is_null_filter_beside_a_target_filter(self):
		advice = ir.advise_finding(
			_explain("Full Table Scan", _ANTI + "c.name IS NULL AND si.company = ?"), evidence_lookup=_lookup(_SI_C, _CU),
		)
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.columns == ("company",), _text(advice)

	@pytest.mark.parametrize("where", ["c.name IS NOT NULL", "c.territory = ?", "NOT c.name IS NULL"])
	def test_a_filter_that_rejects_nulls_makes_it_an_inner_join(self, where):
		advice = ir.advise_finding(_explain("Full Table Scan", _ANTI + where), evidence_lookup=_lookup(_SI_C, _CU))
		assert advice is not None and "customer" in advice.columns, where


# --- texts with no query: a table card and a Missing Index finding ----------------------

_SI_CK = _ev(fields={**_ALL, "is_return": F("Check"), "is_opening": F("Check")}, indexes=(
	("PRIMARY", ["name"], True), ("customer_idx", ["customer"], False), ("po_customer", ["po_no", "customer"], True),
))


class TestTextsWithoutAQuery:
	@pytest.mark.parametrize("columns", [["is_return"], ["is_return", "is_opening"], ["customer", "is_return"]])
	def test_a_card_that_only_hedges_is_no_verdict(self, columns):
		advice = ir.advise_table("tabSales Invoice", columns, evidence_lookup=_lookup(_SI_CK))
		note = ir.card_note(advice)
		assert advice.route == ir.ROUTE_NO_CODE and advice.unknown, note
		assert note.startswith(ir.NO_VERDICT) and "Do not add this index" not in note
		assert "this query" not in note and "Filter on a more selective field" not in note
		assert "if the slow queries look for the rare" in note

	def test_a_missing_index_on_a_check_field_never_names_a_query(self):
		advice = ir.advise_finding(_missing("is_return"), evidence_lookup=_lookup(_SI_CK))
		text = _text(advice)
		assert advice.route == ir.ROUTE_NO_CODE and not advice.unknown, text
		assert "this query" not in text and "Filter on a more selective field" not in text
		assert "Check the slow queries on this column with EXPLAIN to see which value they look for." in text

	def test_a_card_on_a_unique_pair_names_no_query(self):
		advice = ir.advise_table("tabSales Invoice", ["po_no", "customer", "status"], evidence_lookup=_lookup(_SI_CK))
		note = ir.card_note(advice)
		assert advice.route == ir.ROUTE_NO_CODE and note.startswith("Do not add this index."), note
		assert "which the slow queries compare with known values" in note and "this query" not in note

	def test_a_capped_card_names_no_query(self):
		ev = _with_index(_SI_CK, ("po_unique", ["po_no"], True))
		advice = ir.advise_table(
			"tabSales Invoice", ["customer", "status", "company", "posting_date", "po_no"], evidence_lookup=_lookup(ev),
		)
		note = ir.card_note(advice)
		assert advice.route == ir.ROUTE_NO_CODE and "which the slow queries compare with known values" in note, note
		assert "Check the slow queries with EXPLAIN to see which index they use." in note and "this query" not in note

	def test_a_finding_keeps_its_query_wording(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE is_return = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI_CK))
		assert not advice.unknown and "if this query looks for the rare value" in _text(advice)
		assert "Filter on a more selective field as well" in _text(advice)


# --- a shape text names only the shapes found ------------------------------------------

_SH = _ev(fields={**_ALL, "grand_total": F("Int")})


class TestShapeTextNamesWhatItFound:
	@pytest.mark.parametrize("where,rewrite,absent", [
		("po_no LIKE ?", "an exact match or a pattern without a leading wildcard instead of the LIKE",
			("OR branch", "function", "arithmetic")),
		("customer = ? OR status = ?", "one query per OR branch", ("LIKE", "function", "arithmetic")),
		("upper(customer) = ?", "the bare column instead of UPPER() around it", ("LIKE", "OR branch", "arithmetic")),
		("grand_total + 1 > ?", "the bare column compared with a value instead of arithmetic or another column",
			("LIKE", "OR branch", "UPPER")),
	])
	def test_only_the_matching_rewrite(self, where, rewrite, absent):
		advice = ir.advise_finding(_explain("Full Table Scan", f"SELECT name FROM `tabSales Invoice` WHERE {where}"),
			evidence_lookup=_lookup(_SH))
		text = _text(advice)
		assert f"Rewrite the filter ({rewrite}) and check the result with EXPLAIN." in text, text
		for word in absent:
			assert word not in text.split("Rewrite the filter", 1)[1], (word, text)

	def test_one_column_is_never_called_a_composite(self):
		text = _text(ir.advise_finding(_explain("Full Table Scan", "SELECT name FROM `tabSales Invoice` WHERE po_no LIKE ?"),
			evidence_lookup=_lookup(_SH)))
		assert "composite" not in text and "An index on po_no cannot serve that filter." in text

	def test_two_shapes_name_both_rewrites(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE po_no LIKE ? AND upper(customer) = ?"
		text = _text(ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SH)))
		assert "An index on those columns cannot serve that filter." in text
		assert (
			"Rewrite the filter (an exact match or a pattern without a leading wildcard instead of the LIKE, or the "
			"bare column instead of UPPER() around it)"
		) in text


# --- a Filesort or Temporary Table lead names the cause it found ------------------------


class TestSortLeads:
	@pytest.mark.parametrize("order,cause", [
		("lower(po_no)", "the query sorts by an expression (a function, CASE, arithmetic or a parameter), which no index "
			"can return in order"),
		("remarks", "the query sorts by a text column, which Optimus does not index for a sort"),
		("posting_date DESC, company ASC", "the query sorts in mixed directions (ASC and DESC), which this index cannot "
			"return in order"),
		("total", "the query sorts by a select alias, which Optimus cannot match to a column of this table"),
		("no_such_column", "the query sorts by a name that is no column of this table"),
	])
	def test_the_lead_names_the_cause_it_found(self, order, cause):
		q = f"SELECT name, grand_total AS total FROM `tabSales Invoice` WHERE customer = ? ORDER BY {order} LIMIT 5"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SH))
		assert advice.route != ir.ROUTE_NO_CODE, _text(advice)
		assert advice.lead == f"This index narrows the filter, but {cause}, so the sort stays."
		assert advice.sort_stays

	@pytest.mark.parametrize("ftype,query,end", [
		("Temporary Table", "SELECT customer, SUM(grand_total) FROM `tabSales Invoice` WHERE status = ? GROUP BY customer "
			"ORDER BY SUM(grand_total)", "the query sorts its groups by an aggregate"),
		("Filesort", "SELECT name FROM `tabSales Invoice` WHERE status IN (?, ?) AND customer = ? ORDER BY posting_date "
			"LIMIT 5", "the filter on status matches more than one value"),
		("Filesort", "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND posting_date > ? ORDER BY company",
			"the sort column comes after the range condition on posting_date"),
	])
	def test_a_lead_that_keeps_the_sort_never_promises_a_whole_table_read_ends(self, ftype, query, end):
		advice = ir.advise_finding(_explain(ftype, query), evidence_lookup=_lookup(_SH))
		assert advice.lead.startswith(f"This index narrows the filter, but {end}"), advice.lead
		assert "stops reading the whole table" not in advice.lead and advice.sort_stays

	def test_an_unattributed_sort_column_still_names_its_cause(self):
		"""On a query of two tables the SQL parser does not attribute the unqualified ORDER BY
		remarks to the table, yet the scan still finds that it is a text column."""
		q = (
			"SELECT si.name FROM `tabSales Invoice` si JOIN `tabCustomer` c ON c.name = si.customer "
			"WHERE si.company = ? ORDER BY remarks LIMIT 5"
		)
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SH, _CU_J))
		assert advice.route != ir.ROUTE_NO_CODE, _text(advice)
		assert advice.lead == (
			"This index narrows the filter, but the query sorts by a text column, which Optimus does not index for a "
			"sort, so the sort stays."
		)

	def test_a_kept_sort_column_with_an_expression_sort_stays(self):
		"""posting_date is both the range filter and (inside lower()) the sort, so the recipe keeps
		it, but the index cannot return the rows in that order."""
		q = "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND posting_date > ? ORDER BY lower(posting_date) LIMIT 5"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SH))
		assert "posting_date" in advice.columns and advice.sort_stays == "stays", _text(advice)
		assert advice.lead.startswith("This index narrows the filter, but the query sorts by an expression")

	def test_a_lead_that_removes_the_sort_says_so(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE customer = ? ORDER BY posting_date LIMIT 5"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SH))
		assert advice.lead == "Index the filter columns followed by the sort column so the rows come back already sorted."
		assert not advice.sort_stays

	def test_a_maybe_in_list_is_hedged_but_counts_as_removing_the_sort(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE company IN (?) AND customer = ? ORDER BY posting_date LIMIT 5"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SH))
		assert "if the IN list on company has more than one value" in advice.lead and not advice.sort_stays

	def test_a_full_table_scan_keeps_its_lead(self):
		advice = ir.advise_finding(_explain("Full Table Scan", "SELECT name FROM `tabSales Invoice` WHERE customer = ?"),
			evidence_lookup=_lookup(_SH))
		assert advice.lead == "Index the columns this query filters on so it stops reading the whole table."
		assert not advice.sort_stays


def test_the_docs_and_changelog_quote_these_texts():
	from pathlib import Path

	from optimus.renderer import recipe_enrichment

	root = Path(ir.__file__).resolve().parents[2]
	doc = " ".join((root / "docs" / "AI-FIXING.md").read_text(encoding="utf-8").split())
	log = " ".join((root / "CHANGELOG.md").read_text(encoding="utf-8").split())
	title = recipe_enrichment.NO_INDEX_UNKNOWN_TITLE.format(table="<table>", column="<column>")
	for text in (doc, log):
		assert title in text and ir._NARROWS.strip() in text and "EmptyIndexList" in text
		assert "`sort_stays`" in text and "if the rows this query looks for are rare" in text
		assert "`is_return = ? ORDER BY creation DESC LIMIT ?`" in text
	assert recipe_enrichment.SORT_STAYS_NOTES["Filesort"] in doc
	# export keys, the timeout claim, the self-join, the join-bound sort, "may not"
	assert "`code`, `unknown`: no verdict" in doc and '`"may stay"` when it may' in doc
	assert "it escapes the report render (`render_raw`) and the export (`export_session`)" in doc
	assert "analyzer loop and its report step" in log and "escapes the report render and the export" in log
	assert "reads the table more than once (a self-join" in doc and "self-join (the table read more than once" in log
	assert "only a join binds" in doc and "only a join binds" in log
	assert '"may not remove"' in doc and "a bare `NOT col = ?`" in doc and "a bare `NOT col = ?`" in log
	assert "if the slow queries look for the rare value" in doc and "WHERE c.name IS NULL" in doc


# --- the Check note needs an equality, not a collapsed IN (?) ---------------------------


def test_the_check_note_needs_an_equality_not_a_collapsed_in_list():
	q = "SELECT name FROM `tabSales Invoice` WHERE is_return IN (?) ORDER BY creation DESC LIMIT ?"
	advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI_CHECK))
	assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.entry["columns"] == ["is_return", "creation"]
	assert "if the IN list on is_return has more than one value" in advice.lead
	assert not any("first rows in order" in c for c in advice.caveats)


# --- a bare NOT negates its comparison -------------------------------------------------

_SI_NOT = _ev(fields={"customer": F("Link"), "status": F("Select"), "company": F("Link")})


class TestBareNot:
	@pytest.mark.parametrize("where", ["customer = ? AND NOT name = ?", "NOT name = ? AND customer = ?",
		"customer = ? AND NOT `tabSales Invoice`.`name` = ?"])
	def test_a_negated_name_is_no_key_lookup(self, where):
		advice = ir.advise_finding(_explain("Full Table Scan", f"SELECT name FROM `tabSales Invoice` WHERE {where}"),
			evidence_lookup=_lookup(_SI_NOT))
		assert "by name, the primary key" not in _text(advice)
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.columns == ("customer",), _text(advice)

	def test_a_negated_column_is_a_not_comparison(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND NOT status = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI_NOT))
		assert advice.columns == ("customer",)
		assert "Optimus left out status (compared only by !=, <> or NOT" in _text(advice)

	def test_a_plain_name_lookup_stays(self):
		q = "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND name = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI_NOT))
		assert "by name, the primary key" in _text(advice)


# --- a sort recipe whose equality column only a join binds -----------------------------

_SI_J = _ev(fields={"customer": F("Link"), "company": F("Link"), "posting_date": F("Date")})
_CU_J = _ev("Customer", fields={"territory": F("Link"), "customer_group": F("Link")})


class TestJoinBoundSort:
	@pytest.mark.parametrize("query", [
		"SELECT si.name FROM `tabSales Invoice` si JOIN `tabCustomer` c ON c.name = si.customer "
		"WHERE c.territory = ? ORDER BY si.posting_date LIMIT ?",
		"SELECT si.name FROM `tabSales Invoice` si, `tabCustomer` c WHERE c.name = si.customer "
		"AND c.territory = ? ORDER BY si.posting_date LIMIT ?",
	])
	def test_a_join_bound_equality_before_the_sort_is_no_verdict(self, query):
		"""si.customer takes every customer of the territory, so (customer, posting_date) cannot
		return the rows in order: never "the rows come back already sorted"."""
		advice = ir.advise_finding(_explain("Filesort", query), evidence_lookup=_lookup(_SI_J, _CU_J))
		assert advice.route == ir.ROUTE_NO_CODE and advice.unknown, _text(advice)
		assert "already sorted" not in _text(advice)
		assert _text(advice) == (
			"Optimus could not read how this query sorts: the index would put customer before the sort column, "
			"and customer takes its values from a join with another table, not from a value the query gives, so "
			"the index may not return the rows in order. So it gives no index code. Check the query with EXPLAIN."
		)

	@pytest.mark.parametrize("pin", ["c.name IN (?)", "c.name IN (SELECT parent FROM `tabCustomer Group Item` WHERE x = ?)"])
	@pytest.mark.parametrize("joins", [
		"`tabSales Invoice` si, `tabCustomer` c WHERE si.customer = c.name AND {pin}",
		"`tabSales Invoice` si JOIN `tabCustomer` c ON c.name = si.customer WHERE si.customer = c.name AND {pin}",
	])
	def test_a_join_column_pinned_by_an_in_list_or_subquery_is_still_join_bound(self, joins, pin):
		"""The partner column may hold many values, so the sort cannot be returned in order."""
		q = "SELECT si.name FROM " + joins.format(pin=pin) + " ORDER BY si.posting_date LIMIT 20"
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI_J, _CU_J))
		assert advice.route == ir.ROUTE_NO_CODE and advice.unknown, _text(advice)
		assert "already sorted" not in _text(advice)

	def test_a_join_column_pinned_to_one_value_keeps_its_sort_recipe(self):
		q = (
			"SELECT si.name FROM `tabSales Invoice` si, `tabCustomer` c WHERE si.customer = c.name AND c.name = ? "
			"ORDER BY si.posting_date LIMIT 20"
		)
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI_J, _CU_J))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.entry["columns"] == ["customer", "posting_date"], _text(advice)

	def test_a_value_bound_equality_keeps_its_sort_recipe(self):
		q = (
			"SELECT si.name FROM `tabSales Invoice` si LEFT JOIN `tabCustomer` c ON c.name = si.customer "
			"WHERE si.company = ? ORDER BY si.posting_date LIMIT ?"
		)
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI_J, _CU_J))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.entry["columns"] == ["company", "posting_date"], _text(advice)
		assert "already sorted" in advice.lead

	def test_a_join_bound_recipe_that_dropped_the_sort_keeps_its_code(self):
		"""No LIMIT and a usable range: the recipe without the sort is the advice, so no column
		comes before a sort column and the code stands."""
		q = (
			"SELECT si.name FROM `tabSales Invoice` si JOIN `tabCustomer` c ON c.name = si.customer "
			"WHERE c.territory = ? AND si.posting_date > ? ORDER BY si.company"
		)
		advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SI_J, _CU_J))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.entry["columns"] == ["customer", "posting_date"], _text(advice)

	def test_a_join_bound_filter_without_a_sort_keeps_its_code(self):
		q = "SELECT si.name FROM `tabSales Invoice` si JOIN `tabCustomer` c ON c.name = si.customer WHERE c.territory = ?"
		advice = ir.advise_finding(_explain("Full Table Scan", q), evidence_lookup=_lookup(_SI_J, _CU_J))
		assert advice.route == ir.ROUTE_ENSURE_INDEXES and advice.columns == ("customer",), _text(advice)


# --- a sort that only may stay ---------------------------------------------------------


def test_a_sort_from_elsewhere_only_may_stay():
	q = "SELECT customer, COUNT(name) FROM `tabSales Invoice` WHERE status = ? GROUP BY customer"
	advice = ir.advise_finding(_explain("Filesort", q), evidence_lookup=_lookup(_SH))
	assert advice.route == ir.ROUTE_ENSURE_INDEXES and "and may stay." in advice.lead, advice.lead
	assert advice.sort_stays == "may stay"
	other = ir.advise_finding(_explain("Filesort", "SELECT name FROM `tabSales Invoice` WHERE customer = ? ORDER BY "
		"lower(po_no) LIMIT 5"), evidence_lookup=_lookup(_SH))
	assert other.sort_stays == "stays"
