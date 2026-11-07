# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Sensitive-data redaction: pure functions with no Frappe imports (so the
recorder-patch path can call them at app-import, before Frappe is ready).

Two responsibilities:

  * :func:`redact_sensitive`: walks a dict / list and replaces values under
    keys whose name contains a sensitive substring (``password``, ``api_key``,
    ``token``, ...) with ``"<REDACTED:keyname>"``. Used for ``form_dict``,
    ``headers`` and any nested envelope.
  * :func:`redact_sql_literals`: replaces the values that belong to a sensitive
    column or key in a SQL statement with ``'<REDACTED>'``: comparisons in
    either order, ``UPDATE SET``, positional ``INSERT`` rows, ``CASE`` results,
    settings rows and stored-secret value shapes. A statement it cannot read
    is masked whole or omitted, never returned raw.

Called at capture time so raw values never enter Redis; the renderer also calls
them as defense-in-depth. Every function takes an ``extra_keys`` /
``extra_columns`` tuple so operators can add patterns via Optimus Settings.
Extension is additive: there is no way to remove a default pattern, so a config
typo can't disable redaction of a known-sensitive key.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import NamedTuple
from urllib.parse import quote, unquote_plus

_KEY_CACHE_MAX = 4096
URL_PLACEHOLDER = "<REDACTED>"

# Canonical default patterns. These match the historical renderer-side
# values 1:1 so the relocation is behavior-preserving; the test suite
# locks them in.
# NOTE: key matching is SUBSTRING (case-insensitive), so very short tokens are
# avoided e.g. ``sid`` is intentionally NOT here (it would match "consider",
# "inside", …); the Frappe session id rides in the ``Cookie`` header, already
# covered. SQL name matching below is on whole identifiers, so it is safe from that.
DEFAULT_SENSITIVE_KEYS: tuple[str, ...] = (
	"password", "pwd", "api_key", "apikey", "token", "secret",
	"csrf", "authorization", "cookie", "encryption_key",
	"private_key", "session_id",
	# v0.13.x: broaden coverage for common secret / PII field names.
	"access_key", "salt", "hash", "otp", "ssn", "recovery",
	"credit", "card", "bank",
)


def is_sensitive_key(key, *, extra: tuple[str, ...] = ()) -> bool:
	"""Return True when ``key`` looks like a sensitive identifier.

	Substring (case-insensitive) match against the default patterns plus any
	caller-supplied extras, so ``recovery_password`` or ``user_token_v2`` still
	match. A name that is exactly a SQL-list name once normalised to snake_case
	(``X-Api-Key``, ``accessKey``, ``sid``) matches too: those are too short or
	too hyphenated for the substring rule.
	"""
	if not isinstance(key, str) or not key:
		return False
	extra = tuple(extra or ())
	if len(key) > _IDENTIFIER_MAX:
		return _substring_hit(key.lower(), extra)
	return _sensitive_name(key, extra)


def _substring_hit(lower: str, extra: tuple[str, ...]) -> bool:
	return any(p in lower for p in DEFAULT_SENSITIVE_KEYS + extra)


@lru_cache(maxsize=_KEY_CACHE_MAX)
def _sensitive_name(key: str, extra: tuple[str, ...]) -> bool:
	"""``is_sensitive_key`` for a key of at most 128 characters; memoised, as the same few names
	repeat across every row of a form_dict or header set."""
	if _substring_hit(key.lower(), extra):
		return True
	name = _normalize_name(key)
	if _names(()).name.fullmatch(name) is not None:
		return True
	return (
		name in _HEADER_SECRET_NAMES
		or not _HEADER_SECRET_PARTS.isdisjoint(name.split("_"))
		or _SIGNATURE_NAME.search(name) is not None
	)


def is_key_bearing_endpoint(path, cmd) -> bool:
	"""True for a request whose bare ``key`` argument is a credential (a password-reset
	or print-share key). Frappe merges query arguments into ``form_dict``, so the key
	of ``/update-password?key=...`` is recorded there."""
	target = f"{path if isinstance(path, str) else ''} {cmd if isinstance(cmd, str) else ''}".lower()
	return any(token in target for token in _KEY_BEARING_ENDPOINTS)


def redact_sensitive(
	payload,
	*,
	extra_keys: tuple[str, ...] = (),
	mask_key: bool = False,
	mask_code: bool = False,
	header: bool = False,
):
	"""Walk a dict / list / tuple and return a deep copy with values
	under sensitive keys replaced by ``"<REDACTED:keyname>"``. A string value
	that is a URL has its sensitive query values masked (:func:`redact_url`).
	``mask_key`` also masks the top-level bare ``key`` (see
	:func:`is_key_bearing_endpoint`) and ``mask_code`` the bare ``code`` of an OAuth callback (see
	:func:`is_oauth_callback`). ``header`` treats the top-level names as HTTP header names, which
	also masks a ``-Key`` suffix and ``auth`` / ``bearer`` names (:func:`is_secret_header_name`).
	Other scalars pass through unchanged.

	Pure never mutates the input.
	"""
	if isinstance(payload, dict):
		out = {}
		for k, v in payload.items():
			if (
				is_sensitive_key(k, extra=extra_keys)
				or (mask_key and _is_bare_name(k, "key"))
				or (mask_code and _is_bare_name(k, "code"))
				or (header and is_secret_header_name(k))
			):
				out[k] = f"<REDACTED:{k}>"
			else:
				out[k] = redact_sensitive(v, extra_keys=extra_keys)
		# set_value style args: the secret sits under "value", named by "fieldname".
		field = out.get("fieldname")
		if "value" in out and is_sensitive_key(field, extra=extra_keys):
			out["value"] = f"<REDACTED:{field}>"
		return out
	if isinstance(payload, list):
		return _mask_filter_value([redact_sensitive(item, extra_keys=extra_keys) for item in payload], extra_keys)
	if isinstance(payload, tuple):
		return tuple(redact_sensitive(item, extra_keys=extra_keys) for item in payload)
	if isinstance(payload, str):
		return _redact_string(payload, extra_keys)
	return payload


_FILTER_OPERATORS = frozenset(
	{
		"=", "!=", "<>", "<", ">", "<=", ">=", "like", "not like", "in", "not in", "between",
		"not between", "is", "is not", "descendants of", "ancestors of", "not descendants of",
		"not ancestors of", "timespan", "ilike", "not ilike",
	}
)


def _mask_filter_value(row: list, extra_keys: tuple[str, ...]) -> list:
	"""A Frappe filter row ``[doctype, field, op, value]`` or ``[field, op, value]`` whose field is
	sensitive, with its value masked: the key-based walk cannot see a secret in a list."""
	if len(row) not in (3, 4):
		return row
	op = row[-2]
	if not isinstance(op, str) or op.strip().lower() not in _FILTER_OPERATORS:
		return row
	field = row[len(row) - 3]
	if is_sensitive_key(field, extra=extra_keys):
		row[-1] = f"<REDACTED:{field}>"
	return row


def _is_bare_name(name, expected: str) -> bool:
	return isinstance(name, str) and name.lower() == expected


def is_oauth_callback(path, cmd) -> bool:
	"""True for an OAuth login callback, whose bare ``code`` argument is a one-time credential."""
	target = f"{path if isinstance(path, str) else ''} {cmd if isinstance(cmd, str) else ''}".lower()
	return any(token in target for token in _OAUTH_ENDPOINTS)


def is_secret_header_name(name) -> bool:
	"""True for an HTTP header name that carries a credential and is not covered by
	:func:`is_sensitive_key`: a ``-Key`` ending (``Ocp-Apim-Subscription-Key``, ``X-Master-Key``,
	except ``Idempotency-Key``) or an ``auth`` / ``bearer`` part (``X-Auth``, ``X-Bearer``)."""
	if not isinstance(name, str) or not name or len(name) > _IDENTIFIER_MAX:
		return False
	normalised = _normalize_name(name)
	if normalised in _HEADER_KEY_ALLOWED:
		return False
	return normalised.endswith("_key") or normalised == "key" or not _HEADER_AUTH_PARTS.isdisjoint(
		normalised.split("_")
	)


def redact_string_value(text: str, extra_keys: tuple[str, ...] = ()) -> str:
	"""``text`` with secrets masked as ``redact_sensitive`` does for a string value: JSON inside it
	by name, URLs inside it by query parameter and user info. For callers that hold a string, not a
	dict (a SQL string literal, a log line)."""
	return _redact_string(text, tuple(extra_keys or ())) if isinstance(text, str) else text


def _redact_string(text: str, extra_keys: tuple[str, ...]) -> str:
	if text.lstrip().startswith(("{", "[")):
		redacted = _redact_json_string(text, extra_keys)
		if redacted != text:
			return redacted
	return _redact_url_text(text, extra_keys)


