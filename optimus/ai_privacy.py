"""Consent and bounded data minimization at the AI input boundary."""

import json
import math
import re
from contextlib import closing, contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

import sqlparse
from sqlparse import tokens as T

MAX_QUERY_CHARS = 16_000
_METHOD = r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+"
_METHOD_PATH = re.compile(r"/api/(?:v\d+/)?method/[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*/?\Z", re.ASCII)
_DOTTED = re.compile(_METHOD + r"\Z", re.ASCII)
_raw_request = ContextVar("optimus_raw_request", default=None)
_sql_rules = ContextVar("optimus_sql_rules", default=None)


@contextmanager
def sql_redaction_scope():
	"""One current-rules read per payload/prompt build, including nested helpers.

	Failures remain cached as failures for this build; the next build starts
	fresh. No site data or rules survive in a process-global cache.
	"""
	if _sql_rules.get() is not None:
		yield
		return
	token = _sql_rules.set({})
	try:
		yield
	finally:
		_sql_rules.reset(token)


def _sensitive_columns():
	from optimus.settings import _parse_skip_list

	memo = _sql_rules.get()
	if memo is None:
		return _parse_skip_list(_fresh_sensitive_columns())
	if "columns" not in memo:
		memo["columns"] = None
		memo["columns"] = _parse_skip_list(_fresh_sensitive_columns())
	if memo["columns"] is None:
		raise ValueError("Sensitive SQL rules unavailable for this build")
	return memo["columns"]


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


@contextmanager
def raw_request_scope(send_raw):
	"""Carry this prompt's consent requirement through every HTTP retry/hop.

	Only a boolean and send marker are retained; no prompt or site data. Reset
	on every exit so a later private request cannot inherit a raw request's state.
	"""
	token = _raw_request.set({"sent": False} if send_raw else None)
	try:
		yield
	finally:
		_raw_request.reset(token)


def _fresh_raw_values_enabled():
	"""Read committed consent without changing the caller's transaction.

	Frappe's get_connection creates a separate DB-API connection on v15/v16.
	Its first SELECT sees current commits even if the caller retains an older
	REPEATABLE READ snapshot. Close it before HTTP; ordinary read/close failures
	deny a resend, while worker/process interrupts retain their semantics.
	"""
	from optimus.ai_fix import _InterruptGuard

	guard = _InterruptGuard(base=True)
	try:
		with guard:
			import frappe

			db = frappe.db
			if db.db_type not in {"mariadb", "postgres"}:
				return False
			with closing(db.get_connection()) as connection, closing(connection.cursor()) as cursor:
				query = "SELECT value FROM `tabSingles` WHERE doctype = %s AND field = %s"
				if db.db_type == "postgres":
					# v16 sets a schema on connect; v15 uses the connection default.
					schema = getattr(db, "db_schema", None)
					if schema is not None:
						cursor.execute("SET search_path TO %s", (schema,))
					query = query.replace("`", '"')
				cursor.execute(query, ("Optimus Settings", "ai_send_raw_values"))
				row = cursor.fetchone()
				return bool(row) and opted_in(row[0])
	except Exception:
		return False
	if guard.pending():
		raise guard.interrupt()


def raw_retry_allowed():
	request = _raw_request.get()
	return request is None or not request["sent"] or _fresh_raw_values_enabled()


def before_http_send():
	"""A redirect, parameter retry or reask discloses the same prompt again."""
	from optimus.ai_fix import AiFixError

	if not raw_retry_allowed():
		from frappe import _

		raise AiFixError(_("Raw-value consent changed or could not be read. Retry with current AI settings."), kind="config")
	request = _raw_request.get()
	if request is not None:
		request["sent"] = True


