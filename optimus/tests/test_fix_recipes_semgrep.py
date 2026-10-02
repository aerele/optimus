# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Generated index recipes must pass the pinned Frappe Semgrep rules."""

import pytest

from optimus.tests.ai_eval_support import load, require_semgrep
from optimus.tests.test_fix_recipes_index import _every_recipe

pytestmark = pytest.mark.semgrep


def test_index_recipe_code_passes_pinned_semgrep():
	rules = require_semgrep()
	codes = sorted({r["code"] for r in _every_recipe() if r and r.get("code")})
	assert len(codes) >= 5
	texts = {f"recipe{i}": f"```python\n{code}```" for i, code in enumerate(codes)}
	scanned = load("_semgrep").scan_texts(texts, rules)
	assert {key: [h["rule"] for h in value.hits] for key, value in scanned.items() if value.hits} == {}
	assert sum(value.unparsed for value in scanned.values()) == 0
	assert all(value.units == 1 for value in scanned.values())
