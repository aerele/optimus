"""Recipe order follows the SQL sort clause, including a normal terminator."""

import pytest

from optimus.renderer import index_recipes as ir
from optimus.tests.test_index_recipes import F, _ev, _explain, _lookup


@pytest.mark.parametrize("keyword", ["order", "group"])
def test_semicolon_does_not_turn_the_last_sort_column_into_an_expression(keyword):
	query = f"SELECT customer, status FROM `tabSales Invoice` {keyword} BY status, customer;"
	assert ir._clause_columns(
		query, keyword, {"tabSales Invoice"}, {"customer": "customer", "status": "status"}
	) == ["status", "customer"]


@pytest.mark.parametrize("kind,clause", [("Filesort", "ORDER BY"), ("Temporary Table", "GROUP BY")])
@pytest.mark.parametrize("semicolon", ["", ";"])
def test_recipe_uses_clause_order_instead_of_select_mention_order(kind, clause, semicolon):
	query = f"SELECT customer, status FROM `tabSales Invoice` WHERE company = ? {clause} status, customer{semicolon}"
	evidence = _ev(
		fields={"company": F("Link"), "status": F("Select"), "customer": F("Link")},
		indexes=(("wrong_order", ["company", "customer", "status"], False),),
	)
	advice = ir.advise_finding(_explain(kind, query), evidence_lookup=_lookup(evidence))
	assert advice.columns == ("company", "status", "customer")
	assert advice.route != ir.ROUTE_NO_CODE
	# The existing reverse suffix cannot satisfy this ORDER BY; code for a
	# correctly ordered index must be offered rather than a covered-index verdict.
