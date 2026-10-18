# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Capture module: pyinstrument integration and sidecar wraps.

Owns:
  1. Optional pyinstrument import (degrades gracefully if unavailable)
  2. Per-recording pyinstrument session start/stop helpers
  3. Three monkey-patched sidecar wraps on frappe.get_doc /
     frappe.cache.get_value / frappe.permissions.has_permission for
     argument capture (with PII-safe hashing)

Argument values that may contain user data are stored as ``identifier_raw``
(rendered in the report) and ``identifier_safe`` (a sha256[:12] hash, the bucket
key for redundant-call detection). Doctype names and ptypes are NOT hashed.

Activation gate: the wraps and pyinstrument start are gated on
``frappe.local._profiler_active_session_id`` (set by before_request/before_job
only when the session has ``capture_python_tree=True``), so the wraps' hot-path
check is a single attribute lookup and never reads Redis.
"""

import hashlib
import re
import sys as _sys
import time

# A ``FOR UPDATE ... NOWAIT`` / ``LOCK ... NOWAIT`` probe is a DELIBERATE
# non-blocking lock check (Frappe uses them, e.g. the link-check in delete_doc):
# failing fast is the designed path, not contention to report. On Postgres such a
# failure surfaces as QueryTimeoutError, so without this it would become a false
# "lock-wait timeout" finding. Word-boundary match so a column named ``nowait_x``
# doesn't trip it.
_NOWAIT_RE = re.compile(r"\bnowait\b", re.IGNORECASE)

# Optional dependency capture degrades gracefully if pyinstrument is
# not installed (e.g. air-gapped environments, broken pip cache).
try:
	import pyinstrument  # noqa: F401

	_PYINSTRUMENT_AVAILABLE = True
except ImportError:
	_PYINSTRUMENT_AVAILABLE = False


# v0.5.2: how many caller frames to capture for redundant-call attribution.
# Deep enough to walk past framework dispatchers (Document.save →
# run_method → composer → runner → fn → user's validate → wrapped call);
# shallow enough to keep overhead trivial (~1 µs per frame).
_CALLER_STACK_MAX_DEPTH = 20


def _capture_caller_stack(max_depth: int | None = _CALLER_STACK_MAX_DEPTH) -> list:
	"""Return ``{"filename","lineno","function"}`` dicts for the Python frames
	above this function (skipping the wrap itself), for callsite attribution. Uses
	``sys._getframe`` (fast, CPython-specific). Returns [] on any failure rather
	than raising (observability code never breaks the host call).

	``max_depth`` bounds the walk. The hot redundant-call path keeps the default
	20-frame cap to stay cheap; the lock-capture path (only ever walked on the rare
	deadlock / timeout EXCEPTION, never on the happy path) passes ``None`` for an
	unbounded walk, so a deep ORM / hook chain can't push the user frame past the
	cap and get the real contention dropped or misattributed to a framework frame.
	"""
	try:
		# Skip: _capture_caller_stack itself + the wrap function.
		frame = _sys._getframe(2)
	except (ValueError, AttributeError):
		return []

	stack: list = []
	try:
		while frame is not None and (max_depth is None or len(stack) < max_depth):
			code = frame.f_code
			stack.append({
				"filename": code.co_filename,
				"lineno": frame.f_lineno,
				"function": code.co_name,
			})
			frame = frame.f_back
	except Exception:
		# Any unexpected walk failure → return what we collected so far.
		pass
	return stack


def _hash_identifier(value) -> str:
	"""Return a deterministic 12-char sha256 hex prefix of `value`.

	None passes through as None (used by has_permission when name is omitted).
	"""
	if value is None:
		return None
	return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _identify_args(fn_name: str, args: tuple, kwargs: dict):
	"""Build (identifier_raw, identifier_safe) for one captured call.

	Sub-shapes per wrapped function:
	  - get_doc("DocType", "name")     → ((doctype, name), (doctype, hash(name)))
	  - get_doc({"doctype": ..., ...}) → from dict; name may be missing on unsaved
	    docs ("__islocal"=1), leaving just doctype
	  - cache_get(self, key)           → (key, hash(key))
	  - has_permission(doctype, ptype, doc) → ((doctype, name, ptype),
	    (doctype, hash(name), ptype)), name extracted from doc

	Both returns are guaranteed hashable (strings, Nones or tuples of those) for
	use as dict keys in redundant_calls bucketing.
	"""
	if fn_name == "get_doc":
		first = args[0] if len(args) > 0 else kwargs.get("doctype")
		# Dict-arg form: frappe.get_doc({"doctype": "X", "name": "Y", ...})
		if isinstance(first, dict):
			doctype = first.get("doctype")
			name = first.get("name") if not first.get("__islocal") else None
		else:
			doctype = first
			name = args[1] if len(args) > 1 else kwargs.get("name")
		# Coerce to strings/None so the result is always hashable
		doctype = str(doctype) if doctype is not None else None
		name = str(name) if name is not None else None
		return (doctype, name), (doctype, _hash_identifier(name))

	if fn_name == "cache_get":
		# This wraps RedisWrapper.get_value (a method), so args[0] is the
		# RedisWrapper instance (self) and args[1] is the actual key. We
		# wrap at the class level not at frappe.cache because
		# frappe.cache is None at app-import time (no site bound yet).
		key = args[1] if len(args) > 1 else kwargs.get("key")
		# Cache keys may be bytes (Frappe sometimes builds them with the
		# site prefix as bytes); coerce to str for hashability + display.
		if isinstance(key, bytes):
			key = key.decode("utf-8", errors="replace")
		key = str(key) if key is not None else None
		return key, _hash_identifier(key)

	if fn_name == "has_permission":
		# Frappe signature: has_permission(doctype, ptype="read", doc=None, ...)
		# args[0]=doctype, args[1]=ptype, args[2]=doc.
		doctype = args[0] if len(args) > 0 else kwargs.get("doctype")
		ptype = args[1] if len(args) > 1 else kwargs.get("ptype", "read")
		doc = args[2] if len(args) > 2 else kwargs.get("doc")
		# Extract a stable identifier from doc, which may be a Document,
		# a dict, or a string name.
		if doc is None:
			name = None
		elif hasattr(doc, "name"):
			name = getattr(doc, "name", None)
		elif isinstance(doc, dict):
			name = doc.get("name")
		else:
			name = doc
		# Coerce all components to hashable types
		doctype = str(doctype) if doctype is not None else None
		ptype = str(ptype) if ptype is not None else None
		name = str(name) if name is not None else None
		return (doctype, name, ptype), (doctype, _hash_identifier(name), ptype)

	# Unknown return None tuples so the bucket key is hashable but
	# meaningless (the redundant_calls analyzer skips such entries).
	return (None, None), (None, None)


# Maximum entries per recording's sidecar list. Above this, additional
# wraps drop their entries silently and set a truncation flag on the
# request-local context. The analyze pipeline surfaces this as a warning.
# Phase K hardening: lowered from 50_000 to 10_000. At ~500B per
# sidecar entry, 50K entries = ~25MB per recording, which compounds
# on busy multi-recording sessions. 10K still captures the dedup
# signal for 99%+ of real workloads (the most-repeated call patterns
# saturate within the first few thousand entries); rare deep-loop
# pathologies that need >10K entries get a truncation banner and a
# clear hint to refactor the loop.
SIDECAR_CAP_PER_RECORDING = 10_000


def _make_wrap(orig, fn_name: str, local_proxy=None):
	"""Build a sidecar-recording wrapper around ``orig``.

	``local_proxy`` is the request-local namespace holding the activation flag and
	entries (``frappe.local`` in production; a stand-in in tests).

	Properties:
	  - Passthrough when no active session (single attribute lookup).
	  - Records entries on success AND on exception (try/finally).
	  - A re-entrant call from inside one wrap is a passthrough (prevents
	    double-counting ``has_permission`` → ``get_doc``).
	  - Drops entries past SIDECAR_CAP_PER_RECORDING and flags truncation.
	  - Stores the original on ``wrapped._profiler_original`` for uninstall; never
	    double-wraps an already-wrapped ``orig``.
	"""
	def wrapped(*args, **kwargs):
		active = getattr(local_proxy, "_profiler_active_session_id", None)
		if not active:
			return orig(*args, **kwargs)

		in_wrap = getattr(local_proxy, "_profiler_in_wrap", False)
		if in_wrap:
			return orig(*args, **kwargs)

		# Set re-entrancy flag BEFORE doing any work so nested wrapped
		# calls (e.g. has_permission → get_doc) skip recording.
		local_proxy._profiler_in_wrap = True

		# Build the sidecar entry on a best-effort basis. A failure here
		# (malformed args, exotic types) MUST NOT prevent the user's call
		# from running observability code never breaks the host call.
		try:
			identifier_raw, identifier_safe = _identify_args(fn_name, args, kwargs)
			entry = {
				"fn_name": fn_name,
				"identifier_raw": identifier_raw,
				"identifier_safe": identifier_safe,
				# v0.5.2: caller stack lets redundant_calls.analyze
				# find the user frame (vs filtering out framework-
				# only callsites) AND surface file:line in the
				# finding detail so users can actually navigate to
				# the loop they need to fix. Pre-v0.5.2 findings
				# showed only a hashed cache key with no callsite
				# users couldn't act on them.
				"caller_stack": _capture_caller_stack(),
			}
		except Exception:
			entry = None

		try:
			return orig(*args, **kwargs)
		finally:
			local_proxy._profiler_in_wrap = False
			if entry is not None:
				sidecar = getattr(local_proxy, "optimus_sidecar", None)
				if sidecar is None:
					local_proxy.optimus_sidecar = [entry]
				elif len(sidecar) >= SIDECAR_CAP_PER_RECORDING:
					local_proxy.optimus_sidecar_truncated = True
				else:
					sidecar.append(entry)

	wrapped._profiler_original = orig
	return wrapped


# ----- DB lock-contention capture (deadlocks / lock-wait timeouts) ---------
#
# The Frappe recorder only logs queries that SUCCEED (``record_sql`` records
# after ``frappe.db._sql`` returns). A query that raises a deadlock
# (``frappe.QueryDeadlockError``) or lock-wait timeout
# (``frappe.QueryTimeoutError``) is never recorded. ``wrap_db_sql_for_lock_capture``
# layers a thin interceptor on top of the (already recorder-patched)
# ``frappe.local.db.sql`` for a profiled request/job: it records those two
# exception types to a request-local list and RE-RAISES them unchanged, so
# Frappe's own savepoint / suppress / retry behaviour is untouched. The wrap
# lives on the request-scoped DB instance, so it tears down automatically when
# the request ends (no cross-request patch leak).

# Deadlocks are rare; a request that retries one can hit a handful. The cap keeps
# a pathological retry storm from growing the list unbounded.
LOCK_EVENTS_CAP_PER_RECORDING = 200

# Same path trim as frappe.recorder, so FRAMEWORK_PREFIXES matches app-relative paths.
_APPS_PATH_PATTERN = re.compile(r".*/apps/")
_SERVER_SCRIPT_FILE_PREFIX = "<serverscript>"


def _app_frames(stack) -> list:
	"""Keep app and Server Script frames with app-relative filenames, like the recorder."""
	frames = []
	for frame in stack or []:
		filename = (frame.get("filename") or "").replace("\\", "/")
		if "/apps/" in filename:
			frames.append({**frame, "filename": _APPS_PATH_PATTERN.sub("", filename)})
		elif filename.startswith(_SERVER_SCRIPT_FILE_PREFIX):
			frames.append(dict(frame))
	return frames


def _has_user_frame(frames) -> bool:
	from optimus.analyzers.base import FRAMEWORK_PREFIXES

	return any(
		not any(prefix in (f.get("filename") or "") for prefix in FRAMEWORK_PREFIXES)
		for f in frames
	)


def _lock_event_kind(exc) -> str | None:
	"""``"deadlock"`` / ``"lock_wait_timeout"`` for a Frappe lock error, else None.

	The single source of truth for whether an exception is a lock error and
	which kind it is.
	Resolved via ``getattr`` so it stays safe when the attributes are absent
	(older Frappe, or the unit-test frappe stub without them)."""
	import frappe

	deadlock = getattr(frappe, "QueryDeadlockError", None)
	if deadlock is not None and isinstance(exc, deadlock):
		return "deadlock"
	timeout = getattr(frappe, "QueryTimeoutError", None)
	if timeout is not None and isinstance(exc, timeout):
		return "lock_wait_timeout"
	return None


def _normalize_sql_safe(query) -> str:
	"""Return ``query`` with literals replaced by ``?``, or "" on ANY failure.

	NOT ``frappe.recorder.normalize_query``: that one catches its own sqlparse
	errors internally and returns the RAW query (literals intact), so a caller's
	try/except never sees the failure (see recorder.py). Persisting that raw query
	would leak row data into the report, breaking the "no row data" guarantee, so
	we run the same sqlparse normalization here and return "" on failure instead
	of the raw text. ``query`` is str()-coerced first, since db.sql accepts non-str
	query objects (query builders) that would otherwise be dropped to ""."""
	try:
		import re as _re

		import sqlparse

		text = str(query) if query is not None else ""
		if not text:
			return ""
		parsed = sqlparse.parse(text)[0]
		for token in parsed.flatten():
			ttype = str(token.ttype)
			if "Token.Literal" in ttype:
				token.value = "?"
			elif "Comment" in ttype:
				# Comments can carry row data and are NOT Token.Literal, so frappe's
				# normalize_query leaves them intact. Drop them here so nothing leaks.
				token.value = ""
		return _re.sub(r"( IN )\(\?[\s\n\?,]*\)", r"\1(?)", str(parsed), flags=_re.IGNORECASE)
	except Exception:
		# Unparseable / exotic query: store nothing rather than risk raw literals.
		return ""


def _record_lock_event(local_proxy, kind, exc, query, caller_stack, duration_ms=None) -> None:
	"""Append one lock-contention event to ``local_proxy.optimus_lock_events``.

	Best-effort: any failure here is swallowed so it can never mask or alter the
	original DB exception the caller is about to re-raise. The query is stored
	NORMALIZED via ``_normalize_sql_safe`` (literals + comments stripped, "" on any
	parse failure) so no row data is persisted even for an unparseable query. Only
	app frames are kept (``_app_frames``), reversed to outermost->innermost for
	``walk_callsite``; an event with no user frame is skipped so it never uses up
	the cap. ``duration_ms`` is the wall-time the failed query spent before raising
	(a lock-wait timeout can burn the full innodb_lock_wait_timeout, ~50s), surfaced
	so the cost is visible. A ``NOWAIT`` probe is skipped: its failure is the
	caller's designed fast-fail path, not contention worth a finding."""
	try:
		normalized = _normalize_sql_safe(query)
		# Skip a deliberate NOWAIT lock probe (fail-fast by design, not contention).
		# Checked on the NORMALIZED text, where literals are already stripped to
		# ``?``, so a row value that happens to be 'nowait' can't trigger a false
		# skip only the SQL keyword survives normalization.
		if _NOWAIT_RE.search(normalized):
			return
		events = getattr(local_proxy, "optimus_lock_events", None)
		if events is None:
			events = []
			local_proxy.optimus_lock_events = events
		frames = _app_frames(caller_stack)
		if not _has_user_frame(frames):
			return
		if len(events) >= LOCK_EVENTS_CAP_PER_RECORDING:
			local_proxy.optimus_lock_events_truncated = True
			return
		events.append({
			"kind": kind,
			"normalized_query": normalized[:2000],
			# Generic server error text (e.g. "Deadlock found when trying to get
			# lock; try restarting transaction") carries no row data.
			"error": str(exc)[:200],
			"caller_stack": list(reversed(frames)),
			"duration_ms": round(duration_ms, 2) if duration_ms is not None else None,
			"time": time.time(),
		})
	except Exception:
		pass


