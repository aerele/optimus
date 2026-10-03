"""Consent and bounded data minimization at the AI input boundary."""

import json
import math
import re
from urllib.parse import urlsplit

import sqlparse
from sqlparse import tokens as T

MAX_QUERY_CHARS = 16_000
_METHOD = r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+"
_METHOD_PATH = re.compile(r"/api/(?:v\d+/)?method/[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*/?\Z", re.ASCII)
_DOTTED = re.compile(_METHOD + r"\Z", re.ASCII)


def opted_in(value) -> bool:
	return value is True or type(value) is int and value == 1 or isinstance(value, str) and value == "1"


def action_recording(rec) -> dict | None:
	"""Bound and type-check only the metadata used for an AI Steps label."""
	from optimus.ai_fix import _InterruptGuard

	if not isinstance(rec, dict):
		return None
	def text(value, limit=2048):
		return value.strip()[:limit] if isinstance(value, str) else ""

	result = {key: text(rec.get(key)) for key in ("cmd", "path", "method", "event_type")}
	duration = rec.get("duration")
	result["duration"] = round(duration, 1) if type(duration) in (int, float) and 0 <= duration <= 1e12 and math.isfinite(duration) else 0
	fd = rec.get("form_dict")
	fd = fd if isinstance(fd, dict) else {}
	form = {key: text(fd.get(key), 140) for key in ("doctype", "dt", "doc_type", "name", "dn", "action", "method")}
	doc = fd.get("doc")
	guard = _InterruptGuard(base=True)
	try:
		with guard:
			if isinstance(doc, str):
				doc = json.loads(doc) if len(doc) <= 64_000 else None
	except (ValueError, RecursionError):
		doc = None
	if guard.pending():
		rec = fd = doc = form = result = None
		raise guard.interrupt()
	if isinstance(doc, dict):
		form["doc"] = {key: text(doc.get(key), 140) for key in ("doctype", "name")}
		form["doc"]["__islocal"] = opted_in(doc.get("__islocal"))
	result["form_dict"] = form
	return result


def raw_values_enabled() -> bool:
	"""Cached opt-out is sufficient; opt-in also needs a fresh SQL read.

	A worker can retain Frappe's local cache for an entire slice. Recheck after
	the engine's committed Calling intent so a saved revocation applies to the
	next provider call. Missing fields and read failures keep data private.
	"""
	from optimus.ai_fix import _InterruptGuard

	guard = _InterruptGuard(base=True)
	try:
		with guard:
			import frappe

			from optimus.settings import get_config

			if not opted_in(getattr(get_config(), "ai_send_raw_values", False)):
				return False
			return opted_in(frappe.db.get_single_value("Optimus Settings", "ai_send_raw_values", cache=False))
	except Exception:
		return False
	if guard.pending():
		raise guard.interrupt()


def query_text(query, *, send_raw: bool) -> str:
	"""Remove literals and comments, or omit SQL that cannot be safely parsed.

	Do not trust a field merely because it is called normalized_query. Parsing
	is bounded, silent and independent of the recorder's best-effort fallback,
	which can return the original query. Raw consent retains the existing
	sensitive-column redaction.
	"""
	from optimus.ai_fix import _InterruptGuard
	from optimus.redaction import redact_sql_literals

	if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
		return ""
	guard = _InterruptGuard(base=True)
	parts = statements = token = None
	try:
		with guard:
			if send_raw:
				return redact_sql_literals(query.strip())
			parts = []
			statements = sqlparse.parse(query)
			for statement in statements:
				for token in statement.flatten():
					if token.ttype in T.Error:
						return ""
					if token.ttype in T.Comment:
						parts.append(" ")
					elif token.ttype in T.Literal:
						parts.append("?")
					else:
						parts.append(token.value)
			text = "".join(parts).strip()
			if any(marker in text for marker in ("'", '"', "/*", "--", "#")):
				return ""  # An unparsed quote/comment may still hold a value.
			return text
	except Exception:
		return ""
	if guard.pending():
		query = parts = statements = statement = token = text = None
		raise guard.interrupt()


def path_without_names(path) -> str:
	if not isinstance(path, str) or len(path) > 2048:
		return ""
	try:
		path = urlsplit(path).path
	except ValueError:
		return "<path>"
	if not path:
		return ""
	if not path.startswith("/"):
		return path if _DOTTED.fullmatch(path) else "<path>"
	if _METHOD_PATH.fullmatch(path):
		return path
	segments = [part for part in path.split("/") if part]
	for prefix, keep in ((["app"], 2), (["api", "resource"], 3), (["api", "v2", "document"], 4)):
		if segments[:len(prefix)] == prefix:
			return "/" + "/".join(segments[:keep] + (["<name>"] if len(segments) > keep else []))
	return "/<path>"


def private_action(action: dict) -> dict:
	"""Rebuild labels from operation and DocType, never a captured free-text label."""
	from optimus.analyzers.per_action import humanized_label

	cmd = action.get("cmd")
	cmd = cmd if isinstance(cmd, str) and (_DOTTED.fullmatch(cmd) or cmd == "run_doc_method") else ""
	path = path_without_names(action.get("path"))
	doctype = action.get("doctype")
	doctype = doctype if isinstance(doctype, str) and len(doctype) <= 140 else ""
	method = action.get("method")
	method = method if isinstance(method, str) and method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} else ""
	operation = action.get("operation")
	operation = operation if isinstance(operation, str) and operation in {"Save", "Submit", "Cancel", "Update"} else ""
	is_new = opted_in(action.get("is_new"))
	return {"cmd": cmd, "path": path, "method": method, "doctype": doctype, "duration_ms": action.get("duration_ms"),
		"operation": operation, "is_new": is_new,
		"label": humanized_label({"cmd": cmd, "path": path, "method": method,
			"form_dict": {"doctype": doctype, "action": operation, "doc": {"doctype": doctype, "__islocal": is_new}}})}
