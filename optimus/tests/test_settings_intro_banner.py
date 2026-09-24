# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Guards for the Optimus Settings intro banner.

The app-wide intro must render as a single id-scoped element OUTSIDE Frappe's
shared .form-message-container. An earlier fix cleared the whole container to
dedupe the banner, but that also wiped sibling banners Frappe renders there
(e.g. the concurrent-edit warning from form.js show_conflict_message), so the
clobbering approach must not come back.
"""

import os
import re


def _read_js() -> str:
	here = os.path.dirname(__file__)
	path = os.path.join(
		here, "..", "optimus", "doctype", "optimus_settings", "optimus_settings.js"
	)
	with open(path) as f:
		return f.read()


def _strip_comments(js: str) -> str:
	"""Drop // line comments so assertions match on code, not prose."""
	return "\n".join(re.sub(r"//.*$", "", line) for line in js.splitlines())


class TestSettingsIntroBanner:
	def test_intro_is_id_scoped_element(self):
		"""The intro renders through _render_settings_intro into an element with
		the stable .optimus-settings-intro class, so it can be found-or-created
		(idempotent) instead of appended each refresh."""
		js = _read_js()
		assert "_render_settings_intro(frm)" in js, (
			"refresh must render the intro via _render_settings_intro"
		)
		assert "optimus-settings-intro" in js, (
			"the intro element must carry the id-scoped .optimus-settings-intro "
			"class so repeated refreshes reuse the one element"
		)

	def test_does_not_clear_the_shared_message_container(self):
		"""clear_headline() empties the whole .form-message-container, which would
		wipe sibling banners Frappe puts there (the concurrent-edit warning). It
		must not be called from this form's script."""
		code = _strip_comments(_read_js())
		assert "clear_headline(" not in code, (
			"optimus_settings.js must not call clear_headline() it clobbers "
			"sibling banners in the shared message container"
		)

	def test_does_not_use_appending_set_intro(self):
		"""frm.set_intro appends a new .form-message per call (layout.js
		show_message), which is what stacked the duplicate. The id-scoped element
		replaces it, so set_intro must not be called."""
		code = _strip_comments(_read_js())
		assert "set_intro(" not in code, (
			"optimus_settings.js must not call set_intro it appends and stacks; "
			"use the id-scoped _render_settings_intro element instead"
		)

	def test_intro_rendered_outside_message_container(self):
		"""The helper must prepend the intro to .form-layout (outside
		.form-message-container) so it never collides with sibling banners and
		survives Frappe's own container clearing."""
		js = _read_js()
		m = re.search(r"function _render_settings_intro\(frm\)\s*\{.*?\n\}", js, re.DOTALL)
		assert m, "_render_settings_intro helper not found"
		body = m.group(0)
		assert ".form-layout" in body and "prepend" in body, (
			"the intro must be prepended to .form-layout (outside the shared "
			".form-message-container)"
		)
		# It must not target the message container directly.
		assert "form-message-container" not in body, (
			"the intro helper must not insert into .form-message-container"
		)