def wrap_db_sql_for_lock_capture(local_proxy) -> None:
	"""Wrap the request-scoped DB's low-level ``_sql`` to record deadlocks and
	lock-wait timeouts.

	We wrap ``db._sql`` (the underlying executor), NOT ``db.sql``. Frappe's
	recorder patches ``db.sql`` to ``record_sql`` and, in ``do_not_record`` /
	``record_queries`` / teardown, UNPATCHES it with ``db.sql = db._sql`` and may
	re-patch it later. A wrapper living on ``db.sql`` would be silently dropped by
	the first such swap and never restored, so lock capture would quietly stop for
	the rest of the request. ``record_sql`` always delegates to ``db._sql`` and the
	unpatch restores ``db.sql`` TO ``db._sql``, so wrapping ``_sql`` keeps the
	interceptor in the path across every patch / unpatch cycle.

	Call from before_request / before_job. Idempotent per request; a no-op when
	there's no DB or no ``_sql``. Teardown is automatic: the wrap is on the
	per-request DB instance, which Frappe discards when the request ends."""
	db = getattr(local_proxy, "db", None)
	if db is None:
		return
	current = getattr(db, "_sql", None)
	if current is None or getattr(current, "_optimus_lock_wrap", False):
		return

	def wrapped(*args, **kwargs):
		start = time.monotonic()
		try:
			return current(*args, **kwargs)
		except Exception as exc:
			# Detection, query extraction and stack capture are ALL guarded so
			# nothing here can raise and displace the original DB exception: the
			# bare `raise` below always re-raises `exc` unchanged, keeping
			# Frappe's own `except QueryDeadlockError` retry/suppress matching.
			try:
				kind = _lock_event_kind(exc)
				if kind:
					query = args[0] if args else kwargs.get("query")
					# Unbounded stack walk: this only runs on the rare lock
					# EXCEPTION, so cost is irrelevant and a deep ORM / hook chain
					# must not push the user frame past the cap (issue: real
					# contention dropped / misattributed).
					_record_lock_event(
						local_proxy, kind, exc, query,
						_capture_caller_stack(max_depth=None),
						duration_ms=(time.monotonic() - start) * 1000.0,
					)
			except Exception:
				pass
			raise

	wrapped._optimus_lock_wrap = True
	try:
		db._sql = wrapped
	except Exception:
		# Some DB wrappers may disallow attribute assignment degrade to no
		# lock capture rather than break the request.
		pass