def query_text(query, *, send_raw: bool) -> str:
	"""Remove literals and comments, or omit SQL that cannot be safely parsed.

	Do not trust a field merely because it is called normalized_query. Parsing
	is bounded, silent and independent of the recorder's best-effort fallback,
	which can return the original query. Raw consent retains the existing
	sensitive-column redaction.
	"""
	from optimus import safe_call
	from optimus.ai_fix import _InterruptGuard
	from optimus.redaction import DEFAULT_SENSITIVE_SQL_COLUMNS
	from optimus.sql_budget import within_budget

	if not within_budget(query, max_chars=MAX_QUERY_CHARS) or not query.strip():
		return ""
	guard = _InterruptGuard(base=True)
	parts = statements = token = None
	try:
		with guard:
			import frappe
			dialect = safe_call.best_effort(lambda: getattr(frappe.db, "db_type", None), None)
			if not dialect:
				dialect = safe_call.best_effort(lambda: frappe.conf.get("db_type"), None)
			postgres = dialect == "postgres"
			if send_raw:
				columns = _sensitive_columns()
				# Sensitive SQL can use ILIKE, casts, arrays or dialect expressions.
				# Regex replacement cannot prove those private: strip all literals
				# using the same fail-closed parser boundary as redacted mode.
				if not any(col.lower() in query.lower() for col in (*DEFAULT_SENSITIVE_SQL_COLUMNS, *columns)):
					return query.strip()
			# sqlparse accepts backslash escapes even when the database dialect
			# does not. Ambiguous lexing can expose a later literal as SQL text.
			if "\\" in query:
				return ""
			parts = []
			unchecked = []
			statements = sqlparse.parse(query)
			for statement in statements:
				for token in statement.flatten():
					unchecked_value = " "
					if token.ttype in T.Error:
						return ""
					if token.ttype in T.Comment:
						if token.value.startswith("/*") and "/*" in token.value[2:]:
							return ""
						if "'" in token.value or '"' in token.value:
							return ""
						parts.append(" ")
					elif postgres and token.ttype is T.Literal.String.Symbol and re.fullmatch(r'"(?:[^"\x00]|"")*"', token.value):
						# In PostgreSQL these are identifiers, never string values.
						parts.append(token.value)
					elif token.ttype in T.Literal:
						parts.append("?")
					elif postgres and token.ttype is T.Name.Placeholder and re.fullmatch(r"\$[1-9][0-9]*", token.value):
						parts.append(token.value)
					elif postgres and token.ttype in T.Operator and token.value in {"#", "#>", "#>>", "#-"}:
						parts.append(token.value)
					else:
						if any(marker in token.value for marker in ("'", '"', "$", "/*", "*/", "--", "#")):
							return ""  # Unparsed quotes/comments may still contain a value.
						parts.append(token.value)
						unchecked_value = token.value
					unchecked.append(unchecked_value)
			text = "".join(parts).strip()
			if any(marker in "".join(unchecked) for marker in ("'", '"', "$", "/*", "*/", "--", "#")):
				return ""
			return text
	except Exception:
		return ""
	if guard.pending():
		query = parts = unchecked = unchecked_value = statements = statement = token = text = None
		raise guard.interrupt()


_SOURCE_ASSIGNMENT = re.compile(
	r"(?:\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*(?::[^=\n]{1,128})?(?::=|=)|[\"']([A-Za-z_][A-Za-z_0-9-]*)[\"']\s*(?::|\]\s*=))"
	r"(?:\s|\(|\\\r?\n|#[^\n]*(?:\n|$))*(?:[rubfRUBF]{0,2})?[\"']"
)
_SOURCE_CREDENTIAL = re.compile(r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}|xox[baprs]-[A-Za-z0-9-]{12,}|eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")
_SOURCE_SECRET_NAMES = ("password", "passwd", "pwd", "api_key", "apikey", "token", "secret", "authorization", "cookie", "encryption_key", "private_key", "access_key", "credential", "csrf", "session_id")


def source_window(rows, *, api_key=""):
	"""Withhold credential-bearing source; never generate a patch against masked code.

	This detects configured key literals, credential shapes and sensitive literal
	assignments. It is a minimization boundary, not a general secret classifier.
	"""
	from optimus import safe_call
	from optimus.redaction import key_literals, scrub_secrets
	guard = safe_call.InterruptGuard(base=True)
	text = secret = None
	try:
		with guard:
			if not isinstance(rows, (list, tuple)) or len(rows) > 128:
				return []
			if any(not isinstance(row, dict) or not isinstance(row.get("content"), str) for row in rows):
				return []
			if sum(len(row["content"]) for row in rows) > 64_000:
				return []
			text = "\n".join(row["content"] for row in rows)
			secret = key_literals(api_key)
			if scrub_secrets(text, literals=secret) != text or _SOURCE_CREDENTIAL.search(text):
				return []
			for match in _SOURCE_ASSIGNMENT.finditer(text):
				name = (match.group(1) or match.group(2)).lower().replace("-", "_")
				if any(label in name for label in _SOURCE_SECRET_NAMES):
					return []
			return list(rows)
	except Exception:
		return []
	if guard.pending():
		rows = api_key = text = secret = None
		raise guard.interrupt()


def _fresh_sensitive_columns():
	"""Read committed operator rules; cached config falls back to defaults on error.

	Use a separate connection so a worker's repeatable-read view cannot retain
	old rules. Errors propagate to query_text's fail-closed boundary.
	"""
	import frappe

	db = frappe.db
	if db.db_type not in {"mariadb", "postgres"}:
		raise ValueError("Unsupported SQL settings source")
	with closing(db.get_connection()) as connection, closing(connection.cursor()) as cursor:
		query = "SELECT value FROM `tabSingles` WHERE doctype = %s AND field = %s"
		if db.db_type == "postgres":
			schema = getattr(db, "db_schema", None)
			if schema is not None:
				cursor.execute("SET search_path TO %s", (schema,))
			query = query.replace("`", '"')
		cursor.execute(query, ("Optimus Settings", "sensitive_sql_columns"))
		row = cursor.fetchone()
		return row[0] if row else None


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
