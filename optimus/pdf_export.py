# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Lazy PDF generation for the report.

Generated on first request via frappe.utils.pdf.get_pdf (wkhtmltopdf) and cached
to a private File attachment on the Optimus Session; later requests serve from
cache. The analyze pipeline is never touched.
"""

import html
import re
import uuid

import frappe
from frappe import _

from optimus import sql_work
from optimus.artifacts import artifact_file, read_artifact
from optimus.managed import artifact_writer

# v0.5.2 round 4: wkhtmltopdf uses old QtWebKit which doesn't reliably
# render collapsed <details> content even under @media print the
# Observations subsection and Analyzer notes section end up invisible
# in the PDF. We pre-process the HTML to force `open` on every
# <details> before handing it to wkhtmltopdf, so PDF reports contain
# everything the reader sees when they expand sections in the browser.
#
# Pattern matches:  <details class="section">     → <details class="section" open>
#                   <details class="subsection">  → <details class="subsection" open>
#                   <details id="foo">            → <details id="foo" open>
# Already-open details are left alone (idempotent).
_DETAILS_OPEN_RE = re.compile(
	r"<details\b([^>]*?)>",
	re.IGNORECASE,
)


def get_or_generate_pdf(session_uuid: str) -> str:
	"""Return the file_url of the cached PDF, generating on first call.

	Permission check is the caller's responsibility (api.download_pdf).
	Raises on generation failure; does NOT cache failed generations.
	"""
	doc = _load_session(session_uuid)
	if artifact_file(doc.name, "raw_report_pdf_file", doc.raw_report_pdf_file):
		return doc.raw_report_pdf_file
	source = doc.raw_report_file
	content = _load_raw_html(doc)
	# End the read snapshot before the slow external renderer. No parent lock
	# or transaction is held while wkhtmltopdf runs.
	_transaction(lambda: None)
	pdf_bytes = _html_to_pdf(content)

	def publish():
		parent = frappe.db.get_value("Optimus Session", doc.name, "*", as_dict=True, for_update=True)
		if (not parent or parent.get("session_uuid") != session_uuid
			or parent.get("status") != "Ready" or parent.get("raw_report_file") != source):
			frappe.throw(_("The report changed during PDF generation. Please try again."))
		cached = parent.get("raw_report_pdf_file")
		if artifact_file(doc.name, "raw_report_pdf_file", cached):
			return cached
		url = _save_pdf_attachment(doc.name, session_uuid, pdf_bytes)
		frappe.db.set_value("Optimus Session", doc.name, "raw_report_pdf_file", url)
		return url

	return _transaction(publish)


def clear_cached_pdf(session_uuid: str) -> None:
	"""Clear the cached PDF for a session. Called from retry_analyze.

	Best-effort: if the File row is gone or the field isn't set, no-op.
	"""
	docname = frappe.db.get_value(
		"Optimus Session", {"session_uuid": session_uuid}, "name",
	)
	if not docname:
		return
	_transaction(lambda: None)

	def clear():
		parent = frappe.db.get_value("Optimus Session", docname, "*", as_dict=True, for_update=True)
		if not parent or parent.get("session_uuid") != session_uuid:
			return
		file = artifact_file(docname, "raw_report_pdf_file", parent.get("raw_report_pdf_file"))
		if file:
			frappe.delete_doc("File", file.name, force=True, ignore_permissions=True)
		frappe.db.set_value("Optimus Session", docname, "raw_report_pdf_file", None)

	_transaction(clear)


def _load_session(session_uuid: str):
	docname = frappe.db.get_value(
		"Optimus Session", {"session_uuid": session_uuid}, "name",
	)
	if not docname:
		frappe.throw(_("No Optimus Session found for uuid {0}").format(html.escape(str(session_uuid))))
	return frappe.get_doc("Optimus Session", docname)


def _load_raw_html(doc) -> str:
	if not doc.raw_report_file:
		frappe.throw(_("This session has no report to convert."))
	content = read_artifact(doc.name, "raw_report_file", doc.raw_report_file)
	return content.decode("utf-8") if isinstance(content, bytes) else content


def _transaction(operation):
	return sql_work.transaction(operation, commit=frappe.db.commit, rollback=frappe.db.rollback)


def _safe_pdf_html(content):
	"""Remove pdfkit's option channel and credential-dependent image expansion.

	Frappe 15 parses HTML again before pdfkit reads its meta options. Remove
	matching raw tags to a fixed point both before and after HTML normalization,
	including matches hidden in comments, raw-text elements or attribute values.
	"""
	from bs4 import BeautifulSoup
	from tinycss2 import parse_component_value_list

	def strip_meta(value):
		while True:
			clean = re.sub(r"<meta\b[^>]*pdfkit-[^>]*>", "", value, flags=re.I)
			if clean == value:
				return clean
			value = clean

	soup = BeautifulSoup(strip_meta(content), "html5lib")
	# Frappe supplies the requesting user's cookie jar to wkhtmltopdf. Keep
	# CSS, SVG and other resource channels from fetching authenticated images
	# after the img-only inliner, too. Raster data images need no credentials.
	def raster(value):
		return bool(re.match(r"data:image/(?:png|jpeg|gif|webp|avif);base64,", str(value).strip(), re.I))

	def resource_css(tokens):
		for token in tokens:
			if token.type in {"url", "error"} or (
				token.type == "function" and token.lower_name == "url"
			) or (token.type == "at-keyword" and token.lower_value == "import"):
				return True
			children = getattr(token, "arguments", None) or getattr(token, "content", None)
			if children and resource_css(children):
				return True
		return False

	for tag in soup.find_all(["iframe", "frame", "object", "embed", "link", "base", "audio", "video", "source"]):
		tag.decompose()
	for tag in soup.find_all(True):
		for attr in ("src", "poster", "background", "data", "xlink:href"):
			if attr in tag.attrs and not (tag.name == "img" and attr == "src" and raster(tag[attr])):
				if not str(tag[attr]).startswith("#"):
					if tag.name == "img" and attr == "src":
						tag[attr] = ""
					else:
						tag.attrs.pop(attr, None)
		if tag.name == "img":
			tag.attrs.setdefault("src", "")  # Frappe 15 indexes this attribute in its inliner.
		if tag.name != "a" and "href" in tag.attrs and not str(tag["href"]).startswith("#"):
			tag.attrs.pop("href", None)
		tag.attrs.pop("srcset", None)
		if tag.has_attr("style") and resource_css(parse_component_value_list(tag["style"])):
			tag.attrs.pop("style", None)
		if tag.name == "style" and (
			tag.string is None or resource_css(parse_component_value_list(str(tag.string)))
		):
			tag.decompose()
	return strip_meta(str(soup))


def _expand_collapsible_sections(html: str) -> str:
	"""Add the ``open`` attribute to every <details> element so the
	PDF renderer shows their content.

	Idempotent <details open> and <details ... open> are left alone.
	Exposed as module-level for unit-testing.
	"""
	def _inject_open(match: re.Match) -> str:
		attrs = match.group(1).strip()
		# Already open? Preserve as-is.
		if re.search(r"\bopen\b", attrs, re.IGNORECASE):
			return match.group(0)
		if attrs:
			return f"<details {attrs} open>"
		return "<details open>"

	return _DETAILS_OPEN_RE.sub(_inject_open, html)


def _html_to_pdf(html: str) -> bytes:
	"""Run HTML through wkhtmltopdf via frappe.utils.pdf.get_pdf.

	Options tuned for the report (A4, conservative margins, UTF-8, print-media-type
	so @media print rules are honored). Force-opens all <details> blocks first,
	since wkhtmltopdf's QtWebKit doesn't reliably expand collapsed <details>.
	"""
	import frappe.utils.pdf

	html = _safe_pdf_html(_expand_collapsible_sections(html))

	options = {
		"page-size": "A4",
		"margin-top": "15mm",
		"margin-bottom": "15mm",
		"margin-left": "12mm",
		"margin-right": "12mm",
		"encoding": "UTF-8",
		"print-media-type": "",
	}
	return frappe.utils.pdf.get_pdf(html, options)


def _save_pdf_attachment(docname: str, session_uuid: str, pdf_bytes: bytes) -> str:
	"""Insert a private File attached to the Optimus Session.

	Returns the file_url. Raises on failure (caller decides not to cache).
	"""
	filename = f"optimus_report_{session_uuid}_{uuid.uuid4().hex}.pdf"
	file_doc = frappe.get_doc({
		"doctype": "File",
		"file_name": filename,
		"attached_to_doctype": "Optimus Session",
		"attached_to_name": docname,
		"attached_to_field": "raw_report_pdf_file",
		"is_private": 1,
		"content": pdf_bytes,
	})
	with artifact_writer(docname, "raw_report_pdf_file"):
		file_doc.insert(ignore_permissions=True)
	return file_doc.file_url
