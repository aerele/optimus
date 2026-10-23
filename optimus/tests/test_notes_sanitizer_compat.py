"""Render rich notes through both supported sanitizer signatures."""

import re
import sys
from types import ModuleType

import pytest
from bs4 import BeautifulSoup

from optimus import renderer
from optimus.tests.test_renderer_v3 import _fake_doc


@pytest.mark.parametrize("legacy", [True, False])
def test_rich_notes_survive_legacy_signature_with_resource_tags_removed(monkeypatch, legacy):
	calls = []
	def old(value, *, always_sanitize=False):
		assert always_sanitize
		calls.append(value)
		return re.sub(r"<script\b[^>]*>.*?</script>", "", value, flags=re.S | re.I)
	def modern(value, *, always_sanitize=False, disallowed_tags=()):
		value = old(value, always_sanitize=always_sanitize)
		for tag in disallowed_tags:
			value = re.sub(r"<" + tag + r"\b[^>]*>", "", value, flags=re.I)
		return value
	module = ModuleType("frappe.utils.html_utils")
	module.sanitize_html = old if legacy else modern
	monkeypatch.setitem(sys.modules, module.__name__, module)
	notes = '<ol><li>Open Customer list.</li></ol><meta data-note-canary="1"><link data-note-canary="1"><script>BAD()</script>'
	html = renderer.render_raw(_fake_doc(notes=notes), [])
	soup = BeautifulSoup(html, "html.parser")
	assert any("Open Customer list." in li.get_text() for li in soup.find_all("li"))
	assert not soup.select("[data-note-canary]")
	assert "BAD()" not in html and calls
