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

import functools
import hashlib
import re
import sys as _sys
import time

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


def _capture_caller_stack() -> list:
	"""Return ``{"filename","lineno","function"}`` dicts for the Python frames
	above this function (skipping the wrap itself), for redundant-call callsite
	attribution. Uses ``sys._getframe`` (fast, CPython-specific), depth-bounded at
	``_CALLER_STACK_MAX_DEPTH``. Returns [] on any failure rather than raising
	(observability code never breaks the host call).
	"""
	try:
		# Skip: _capture_caller_stack itself + the wrap function.
		frame = _sys._getframe(2)
	except (ValueError, AttributeError):
		return []

	stack: list = []
	try:
		while frame is not None and len(stack) < _CALLER_STACK_MAX_DEPTH:
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

# Patched at CLASS level because the recorder swaps db.sql per instance and may be off.
LOCK_EVENTS_CAP_PER_RECORDING = 200

_LOCK_STACK_TAIL_FRAMES = 10

# Frappe code that catches a lock error itself: (path suffix, function). Not dead: user_callsite
# picks the deepest user frame even when Frappe frames sit below it.
_FRAPPE_HANDLED_LOCK_SITES = (
	("frappe/sessions.py", "update_user_metadata"),  # savepoint(QueryDeadlockError)
	("frappe/utils/change_log.py", "update_last_known_versions"),  # suppress(...)
	("frappe/utils/dashboard.py", "generate_and_cache_results"),  # suppress(...)
)

_QUOTED_OR_COMMENT_RE = re.compile(
	r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.|\"\")*\"|`[^`]*`|/\*.*?\*/|--[^\n]*",
	re.DOTALL,
)

# NOWAIT probes fail fast by design (and surface as QueryTimeoutError on Postgres), so skip them.
_NOWAIT_RE = re.compile(r"\bnowait\b", re.IGNORECASE)

_PG_QUERY_CANCELED = "57014"  # Postgres SQLSTATE for a statement timeout


def _is_nowait_probe(query) -> bool:
	"""True for a ``... NOWAIT`` lock probe, judged on the raw text."""
	return bool(_NOWAIT_RE.search(_QUOTED_OR_COMMENT_RE.sub(" ", str(query or ""))))


def _is_statement_timeout(exc) -> bool:
	"""True when a QueryTimeoutError wraps a Postgres statement timeout (not a lock wait)."""
	cause = exc.__cause__ or next((a for a in exc.args if isinstance(a, BaseException)), None)
	return getattr(cause, "pgcode", None) == _PG_QUERY_CANCELED


def _lock_event_kind(exc) -> str | None:
	"""``"deadlock"`` / ``"lock_wait_timeout"`` for a Frappe lock error, else None."""
	# getattr: older Frappe and the unit-test stub may lack these classes.
	import frappe

	deadlock = getattr(frappe, "QueryDeadlockError", None)
	if deadlock is not None and isinstance(exc, deadlock):
		return "deadlock"
	timeout = getattr(frappe, "QueryTimeoutError", None)
	if timeout is not None and isinstance(exc, timeout) and not _is_statement_timeout(exc):
		return "lock_wait_timeout"
	return None


def _handled_by_frappe(frames) -> bool:
	"""True when a frame is one of Frappe's own lock-error handlers."""
	for frame in frames:
		filename = (frame.get("filename") or "").replace("\\", "/")
		function = frame.get("function")
		if any(filename.endswith(path) and function == fn for path, fn in _FRAPPE_HANDLED_LOCK_SITES):
			return True
	return False


