# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Sensitive-data redaction: pure functions with no Frappe imports (so the
recorder-patch path can call them at app-import, before Frappe is ready).

Two responsibilities:

  * :func:`redact_sensitive`: walks a dict / list and replaces values under
    keys whose name contains a sensitive substring (``password``, ``api_key``,
    ``token``, ...) with ``"<REDACTED:keyname>"``. Used for ``form_dict``,
    ``headers`` and any nested envelope.
  * :func:`redact_sql_literals`: replaces literal RHS values in
    ``<sensitive_column> = '...'`` SQL comparisons with ``'<REDACTED>'``.
    Best-effort regex; misses UPDATE SET clauses and obscure shapes but covers
    the >95% case (``WHERE password = 'admin123'``).

Called at capture time so raw values never enter Redis; the renderer also calls
them as defense-in-depth. Every function takes an ``extra_keys`` /
``extra_columns`` tuple so operators can add patterns via Optimus Settings.
Extension is additive: there is no way to remove a default pattern, so a config
typo can't disable redaction of a known-sensitive key.
"""

from __future__ import annotations

import re
from functools import lru_cache

# Canonical default patterns. These match the historical renderer-side
# values 1:1 so the relocation is behavior-preserving; the test suite
# locks them in.
# NOTE: key matching is SUBSTRING (case-insensitive), so very short tokens are
# avoided e.g. ``sid`` is intentionally NOT here (it would match "consider",
# "inside", …); the Frappe session id rides in the ``Cookie`` header, already
# covered. SQL-column matching below is word-boundary, so it's safe from that.
DEFAULT_SENSITIVE_KEYS: tuple[str, ...] = (
	"password", "pwd", "api_key", "apikey", "token", "secret",
	"csrf", "authorization", "cookie", "encryption_key",
	"private_key", "session_id",
	# v0.13.x: broaden coverage for common secret / PII field names.
	"access_key", "salt", "hash", "otp", "ssn", "recovery",
	"credit", "card", "bank",
)
DEFAULT_SENSITIVE_SQL_COLUMNS: tuple[str, ...] = (
	"password", "pwd", "api_key", "apikey", "token", "secret",
	"csrf", "authorization", "cookie", "encryption_key",
	"private_key", "session_id",
	"access_key", "salt", "hash", "otp", "ssn", "recovery",
	"credit", "card", "bank",
)


def is_sensitive_key(key, *, extra: tuple[str, ...] = ()) -> bool:
	"""Return True when ``key`` looks like a sensitive identifier.

	Substring (case-insensitive) match against the default patterns
	plus any caller-supplied extras. Substring (not equality) so that
	``recovery_password`` or ``user_token_v2`` still match.
	"""
	if not isinstance(key, str) or not key:
		return False
	lower = key.lower()
	patterns = DEFAULT_SENSITIVE_KEYS + tuple(extra or ())
	return any(p in lower for p in patterns)


def redact_sensitive(payload, *, extra_keys: tuple[str, ...] = ()):
	"""Walk a dict / list / tuple and return a deep copy with values
	under sensitive keys replaced by ``"<REDACTED:keyname>"``. Non-
	container scalars pass through unchanged.

	Pure never mutates the input.
	"""
	if isinstance(payload, dict):
		out = {}
		for k, v in payload.items():
			if is_sensitive_key(k, extra=extra_keys):
				out[k] = f"<REDACTED:{k}>"
			else:
				out[k] = redact_sensitive(v, extra_keys=extra_keys)
		return out
	if isinstance(payload, list):
		return [redact_sensitive(item, extra_keys=extra_keys) for item in payload]
	if isinstance(payload, tuple):
		return tuple(redact_sensitive(item, extra_keys=extra_keys) for item in payload)
	return payload


@lru_cache(maxsize=16)
def _sql_literal_regex(columns: tuple[str, ...]) -> re.Pattern:
	"""Compile the SQL-literal regex for a given column tuple. Cached (small
	maxsize) so the patched-recorder hot path stays fast: each unique
	``extra_columns`` tuple compiles once per process, with most deployments
	having a single extras list from Optimus Settings.
	"""
	# RHS literal alternatives, tried left-to-right: double-quoted, single-quoted,
	# parenthesised IN-list, then a BARE token (unquoted number/hex/identifier)
	# the last catches ``WHERE password = 123`` / ``= 0xDEAD`` / ``= admin`` which
	# the quoted-only pattern leaked verbatim.
	return re.compile(
		r"""(\b(?:""" + "|".join(re.escape(c) for c in columns)
		+ r""")\b\s*(?:=|LIKE|IN)\s*)("[^"]*"|'[^']*'|\([^)]*\)|[\w.+-]+)""",
		re.IGNORECASE,
	)


def redact_sql_literals(sql_str: str, *, extra_columns: tuple[str, ...] = ()) -> str:
	"""Return ``sql_str`` with literal values in ``<sensitive_column> = '...'``
	comparisons replaced by ``'<REDACTED>'``.

	Best-effort regex: quick-exits when no sensitive substring appears, then
	replaces ``column (=|LIKE|IN) literal`` (quoted or parenthesised literals).
	Misses multi-line CTEs and computed values; capture-time plus render-time
	application means a single miss doesn't leak.
	"""
	if not sql_str or not isinstance(sql_str, str):
		return sql_str or ""
	columns = DEFAULT_SENSITIVE_SQL_COLUMNS + tuple(extra_columns or ())
	# Fast-path: skip the regex entirely if no sensitive name appears.
	# Saves the ~3-5µs regex cost per call on the 95% of queries that
	# touch nothing sensitive (a real hot path in capture-time use).
	lower = sql_str.lower()
	if not any(p.lower() in lower for p in columns):
		return sql_str
	try:
		return _sql_literal_regex(columns).sub(r"\1'<REDACTED>'", sql_str)
	except Exception:
		return sql_str


def redact_call_queries(calls, *, extra_columns: tuple[str, ...] = ()) -> None:
	"""Apply :func:`redact_sql_literals` over a recording's ``calls``
	list in place. Touches ``query`` + ``normalized_query`` fields.

	Mutates input different shape from the dict/list redactor because
	calls lists are large and copying is wasteful when the caller
	already owns the recording dict.
	"""
	if not isinstance(calls, list):
		return
	for call in calls:
		if not isinstance(call, dict):
			continue
		if call.get("query"):
			call["query"] = redact_sql_literals(call["query"], extra_columns=extra_columns)
		if call.get("normalized_query"):
			call["normalized_query"] = redact_sql_literals(
				call["normalized_query"], extra_columns=extra_columns
			)