def _redact_url_text(text: str, extra_keys: tuple[str, ...]) -> str:
	"""``text`` that is a URL, or holds URL-shaped words, with their secrets masked; prose is untouched."""
	if not _may_hold_url(text):
		return text
	return _CANDIDATE_RUN.sub(lambda m: redact_url_run(m.group(0), extra_keys), text)


def redact_url_run(run: str, extra_keys: tuple[str, ...] = (), placeholder: str = URL_PLACEHOLDER) -> str:
	"""``redact_url`` for a URL found in running text: trailing punctuation (``).,;``, an unbalanced
	``]``) is not part of the URL and is put back after it."""
	end = len(run)
	unmatched = run.count("]") - run.count("[")
	while end and (run[end - 1] in _TRAILING or (run[end - 1] == "]" and unmatched > 0)):
		unmatched -= run[end - 1] == "]"
		end -= 1
	return redact_url(run[:end], extra_keys, placeholder) + run[end:]


def _may_hold_url(text: str) -> bool:
	return _looks_like_url(text)


def _redact_json_string(text: str, extra_keys: tuple[str, ...]) -> str:
	"""Redact inside a JSON-encoded object or list (savedocs ``doc``, ``args``)."""
	if not text.lstrip().startswith(("{", "[")):
		return text
	try:
		parsed = json.loads(text)
		if not isinstance(parsed, dict | list):
			return text
		redacted = redact_sensitive(parsed, extra_keys=extra_keys)
	except (ValueError, RecursionError):
		return _mask_unparsed_json(text, extra_keys)
	return text if redacted == parsed else json.dumps(redacted)


_JSON_PAIR = re.compile(r'("(?:[^"\\]|\\.){1,128}")(\s*:\s*)("(?:[^"\\]|\\.)*(?:"|$)|[^,{}\[\]\s"]+)')


def _mask_unparsed_json(text: str, extra_keys: tuple[str, ...]) -> str:
	"""JSON text that does not parse (cut short, or nested too deeply): the values after secret names are
	masked pair by pair, so a cut or deep value is not stored raw."""

	def mask(match):
		name = match.group(1)[1:-1]
		if not is_sensitive_key(name, extra=extra_keys):
			return match.group(0)
		return f'{match.group(1)}{match.group(2)}"{URL_PLACEHOLDER}"'

	return _JSON_PAIR.sub(mask, text)


# ---------------------------------------------------------------------------
# URL query secrets. Values are replaced in place, so a URL with nothing to mask
# comes back byte for byte (no parse-and-rebuild that would rewrite ``+`` or ``%20``).
# ONE rule names the secret parameters: ``is_secret_param``.
# ---------------------------------------------------------------------------

URL_MAX_CHARS = 8192
_KEY_BEARING_ENDPOINTS = (
	"update-password",
	"update_password",
	"printview",
	"download_pdf",
	"test_password_strength",
)
# Parameter names that are secrets in a URL but not as a form field or header name (an exact
# name or a ``_sig`` / ``_signature`` ending, so ``item_code`` and ``session_user`` stay visible):
# Frappe's share/reset ``key`` and signed-link ``_signature``, OAuth ``code``, ``jwt``, ``session``,
# cloud-signed-URL ``X-Amz-Signature`` (``X-Amz-Credential`` and ``-Security-Token`` match the name list).
_URL_SECRET_NAMES = frozenset(
	{
		"key", "signature", "sig", "code", "auth", "jwt", "bearer", "session", "pin", "refresh", "reset_key",
	}
)
_URL_SECRET_SUFFIX = re.compile(r"_(?:signature|sig)$")
# Header and field names that are secrets by exact normalised name, by a part of it, or by a
# ``_signature`` ending (``X-Hub-Signature-256``); a bare ``signature`` is not one.
_HEADER_SECRET_NAMES = frozenset(
	{"x_session", "www_authenticate", "proxy_authenticate", "authentication", "sec_websocket_key", "sec_web_socket_key"}
)
_HEADER_SECRET_PARTS = frozenset({"jwt", "assertion"})
# Header names only (a form field called ``sort_key`` or ``auth`` is not a credential).
_HEADER_AUTH_PARTS = frozenset({"auth", "bearer"})
_HEADER_KEY_ALLOWED = frozenset({"idempotency_key", "x_idempotency_key"})
_OAUTH_ENDPOINTS = ("oauth", "login_via", "social_login")
_SIGNATURE_NAME = re.compile(r"_signature(?:_\d+)?$")
_PARAM_SPLIT = re.compile(r"([&;])")
# Text holds URLs between whitespace, quotes and angle brackets; a run of anything else is a candidate when it has a ``?`` or ``#`` followed by an ``=``, or an ``@``
# (anchored at the start of a run, so a long run without one is scanned once, not from every
# position, and prose costs no Python call per word).
_CANDIDATE_RUN = re.compile(r"""(?<![^\s'"<>])[^\s'"<>?#@]*(?:[?#][^\s'"<>=]*=|@)[^\s'"<>]*""")
_TRAILING = frozenset(").,;")
# The look-behind lets the scheme start only at the start of a run of scheme characters, so a long
# run is scanned once and not from every position (linear, not quadratic); the user info stops at a space.
_USERINFO = re.compile(r"(?<![A-Za-z0-9+.-])((?:[A-Za-z][A-Za-z0-9+.-]*:)?//)[^/?#\s]*@")
_NESTED_URL_DEPTH = 4
_JSON_VALUE_STARTS = ("%7b", "%5b", "{", "[")
_NAME_SEPARATORS = re.compile(r"[-. ]")


def redact_url_field(row, field: str, extra_keys: tuple[str, ...] = ()):
	"""``row`` (a dict) with its ``field`` URL masked; a copy only when it changes."""
	if not isinstance(row, dict):
		return row
	value = row.get(field)
	masked = redact_url(value, extra_keys)
	return row if masked == value else {**row, field: masked}


def _normalize_name(key: str) -> str:
	"""``X-Api-Key`` -> ``x_api_key``, ``accessKey`` -> ``access_key``."""
	return _NAME_SEPARATORS.sub("_", _snake_case(key))


def is_secret_param(name: str, extra: tuple[str, ...] = ()) -> bool:
	"""True when the query/fragment parameter ``name`` (still percent-encoded) holds a secret.
	A name too long to be a real parameter counts as one."""
	if len(name) > _IDENTIFIER_MAX:
		return True
	return _secret_param(name, tuple(extra or ()))


@lru_cache(maxsize=_KEY_CACHE_MAX)
def _secret_param(name: str, extra: tuple[str, ...]) -> bool:
	decoded = unquote_plus(name).strip()
	if "%" in decoded:
		decoded = unquote_plus(decoded).strip()
	if is_sensitive_key(decoded, extra=extra):
		return True
	normalised = _normalize_name(decoded)
	return normalised in _URL_SECRET_NAMES or _URL_SECRET_SUFFIX.search(normalised) is not None


def redact_url(text, extra_keys: tuple[str, ...] = (), placeholder: str = URL_PLACEHOLDER):
	"""``text`` with the value of every secret query or fragment parameter replaced by
	``placeholder``; the rest is unchanged. A URL over 8 KiB, or one that cannot be read, has
	its query omitted instead. Pure, linear, never raises; non-strings pass through."""
	if not isinstance(text, str):
		return text
	if len(text) > URL_MAX_CHARS:
		return _redact_long_url(text, placeholder)
	text = _mask_userinfo(text, placeholder)
	cut = _query_start(text)
	if cut < 0:
		return text
	if "=" not in text:
		return text
	try:
		return _mask_url(text, tuple(extra_keys or ()), placeholder)
	except (ValueError, RecursionError):
		return _omit_query(text, cut)


def _query_start(text: str) -> int:
	"""Index of the first ``?`` or ``#``, or -1."""
	return min((i for i in (text.find("?"), text.find("#")) if i >= 0), default=-1)


def _redact_long_url(text: str, placeholder: str) -> str:
	"""A URL over the limit: no regex reads more than the first ``URL_MAX_CHARS`` characters (the
	user info of a real URL sits there), and the query is omitted."""
	cut = _query_start(text)
	end = len(text) if cut < 0 else cut
	head = _mask_userinfo(text[: min(end, URL_MAX_CHARS)], placeholder)
	base = head + text[URL_MAX_CHARS:end]
	return base if cut < 0 else _omit_query(base + text[cut], len(base))


