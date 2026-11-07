# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""A per-run cache in front of ``frappe.db`` for Frappe's index optimizer.

Frappe's ``_optimize_query`` re-reads ``DESCRIBE``, ``SHOW INDEX``, the
information_schema row count and ``COUNT(DISTINCT col)`` for every table on every
call. Optimus calls it once per unique query shape, so a session with hundreds of
shapes on one table repeats the same reads hundreds of times, and a
``COUNT(DISTINCT)`` that hits the statement timeout is retried for every shape.

``cached_db`` swaps ``frappe.local.db`` (what ``frappe.db`` resolves to, per request)
for a proxy that remembers those four statements for the exact table and column they
name, successes and failures alike, and ``table_exists`` too. Frappe's own optimizer
code runs unchanged; any other statement passes straight through. Only errors that
repeat (see ``_DETERMINISTIC_ERRORS``) are remembered, and one is raised again as a
fresh exception of the same type, never the stored object, tagged with the statement
that failed (``failed_statement``). RQ job timeouts are never remembered. The real db
object is put back on every exit path, unless something else replaced the proxy.
"""

import re
from contextlib import contextmanager

_STATEMENTS = (
	("describe", re.compile(r"describe (`[^`]+`)", re.I)),
	("show_index", re.compile(r"show index from (`[^`]+`)", re.I)),
	(
		"row_count",
		re.compile(
			r"select table_rows from information_schema\.tables where table_name = '([^']+)'",
			re.I,
		),
	),
	("cardinality", re.compile(r"select count\(distinct (.+?)\) from (`[^`]+`)", re.I)),
)

# Errors that repeat for the same statement: syntax error, no such table, unknown column.
# Only these are remembered; anything else (connection lost, lock wait, deadlock) is raised
# without caching so a later shape retries.
_DETERMINISTIC_ERRORS = frozenset({1064, 1146, 1054})

# 1969 (statement timeout) repeats only for the full-table COUNT(DISTINCT); on the cheap
# metadata reads it is a one-off lock or load blip that must not poison the whole table.
_TIMEOUT_ERROR = 1969
_TIMEOUT_REPEATS_FOR = "cardinality"

# Set on an error raised from the cache, naming the optimizer statement that failed (and,
# for the cardinality statement, the column it counted).
STATEMENT_ATTRIBUTE = "optimus_statement"
COLUMN_ATTRIBUTE = "optimus_column"

_OWN_ATTRIBUTES = ("_real", "_memo", "_passthrough", "_tables", "_hits", "_misses")


def _recognise(query) -> tuple | None:
	"""The cache key of one of Frappe's optimizer statements, or ``None``."""
	if not isinstance(query, str):
		return None
	text = " ".join(query.split())
	for kind, pattern in _STATEMENTS:
		match = pattern.fullmatch(text)
		if match:
			return (kind, *match.groups())
	return None


class CachingDb:
	"""Wraps the real db object; see the module docstring."""

	def __init__(self, real, passthrough_errors: tuple = ()):
		object.__setattr__(self, "_real", real)
		object.__setattr__(self, "_memo", {})
		object.__setattr__(self, "_passthrough", tuple(passthrough_errors))
		object.__setattr__(self, "_tables", {})
		object.__setattr__(self, "_hits", 0)
		object.__setattr__(self, "_misses", 0)

	def __getattr__(self, name):
		return getattr(self._real, name)

	def __setattr__(self, name, value):
		if name in _OWN_ATTRIBUTES:
			object.__setattr__(self, name, value)
			return
		setattr(self._real, name, value)

	def table_exists(self, doctype, *args, **kwargs):
		if args or kwargs:
			return self._real.table_exists(doctype, *args, **kwargs)
		if doctype not in self._tables:
			self._tables[doctype] = self._real.table_exists(doctype)
		return self._tables[doctype]

	def sql(self, query, *args, **kwargs):
		key = _recognise(query)
		if key is None or args or set(kwargs) - {"as_dict"}:
			return self._real.sql(query, *args, **kwargs)
		recognised = key
		key = (*key, bool(kwargs.get("as_dict")))
		if key in self._memo:
			self._hits += 1
		else:
			self._misses += 1
			self._memo[key] = self._run(query, kwargs, recognised[0])
		return self._replay(self._memo[key], recognised)

	def optimus_cache_stats(self) -> tuple[int, int]:
		"""``(statements served from the cache, statements sent to the database)``; both
		count only the recognised optimizer statements."""
		return self._hits, self._misses

	def _run(self, query, kwargs, kind) -> tuple:
		"""Run the statement; an error comes back as an outcome only when it is worth remembering,
		otherwise it is raised and the next caller tries again."""
		try:
			return ("ok", self._real.sql(query, **kwargs))
		except Exception as error:
			if (
				isinstance(error, self._passthrough)
				or not _is_deterministic(error, kind)
				or not _can_rebuild(error)
			):
				raise
			return ("error", type(error), error.args)

	@staticmethod
	def _replay(outcome: tuple, recognised: tuple):
		if outcome[0] == "ok":
			return outcome[1]
		_, error_type, args = outcome
		error = error_type(*args)
		setattr(error, STATEMENT_ATTRIBUTE, recognised[0])
		if recognised[0] == "cardinality":
			setattr(error, COLUMN_ATTRIBUTE, recognised[1])
		raise error


def failed_statement(error: BaseException) -> str | None:
	"""Which of the four optimizer statements raised ``error`` (``"cardinality"`` and so on),
	when it came out of the cache; ``None`` for any other error."""
	return getattr(error, STATEMENT_ATTRIBUTE, None)


def failed_column(error: BaseException) -> str | None:
	"""The column of the cardinality statement that raised ``error``, when it came out of the cache."""
	return getattr(error, COLUMN_ATTRIBUTE, None)


def _is_deterministic(error: Exception, kind: str) -> bool:
	"""True when the same statement would fail the same way again (syntax, no such table or
	column, and a statement timeout of the cardinality count), as opposed to a lost connection,
	lock wait, deadlock or a timeout of a metadata read."""
	if not error.args or not isinstance(error.args[0], int):
		return False
	if error.args[0] == _TIMEOUT_ERROR:
		return kind == _TIMEOUT_REPEATS_FOR
	return error.args[0] in _DETERMINISTIC_ERRORS


def _can_rebuild(error: Exception) -> bool:
	"""True when a new exception of the same type can be made from the error's args."""
	try:
		type(error)(*error.args)
	except Exception:
		return False
	return True


@contextmanager
def cached_db(frappe_module, passthrough_errors: tuple = ()):
	"""Install the cache on ``frappe_module.local.db`` and yield whether it was installed.

	Yields ``False`` (and changes nothing) when there is no ``local.db`` to wrap."""
	local = getattr(frappe_module, "local", None)
	real = getattr(local, "db", None)
	if real is None or isinstance(real, CachingDb):
		yield False
		return
	proxy = CachingDb(real, passthrough_errors)
	local.db = proxy
	try:
		yield True
	finally:
		if local.db is proxy:
			local.db = real
