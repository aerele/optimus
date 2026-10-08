# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Fresh job deadlines survive the new recipe and eligibility boundaries."""

from functools import partial
from types import SimpleNamespace

import pytest

from optimus import ai_fix, ai_grounding, analyze
from optimus.renderer import index_recipes, recipe_enrichment, source
from optimus.tests.test_ai_loop_facts import _finding
from optimus.tests.test_ai_payload_grounding_window import _row

pytestmark = pytest.mark.rq
Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException


@pytest.mark.parametrize("path", [
	"scope", "loop", "facts", "metadata", "finding", "table", "grounding", "gate", "gate_recipe", "log", "touch",
	"acquire", "holder", "release",
])
def test_new_boundaries_preserve_a_fresh_job_timeout(monkeypatch, tmp_path, path):
	original = Timeout("fake deadline")

	def interrupted(*args, **kwargs):
		raise original

	if path == "scope":
		monkeypatch.setattr("optimus.settings.get_config", interrupted)
		call = ai_fix._app_scope
	elif path == "loop":
		monkeypatch.setattr(ai_grounding, "loop_facts_from_window", interrupted)
		call = partial(ai_fix._loop_facts_text, _finding())
	elif path == "facts":
		# Fix round 1, F4: the whole-file facts analyze computes for a readable N+1 Query.
		src = tmp_path / "mod.py"
		src.write_text("def f(rows):\n\tfor r in rows:\n\t\tfrappe.get_doc('Item', r)\n")
		monkeypatch.setattr(ai_grounding, "loop_facts_from_tree", interrupted)
		call = partial(analyze._ai_payload_for_finding, _row(str(src), 3, "f", finding_type="N+1 Query"), {})
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
	elif path in ("gate", "gate_recipe"):
		# Task 7: the gate fails closed on an ordinary error, but a deadline still stops the job.
		monkeypatch.setattr(ai_grounding, "hot_line_gate", interrupted)
		hot_line = {"finding_type": "Hot Line", "technical_detail": {"file": "apps/myapp/myapp/x.py"}}
		call = partial(ai_fix.llm_gate_note, hot_line) if path == "gate" else partial(
			recipe_enrichment.apply_finding_recipes, [hot_line], evidence_lookup=lambda table: None,
		)
	elif path == "log":
		# Task 7 fix round 1: the bench-log line for failed index advice.
		import frappe

		monkeypatch.setattr(frappe, "logger", interrupted, raising=False)
		call = partial(recipe_enrichment.log_recipe_failures, 1)
	elif path == "touch":
		# Task 7 fix round 1: the single-flight heartbeat's Redis call.
		import frappe

		monkeypatch.setattr(frappe, "cache", SimpleNamespace(get_value=interrupted, set_value=interrupted), raising=False)
		call = partial(analyze._touch_singleflight, "A")
	elif path in ("acquire", "holder", "release"):
		# T10 fix round 1: the single-flight gate, the janitor's holder check and the release.
		import frappe

		from optimus.tests.singleflight_fakes import FlagCache

		cache = FlagCache("OTHER", ttl=10)
		cache.get_value = interrupted
		monkeypatch.setattr(frappe, "cache", cache, raising=False)
		monkeypatch.setattr(frappe, "conf", {}, raising=False)
		monkeypatch.setattr(analyze, "is_scheduler_disabled", lambda: False)
		call = {
			"acquire": partial(analyze._acquire_singleflight, "A", "PS-A", None),
			"holder": partial(analyze.is_singleflight_holder, "A"),
			"release": partial(analyze._release_singleflight, "A"),
		}[path]
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