def _mask_userinfo(text: str, placeholder: str) -> str:
	"""``//user:pass@host`` -> ``//<placeholder>@host`` (up to the last ``@`` of the authority)."""
	if "@" not in text:
		return text
	return _USERINFO.sub(lambda m: m.group(1) + placeholder + "@", text, count=1)


def _omit_query(text: str, cut: int) -> str:
	return text[:cut] + ("?<query omitted>" if text[cut] == "?" else "#<fragment omitted>")


def _mask_url(text: str, extra: tuple[str, ...], placeholder: str, depth: int = 0) -> str:
	before, hash_sep, fragment = text.partition("#")
	base, query_sep, query = before.partition("?")
	out = base + query_sep + _mask_params(query, extra, placeholder, depth) + hash_sep
	route, route_sep, route_query = fragment.partition("?")
	if route_sep:
		return out + route + route_sep + _mask_params(route_query, extra, placeholder, depth)
	return out + _mask_params(fragment, extra, placeholder, depth)


def _mask_params(segment: str, extra: tuple[str, ...], placeholder: str, depth: int = 0) -> str:
	parts = _PARAM_SPLIT.split(segment)
	for i in range(0, len(parts), 2):
		parts[i] = _mask_param(parts[i], extra, placeholder, depth)
	return "".join(parts)


def _mask_param(part: str, extra: tuple[str, ...], placeholder: str, depth: int = 0) -> str:
	name, eq, value = part.partition("=")
	if eq:
		if value and is_secret_param(name, extra):
			return f"{name}={placeholder}"
		value = _mask_json_value(value, extra)
		return f"{name}={_mask_nested_url(value, extra, placeholder, depth)}"
	if "%" not in part:
		return part
	name, eq, _ = unquote_plus(part).partition("=")
	if eq and is_secret_param(name, extra):
		return f"{quote(name, safe='')}={placeholder}"
	return part


def _looks_like_url(text: str) -> bool:
	"""Cheap, linear test: a query or fragment with an ``=``, or a ``//`` with an ``@``."""
	if ("?" in text or "#" in text) and "=" in text:
		return True
	return "//" in text and "@" in text


def _mask_nested_url(value: str, extra: tuple[str, ...], placeholder: str, depth: int) -> str:
	"""A parameter whose value is itself a URL or a path with a query (``redirect-to=%2Fupdate-password%3Fkey%3D...``,
	``next=/a?token=...``), masked inside, whether it is percent-encoded or not. Past ``_NESTED_URL_DEPTH`` levels
	the value is masked whole."""
	decoded = unquote_plus(value) if "%" in value else value
	if "%" in decoded:
		decoded = unquote_plus(decoded)
	if not _looks_like_url(decoded):
		return value
	if depth >= _NESTED_URL_DEPTH:
		return placeholder
	inner = _mask_userinfo(decoded, placeholder)
	if _query_start(inner) >= 0 and "=" in inner:
		inner = _mask_url(inner, extra, placeholder, depth + 1)
	if inner == decoded:
		return value
	return quote(inner, safe="") if "%" in value else inner


def _mask_json_value(value: str, extra: tuple[str, ...]) -> str:
	"""A JSON-valued parameter (``filters=%7B...%7D``) with its secrets masked inside."""
	if not value.lower().startswith(_JSON_VALUE_STARTS):
		return value
	decoded = unquote_plus(value)
	redacted = _redact_json_string(decoded, extra)
	return value if redacted == decoded else quote(redacted, safe="")


# ---------------------------------------------------------------------------
# SQL literal redaction. A linear lexer cuts the statement into tokens, then one
# pass over them (a stack of bracket frames, no scan-back) finds the values that
# belong to a sensitive name. Pure and bounded: every loop walks the tokens once.
# ---------------------------------------------------------------------------

OMITTED_SQL = "[SQL omitted: could not be read safely]"
PLACEHOLDER = "'<REDACTED>'"
LITERALS_MASKED_KEY = "optimus_literals_masked"
# The comment the analyzer appends to a cut query; it is the one comment redaction keeps.
SQL_TRUNCATION_MARKER = "-- [Optimus: query truncated"
# A longer statement is omitted without being read: the masking pass is linear, but a bulk
# insert of megabytes would still cost seconds and memory on the request path.
MAX_REDACTED_SQL_CHARS = 262_144
_MAX_TOKENS = 120_000
_MAX_DOLLAR_TAGS = 50
_TRUNCATION_NOTE = re.compile(re.escape(SQL_TRUNCATION_MARKER) + r" at \d+ of \d+ characters\]")
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_]\w*\$")

_BACKTICK = r"(?P<bt>`[^`]*(?:``[^`]*)*`)"
_MARIADB_STRINGS = (
	r"""(?P<str>(?:_[A-Za-z0-9]+[ \t]*|[NnXxBb])?'[^'\\]*(?:(?:\\[\s\S]|'')[^'\\]*)*')"""
	r"""|(?P<dq>"[^"\\]*(?:(?:\\[\s\S]|"")[^"\\]*)*")"""
)
_POSTGRES_STRINGS = (
	r"""(?P<str>[Ee]'[^'\\]*(?:(?:\\[\s\S]|'')[^'\\]*)*'|(?:[UuNnXxBb]&?)?'[^']*(?:''[^']*)*'"""
	r"""|\$(?P<tag>[A-Za-z_]\w*)\$[\s\S]*?\$(?P=tag)\$|\$\$[\s\S]*?\$\$)"""
	r"""|(?P<id>"[^"]*(?:""[^"]*)*")"""
)
# The conditional comment opener is its own token, so the statement after it stays readable.
_MARIADB_COMMENTS = r"(?P<cm>/\*(?!M?!)[\s\S]*?\*/|--(?=[ \t\r\n]|$)[^\n]*|\#[^\n]*)|(?P<cond>/\*M?!)"
_POSTGRES_COMMENTS = r"(?P<cm>/\*[\s\S]*?\*/|--[^\n]*)"
_TAIL = r"""
	|(?P<open>['"`][\s\S]*|/\*[\s\S]*)
	|(?P<w>[A-Za-z_\x80-\U0010ffff][\w$]*)
	|(?P<ph>%\([A-Za-z0-9_]*\)s|%s|\?{placeholders})
	|(?P<op><=>|!=|<>|<=|>=|::|\|\||->>|->|&&|@@|@>|<@|!~\*|!~|~\*|[=<>(),.;+\-*/%!~^&|@:\[\]{{}}])
	|(?P<num>(?:0[xX][0-9A-Fa-f]+|0[bB][01]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)(?![\w$]))
	|(?P<w2>\d[\w$]*)
	|(?P<o>\S))
"""


def _build_lexer(strings: str, comments: str, placeholders: str) -> re.Pattern:
	"""Alternatives are ordered: comments and placeholders before operators, numbers before
	``w2``, and ``w`` never starts with ``$`` (a PostgreSQL ``$1``)."""
	return re.compile(
		r"\s*(?:" + _BACKTICK + "|" + strings + "|" + comments + _TAIL.format(placeholders=placeholders),
		re.VERBOSE,
	)


_LEXERS = {
	"mariadb": _build_lexer(_MARIADB_STRINGS, _MARIADB_COMMENTS, ""),
	"postgres": _build_lexer(_POSTGRES_STRINGS, _POSTGRES_COMMENTS, r"|\$\d+"),
}
_TOKEN_KIND = {"w2": "w"}
_STRING_KINDS = ("str", "open", "dq", "id")


def _lex(sql: str, dialect: str) -> list[tuple[str, int, int]]:
	"""Return ``(kind, start, end)`` for every token. Kinds: bt, str, dq (a MariaDB string),
	id (a PostgreSQL quoted name), cm, cond (``/*!``), open (an unterminated quote or
	comment), w, ph, op, num, o."""
	tokens = []
	for match in _LEXERS[dialect].finditer(sql):
		group = match.lastgroup
		tokens.append((_TOKEN_KIND.get(group, group), match.start(group), match.end()))
	return tokens


def _lex_agreed(sql: str) -> list[tuple[str, int, int]] | None:
	"""Lex for an unknown dialect: the MariaDB tokens, but only when both dialects cut the same
	string spans (a backslash before a quote is where they differ)."""
	mariadb, postgres = _lex(sql, "mariadb"), _lex(sql, "postgres")
	if _string_spans(mariadb) != _string_spans(postgres):
		return None
	return mariadb


def _string_spans(tokens) -> list[tuple[int, int]]:
	return [(start, end) for kind, start, end in tokens if kind in _STRING_KINDS]


def _lexically_untrusted(sql: str, tokens) -> bool:
	"""True when the token boundaries cannot be relied on, so no later literal can be located:
	an unterminated quote or comment, a quote inside a comment, or a nested comment."""
	for kind, start, end in tokens:
		if kind == "open":
			return True
		if kind == "cm" and _comment_is_odd(sql[start:end]):
			return True
	return False