# Default pyinstrument sample interval in milliseconds. Overridable via
# site_config.json: optimus_sampler_interval_ms. 1ms is pyinstrument's
# default and balances fidelity vs overhead well.
DEFAULT_SAMPLER_INTERVAL_MS = 1


def _start_pyi_session(local_proxy, interval_ms: float = DEFAULT_SAMPLER_INTERVAL_MS):
	"""Start a pyinstrument profiler scoped to this request.

	Stores it on ``local_proxy.optimus_pyinstrument`` so ``after_request`` /
	``after_job`` can stop and serialize it. Returns the profiler, or None if
	pyinstrument is unavailable.
	"""
	if not _PYINSTRUMENT_AVAILABLE:
		return None
	try:
		from pyinstrument import Profiler

		# pyinstrument expects interval in seconds (float)
		prof = Profiler(interval=interval_ms / 1000.0, async_mode="enabled")
		prof.start()
		local_proxy.optimus_pyinstrument = prof
		return prof
	except Exception:
		# Any failure to start pyinstrument is non-fatal degrade to
		# SQL-only capture for this recording.
		return None


def _force_stop_inflight_capture(local_proxy):
	"""Stop any in-flight pyinstrument session and clear all capture state.

	Called before flipping the active flag so a previous in-flight capture from the
	same worker doesn't leak into the new session.
	"""
	prof = getattr(local_proxy, "optimus_pyinstrument", None)
	if prof is not None:
		try:
			prof.stop()
		except Exception:
			pass
		try:
			delattr(local_proxy, "optimus_pyinstrument")
		except AttributeError:
			pass

	for attr in (
		"_profiler_active_session_id",
		"optimus_sidecar",
		"optimus_sidecar_truncated",
		"optimus_lock_events",
		"optimus_lock_events_truncated",
		"_profiler_in_wrap",
	):
		try:
			delattr(local_proxy, attr)
		except AttributeError:
			pass


