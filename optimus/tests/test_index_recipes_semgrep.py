# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The generated ensure_indexes() modules pass the pinned Frappe Semgrep rules (its
commit sits inside the per-entry try, which frappe-manual-commit allows)."""

import pytest

from optimus.tests.ai_eval_support import load, require_semgrep
from optimus.tests.test_index_recipes import _every_advice

pytestmark = pytest.mark.semgrep


def test_generated_ensure_indexes_code_passes_pinned_semgrep():
	rules = require_semgrep()
	codes = sorted({a.code for a in _every_advice() if a is not None and a.code})
	assert len(codes) >= 5
	texts = {f"recipe{i}": f"```python\n{code}```" for i, code in enumerate(codes)}
	scanned = load("_semgrep").scan_texts(texts, rules)
	assert {key: [h["rule"] for h in value.hits] for key, value in scanned.items() if value.hits} == {}
	assert sum(value.unparsed for value in scanned.values()) == 0
	assert all(value.units == 1 for value in scanned.values())
