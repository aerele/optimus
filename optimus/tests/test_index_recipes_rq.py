# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Best-effort advisor inputs keep the job's hard deadline: a timeout inside the query
parser or the evidence read escapes as a fresh instance with clean frames."""

import pytest

from optimus.renderer import index_recipes as ir
from optimus.renderer import recipe_enrichment

pytestmark = pytest.mark.rq
_JobTimeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException


@pytest.mark.parametrize("source", ["evidence", "query"])
def test_advisor_input_timeout_escapes_with_clean_frames(monkeypatch, source):
	original = _JobTimeout("test deadline")

	def interrupted(*args):
		raise original

	if source == "evidence":
		monkeypatch.setattr(recipe_enrichment, "_read_table_evidence", interrupted)

		def call():
			return ir.advise_table("tabInvoice", ["customer", "status"], evidence_lookup=recipe_enrichment.make_evidence_lookup())
	else:
		monkeypatch.setattr("optimus.analyzers.table_breakdown._parse_query", interrupted)

		def call():
			return ir.advise_finding({"finding_type": "Full Table Scan", "technical_detail": {
				"table": "tabInvoice", "normalized_query": "select * from `tabInvoice` where customer = ?",
			}}, evidence_lookup=lambda table: None)
	with pytest.raises(_JobTimeout) as caught:
		call()
	assert caught.value is not original
	assert caught.value.__context__ is None and caught.value.__cause__ is None
	tb = caught.value.__traceback__
	while tb:
		assert tb.tb_frame.f_code is not interrupted.__code__
		tb = tb.tb_next
