# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""AI Markdown renders through md_to_html and a strict nh3
allowlist. No images, forms, inline styles, ids, scripts or javascript:
links; an anchor's href survives only for a small set of trusted
documentation hosts (an attribute_filter host allowlist); generics and HTML
inside code stay visible as escaped text; diff colouring still applies."""

import sys

import pytest

from optimus.renderer import finding_enrichment as fe

_HOSTILE = [
	"![p](https://attacker.example/p.png)",
	'<img src="https://attacker.example/p.png" onerror="alert(1)">',
	'<form action="https://attacker.example/steal"><input type="password" name="pw"><button>Sign in</button></form>',
	'<div style="position:fixed;top:0;left:0;width:100%;height:100%">overlay</div>',
	'<p style="color:red">styled</p>',
	"[click](javascript:alert(1))",
	'<a href="JaVaScRiPt:alert(1)">x</a>',
	'<a href="&#x6A;avascript:alert(1)">x</a>',
	'<iframe src="https://attacker.example/frame"></iframe>',
	'<svg onload="alert(1)"><image href="https://attacker.example/i.png"/></svg>',
	"<script>alert(1)</script>",
	"<style>body{display:none}</style>",
	'<link rel="stylesheet" href="https://attacker.example/x.css">',
	'<meta http-equiv="refresh" content="0;url=https://attacker.example">',
	'<object data="https://attacker.example/x.swf"></object><embed src="https://attacker.example/y">',
	'<base href="https://attacker.example/">',
	'<table background="https://attacker.example/bg.png"><tr><td background="https://attacker.example/c.png">c</td></tr></table>',
	'<video poster="https://attacker.example/p.png" src="https://attacker.example/v.mp4"></video>',
	'<h2 id="top">Heading</h2>\n\n## Markdown heading',
	"<!-- hidden comment -->",
	"[off-domain](https://attacker.example/x)",
]
_FORBIDDEN = (
	"<img", "<form", "<input", "<button", "<iframe", "<svg", "<image", "<script", "<style", "<link",
	"<meta", "<object", "<embed", "<base", "<video", "style=", "javascript:", "onerror", "onload",
	"src=", "background=", "poster=", " id=", "<!--", "attacker.example",
)


# Every href the link tests use, grouped by
# the guard that refuses it, so the agreement test below covers the whole set.
_BACKSLASH_DIFFERENTIAL = "https://evil.com\\@docs.frappe.io/x"
_PERCENT_BACKSLASH = ["https://evil.com%5C@docs.frappe.io/x", "https://evil.com%5c@docs.frappe.io/x"]
_USERINFO = ["https://user@docs.frappe.io/x", "https://user:pw@docs.frappe.io/x", "https://evil.com%40docs.frappe.io/x"]
_PORTS = ["https://docs.frappe.io:8443/x", "https://docs.frappe.io:443/x"]
_LOOKALIKES = [
	"https://evil.docs.frappe.io/x",
	"https://www.frappeframework.com/x",
	"https://docs.frappe.io./x",
	"https://frappe.io/x",
	"https://gist.github.com/frappe/x",
	"https://github.com.attacker.example/frappe/x",
]
_DOT_SEGMENT_ESCAPES = [
	"https://github.com/frappe/../someoneelse/x",
	"https://github.com/frappe/..\\someoneelse/x",
	"https://github.com/frappe/%2e%2e/someoneelse/x",
	"https://github.com/frappe/%2E%2E/someoneelse/x",
]
_BAD_CHARACTERS = [
	"https://docs.frappe.io/a b",
	"https://docs.frappe.io/\tx",
	"https://docs.frappe.io/x\x00",
	"https://docs.frappe.io/café",
	"https://docs.frappe.іo/x",
]
_NON_HTTP = ["javascript://docs.frappe.io/%0aalert(1)", "ftp://docs.frappe.io/x", "//docs.frappe.io/x"]
_CYCLE1_REJECTED = [
	"https://attacker.example/phish",
	"https://frappeframework.com.attacker.example/x",
	"https://docs.frappe.io.attacker.example/x",
	"https://github.com/someoneelse/thing",
	"mailto:support@example.com",
	"javascript:alert(1)",
	"JaVaScRiPt:alert(1)",
	"",
]
_ALLOWED_HREFS = [
	"http://docs.frappe.io/x",
	"https://DOCS.FRAPPE.IO/x",
	"https://docs.erpnext.com/",
	"https://github.com/frappe",
	"https://github.com/frappe/frappe/issues/123?x=1#y",
]
_CYCLE1_ALLOWED = [
	"https://frappeframework.com",
	"https://docs.frappe.io/framework/user/en/introduction",
	"https://github.com/frappe/frappe/issues/123",
]
_REJECTED_HREFS = (
	[_BACKSLASH_DIFFERENTIAL] + _PERCENT_BACKSLASH + _USERINFO + _PORTS + _LOOKALIKES
	+ _DOT_SEGMENT_ESCAPES + _BAD_CHARACTERS + _NON_HTTP + _CYCLE1_REJECTED
)


@pytest.mark.parametrize("snippet", _HOSTILE)
def test_hostile_markdown_is_stripped(snippet):
	out = fe._markdown_to_safe_html("**Fix**\n\n" + snippet + "\n").lower()
	for bad in _FORBIDDEN:
		assert bad not in out, (bad, out)


def test_generics_do_not_block_markdown():
	"""frappe.utils.markdown returned the text unconverted when it saw a
	``<...>`` pair; md_to_html converts it."""
	out = fe._markdown_to_safe_html("**Fix**\n\nUse `List<int>` here and a < b > c.\n\n```python\nx = 1\n```\n")
	assert "<strong>Fix</strong>" in out
	assert "<code>List&lt;int&gt;</code>" in out
	assert "<pre>" in out


def test_html_inside_code_fence_is_escaped_not_stripped():
	"""HTML and Jinja in a fenced block stay readable as text."""
	md = '```html\n<form action="/x"><img src="/logo.png"></form>\n{{ doc.name }}\n```\n'
	out = fe._markdown_to_safe_html(md)
	assert "&lt;form" in out and "&lt;img" in out and "{{ doc.name }}" in out
	assert "<form" not in out and "<img" not in out


def test_links_keep_https_and_get_rel():
	out = fe._markdown_to_safe_html("[docs](https://frappeframework.com)")
	assert 'href="https://frappeframework.com"' in out
	assert 'rel="noopener noreferrer nofollow"' in out


def test_allowed_host_with_path_keeps_href():
	out = fe._markdown_to_safe_html("[docs](https://docs.frappe.io/framework/user/en/introduction)")
	assert 'href="https://docs.frappe.io/framework/user/en/introduction"' in out


def test_github_frappe_subpath_keeps_href():
	out = fe._markdown_to_safe_html("[repo](https://github.com/frappe/frappe/issues/123)")
	assert 'href="https://github.com/frappe/frappe/issues/123"' in out


def test_github_non_frappe_path_is_rejected():
	"""a real GitHub org that isn't /frappe still loses href."""
	out = fe._markdown_to_safe_html("[repo](https://github.com/someoneelse/thing)")
	assert "href=" not in out
	assert "repo" in out