def _normalize_sql_safe(query, keep_quoted_identifiers=False) -> str:
	"""``query`` with literals and comments replaced or dropped, or "" on any failure."""
	# Not frappe.recorder.normalize_query: on a parse failure it returns the raw query, leaking values.
	try:
		import sqlparse
		from sqlparse import tokens as T

		text = str(query) if query is not None else ""
		if not text:
			return ""
		parsed = sqlparse.parse(text)[0]
		for token in parsed.flatten():
			if token.ttype in T.Comment:
				token.value = ""
			elif token.ttype in T.Literal and not (
				keep_quoted_identifiers and token.ttype in T.String.Symbol
			):
				token.value = "?"
		return re.sub(r"( IN )\(\?[\s\n\?,]*\)", r"\1(?)", str(parsed), flags=re.IGNORECASE)
	except Exception:
		# Store nothing rather than risk raw literals.
		return ""


def _lock_caller_frames() -> list:
	"""App and Server Script frames above the failed query, outermost first."""
	try:
		from frappe.recorder import get_current_stack_frames

		return list(get_current_stack_frames())
	except Exception:
		return []


def job_retry_state(create: bool = True) -> dict | None:
	"""Per-RQ-job dict that outlives ``frappe.destroy()`` across Frappe's in-process retry."""
	# execute_job destroys frappe.local and re-runs on a deadlock; the RQ job object survives.
	# Relies on rq.Job accepting arbitrary attributes.
	try:
		from rq import get_current_job

		job = get_current_job()
		if job is None:
			return None
		state = getattr(job, "_optimus_retry_state", None)
		if state is None and create:
			state = job._optimus_retry_state = {}
		return state
	except Exception:
		return None


_lock_capture_warned = False
_installed_lock_wrap = None


def _warn_lock_capture_once(what: str, exc: BaseException) -> None:
	"""Log the first lock-capture failure of this process; later ones stay silent."""
	global _lock_capture_warned
	if _lock_capture_warned:
		return
	_lock_capture_warned = True
	try:
		import frappe

		frappe.logger("optimus").warning(
			f"optimus: lock capture {what} failed ({type(exc).__name__}); "
			"Lock Contention findings may be missing"
		)
	except Exception:
		pass


def _log_lock_skipped(reason: str) -> None:
	"""Leave a debug trace for a lock error that is deliberately not reported."""
	try:
		import frappe

		frappe.logger("optimus").debug(f"optimus: lock error not reported ({reason})")
	except Exception:
		pass


def _append_lock_event(
	local_proxy, kind, exc, query, duration_ms=None, keep_quoted_identifiers=False
) -> None:
	events = getattr(local_proxy, "optimus_lock_events", None)
	if events is not None and len(events) >= LOCK_EVENTS_CAP_PER_RECORDING:
		# Checked before the stack walk so a lock storm stays cheap.
		_mark_lock_events_truncated(local_proxy)
		return
	if _is_nowait_probe(query):
		_log_lock_skipped("NOWAIT probe")
		return
	frames = _lock_caller_frames()
	if _handled_by_frappe(frames):
		_log_lock_skipped("handled by Frappe")
		return

	from optimus.analyzers.base import installed_apps_allowlist, user_callsite
	from optimus.settings import get_config

	callsite = user_callsite(
		frames, tracked_apps=get_config().tracked_apps, installed_apps=installed_apps_allowlist()
	)
	if callsite is None:
		_log_lock_skipped("no callsite in tracked apps")
		return

	at = next(i for i, frame in enumerate(frames) if frame is callsite)
	if events is None:
		events = local_proxy.optimus_lock_events = []
		state = job_retry_state()
		if state is not None:
			# Shared list: a Frappe retry restores it after frappe.local is wiped.
			state["lock_events"] = events
	events.append({
		"kind": kind,
		"normalized_query": _normalize_sql_safe(query, keep_quoted_identifiers)[:2000],
		"error": (str(exc).splitlines() or [""])[0][:200],
		"caller_stack": [callsite, *frames[at + 1 :][-_LOCK_STACK_TAIL_FRAMES:]],
		"duration_ms": round(duration_ms, 2) if duration_ms is not None else None,
		"time": time.time(),
	})


