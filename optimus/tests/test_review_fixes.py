# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Regression guards for the post-review fixes (api needs a live bench for true
integration, so the contract is pinned by source-inspection, matching
test_suggest_fix_api.py)."""

import os
import re

_HERE = os.path.dirname(__file__)
_API_PATH = os.path.join(_HERE, "..", "api.py")


def _read(path: str) -> str:
	with open(path, encoding="utf-8") as f:
		return f.read()


def _fn_body(src: str, name: str) -> str:
	start = src.index(f"def {name}(")
	search_from = src.find("\n", start) + 1
	nxt = re.search(r"\n(?:def |@frappe\.whitelist)", src[search_from:])
	end = search_from + (nxt.start() if nxt else len(src) - search_from)
	return src[start:end]


# --- HIGH-1: steps tokens captured on the auto-analyze path -----------------


# --- export_session parity --------------------------------------------------


class TestExportSessionTokenParity:
	def test_export_includes_token_fields(self):
		body = _fn_body(_read(_API_PATH), "export_session")
		assert '"ai_tokens_spent"' in body
		assert '"ai_refresh_count"' in body
		assert '"ai_steps_tokens"' in body