def test_off_domain_href_is_dropped_text_kept():
	out = fe._markdown_to_safe_html("[click here](https://attacker.example/phish)")
	assert "href=" not in out
	assert "attacker.example" not in out
	assert "click here" in out


def test_subdomain_spoof_is_rejected():
	"""a suffix trick on an allowed host is not the host."""
	out = fe._markdown_to_safe_html("[evil](https://frappeframework.com.attacker.example/x)")
	assert "href=" not in out


def test_host_prefix_spoof_is_rejected():
	out = fe._markdown_to_safe_html("[evil](https://docs.frappe.io.attacker.example/x)")
	assert "href=" not in out


def test_mailto_link_has_no_allowlisted_host_so_href_is_dropped():
	"""mailto: is not http/https and has no AI_LINK_HOSTS host, so its href is
	always dropped; the link text still renders as plain text."""
	out = fe._markdown_to_safe_html("[mail us](mailto:support@example.com)")
	assert "href=" not in out
	assert "mail us" in out


def test_backslash_userinfo_differential_is_rejected():
	"""urlparse reads host docs.frappe.io, a browser
	(WHATWG URL) treats the backslash as "/" and opens evil.com."""
	assert not fe.ai_link_allowed(_BACKSLASH_DIFFERENTIAL)
	for md in (
		f"[docs]({_BACKSLASH_DIFFERENTIAL})",
		f'<a href="{_BACKSLASH_DIFFERENTIAL}">docs</a>',
		'<a href="https://evil.com&#92;@docs.frappe.io/x">docs</a>',
	):
		out = fe._markdown_to_safe_html(md)
		assert "href=" not in out, (md, out)
		assert "docs" in out


@pytest.mark.parametrize("href", _PERCENT_BACKSLASH)
def test_percent_encoded_backslash_is_rejected(href):
	assert not fe.ai_link_allowed(href)
	assert "href=" not in fe._markdown_to_safe_html(f"[docs]({href})")


@pytest.mark.parametrize("href", _USERINFO)
def test_userinfo_host_is_rejected(href):
	assert not fe.ai_link_allowed(href)
	assert "href=" not in fe._markdown_to_safe_html(f"[docs]({href})")


@pytest.mark.parametrize("href", _PORTS)
def test_port_is_rejected(href):
	assert not fe.ai_link_allowed(href)
	assert "href=" not in fe._markdown_to_safe_html(f"[docs]({href})")


