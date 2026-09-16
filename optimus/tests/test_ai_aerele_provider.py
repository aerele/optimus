# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""``Aerele`` is the hosted/managed AI provider option (token packs from
aerele.in instead of your own Anthropic / OpenAI key). It is TEMPORARILY
DISABLED until Aerele's billing and managed LLM gateway are ready: removed
from the ``ai_provider`` Select and commented out of ``_PROVIDER_DEFAULTS``
in ai_fix.py.

These tests guard that disabled state so the option can't reappear by
accident. To re-enable, restore the provider entry + Select option (see the
comment in ai_fix.py) and flip these assertions back. The
``_aerele_call_metadata`` wiring stays intact (covered by test_ai_fix.py);
only the selectable provider is switched off.
"""

from __future__ import annotations

import json
import pathlib

from optimus import ai_fix


class TestAereleProviderDisabled:
	def test_aerele_not_in_provider_defaults(self):
		"""While disabled, Aerele must NOT resolve as a provider so a
		stale ``ai_provider = "Aerele"`` setting fails cleanly with the
		'Unknown AI provider' error rather than silently calling a
		not-yet-ready endpoint."""
		assert "Aerele" not in ai_fix._PROVIDER_DEFAULTS

	def test_aerele_not_in_provider_select_options(self):
		"""The DocType's ``ai_provider`` Select must NOT offer Aerele while
		it's disabled, so operators can't pick it."""
		settings_json = (
			pathlib.Path(__file__).parent.parent
			/ "optimus" / "doctype" / "optimus_settings" / "optimus_settings.json"
		)
		doc = json.loads(settings_json.read_text())
		by_name = {f["fieldname"]: f for f in doc["fields"]}
		options = by_name["ai_provider"]["options"]
		assert "Aerele" not in options.split("\n")
