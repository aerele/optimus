# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""``Aerele`` is the hosted/managed AI provider option (token packs bought at
aerele.in instead of bringing your own Anthropic / OpenAI key). It is
**TEMPORARILY DISABLED** until Aerele's billing + managed LLM gateway are
production-ready removed from the ``ai_provider`` Select and commented out
of ``_PROVIDER_DEFAULTS`` in ai_fix.py.

These tests now GUARD that disabled state so the option can't reappear by
accident before the backend is ready. To RE-ENABLE Aerele, restore the
provider entry + Select option (see the comment in ai_fix.py), then flip
these assertions back to the originals:

    entry = ai_fix._PROVIDER_DEFAULTS["Aerele"]
    assert entry["protocol"] == "openai"
    assert entry["needs_key"] is True
    assert entry["base_url"].startswith("https://") and "aerele" in entry["base_url"]
    assert entry["model"]
    assert "Aerele" in options.split("\\n")

The ``_aerele_call_metadata`` wiring is intentionally left intact in ai_fix.py
(and still covered by test_ai_fix.py), ready for re-enable only the
selectable provider is switched off.
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