@pytest.mark.parametrize("href", _LOOKALIKES)
def test_lookalike_subdomain_is_rejected(href):
	assert not fe.ai_link_allowed(href)
	assert "href=" not in fe._markdown_to_safe_html(f"[docs]({href})")


@pytest.mark.parametrize("href", _DOT_SEGMENT_ESCAPES)
def test_github_dot_segment_escape_is_rejected(href):
	"""A browser resolves each of these to github.com/someoneelse/x."""
	assert not fe.ai_link_allowed(href)
	assert "href=" not in fe._markdown_to_safe_html(f"[repo]({href})")


@pytest.mark.parametrize("href", _BAD_CHARACTERS)
def test_whitespace_control_and_non_ascii_are_rejected(href):
	assert not fe.ai_link_allowed(href)


@pytest.mark.parametrize("href", _NON_HTTP)
def test_non_http_scheme_is_rejected(href):
	assert not fe.ai_link_allowed(href)


@pytest.mark.parametrize("href", _ALLOWED_HREFS)
def test_allowed_links_are_kept(href):
	assert fe.ai_link_allowed(href)
	assert f'href="{href}"' in fe._markdown_to_safe_html(f"[docs]({href})")


def test_attribute_filter_agrees_with_ai_link_allowed():
	"""The render-time filter and the public matcher PR #54's guardrail calls
	must give the same verdict on every href these tests know, so a guardrail
	note can never disagree with what the report renders."""
	allowed = _ALLOWED_HREFS + _CYCLE1_ALLOWED
	assert all(fe.ai_link_allowed(h) for h in allowed)
	assert not any(fe.ai_link_allowed(h) for h in _REJECTED_HREFS)
	mismatches = [
		h for h in allowed + _REJECTED_HREFS
		if fe._ai_html_attribute_filter("a", "href", h) != (h if fe.ai_link_allowed(h) else None)
	]
	assert mismatches == []
	assert fe._ai_html_attribute_filter("a", "title", "user@docs.frappe.io") == "user@docs.frappe.io"


def test_ai_link_hosts_is_the_public_contract():
	"""PR #54's markup guardrail imports these two names instead of keeping
	its own host list or its own matcher."""
	assert fe.AI_LINK_HOSTS == ("frappeframework.com", "docs.frappe.io", "docs.erpnext.com", "github.com/frappe")
	assert callable(fe.ai_link_allowed)


def test_diff_highlighting_still_applies():
	out = fe._markdown_to_safe_html("```diff\n- a = 1\n+ a = 2\n```\n")
	assert '<pre class="dh">' in out
	assert 'class="dh-line dh-del">- a = 1' in out
	assert 'class="dh-line dh-add">+ a = 2' in out


def test_class_is_never_an_allowed_attribute():
	"""nh3 panics (PanicException, a BaseException the fallback cannot catch)
	when "class" is both an allowed attribute and governed by allowed_classes."""
	for tag, attrs in fe._AI_HTML_ATTRIBUTES.items():
		assert "class" not in attrs, tag


def test_code_language_class_survives_without_panic():
	assert 'class="python language-python"' in fe._markdown_to_safe_html("```python\nx = 1\n```\n")


def test_markdown2_fallback_matches_frappe(monkeypatch):
	sample = "**Fix**\n\nUse `List<int>`.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```diff\n- a\n+ b\n```\n"
	with_frappe = fe._markdown_to_safe_html(sample)
	monkeypatch.setitem(sys.modules, "frappe.utils.data", None)
	assert fe._markdown_to_safe_html(sample) == with_frappe


def test_none_and_empty_are_safe():
	assert fe._markdown_to_safe_html(None) == ""
	assert fe._markdown_to_safe_html("") == ""


@pytest.mark.parametrize("href", [None, 42, {}, "https://[invalid/"])
def test_malformed_link_is_refused(href):
	assert fe.ai_link_allowed(href) is False


@pytest.mark.parametrize("failure", ["missing_nh3", "missing_markdown2", "conversion_error", "empty_html", "clean_error"])
def test_render_failure_returns_escaped_text(monkeypatch, failure):
	import html

	raw = '**Fix**\n\n<img src="https://attacker.example/p.png"> & <script>bad()</script>'

	def fail(*args, **kwargs):
		raise ValueError("synthetic failure")

	if failure == "missing_nh3":
		monkeypatch.setitem(sys.modules, "nh3", None)
	elif failure == "missing_markdown2":
		monkeypatch.setitem(sys.modules, "frappe.utils.data", None)
		monkeypatch.setitem(sys.modules, "markdown2", None)
	elif failure == "conversion_error":
		monkeypatch.setattr(fe, "_md_to_html", fail)
	elif failure == "empty_html":
		monkeypatch.setattr(fe, "_md_to_html", lambda raw: "")
	else:
		import nh3

		monkeypatch.setattr(nh3, "clean", fail)
	assert fe._markdown_to_safe_html(raw) == "<pre>" + html.escape(raw) + "</pre>"
