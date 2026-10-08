# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Fresh job deadlines survive the new recipe and eligibility boundaries."""

from functools import partial

import pytest

from optimus import ai_fix, ai_grounding, analyze
from optimus.renderer import index_recipes, recipe_enrichment, source
from optimus.tests.test_ai_loop_facts import _finding

pytestmark = pytest.mark.rq
Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException


@pytest.mark.parametrize("path", ["scope", "loop", "metadata", "finding", "table", "grounding"])
def test_new_boundaries_preserve_a_fresh_job_timeout(monkeypatch, path):
	original = Timeout("fake deadline")

	def interrupted(*args, **kwargs):
		raise original

	if path == "scope":
		monkeypatch.setattr("optimus.settings.get_config", interrupted)
		call = ai_fix._app_scope
	elif path == "loop":
		monkeypatch.setattr(ai_grounding, "loop_facts_from_window", interrupted)
		call = partial(ai_fix._loop_facts_text, _finding())
	elif path == "metadata":
		monkeypatch.setattr(recipe_enrichment, "_read_table_evidence", interrupted)
		call = partial(recipe_enrichment.make_evidence_lookup(), "tabInvoice")
	elif path == "finding":
		monkeypatch.setattr(index_recipes, "advise_finding", interrupted)
		call = partial(recipe_enrichment.apply_finding_recipes,
			[{"finding_type": "Missing Index", "technical_detail": {}}], evidence_lookup=lambda table: None,
		)
	elif path == "table":
		monkeypatch.setattr(index_recipes, "advise_table", interrupted)
		call = partial(recipe_enrichment.apply_table_recipes,
			[{"table": "tabInvoice", "recommended_index": {"columns": ["customer"]}}], evidence_lookup=lambda table: None,
		)
	else:
		monkeypatch.setattr(source, "_source_lines", interrupted)
		call = partial(analyze._ai_grounding_window, "fake.py", 1, {})
	with pytest.raises(Timeout) as caught:
		call()
	assert caught.value is not original
	assert caught.value.__context__ is None and caught.value.__cause__ is None
	tb = caught.value.__traceback__
	while tb:
		assert tb.tb_frame.f_code is not interrupted.__code__
		tb = tb.tb_next
