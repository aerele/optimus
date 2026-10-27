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


@pytest.mark.parametrize("header", [False, True])
@pytest.mark.parametrize("content", [
	'<svg><style><!-- comment -->text{fill:url("/private/files/secret.svg")}</style></svg>',
	'<svg><style><a href="/private/files/secret.svg">text</a></style></svg>',
	'<svg><style><g/><a href="/private/files/secret.svg">text</a></style></svg>',
	'<svg><style>&lt;/style&gt;&lt;img src=/private/files/secret.svg&gt;</style></svg>',
	'<svg><style><![CDATA[</style><img src=/private/files/secret.svg>]]></style></svg>',
	'<svg><script>&lt;/script&gt;&lt;img src=/private/files/secret.svg&gt;</script></svg>',
	'<svg><script><![CDATA[</script><img src=/private/files/secret.svg>]]></script></svg>',
])
def test_pdf_normalizer_checks_foreign_style_with_comment_children(content, header):
	from bs4 import BeautifulSoup

	from optimus.pdf_export import _safe_pdf_html

	if header:
		content = '<div id="header-html">' + content + '</div>'
	clean = _safe_pdf_html(content)
	assert "/private/files/secret.svg" not in clean
	post = BeautifulSoup(clean, "html5lib")
	assert not post.find("img")
	assert not BeautifulSoup(str(post), "html.parser").find("img")


@pytest.mark.parametrize("header", [False, True])
@pytest.mark.parametrize("encoding", ["raw", "&lt;", "&#60;", "&#x3c;"])
@pytest.mark.parametrize("leaf", ["mglyph", "malignmark"])
@pytest.mark.parametrize("parent", ["mtext", "mi"])
@pytest.mark.parametrize("table", ["table", "tbody"])
def test_pdf_normalizer_stays_safe_across_namespace_changes(leaf, parent, table, encoding, header):
	from bs4 import BeautifulSoup

	payload = '<img src="/private/files/canary.png">'
	if encoding != "raw":
		payload = ("</style>" + payload).replace("<", encoding).replace(">", "&gt;")
	body = f"<{leaf}><style>{payload}</style></{leaf}>"
	if table == "tbody":
		body = "<tbody>" + body + "</tbody>"
	content = f"<math><{parent}><table>{body}</table></{parent}></math>"
	if header:
		content = '<div id="header-html">' + content + '</div>'
	clean = pdf_export._safe_pdf_html(content)
	allowed = {tag.name for tag in BeautifulSoup(clean, "html5lib").find_all(True)} | {"html", "head", "body", "tbody"}
	for parser in ("html5lib", "html.parser"):
		soup = BeautifulSoup(clean, parser)
		assert {tag.name for tag in soup.find_all(True)} <= allowed
		assert not any(tag.get("src") for tag in soup.find_all("img"))
		assert not soup.find("meta", attrs={"name": lambda value: value and value.lower().startswith("pdfkit-")})
		assert "<!--" not in str(soup)
		clean = str(soup)


@pytest.mark.parametrize("legacy", [False, True])
def test_pdf_normalizer_keeps_real_report_css_without_raw_markup(legacy):
	from bs4 import BeautifulSoup
	from tinycss2 import parse_component_value_list, serialize

	from optimus import renderer
	from optimus.tests.test_renderer_v3 import _fake_doc

	content = renderer.render_raw(_fake_doc(), [])
	if legacy:
		from pathlib import Path
		content = (Path(__file__).parents[2] / "docs/optimus_raw_report_eb7d4af777c13a4e821529.html").read_text()
	before = BeautifulSoup(content, "html5lib").find_all("style")
	after = BeautifulSoup(pdf_export._safe_pdf_html(content), "html5lib").find_all("style")
	assert len(before) == len(after) > 0
	for original, sanitized in zip(before, after, strict=True):
		assert "<" not in str(sanitized.string) and "&" not in str(sanitized.string)
		assert serialize(parse_component_value_list(str(original.string), skip_comments=True)) == serialize(
			parse_component_value_list(str(sanitized.string), skip_comments=True)
		)


def test_pdf_normalizer_canonicalizes_declarations_and_keeps_visible_code():
	from bs4 import BeautifulSoup, Comment, Doctype

	content = '<!DOCTYPE html PUBLIC "example"><!-- -- > <img src=/private/files/canary.png> --><pre>&lt;!-- visible code --&gt;</pre>'
	clean = pdf_export._safe_pdf_html(content)
	for parser in ("html5lib", "html.parser"):
		soup = BeautifulSoup(clean, parser)
		assert [str(node) for node in soup.contents if isinstance(node, Doctype)] == ["html"]
		assert not soup.find_all(string=lambda node: isinstance(node, Comment))
		assert not any(tag.get("src") for tag in soup.find_all("img"))
		assert soup.pre.get_text() == "<!-- visible code -->"
		clean = str(soup)


def test_pdf_normalizer_withholds_markup_in_css_but_keeps_comment_free_css():
	from bs4 import BeautifulSoup

	content = '<style>/* <img src=/private/files/canary.png> */body{color:blue}</style>'
	soup = BeautifulSoup(pdf_export._safe_pdf_html(content), "html5lib")
	assert soup.style is not None and "body{color:blue}" in str(soup.style.string)
	assert "<" not in str(soup.style.string)
	assert not BeautifulSoup(pdf_export._safe_pdf_html('<style>p::before{content:"<img>"}</style>'), "html5lib").find("style")


@pytest.mark.parametrize("header", [False, True])
@pytest.mark.parametrize("payload", [
	"<!-- -- > <img src=/private/files/canary.png> -->",
	"<?bogus <img src=/private/files/canary.png>>",
	'<!DOCTYPE html PUBLIC "<img src=/private/files/canary.png>">',
])
def test_pdf_normalizer_removes_raw_declarations(payload, header):
	from bs4 import BeautifulSoup, Doctype
	from bs4.element import PreformattedString

	if header:
		payload = '<div id="header-html">' + payload + '</div>'
	clean = pdf_export._safe_pdf_html(payload)
	for parser in ("html5lib", "html.parser"):
		soup = BeautifulSoup(clean, parser)
		assert not soup.find("img")
		assert not soup.find_all(string=lambda node: isinstance(node, PreformattedString) and not isinstance(node, Doctype))
		assert all(str(node) == "html" for node in soup.contents if isinstance(node, Doctype))
		clean = str(soup)


@pytest.mark.parametrize("prefix", ["", "<!doctype html>"])
def test_pdf_normalizer_preserves_the_presence_of_a_doctype(prefix):
	from bs4 import BeautifulSoup, Doctype

	clean = pdf_export._safe_pdf_html(prefix + "<html><body>Legacy report</body></html>")
	soup = BeautifulSoup(clean, "html5lib")
	assert [str(node) for node in soup.contents if isinstance(node, Doctype)] == (["html"] if prefix else [])


@pytest.mark.parametrize("markup", ["<img>", "&lt;img&gt;", "&#60;img>", "&#x3c;img>"])
def test_pdf_normalizer_withholds_encoded_markup_in_css(markup):
	from bs4 import BeautifulSoup

	assert not BeautifulSoup(pdf_export._safe_pdf_html('<style>p::before{content:"' + markup + '"}</style>'), "html5lib").find("style")
