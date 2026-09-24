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


# ---------------------------------------------------------------------------
# Secret scrubbing for log text (PR-0a). Used by ai_fix.log_ai_failure on
# every AI-surface Error Log message and by optimus.maintenance to scrub rows
# written before the fix. Three shapes cover every leak found in real rows:
# a dict repr carrying an auth header, a dict repr carrying an api-key field,
# and a bare "Bearer <token>" anywhere else (exception text, echoed bodies);
# a fourth masks credentials in a URL (a Base URL typed as user:pass@host).
# ---------------------------------------------------------------------------

SECRET_PLACEHOLDER = "********"

_SECRET_PATTERNS: tuple[re.Pattern, ...] = (
	# 'authorization': 'Bearer <tok>' (either quote style; also Basic / Token)
	re.compile(
		r"""((['"])(?:proxy-)?authorization\2\s*:\s*(['"])(?:bearer|basic|token)\s+)(?!\*{8}\3)[^'"\s]+(\3)""",
		re.IGNORECASE,
	),
	# 'x-api-key' / 'x-goog-api-key' / 'api_key' / 'api-key' / 'apikey' : '<tok>'
	re.compile(
		r"""((['"])(?:x-api-key|x-goog-api-key|api[_-]?key|apikey)\2\s*:\s*(['"]))(?!\*{8}\3)[^'"]+(\3)""",
		re.IGNORECASE,
	),
	# bare "Bearer <tok>" anywhere else. The token is any run of non-quote,
	# non-space characters, so a pasted smart quote inside or in front of the
	# key (Bearer \u2019sk-...) is masked too; it never ends on a backslash, so
	# a JSON-escaped quote after it (Deleted Document data) stays intact.
	re.compile(r"""(\bBearer\s+)(?!\*{8})[^'"\s]{7,}[^'"\s\\]()()()"""),
	# credentials in a URL: scheme://user:password@host, up to the LAST "@"
	# before the path (a password may hold a raw "@"); never across a quote,
	# so an address in the next field of compact JSON is not swallowed
	re.compile(r"""(://)()()(?!\*{8}@)[^/\s'"]+(@)"""),
)

# A literal shorter than this is never replaced: it would shred ordinary words
# (tests and misconfigured sites use keys like "k"); real provider keys are
# far longer.
_MIN_LITERAL_LEN = 8


def _literal_length(api_key) -> int:
	"""The sort key of a ``scrub_secrets`` literal: its length (0 for a value
	that is not a string). The parameter holds the key, so it is named
	``api_key``, a name the traceback sanitizers redact."""
	return len(api_key) if isinstance(api_key, str) else 0


def scrub_secrets(text: str, *, literals: tuple[str, ...] = ()) -> str:
	"""Return ``text`` with API keys replaced by ``********``.

	Replaces every exact occurrence of each ``literals`` entry that is a
	string of at least 8 characters (the live key, when the caller knows
	it), longest first, so a literal inside another one (a stored key and
	the key a request was sent with can overlap) never leaves part of the
	longer one behind; then the header / field / Bearer / URL-credential
	shapes. Idempotent: an already-masked value is never matched again, so a
	second pass changes nothing. Non-string input is returned unchanged.

	SECURITY: the key is held only in locals whose names Frappe's traceback
	sanitizer and Sentry both redact (``secret``, ``api_key``); ``literals``
	is dropped first. Callers still guard the call and never log a failure
	of it with frame locals: ``text`` itself holds the unmasked value.
	"""
	if not text or not isinstance(text, str):
		return text
	secret = tuple(sorted(literals or (), key=_literal_length, reverse=True))
	del literals
	out = text
	for api_key in secret:
		if isinstance(api_key, str) and len(api_key) >= _MIN_LITERAL_LEN:
			out = out.replace(api_key, SECRET_PLACEHOLDER)
	for pattern in _SECRET_PATTERNS:
		out = pattern.sub(lambda m: m.group(1) + SECRET_PLACEHOLDER + (m.group(4) or ""), out)
	return out