def _comment_is_odd(text: str) -> bool:
	return "'" in text or '"' in text or (text.startswith("/*") and "/*" in text[2:])


# Names. A compound name also matches as one underscore-separated part of an identifier
# (reset_password_key, api_secret); an exact name must be the whole identifier.
_COMPOUND_SQL_NAMES = (
	"password",
	"pwd",
	"api_key",
	"apikey",
	"token",
	"secret",
	"encryption_key",
	"private_key",
	"session_id",
	"access_key",
	"otp",
	"ssn",
	"salt",
	"passwd",
	"passkey",
	"hmac",
	"credential",
	"credentials",
	"otpsecret",
	"card_number",
	"card_no",
	"bank_account_no",
	"bank_ac_no",
	"iban",
	"cvv",
	"cvc",
	"authorization",
)
_EXACT_SQL_NAMES = ("csrf", "cookie", "hash", "recovery", "sid", "sessiondata", "bank_party_account_number")
DEFAULT_SENSITIVE_SQL_COLUMNS: tuple[str, ...] = _COMPOUND_SQL_NAMES + _EXACT_SQL_NAMES

# A string literal shaped like a stored secret is masked wherever it sits.
_SECRET_SHAPE = re.compile(
	r"gAAAAA[A-Za-z0-9_-]{90,}={0,2}|\$pbkdf2(?:-sha\d+)?\$\d+\$\S+|\$argon2(?:id|i|d)\$\S+"
	r"|\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}"
)
_SECRET_SHAPE_MARKS = ("gAAAAA", "$pbkdf2-", "$argon2", "$2a$", "$2b$", "$2y$")
_SHORT_NAME_LENGTH = 4
_KEY_TEXT_MAX = 256
_IDENTIFIER_MAX = 128
_NAME_PARTS_MAX = 16
_MEMO_MAX = 4096
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _snake_case(text: str) -> str:
	"""Lowercase with an underscore before each capital: ``accessToken`` -> ``access_token``."""
	return _CAMEL_BOUNDARY.sub("_", text).lower()


def _normalize_extras(extras) -> tuple[str, ...]:
	cleaned = (_snake_case(extra.strip()) for extra in extras if isinstance(extra, str))
	return tuple(dict.fromkeys(extra for extra in cleaned if extra))


class _Names:
	"""The compiled name rules for one tuple of Settings extras."""

	def __init__(self, extras):
		extras = _normalize_extras(extras)
		compound = "|".join(re.escape(name) for name in _COMPOUND_SQL_NAMES)
		exact = "|".join(re.escape(name) for name in (*_EXACT_SQL_NAMES, *extras))
		parts = _NAME_PARTS_MAX
		name = rf"(?:[a-z0-9]+_){{0,{parts}}}(?:{compound})(?:_[a-z0-9]+){{0,{parts}}}|{exact}"
		self.name = re.compile(name)
		self.key = re.compile(rf"(?:.*[^a-z0-9])?(?:{name})", re.DOTALL)
		self.json_key = re.compile(rf"""\\?"(?:{name})\\?"\s*[:,]""")
		# The probe also takes each name without underscores, as a camelCase column lowercases to it.
		every = tuple(
			dict.fromkeys(
				n for name in (*DEFAULT_SENSITIVE_SQL_COLUMNS, *extras) for n in (name, name.replace("_", ""))
			)
		)
		self.long = tuple(n for n in every if len(n) > _SHORT_NAME_LENGTH)
		self.short = tuple(n for n in every if len(n) <= _SHORT_NAME_LENGTH)
		self.short_boundary = re.compile(
			"(?<![a-z0-9])(?:" + "|".join(re.escape(n) for n in self.short) + ")(?![a-z0-9])"
		)
		self._memo: dict[str, bool] = {}
		self._normalized: dict[str, str] = {}

	def normalize(self, text: str) -> str:
		"""``text`` as a lowercase snake_case name; memoised, as names repeat within a statement."""
		hit = self._normalized.get(text)
		if hit is None:
			hit = _snake_case(text)
			if len(text) <= _IDENTIFIER_MAX and len(self._normalized) < _MEMO_MAX:
				self._normalized[text] = hit
		return hit

	def is_name(self, identifier: str | None) -> bool:
		"""True for a sensitive identifier. An identifier too long to be a real column counts as
		sensitive instead of costing a long match."""
		if identifier is None:
			return False
		if len(identifier) > _IDENTIFIER_MAX:
			return True
		hit = self._memo.get(identifier)
		if hit is None:
			hit = self.name.fullmatch(identifier) is not None
			if len(self._memo) < _MEMO_MAX:
				self._memo[identifier] = hit
		return hit

	def is_key(self, text: str) -> bool:
		"""True when ``text`` (a stored key such as ``a@b.c_otpsecret``) ends in a sensitive name.
		An absurdly long key counts as sensitive instead of costing a long match."""
		if len(text) > _KEY_TEXT_MAX:
			return True
		return self.key.fullmatch(text) is not None

	def hit(self, sql: str) -> bool:
		lower = sql.lower()
		if any(name in lower for name in self.long):
			return True
		if any(name in lower for name in self.short) and self.short_boundary.search(lower):
			return True
		if "identified" in lower or "aes_" in lower:
			return True
		return ("$" in sql or "gAAAAA" in sql) and any(mark in sql for mark in _SECRET_SHAPE_MARKS)


@lru_cache(maxsize=16)
def _names(extras: tuple[str, ...]) -> _Names:
	return _Names(extras)


def sql_names_hit(sql: str, extra_columns: tuple[str, ...] = ()) -> bool:
	"""True when ``sql`` names a sensitive column or holds a stored-secret value shape.
	Short names (``sid``, ``otp``) must stand alone, so ``show_sidebar`` is not a hit."""
	return _names(tuple(extra_columns or ())).hit(sql)


# Statement grammar.
_BOUNDARY_WORDS = frozenset(
	"and or xor where on set having when then else end order group limit offset union intersect "
	"except select from values value returning into join using update delete insert replace conflict "
	"do for with as".split()
)
_COMPARISONS = frozenset(
	"= != <> <=> < > <= >= like ilike regexp rlike in between is ~ ~* !~ !~* && @@ @> <@".split()
)
_NEGATABLE = frozenset("like ilike in between regexp rlike similar".split())
_NOT_A_VALUE = frozenset("null default true false unknown not".split())
# Functions whose arguments are compared with each other, not with an operator.
_SAME_SIDE_FUNCTIONS = frozenset(
	"locate instr position replace ifnull coalesce nullif field find_in_set elt strcmp concat concat_ws "
	"json_set json_insert json_replace".split()
)
# Functions whose every literal argument is a secret.
_SECRET_ARGUMENT_FUNCTIONS = frozenset({"password", "old_password", "aes_encrypt", "aes_decrypt"})
# Keywords that are also function names, as in ``REPLACE(password, 'a', 'b')``.
_KEYWORD_FUNCTIONS = frozenset({"replace", "insert"})
_PATH_ARROWS = ("->>", "->")
_KEY_IDENTS = frozenset({"field", "defkey", "key", "parameter"})
_VALUE_IDENTS = frozenset({"value", "defvalue"})
_KEY_VALUE_PAIRS = (("field", "value"), ("defkey", "defvalue"), ("key", "value"), ("parameter", "value"))
_LITERAL_KINDS = frozenset({"str", "num", "dq"})
_GROUPING_KINDS = frozenset({"p", "call"})

# Where a statement is in its INSERT, to map each VALUES row onto the column list.
_NO_INSERT, _AWAIT_COLUMNS, _AWAIT_VALUES, _IN_VALUES = 0, 1, 2, 3


class _Elem(NamedTuple):
	first: int | None
	last: int | None
	valued: bool
	literal: bool
	sensitive: bool
	key: str | None
	ident: str | None


class _Pending(NamedTuple):
	"""A comparison operator waiting for its right-hand side."""

	op: str
	first: int | None
	last: int | None
	sensitive: bool
	literal: bool
	ident: str | None
	group: _Frame | None