def _mark_lock_events_truncated(local_proxy) -> None:
	local_proxy.optimus_lock_events_truncated = True
	state = job_retry_state(create=False)
	if state is not None:
		state["lock_events_truncated"] = True


def _record_lock_event(
	local_proxy, kind, exc, query, duration_ms=None, keep_quoted_identifiers=False
) -> None:
	"""Append one lock event to ``local_proxy.optimus_lock_events``; only an RQ timeout escapes."""
	from optimus.safe_call import InterruptGuard

	guard = InterruptGuard()
	failure = None
	try:
		with guard:
			_append_lock_event(local_proxy, kind, exc, query, duration_ms, keep_quoted_identifiers)
	except Exception as err:  # must never mask the DB exception the caller re-raises
		failure = err
	if guard.pending():
		raise guard.interrupt()
	if failure is not None:
		_warn_lock_capture_once("recording", failure)


def _record_in_wrap(db, local_proxy, exc, args, kwargs, start) -> None:
	"""Record ``exc`` if it is a lock error; only an RQ job timeout escapes."""
	from optimus.safe_call import InterruptGuard

	local_proxy._optimus_recording_lock = True
	guard = InterruptGuard()
	failure = None
	try:
		with guard:
			kind = _lock_event_kind(exc)
			if kind:
				_record_lock_event(
					local_proxy, kind, exc,
					args[0] if args else kwargs.get("query"),
					duration_ms=(time.monotonic() - start) * 1000.0,
					keep_quoted_identifiers=getattr(db, "db_type", None) == "postgres",
				)
	except Exception as err:  # must never mask the DB exception the caller re-raises
		failure = err
	finally:
		local_proxy._optimus_recording_lock = False
	if guard.pending():
		raise guard.interrupt()
	if failure is not None:
		_warn_lock_capture_once("wrapper", failure)


def _make_lock_wrap(orig, local_proxy):
	"""Wrap ``Database.sql`` to record lock errors, then re-raise them unchanged."""

	@functools.wraps(orig)
	def wrapped(self, *args, **kwargs):
		if not getattr(local_proxy, "optimus_session_id", None):
			return orig(self, *args, **kwargs)
		start = time.monotonic()
		try:
			return orig(self, *args, **kwargs)
		except Exception as exc:
			# The flag stops the settings read inside recording from re-entering.
			if not getattr(local_proxy, "_optimus_recording_lock", False):
				_record_in_wrap(self, local_proxy, exc, args, kwargs, start)
			raise

	wrapped._optimus_lock_wrap = True
	wrapped._optimus_original = orig
	return wrapped


def _database_class():
	"""``frappe.database.database.Database``, or None where it is unavailable."""
	try:
		from frappe.database.database import Database
	except ImportError:
		return None
	return Database


def install_lock_capture(local_proxy) -> None:
	"""Patch ``Database.sql`` once to record lock errors (idempotent; no-op without Database)."""
	database = _database_class()
	if database is None or getattr(database.sql, "_optimus_lock_wrap", False):
		return
	global _installed_lock_wrap
	try:
		database.sql = _installed_lock_wrap = _make_lock_wrap(database.sql, local_proxy)
	except Exception as exc:
		_warn_lock_capture_once("install", exc)  # no lock capture beats a broken app load


def uninstall_lock_capture() -> None:
	"""Restore the original ``Database.sql``."""
	database = _database_class()
	# Only our own wrapper is unwrapped; a later wrapper stacked on top is left alone.
	if database is not None and database.sql is _installed_lock_wrap:
		database.sql = _installed_lock_wrap._optimus_original


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

	install_lock_capture(frappe.local)


def uninstall_wraps():
	"""Restore originals. Used by before_uninstall and tests."""
	uninstall_lock_capture()
	for module, attr, _fn_name in _wrap_targets():
		current = getattr(module, attr)
		if getattr(current, "_profiler_is_our_wrap", False):
			setattr(module, attr, current._profiler_original)
