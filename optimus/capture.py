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
import sys as _sys

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