class _Frame:
	"""One bracket level (or a CASE) with the segment being read in it."""

	__slots__ = (
		"kind",
		"opened",
		"pend",
		"upper_bound",
		"boundary",
		"any_lit",
		"any_sens",
		"elems",
		"subquery",
		"mask_results",
		"results",
		"first",
		"last",
		"lit",
		"sens",
		"cased",
		"ntok",
		"word",
		"ident",
		"group",
	)

	def __init__(self, kind: str, opened: int = -1):
		self.kind = kind
		self.opened = opened
		self.pend: _Pending | None = None
		self.upper_bound = False
		self.boundary = ""
		self.any_lit = self.any_sens = False
		self.elems: list[_Elem] = []
		self.subquery = False
		self.mask_results = self.results = False
		self.reset_segment()

	def reset_segment(self):
		self.first = self.last = None
		self.lit = self.sens = self.cased = False
		self.ntok = 0
		self.word = self.ident = None
		self.group = None

	def add(self, index, *, literal=False, sensitive=False, word=None, ident=None, counted=True):
		if self.first is None:
			self.first = index
		self.last = index
		self.lit |= literal
		self.sens |= sensitive
		self.group = None
		if counted:
			self.ntok += 1
			self.word, self.ident = word, ident


def _span_order(span):
	return span[0], -span[1]


