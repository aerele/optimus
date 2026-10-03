# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Best-effort recipe inputs must preserve the job's hard deadline."""

import pytest

from optimus.renderer import fix_recipes as fr

pytestmark = pytest.mark.rq
_JobTimeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException


@pytest.mark.parametrize("source", ["metadata", "query"])
def test_recipe_input_timeout_escapes_with_clean_frames(monkeypatch, source):
	original = _JobTimeout("test deadline")

	def interrupted(*args):
		raise original

	if source == "metadata":
		def call():
			return fr.table_card_columns("tabInvoice", ["customer", "status"], meta_lookup=interrupted)
	else:
		monkeypatch.setattr("optimus.analyzers.table_breakdown._parse_query", interrupted)
		def call():
			return fr.index_recipe({"finding_type": "Full Table Scan", "technical_detail": {
				"table": "tabInvoice", "normalized_query": "select * from `tabInvoice` where customer = ?",
			}}, meta_lookup=lambda dt: None)
	with pytest.raises(_JobTimeout) as caught:
		call()
	assert caught.value is not original
	assert caught.value.__context__ is None and caught.value.__cause__ is None
	tb = caught.value.__traceback__
	while tb:
		assert tb.tb_frame.f_code is not interrupted.__code__
		tb = tb.tb_next