# ----- Wrap installation on the real frappe modules -----------------------
#
# Installed once at app import time from optimus/__init__.py.
# install_wraps() is idempotent: calling it twice does not double-wrap,
# and pre-existing wraps from other apps are detected via the
# _profiler_original attribute convention.


def _wrap_targets():
	"""Return the (module, attr_name, fn_name) tuples to wrap.

	Lazy so importing capture.py doesn't import frappe.permissions or
	frappe.utils.redis_wrapper (circular-import risk at app load).

	Wraps ``RedisWrapper.get_value`` (the class method), NOT ``frappe.cache
	.get_value``, because ``frappe.cache`` is None at app-import time. Being a
	method wrap, the wrapper sees ``self`` as args[0] and the key as args[1]
	(handled in ``_identify_args`` for ``cache_get``).
	"""
	import frappe
	import frappe.permissions
	import frappe.utils.redis_wrapper

	return [
		(frappe, "get_doc", "get_doc"),
		(frappe.utils.redis_wrapper.RedisWrapper, "get_value", "cache_get"),
		(frappe.permissions, "has_permission", "has_permission"),
	]


def install_wraps():
	"""Install all three sidecar wraps. Idempotent.

	If `frappe.get_doc` is already a `_profiler_is_our_wrap`-tagged wrapper,
	we do not double-wrap.
	"""
	import frappe

	for module, attr, fn_name in _wrap_targets():
		current = getattr(module, attr)
		if getattr(current, "_profiler_is_our_wrap", False):
			continue  # already wrapped by us
		new_wrap = _make_wrap(current, fn_name, local_proxy=frappe.local)
		new_wrap._profiler_is_our_wrap = True
		setattr(module, attr, new_wrap)


def uninstall_wraps():
	"""Restore originals. Used by before_uninstall and tests."""
	for module, attr, _fn_name in _wrap_targets():
		current = getattr(module, attr)
		if getattr(current, "_profiler_is_our_wrap", False):
			setattr(module, attr, current._profiler_original)