class _Masker:
	"""Finds the spans to replace and builds the output, or the all-literals-masked fallback."""

	def __init__(self, sql, tokens, dialect, names):
		self.sql, self.tokens, self.dialect, self.names = sql, tokens, dialect, names
		self.sig = [token for token in tokens if token[0] != "cm"]
		self.kinds = [kind for kind, _, _ in self.sig]
		self.text = [sql[start:end] for _, start, end in self.sig]
		self.spans: list[tuple[int, int, str]] = []
		self.doubt = self.stopped = False
		self.stack = [_Frame("top")]
		self.index = 0
		self.columns: list[str | None] | None = None
		self._reset_statement()

	def run(self) -> tuple[str, bool]:
		self._scan()
		if self.doubt:
			return self._mask_everything(), True
		return self._apply_spans(), False

	# -- token access
	def _word(self, index) -> str:
		if 0 <= index < len(self.sig) and self.kinds[index] == "w":
			return self.text[index].lower()
		return ""

	def _text_is(self, index, value) -> bool:
		return 0 <= index < len(self.sig) and self.text[index] == value

	def _identifier(self, index) -> str | None:
		kind = self.kinds[index]
		if kind == "w":
			return self.names.normalize(self.text[index])
		if kind in ("bt", "id") or (kind == "dq" and self.dialect is None):
			# No real column name is this long, and a string is not a name.
			return (
				self.names.normalize(self.text[index][1:-1])
				if len(self.text[index]) <= _IDENTIFIER_MAX
				else None
			)
		return None

	def _string_body(self, index) -> str:
		text = self.text[index]
		quote = text.find("'")
		return text[quote + 1 : -1] if self.kinds[index] == "str" and quote >= 0 else text[1:-1]

	# -- spans
	def _mask(self, first, last, *, like=False):
		replacement = PLACEHOLDER
		if like and first == last and self.kinds[first] == "str":
			wildcard = self._string_body(first)[:1]
			if wildcard in ("%", "_"):
				replacement = f"'{wildcard}<REDACTED>'"
		self.spans.append((self.sig[first][1], self.sig[last][2], replacement))

	def _mask_operand(self, first, last, group, *, like=False):
		"""Mask one operand. A row keeps its arity: each value of it is masked, not the whole."""
		if group is None:
			self._mask(first, last, like=like)
			return
		for elem in group.elems:
			if elem.valued and elem.first is not None:
				self._mask(elem.first, elem.last)

	def _apply_spans(self) -> str:
		sql, out, position = self.sql, [], 0
		for start, end, replacement in sorted(self.spans, key=_span_order):
			if start < position:
				continue
			out.append(sql[position:start])
			out.append(replacement)
			position = end
		if not out:
			return sql
		out.append(sql[position:])
		return "".join(out)

	# -- the pass
	def _is_truncation_note(self, start, end) -> bool:
		"""The analyzer's own note, and only as the last thing in the statement."""
		return _TRUNCATION_NOTE.fullmatch(self.sql, start, end) is not None and not self.sql[end:].strip()

	def _scan(self):
		for kind, start, end in self.tokens:
			if kind == "cm" and not self._is_truncation_note(start, end):
				self.spans.append((start, end, " "))
			elif kind == "cond":
				self.doubt = True
		count = len(self.sig)
		while self.index < count and not self.stopped:
			self._step()
			self.index += 1
		if self.stopped:
			return
		if len(self.stack) != 1:
			self.doubt = True
			return
		self._end_segment(self.stack[0], None)
		self._end_statement()

	def _step(self):
		index = self.index
		kind, text = self.kinds[index], self.text[index]
		frame = self.stack[-1]
		if kind == "w":
			self._step_word(index, text.lower(), frame)
		elif kind == "op":
			self._step_operator(index, text, frame)
		else:
			if kind == "str" and self._is_secret_literal(index):
				self._mask(index, index)
			self._operand(index, kind, frame)

	def _step_operator(self, index, text, frame):
		if text == "(":
			self._open_paren()
		elif text == "[":
			frame.add(index)
			self.stack.append(_Frame("p", index))
		elif text in (")", "]"):
			self._close_paren()
		elif text in (",", ";"):
			self._boundary(text)
		elif text == "~" and frame.pend is not None and frame.first is None:
			self._operand(index, "op", frame)
		elif text in _COMPARISONS:
			self._comparison(1)
		else:
			self._operand(index, "op", frame)

	def _step_word(self, index, word, frame):
		if word == "case":
			self._open_case()
		elif word == "end" and frame.kind == "case":
			self._close_case()
		elif word in _KEYWORD_FUNCTIONS and self._text_is(index + 1, "("):
			self._operand(index, "w", frame)
		elif word == "identified":
			self._mask_identified_by(index)
			self._operand(index, "w", frame)
		elif (
			word == "and" and frame.pend is not None and frame.pend.op == "between" and not frame.upper_bound
		):
			self._end_segment(frame, self._parent())
			frame.upper_bound = True
		elif width := self._comparison_width(index, word):
			self._comparison(width)
		elif self._is_boundary_word(word):
			self._boundary(word)
		else:
			self._operand(index, "w", frame)

	def _mask_identified_by(self, index):
		"""The password of ``IDENTIFIED [WITH plugin] BY [PASSWORD] 'value'``."""
		at = index + 1 + 2 * (self._word(index + 1) == "with")
		if self._word(at) != "by":
			return
		at += 1 + (self._word(at + 1) == "password")
		if at >= len(self.kinds) or self.kinds[at] not in ("str", "dq"):
			return
		last = at
		while last + 1 < len(self.kinds) and self.kinds[last + 1] in ("str", "dq"):
			last += 1
		self._mask(at, last)

	def _is_secret_literal(self, index) -> bool:
		text = self.text[index]
		if ("$" in text or "gAAAAA" in text) and _SECRET_SHAPE.fullmatch(self._string_body(index)):
			return True
		return '"' in text and self.names.json_key.search(self.names.normalize(text)) is not None

	def _is_boundary_word(self, word) -> bool:
		if word not in _BOUNDARY_WORDS:
			return False
		if word in ("value", "values"):
			return len(self.stack) == 1 and self.insert_state in (_AWAIT_COLUMNS, _AWAIT_VALUES)
		return True

	def _parent(self):
		return self.stack[-2] if len(self.stack) > 1 else None

	def _operand(self, index, kind, frame):
		name = self._identifier(index)
		if len(self.stack) == 1:
			self.at_statement_start = False
		if kind == "op" and self.text[index] == ".":
			frame.add(index, counted=False)
			return
		plain_word = kind == "w" and not self._is_qualified(index) and not self._text_is(index + 1, "(")
		frame.add(
			index,
			literal=kind in _LITERAL_KINDS,
			sensitive=self.names.is_name(name) or (kind == "str" and self._is_sensitive_json_path(index)),
			word=name if plain_word else None,
			ident=name,
		)

	def _is_qualified(self, index) -> bool:
		return self._text_is(index + 1, ".") or self._text_is(index - 1, ".")

	def _is_sensitive_json_path(self, index) -> bool:
		after_arrow = index > 0 and self.text[index - 1] in _PATH_ARROWS
		if not after_arrow and "$" not in self.text[index]:
			return False
		body = self._string_body(index)
		return (after_arrow or body.startswith("$")) and self.names.is_key(self.names.normalize(body))

	# -- brackets and CASE
	def _open_paren(self):
		index, frame = self.index, self.stack[-1]
		kind = "p"
		if frame.pend is not None and frame.pend.op == "in" and frame.first is None:
			kind = "in"
		elif len(self.stack) == 1 and self.insert_state == _AWAIT_COLUMNS:
			kind = "cols"
		elif len(self.stack) == 1 and self.insert_state == _IN_VALUES:
			kind = "row"
		elif self._word(index - 1) in _SAME_SIDE_FUNCTIONS:
			kind = "call"
		elif self._word(index - 1) in _SECRET_ARGUMENT_FUNCTIONS:
			kind = "secret_call"
		frame.add(index)
		self.stack.append(_Frame(kind, index))

	def _close_paren(self):
		if len(self.stack) == 1:
			self.doubt = self.stopped = True
			return
		done, parent = self._pop_group()
		if done.kind == "cols":
			self.columns = [elem.ident for elem in done.elems]
			self.insert_state = _AWAIT_VALUES
		elif done.kind == "row":
			self._finish_row(done)
		elif done.kind == "call":
			self._mask_same_side(done)
		elif done.kind == "secret_call":
			self._mask_literal_arguments(done)
		if done.kind == "in" and parent.pend is not None:
			if done.any_sens and not done.subquery and parent.pend.literal:
				self._mask_operand(parent.pend.first, parent.pend.last, parent.pend.group)
			parent.pend = None
			return
		parent.lit |= done.any_lit
		if not done.subquery:
			parent.sens |= done.any_sens
		if done.kind in _GROUPING_KINDS and len(done.elems) > 1 and done.opened == parent.first:
			parent.group = done

	def _pop_group(self):
		done = self.stack.pop()
		parent = self.stack[-1]
		self._end_segment(done, parent)
		parent.last = self.index
		parent.word = parent.ident = None
		parent.group = None
		return done, parent

	def _open_case(self):
		frame = self.stack[-1]
		pend = frame.pend
		case = _Frame("case", self.index)
		case.mask_results = pend is not None and pend.sensitive
		frame.add(self.index)
		self.stack.append(case)

	def _close_case(self):
		done, parent = self._pop_group()
		parent.cased |= done.mask_results
		parent.lit |= done.any_lit
		parent.sens |= done.any_sens

	def _mask_literal_arguments(self, call):
		for elem in call.elems:
			if elem.literal and elem.first is not None:
				self._mask(elem.first, elem.last)

	def _mask_same_side(self, call):
		"""A literal beside a sensitive argument, as in ``LOCATE('x', password)``."""
		if not call.any_sens:
			return
		for elem in call.elems:
			if elem.literal and not elem.sensitive and elem.first is not None:
				self._mask(elem.first, elem.last)

	# -- comparisons
	def _comparison_width(self, index, word) -> int:
		"""Tokens taken by the comparison word at ``index``, or 0 when it is not one."""
		if word == "not":
			follower = self._word(index + 1)
			return 1 + self._comparison_width(index + 1, follower) if follower in _NEGATABLE else 0
		if word == "similar":
			return 2 if self._word(index + 1) == "to" else 0
		if word == "member":
			return 2 if self._word(index + 1) == "of" else 0
		if word == "is":
			after_not = index + 1 + (self._word(index + 1) == "not")
			distinct = self._word(after_not) == "distinct" and self._word(after_not + 1) == "from"
			return after_not + 2 - index if distinct else 1
		return 1 if word in _COMPARISONS else 0

	def _operator_name(self) -> str:
		first = self.index + (self._word(self.index) == "not")
		return self._word(first) or self.text[first]

	def _comparison(self, width):
		frame = self.stack[-1]
		pend = _Pending(
			self._operator_name(),
			frame.first,
			frame.last,
			frame.sens,
			frame.lit and frame.first is not None,
			frame.ident,
			frame.group,
		)
		self._end_segment(frame, self._parent())
		frame.pend, frame.upper_bound = pend, False
		self.index += width - 1

	def _boundary(self, word):
		frame = self.stack[-1]
		self._end_segment(frame, self._parent())
		frame.pend = None
		frame.boundary = word
		if frame.kind == "case":
			frame.results = word in ("then", "else")
		if word in ("select", "with") and frame.kind != "top":
			frame.subquery = True
		if len(self.stack) == 1:
			self._statement_boundary(word)

	# -- segments
	def _valued(self, frame) -> bool:
		"""A segment that can hold a secret: a literal, or one bare word that is not a name."""
		if frame.lit:
			return True
		word = frame.word
		return (
			frame.ntok == 1 and word is not None and word not in _NOT_A_VALUE and not self.names.is_name(word)
		)

	def _end_segment(self, frame, parent):
		if frame.first is not None:
			self._settle_pending(frame)
			self._settle_element(frame, parent)
			self._record_element(frame)
		frame.any_lit |= frame.lit
		frame.any_sens |= frame.sens
		frame.reset_segment()

	def _settle_pending(self, frame):
		pend = frame.pend
		if pend is None:
			return
		if pend.sensitive and self._valued(frame):
			if not frame.cased:
				self._mask_operand(frame.first, frame.last, frame.group, like=pend.op in ("like", "ilike"))
		elif frame.sens and pend.literal:
			self._mask_operand(pend.first, pend.last, pend.group)
		self._note_key_value(frame, pend)

	def _note_key_value(self, frame, pend):
		"""Track ``field = 'api_key'`` and ``value = x`` of a settings row for the statement end."""
		if pend.ident in _VALUE_IDENTS and self._valued(frame):
			self.value_candidates.append((frame.first, frame.last))
		if pend.ident in _KEY_IDENTS and self._is_sensitive_key_literal(frame):
			self.key_flagged = True

	def _settle_element(self, frame, parent):
		"""The elements of ``IN (...)`` under a sensitive left side, and the results of a CASE
		that is assigned to a sensitive column."""
		if frame.kind == "in":
			pend = parent.pend
			if pend is not None and pend.ident in _KEY_IDENTS and self._is_sensitive_key_literal(frame):
				self.key_flagged = True
			if pend is not None and pend.sensitive and self._is_maskable_element(frame):
				self._mask_operand(frame.first, frame.last, frame.group)
		elif frame.kind == "case" and frame.results and frame.mask_results and self._valued(frame):
			self._mask_operand(frame.first, frame.last, frame.group)

	def _is_maskable_element(self, frame) -> bool:
		"""An ``IN (...)`` element that holds a value. In a subquery only a literal in the select
		list counts: its columns, conditions and LIMIT are not values of the sensitive column."""
		if not frame.subquery:
			return self._valued(frame)
		return frame.lit and frame.boundary == "select" and frame.pend is None

	def _is_sensitive_key_literal(self, frame) -> bool:
		return self._single_string(frame) and self.names.is_key(self._key_of(frame))

	def _single_string(self, frame) -> bool:
		return (
			frame.first is not None and frame.first == frame.last and self.kinds[frame.first] in ("str", "dq")
		)

	def _key_of(self, frame) -> str:
		return self.names.normalize(self._string_body(frame.first))

	def _record_element(self, frame):
		kind = frame.kind
		if kind not in ("row", "cols", "call", "secret_call", "p"):
			return
		key = self._key_of(frame) if kind == "row" and self._single_string(frame) else None
		frame.elems.append(
			_Elem(
				frame.first,
				frame.last,
				self._valued(frame),
				frame.lit,
				frame.sens,
				key,
				frame.ident if frame.ntok == 1 else None,
			)
		)

	# -- INSERT / REPLACE
	def _reset_statement(self):
		self.insert_state = _NO_INSERT
		self.columns = None
		self.at_statement_start = True
		self.value_candidates: list[tuple[int, int]] = []
		self.key_flagged = False

	def _end_statement(self):
		if self.key_flagged:
			for first, last in self.value_candidates:
				self._mask(first, last)
		self._reset_statement()

	def _statement_boundary(self, word):
		at_start, self.at_statement_start = self.at_statement_start, word == ";"
		state = self.insert_state
		if word == ";":
			self._end_statement()
		elif word == "insert" or (word == "replace" and at_start):
			self._end_statement()
			self.insert_state = _AWAIT_COLUMNS
		elif word in ("values", "value") and state in (_AWAIT_COLUMNS, _AWAIT_VALUES):
			self.insert_state = _IN_VALUES
		elif word in ("select", "with") and state in (_AWAIT_COLUMNS, _AWAIT_VALUES):
			self.doubt |= state == _AWAIT_COLUMNS or self._columns_hold_secrets()
			self.insert_state = _NO_INSERT
		elif (state == _IN_VALUES and word != ",") or (state == _AWAIT_COLUMNS and word == "set"):
			self.insert_state = _NO_INSERT

	def _columns_hold_secrets(self) -> bool:
		columns = self.columns or ()
		pair = any(key in columns and value in columns for key, value in _KEY_VALUE_PAIRS)
		return pair or any(self.names.is_name(column) for column in columns)

	def _finish_row(self, row):
		columns = self.columns
		if columns is None or len(columns) != len(row.elems):
			self.doubt = True
			return
		for elem, column in zip(row.elems, columns, strict=True):
			if self.names.is_name(column) and elem.valued and elem.first is not None:
				self._mask(elem.first, elem.last)
		for key_column, value_column in _KEY_VALUE_PAIRS:
			if key_column in columns and value_column in columns:
				key = row.elems[columns.index(key_column)].key
				value = row.elems[columns.index(value_column)]
				if key and self.names.is_key(key) and value.valued and value.first is not None:
					self._mask(value.first, value.last)

	# -- fallback
	def _mask_everything(self) -> str:
		"""Mask every literal and keep the statement readable."""
		sql, out, position, previous, ordinal = self.sql, [], 0, None, 0
		for kind, start, end in self.tokens:
			out.append(sql[position:start])
			position = end
			text = sql[start:end]
			if kind == "cm":
				out.append(text if self._is_truncation_note(start, end) else " ")
				continue
			out.append(self._doubt_replacement(kind, text, previous, ordinal))
			previous = text.lower()
			ordinal += 1
		out.append(sql[position:])
		return "".join(out)

	def _doubt_replacement(self, kind, text, previous, ordinal) -> str:
		if kind in ("str", "dq"):
			return PLACEHOLDER
		if kind == "num":
			return "0"
		if kind == "w" and previous in _COMPARISONS and self._bare_value(text, ordinal):
			return PLACEHOLDER
		return text

	def _bare_value(self, text, ordinal) -> bool:
		lower = text.lower()
		if lower in _NOT_A_VALUE or lower in _BOUNDARY_WORDS:
			return False
		return not (self._text_is(ordinal + 1, ".") or self._text_is(ordinal + 1, "("))


