# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for lazy PDF export."""

import pytest

from optimus import pdf_export


@pytest.fixture
def mock_session(monkeypatch):
	from types import SimpleNamespace

	import frappe

	state = {"pdf_call_count": 0}

	doc = SimpleNamespace(
		name="PS-001",
		session_uuid="uuid-001",
		raw_report_file="/private/files/safe_report_uuid-001.html",
		raw_report_pdf_file=None,
	)

	class FakeFileRow:
		name = "html-file"
		def get_content(self):
			return b"<html><body>fake report</body></html>"

	class FakeFileDoc:
		file_url = "/private/files/optimus_raw_report_uuid-001.pdf"

		def insert(self, ignore_permissions=False):
			pass

	class FakeDB:
		def get_value(self, doctype, filters, field=None, as_dict=False, **kwargs):
			if doctype == "File":
				url = filters.get("file_url")
				return "html-file" if url == doc.raw_report_file else ("pdf-file" if url and url == doc.raw_report_pdf_file else None)
			if isinstance(filters, dict) and filters.get("session_uuid") == "uuid-001":
				return doc.name
			if doctype == "Optimus Session" and filters == "PS-001":
				if field == "*":
					return {**vars(doc), "status": "Ready"}
				return doc.raw_report_pdf_file if field == "raw_report_pdf_file" else None
			return None

		def set_value(self, doctype, name, field, value=None):
			if isinstance(field, dict):
				for k, v in field.items():
					setattr(doc, k, v)
			else:
				setattr(doc, field, value)

		def commit(self):
			pass

		def rollback(self):
			pass

	def fake_get_doc(doctype, name=None):
		if doctype == "Optimus Session" and name == "PS-001":
			return doc
		if isinstance(doctype, dict) and doctype.get("doctype") == "File":
			return FakeFileDoc()
		if doctype == "File":
			return FakeFileRow()
		return None

	def fake_get_pdf(html, options=None):
		state["pdf_call_count"] += 1
		return b"%PDF-1.4 fake pdf bytes"

	import frappe.utils.pdf as _pdf_module

	monkeypatch.setattr(frappe, "db", FakeDB(), raising=False)
	monkeypatch.setattr(frappe, "get_doc", fake_get_doc, raising=False)
	monkeypatch.setattr(_pdf_module, "get_pdf", fake_get_pdf, raising=False)

	return doc, state


def test_first_call_generates_and_caches(mock_session):
	doc, state = mock_session
	url = pdf_export.get_or_generate_pdf("uuid-001")
	assert url is not None
	assert "pdf" in url
	assert state["pdf_call_count"] == 1
	assert doc.raw_report_pdf_file == url


def test_second_call_returns_cached_url(mock_session):
	doc, state = mock_session
	url1 = pdf_export.get_or_generate_pdf("uuid-001")
	url2 = pdf_export.get_or_generate_pdf("uuid-001")
	assert url1 == url2
	assert state["pdf_call_count"] == 1


def test_generation_failure_raises_and_does_not_cache(monkeypatch, mock_session):
	import frappe.utils.pdf as _pdf_module

	doc, state = mock_session
	# Reset doc's cache slot
	doc.raw_report_pdf_file = None

	def fake_get_pdf_failure(html, options=None):
		raise RuntimeError("wkhtmltopdf crashed")

	monkeypatch.setattr(_pdf_module, "get_pdf", fake_get_pdf_failure, raising=False)

	with pytest.raises(Exception):
		pdf_export.get_or_generate_pdf("uuid-001")

	assert doc.raw_report_pdf_file is None


def test_pdf_normalizer_blocks_authenticated_resource_channels():
	from bs4 import BeautifulSoup
	html='''<html><head><meta charset="utf-8"><meta name="PDFKIT-cookie-jar" content="/tmp/forbidden"><style>@import "https://example.invalid/x";</style><style>body{color:blue}</style></head><body><img src="/private/files/secret.png"><img src="data:image/png;base64,aGVsbG8="><img src="data:image/svg+xml;base64,PHN2Zz4="><div style="background:u\\72l('/private/files/secret.png')">text</div><svg><image href="/private/files/secret.png"/></svg><iframe srcdoc="image"></iframe></body></html>'''
	result=pdf_export._safe_pdf_html(html)
	soup=BeautifulSoup(result,'html5lib')
	assert soup.find('meta',charset='utf-8')
	assert 'pdfkit-' not in result.lower()
	assert '/private/files/secret.png' not in result
	assert [image['src'] for image in soup.find_all('img')]==['','data:image/png;base64,aGVsbG8=','']
	assert not soup.find('iframe')
	assert 'body{color:blue}' in result
	assert not soup.find('div').has_attr('style')