def redact_sql_outcome(
	sql_str: str, *, extra_columns: tuple[str, ...] = (), dialect: str | None = None
) -> tuple[str, bool]:
	"""Return ``(text, all_masked)``: ``sql_str`` with the values of sensitive names replaced by
	``'<REDACTED>'``, and whether the statement could not be read well enough, so every literal
	was masked (``all_masked``) or the statement was replaced by ``OMITTED_SQL``.

	``dialect`` is ``"mariadb"``, ``"postgres"`` or None. With None the statement is read both
	ways and omitted when the two readings disagree on a string.
	"""
	names = _names(tuple(extra_columns or ()))
	if not sql_str or not names.hit(sql_str):
		return sql_str, False
	if len(sql_str) > MAX_REDACTED_SQL_CHARS or (dialect != "mariadb" and _has_many_dollar_tags(sql_str)):
		return OMITTED_SQL, True
	if dialect in _LEXERS:
		tokens = _lex(sql_str, dialect)
	else:
		dialect, tokens = None, _lex_agreed(sql_str)
		if tokens is None:
			return OMITTED_SQL, True
	if len(tokens) > _MAX_TOKENS or _lexically_untrusted(sql_str, tokens):
		return OMITTED_SQL, True
	return _Masker(sql_str, tokens, dialect, names).run()


def _has_many_dollar_tags(sql: str) -> bool:
	"""A PostgreSQL ``$tag$`` quote is searched for its closer, so many distinct unclosed tags cost
	quadratic time; a real statement holds only a few."""
	return "$" in sql and len(_DOLLAR_TAG.findall(sql)) > _MAX_DOLLAR_TAGS


def redact_sql_fail_closed(
	sql_str: str, *, extra_columns: tuple[str, ...] = (), dialect: str | None = None, on_error=None
) -> tuple[str, bool]:
	""":func:`redact_sql_outcome` that never raises: a failure gives ``OMITTED_SQL``, and the
	exception (never the SQL) goes to ``on_error`` so a caller can log it."""
	try:
		return redact_sql_outcome(sql_str, extra_columns=extra_columns, dialect=dialect)
	except Exception as error:
		if on_error is not None:
			on_error(error)
		return OMITTED_SQL, True


def redact_sql_literals(
	sql_str: str, *, extra_columns: tuple[str, ...] = (), strict: bool = False, dialect: str | None = None
) -> str:
	"""Return ``sql_str`` with the literal values of sensitive columns replaced by ``'<REDACTED>'``.

	Covers comparisons (``=``, ``LIKE``, ``IN``, ``BETWEEN``, ``REGEXP`` and the rest, in either
	order), ``UPDATE SET``, positional ``INSERT`` rows, ``CASE`` results, settings rows keyed by
	field name, and stored-secret value shapes. Never returns the input after a failure: the
	statement is omitted, or the error is raised when ``strict``.
	"""
	if not sql_str or not isinstance(sql_str, str):
		return sql_str or ""
	if strict:
		return redact_sql_outcome(sql_str, extra_columns=extra_columns, dialect=dialect)[0]
	return redact_sql_fail_closed(sql_str, extra_columns=extra_columns, dialect=dialect)[0]


def redact_call_queries(
	calls, *, extra_columns: tuple[str, ...] = (), dialect: str | None = None, on_error=None
) -> None:
	"""Apply :func:`redact_sql_fail_closed` over a recording's ``calls`` list in place. Touches the
	``query`` and ``normalized_query`` fields, and sets ``optimus_literals_masked`` on a call
	whose statement was masked whole.

	Mutates input different shape from the dict/list redactor because
	calls lists are large and copying is wasteful when the caller
	already owns the recording dict.
	"""
	if not isinstance(calls, list):
		return
	for call in calls:
		if not isinstance(call, dict):
			continue
		for field in ("query", "normalized_query"):
			if not call.get(field) or not isinstance(call[field], str):
				continue
			call[field], masked = redact_sql_fail_closed(
				call[field], extra_columns=extra_columns, dialect=dialect, on_error=on_error
			)
			if masked:
				call[LITERALS_MASKED_KEY] = True


# ---------------------------------------------------------------------------
# Secret scrubbing for log text (PR-0a). Used by ai_fix.log_ai_failure on
# every AI-surface Error Log message and by optimus.maintenance to scrub rows
# written before the fix. The shapes, each masked without the key literal (a
# key no longer stored, after a rotation):
# - a dict repr entry carrying an auth header (authorization or
#   proxy-authorization with the Bearer, Basic, Token, Key or ApiKey scheme)
#   or an api-key field (x-api-key, x-goog-api-key, api_key, api-key,
#   apikey), with a str or bytes value;
# - "Bearer <token>" anywhere (exception text, echoed bodies), as spelled;
# - bearer, basic or token in any case followed by a token holding a digit
#   (a random key has one), so prose such as "the bearer of" or "token
#   expired" is left;
# - an "Authorization: <scheme> <token>" header line, any case, with one of
#   the five schemes above;
# - an x-api-key or x-goog-api-key header line, and an api-key or api_key
#   header line whose token holds a digit;
# - a header value quoted in an HTTP library's error message;
# - credentials in a URL (a Base URL typed as user:pass@host).
# A token is a run of 8 or more characters with no quote and no whitespace
# that never ends on a backslash, so a JSON-escaped quote after it stays.
# Nothing else is masked by shape: another header, or a key in running prose
# with no scheme or header name in front of it, is masked only as a literal.
# ---------------------------------------------------------------------------

SECRET_PLACEHOLDER = "********"

# The schemes an Authorization value can start with. The authorization rules
# that mask a schemeless token skip a value that is only a scheme (see
# ``_SCHEME_ONLY``).
_AUTH_SCHEMES = ("bearer", "basic", "token", "key", "apikey")
# Lookahead for those rules: a scheme followed by white space (its token is
# the business of the scheme rules), or a scheme, or the start of one, that
# ends the text (a field cut inside the word, as a title is cut to its
# column, which no token follows).
_SCHEME_ONLY = (
	"(?:(?:" + "|".join(_AUTH_SCHEMES) + r")\s|(?:"
	+ "|".join(sorted({w[:n] for w in _AUTH_SCHEMES for n in range(1, len(w) + 1)}, key=len, reverse=True))
	+ r")\Z)"
)

_SECRET_PATTERNS: tuple[re.Pattern, ...] = (
	# 'authorization': 'Bearer <tok>' (either quote style; also Basic, Token,
	# Key, ApiKey; the value may be bytes: b'Bearer <tok>'). A frame-local
	# cut may remove the closing quote; stop at that line's end. A cut
	# through an existing placeholder must not expand it again.
	re.compile(
		r"""((['"])(?:proxy-)?authorization\2\s*:\s*b?(['"])(?:bearer|basic|token|key|apikey)\s+)(?!\*{8}\3|\*{1,8}\r?$)[^'"\s]+(\3|(?=\r?$))""",
		re.IGNORECASE | re.MULTILINE,
	),
	# 'x-api-key' / 'x-goog-api-key' / 'api_key' / 'api-key' / 'apikey' : '<tok>'
	# (the value may be bytes: b'<tok>')
	re.compile(
		r"""((['"])(?:x-api-key|x-goog-api-key|api[_-]?key|apikey)\2\s*:\s*b?(['"]))(?!\*{8}\3|\*{1,8}\r?$)[^'"\r\n]+(\3|(?=\r?$))""",
		re.IGNORECASE | re.MULTILINE,
	),
	# bare "Bearer <tok>" anywhere else. The token is any run of non-quote,
	# non-space characters, so a pasted smart quote inside or in front of the
	# key (Bearer \u2019sk-...) is masked too; it never ends on a backslash, so
	# a JSON-escaped quote after it (Deleted Document data) stays intact.
	re.compile(r"""(\bBearer\s+)(?!\*{8})[^'"\s]{7,}[^'"\s\\]()()()"""),
	# an x-api-key / x-goog-api-key header line as http.client builds it (its
	# putheader local: b'x-api-key: <key>'), with no key literal to match it
	# (a rotated key): the token, as for Bearer, is a run of 8 or more
	# non-quote, non-space characters that never ends on a backslash
	re.compile(r"""(\bx-(?:goog-)?api-key:[ \t]*)(?!\*{8})[^'"\s]{7,}[^'"\s\\]()()()""", re.IGNORECASE),
	# bearer / basic / token in any case, when the token holds a digit (a
	# random key has one; "the bearer of", "token expired" do not)
	re.compile(
		r"""(\b(?:bearer|basic|token)\s+)(?!\*{8})(?=[^'"\s]*[0-9])[^'"\s]{7,}[^'"\s\\]()()()""", re.IGNORECASE,
	),
	# an Authorization header line: Authorization: <scheme> <token>, any case
	re.compile(
		r"""(\b(?:proxy-)?authorization:[ \t]*(?:bearer|basic|token|key|apikey)[ \t]+)(?!\*{8})"""
		r"""[^'"\s]{7,}[^'"\s\\]()()()""",
		re.IGNORECASE,
	),
	# a bare api-key / api_key header line (Azure OpenAI's api-key header),
	# when the token holds a digit ("api_key: required" is prose)
	re.compile(
		r"""(\bapi[-_]key:[ \t]*)(?!\*{8})(?=[^'"\s]*[0-9])[^'"\s]{7,}[^'"\s\\]()()()""", re.IGNORECASE,
	),
	# a header value an HTTP library could not send, quoted in its message:
	# http.client's "Invalid header value b'<value>'" and requests' "... in
	# header value: '<value>'"; the quoted repr is masked whole, its escape
	# pairs consumed together (disjoint alternatives keep overflow linear)
	re.compile(r"""(\b(?:Invalid header value|in header value:) b?(['"]))(?!\*{8}\2|\*{1,8}\r?$)(?:\\[^\r\n]|(?!\2)[^\\\r\n])+()(\2|(?=\r?$))""", re.MULTILINE),
	# credentials in a URL's authority: scheme://user:password@host, up to the
	# LAST "@" before the path, query or fragment (a password may hold a raw
	# "@"); the userinfo holds no "/", "?" or "#", so an address in a query
	# string (?to=a@b.com) or a fragment is not a credential; never across a
	# quote, so an address in the next field of compact JSON is not swallowed
	re.compile(r"""(://)()()(?!\*{8}@)[^/?#\s'"]+(@)"""),
	# Explicit Authorization containers also hold schemeless/rotated tokens.
	# Exclude known schemes here: their rules above preserve the scheme and
	# truncated placeholders, and repeated scrubbing must remain idempotent.
	# A scheme word followed by a token, or a scheme word (or the start of
	# one) alone at the end of the text, is one too: a field cut inside
	# "Bearer" (a title cut to its column) leaves the scheme with no token,
	# and masking that bare word would lengthen the text and change it on
	# every pass.
	re.compile(
		rf"""((['"])(?:proxy-)?authorization\2\s*:\s*b?(['"]))(?!\*|{_SCHEME_ONLY})[^'"\r\n]+(\3|(?=\r?$))""",
		re.IGNORECASE | re.MULTILINE,
	),
	re.compile(
		rf"""(\b(?:proxy-)?authorization:[ \t]*)(?![ \t]|\*|{_SCHEME_ONLY})[^'"\r\n]+()()()""",
		re.IGNORECASE,
	),
)

# The rest of a URL after its scheme, for the query-parameter rule (``is_secret_param``). Anchored on
# "://" so a long run of letters is not rescanned from every position; a backslash ends it so a
# JSON-escaped quote after the URL survives.
_URL_TAIL = re.compile(r"""://[^\s'"<>\\]+""")

# A literal shorter than this is never replaced: it would shred ordinary words
# (tests and misconfigured sites use keys like "k"); real provider keys are
# far longer. The one minimum key length of the AI key handling: ai_fix, the
# scrub (optimus.maintenance) and this module use it; error_log_mask keeps a
# copy (a test pins them equal).
MIN_KEY_LEN = 8


# The most characters of one text any scrub pass reads: a provider's reply
# (ai_fix._response_detail) and each text field of a record the Error Log
# hook masks (maintenance._masked_record) are cut to it first, so a planted
# multi-megabyte value costs a bounded amount of regex work.
SCRUB_TEXT_CAP = 65536


def key_literals(api_key) -> tuple[str, ...]:
	"""``api_key`` in every form text can hold it, for ``scrub_secrets(...,
	literals=...)`` and for the stored-key checks: raw, JSON-escaped
	(``json.dumps(api_key)[1:-1]``: a JSON body, Deleted Document data) and
	repr-escaped (``repr(api_key)[1:-1]``: a frame local Frappe prints by
	repr), without duplicates, raw first. ``()`` when there is no key (not a
	string, or empty). The parameter holds the key, so it is named
	``api_key``, a name the traceback sanitizers redact; pass the result
	straight into the call that uses it. ``optimus.error_log_mask`` keeps a
	stdlib-only copy (``_key_literals``) for the process whose Optimus
	modules cannot be imported; a test pins the two equal."""
	if not isinstance(api_key, str) or not api_key:
		return ()
	return tuple(dict.fromkeys((api_key, json.dumps(api_key)[1:-1], repr(api_key)[1:-1])))


def _literal_length(api_key) -> int:
	"""The sort key of a ``scrub_secrets`` literal: its length (0 for a value
	that is not a string). The parameter holds the key, so it is named
	``api_key``, a name the traceback sanitizers redact."""
	return len(api_key) if isinstance(api_key, str) else 0


def _form_key_extras() -> tuple[str, ...]:
	"""The Settings extras, from the one place every caller reads them (imported late: this module
	stays importable before Frappe is)."""
	from optimus.redaction_extras import form_key_extras

	return form_key_extras()


def scrub_secrets(text: str, *, literals: tuple[str, ...] = ()) -> str:
	"""Return ``text`` with API keys replaced by ``********``.

	Replaces every exact occurrence of each ``literals`` entry that is a
	string of at least 8 characters (the live key, when the caller knows
	it), longest first, so a literal inside another one (a stored key and
	the key a request was sent with can overlap) never leaves part of the
	longer one behind; then the shapes listed above ``_SECRET_PATTERNS``
	(header entries and lines with a scheme or an api-key name, Bearer,
	URL credentials): a key in running prose with none of those in front of
	it is masked only as a literal. Idempotent: an already-masked value is never matched again, so a
	second pass changes nothing. Non-string input is returned unchanged.

	SECURITY: the key arrives in the ``literals`` parameter, a name neither
	Frappe's traceback sanitizer nor Sentry redacts, so it is moved into
	``secret`` and ``literals`` dropped before any scrubbing, and before the
	sort, which calls its key back in Python, where an interrupt can land.
	From then on the key is held only in locals whose names both redact
	(``secret``, ``api_key``). Callers still guard the call and never log a
	failure of it with frame locals: ``text`` itself holds the unmasked
	value.
	"""
	if not text or not isinstance(text, str):
		return text
	secret = list(literals or ())
	del literals
	secret.sort(key=_literal_length, reverse=True)
	out = text
	for api_key in secret:
		if isinstance(api_key, str) and len(api_key) >= MIN_KEY_LEN:
			out = out.replace(api_key, SECRET_PLACEHOLDER)
	for pattern in _SECRET_PATTERNS:
		out = pattern.sub(lambda m: m.group(1) + SECRET_PLACEHOLDER + (m.group(4) or ""), out)
	if "://" in out:
		extra_keys = _form_key_extras()
		out = _URL_TAIL.sub(
			lambda m: redact_url_run(m.group(0), extra_keys, placeholder=SECRET_PLACEHOLDER), out
		)
	return out
