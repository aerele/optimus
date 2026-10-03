# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Phase-2 line-profiler capture core.

Two layers:
  * Pure: ``aggregate_samples(samples, picks)`` merges per-request
    line_profiler stats into the analyzer's input shape.
  * Impure: ``start_line_profile_pass`` / ``stop_line_profile_pass`` own the
    Redis-backed run lifecycle; ``is_active`` is the hot-path predicate for
    hooks; ``make_profiler`` / ``serialize_stats`` / ``flush_samples`` form
    the per-request enable/disable cycle; ``_get_or_resolve_picks`` caches
    resolved function objects per worker.

The ``frappe`` and ``line_profiler`` imports are guarded so the module loads
under standalone pytest even when neither is installed; calling an impure
function without its dependency raises ``RuntimeError``.
"""

import importlib
import inspect
import json
import pickle
import sys
import threading
from dataclasses import dataclass
from functools import wraps

from optimus import redis_keys as _redis_keys
from optimus.line_profile import diff

# ---------------------------------------------------------------------------
# Optional dependencies guarded so the pure layer loads everywhere
# ---------------------------------------------------------------------------

try:
	import frappe  # type: ignore[import-not-found]
	_FRAPPE_AVAILABLE = True
except ImportError:
	frappe = None  # type: ignore[assignment]
	_FRAPPE_AVAILABLE = False

try:
	from frappe import _
except ImportError:
	def _(message):
		return message


try:
	from line_profiler import LineProfiler  # type: ignore[import-not-found]
	_LP_AVAILABLE = True
except ImportError:
	LineProfiler = None  # type: ignore[assignment,misc]
	_LP_AVAILABLE = False


def is_line_profiler_available() -> bool:
	"""Form UI calls this to decide whether to enable the Run button."""
	return _LP_AVAILABLE


def _require_frappe() -> None:
	if not _FRAPPE_AVAILABLE:
		raise RuntimeError(
			"frappe must be importable for this operation run under bench."
		)


def _require_line_profiler() -> None:
	if not _LP_AVAILABLE:
		raise RuntimeError(
			"line_profiler is not installed run "
			"`bench pip install line_profiler` to enable phase 2."
		)


# ---------------------------------------------------------------------------
# Redis key shapes (mirroring the phase-1 conventions in session.py)
# ---------------------------------------------------------------------------

# v0.12.20: the per-user active flag + per-run picks/source/samples/
# budget_hit keys are now built via ``optimus.redis_keys`` (the v0.12.0
# centralized source-of-truth). The local ``_active_key`` /
# ``_picks_key`` / ``_source_key`` / ``_samples_key`` /
# ``_budget_hit_key`` helpers below have been retired call sites use
# ``_redis_keys.lp_active(user)`` etc. directly. Key strings are
# byte-identical to the pre-v0.12.20 local helpers, so on-disk Redis
# values from older bench versions resolve unchanged.

SESSION_TTL_SECONDS = 10 * 60  # match phase-1's session TTL
DATA_TTL_SECONDS = 24 * 60 * 60  # orphaned inputs must not survive indefinitely


# ---------------------------------------------------------------------------
# Worker-resident caches
# ---------------------------------------------------------------------------

# After the first request in a worker resolves picks for a run, subsequent
# requests reuse the resolved function objects. Cleared by stop_line_profile_pass.
_resolved_fns_by_run: dict[str, list] = {}


# ---------------------------------------------------------------------------
# Pick resolution helper (lighter than picker.resolve_freeform just
# returns the function object, used by the worker cache)
# ---------------------------------------------------------------------------


def _interrupt_boundary(fn):
	"""Discard interrupted capture frames while preserving the worker's stop signal."""
	@wraps(fn)
	def guarded(*args, **kwargs):
		from optimus.ai_fix import _InterruptGuard
		guard = _InterruptGuard(base=True)
		with guard:
			return fn(*args, **kwargs)
		args = kwargs = None
		raise guard.interrupt()
	return guarded


def _raise_if_interrupt(exc):
	"""Pass a timeout through a best-effort catch to its outer interrupt boundary."""
	from optimus.ai_fix import _job_timeout_types
	if isinstance(exc, _job_timeout_types()):
		raise exc


@_interrupt_boundary
def _resolve_attr(dotted_path: str):
	"""Resolve a dotted path to its underlying function object.

	Mirrors ``picker.resolve_freeform`` but returns just the callable. None
	on any resolution failure caller decides the surfacing.
	"""
	from optimus.renderer.source import _installed_apps

	if not isinstance(dotted_path, str) or len(dotted_path) > 500 or not all(p.isidentifier() for p in dotted_path.split(".")):
		return None
	may_import = dotted_path.split(".", 1)[0] in _installed_apps()
	parts = dotted_path.split(".")
	module = None
	module_parts = 0
	for i in range(len(parts), 0, -1):
		try:
			module = importlib.import_module(".".join(parts[:i])) if may_import else sys.modules.get(".".join(parts[:i]))
			if module is None:
				continue
			module_parts = i
			break
		except (ImportError, TypeError, ValueError):
			# Mirror picker._resolve_freeform_exact: a malformed prefix
			# a relative "...pkg" name (TypeError) or an empty name
			# (ValueError) is just "not importable". Honour this
			# function's documented "None on any resolution failure"
			# contract instead of letting it escape as a 500.
			continue
	if module is None:
		return None
	obj = module
	for attr in parts[module_parts:]:
		try:
			obj = getattr(obj, attr)
		except AttributeError:
			return None
	return obj


@_interrupt_boundary
def _capture_source_lines(fn) -> list[dict]:
	"""Snapshot only bounded, permitted on-disk source for the selected callable."""
	from optimus.renderer import source

	try:
		code = getattr(inspect.unwrap(fn), "__code__", None)
		if code is None:
			return []
		lines = source._source_lines(code.co_filename)
		if not lines:
			return []
		first_lineno = code.co_firstlineno
		source_lines = inspect.getblock(lines[first_lineno - 1:])
	except (OSError, TypeError, ValueError):
		return []
	return [{"lineno": first_lineno + i, "content": line.rstrip("\n")} for i, line in enumerate(source_lines)]


class CaptureInputError(ValueError):
	"""Fixed diagnostic for missing, corrupt or excessive capture input."""


MAX_CAPTURE_BYTES = 16 * 1024 * 1024
MAX_SAMPLE_BATCHES = 10_000
MAX_SAMPLE_RECORDS = 250_000
MAX_SOURCE_LINES = 100_000


def _bounded_text(value, limit):
	return isinstance(value, str) and bool(value) and len(value) <= limit and "\x00" not in value


def _uint(value, *, positive=False):
	return type(value) is int and (1 if positive else 0) <= value <= 2**63 - 1


def _decode_capture_json(raw):
	from optimus.ai_fix import _InterruptGuard
	from optimus.recording_bundle import _constant, _pairs

	if not isinstance(raw, (str, bytes)) or not raw or len(raw) > MAX_CAPTURE_BYTES:
		raise CaptureInputError("Phase 2 capture input is missing or exceeds its size limit")
	guard = _InterruptGuard(base=True)
	invalid = False
	try:
		with guard:
			if isinstance(raw, str) and len(raw.encode("utf-8")) > MAX_CAPTURE_BYTES:
				invalid = True
			else:
				return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
	except (ValueError, TypeError, RecursionError):
		invalid = True
	if guard.pending():
		raw = None
		raise guard.interrupt()
	if invalid:
		raw = None
		raise CaptureInputError("Phase 2 capture input is malformed or exceeds its size limit") from None


def _validate_captured_picks(picks):
	if not isinstance(picks, list) or not 1 <= len(picks) <= MAX_PICKS:
		raise CaptureInputError("Phase 2 captured picks are missing or malformed")
	seen, total_lines = set(), 0
	for pick in picks:
		if (not isinstance(pick, dict) or not _bounded_text(pick.get("dotted_path"), 500)
			or not _bounded_text(pick.get("qualname"), 500) or not _bounded_text(pick.get("file"), 4096)
			or not _uint(pick.get("first_lineno"), positive=True)):
			raise CaptureInputError("Phase 2 captured function metadata is malformed")
		if pick["dotted_path"] in seen:
			raise CaptureInputError("Phase 2 captured functions are duplicated")
		seen.add(pick["dotted_path"])
		lines = pick.get("source_lines")
		if not isinstance(lines, list) or not lines:
			raise CaptureInputError("Phase 2 source snapshot is missing")
		total_lines += len(lines)
		if total_lines > MAX_SOURCE_LINES:
			raise CaptureInputError("Phase 2 source snapshot exceeds its line limit")
		previous = 0
		for line in lines:
			if (not isinstance(line, dict) or not _uint(line.get("lineno"), positive=True)
				or line["lineno"] <= previous or not isinstance(line.get("content"), str)
				or len(line["content"]) > 20_000):
				raise CaptureInputError("Phase 2 source snapshot is malformed")
			previous = line["lineno"]


def _validate_samples(samples):
	if not isinstance(samples, list) or len(samples) > MAX_SAMPLE_BATCHES:
		raise CaptureInputError("Phase 2 sample batches are malformed or excessive")
	count = 0
	for batch in samples:
		if not isinstance(batch, list):
			raise CaptureInputError("Phase 2 sample batch is malformed")
		count += len(batch)
		if count > MAX_SAMPLE_RECORDS:
			raise CaptureInputError("Phase 2 samples exceed their record limit")
		for record in batch:
			if (not isinstance(record, dict) or not _bounded_text(record.get("file"), 4096)
				or not _bounded_text(record.get("qualname"), 500) or not _uint(record.get("lineno"), positive=True)
				or not _uint(record.get("hits")) or not _uint(record.get("total_us"))):
				raise CaptureInputError("Phase 2 sample record is malformed")


def aggregate_samples(samples: list[list[dict]], picks: list[dict]) -> list[dict]:
	"""Merge per-request line_profiler samples into the analyzer's input shape.

	``samples`` is a list of per-request batches, each a list of line records
	``{file, qualname, lineno, hits, total_us}``. ``picks`` is one entry per
	picked function with source captured at start time
	``{dotted_path, qualname, file, first_lineno, source_lines: [{lineno, content}]}``.

	Returns the analyzer's ``results_json`` shape (one entry per pick) with
	per-line ``hits``, ``total_ms``, ``per_hit_us`` and ``content_hash``.
	Samples that match no pick are silently dropped, as are lines no longer in
	the pick's captured source (the start-time source is authoritative).
	"""
	_validate_captured_picks(picks)
	_validate_samples(samples)
	# Build a lookup: (file, qualname, lineno) → cumulative {hits, total_us}
	totals: dict[tuple[str, str, int], dict] = {}
	for batch in samples:
		for record in batch:
			key = (record["file"], record["qualname"], int(record["lineno"]))
			entry = totals.get(key)
			if entry is None:
				totals[key] = {
					"hits": int(record.get("hits") or 0),
					"total_us": int(record.get("total_us") or 0),
				}
			else:
				entry["hits"] += int(record.get("hits") or 0)
				entry["total_us"] += int(record.get("total_us") or 0)

	results = []
	for pick in picks:
		file_path = pick["file"]
		qualname = pick["qualname"]
		lines_out = []
		for src in pick.get("source_lines", []):
			lineno = src["lineno"]
			content = src["content"]
			merged = totals.get((file_path, qualname, lineno))
			hits = merged["hits"] if merged else 0
			total_us = merged["total_us"] if merged else 0
			total_ms = total_us / 1000.0
			per_hit_us = round(total_us / hits, 2) if hits else 0.0
			lines_out.append({
				"lineno": lineno,
				"content": content,
				"content_hash": diff.content_hash(content),
				"hits": hits,
				"total_ms": round(total_ms, 4),
				"per_hit_us": per_hit_us,
			})
		results.append({
			"dotted_path": pick["dotted_path"],
			"qualname": qualname,
			"file": file_path,
			"lines": lines_out,
		})
	return results


# ---------------------------------------------------------------------------
# Lifecycle (impure frappe + Redis required)
# ---------------------------------------------------------------------------


class CaptureError(Exception):
	"""Raised when start/stop validation fails for reasons the API surface
	should communicate to the customer (e.g. all picks ineligible)."""


MAX_PICKS = 100


@dataclass(frozen=True)
class PreparedCapture:
	resolved: tuple[dict, ...]
	picks_json: str
	source_json: str


def validate_picks(picks):
	if not isinstance(picks, list) or not 1 <= len(picks) <= MAX_PICKS:
		raise CaptureError(_("Select between 1 and 100 functions to line-profile."))
	for entry in picks:
		dotted = entry.get("dotted_path") if isinstance(entry, dict) else None
		if (not isinstance(dotted, str) or not 1 <= len(dotted) <= 500 or "." not in dotted
			or not all(part.isidentifier() for part in dotted.split("."))):
			raise CaptureError(_("Each selected function needs a valid dotted path."))
		source = entry.get("source", "freeform")
		if not isinstance(source, str) or source not in {"freeform", "curated", "auto_expand"}:
			raise CaptureError(_("Each selected function needs a known source label."))


@_interrupt_boundary
def prepare_line_profile_picks(picks: list[dict]) -> PreparedCapture:
	"""Resolve and snapshot before acquiring SQL admission locks."""
	_require_line_profiler()
	from optimus.line_profile import picker

	validate_picks(picks)
	from optimus.renderer.source import _installed_apps
	installed = _installed_apps()
	if any(entry["dotted_path"].split(".", 1)[0] not in installed for entry in picks):
		raise CaptureError(_("Select a function from an installed app."))
	resolved: list[dict] = []
	seen = set()
	for entry in picks:
		dotted = entry["dotted_path"]
		if dotted in seen:
			continue
		seen.add(dotted)
		try:
			meta = picker.resolve_freeform(dotted)
		except picker.PickerError as exc:
			resolved.append({
				"dotted_path": dotted,
				"source": entry.get("source", "freeform"),
				"eligible": False,
				"ineligible_reason": str(exc),
			})
			continue
		meta["source"] = entry.get("source", "freeform")
		resolved.append(meta)

	eligible = [r for r in resolved if r.get("eligible")]
	if not eligible:
		raise CaptureError(_("No selected function has eligible source. Check the selected paths."))

	# Snapshot source for each eligible pick. Stored as a dict keyed by
	# dotted_path so aggregate_samples can pull lines per pick.
	source_snapshot: dict[str, list[dict]] = {}
	picks_meta: list[dict] = []
	snapshot_bytes = 0
	for r in eligible:
		fn = _resolve_attr(r["dotted_path"])
		lines = _capture_source_lines(fn) if fn else []
		if not lines:
			raise CaptureError(_("A selected function has no readable source. Choose another function."))
		snapshot_bytes += len(json.dumps(lines).encode()) + len(r["dotted_path"].encode()) + 8
		if snapshot_bytes > MAX_CAPTURE_BYTES:
			raise CaptureError(_("Selected source exceeds the capture size limit. Select fewer functions."))
		source_snapshot[r["dotted_path"]] = lines
		picks_meta.append({
			"dotted_path": r["dotted_path"],
			"qualname": r["qualname"],
			"file": r["file"],
			"first_lineno": r["lineno"],
			"source": r["source"],
		})

	_validate_captured_picks([{**pick, "source_lines": source_snapshot[pick["dotted_path"]]} for pick in picks_meta])
	picks_json, source_json = json.dumps(picks_meta), json.dumps(source_snapshot)
	if len(picks_json.encode()) + len(source_json.encode()) > MAX_CAPTURE_BYTES:
		raise CaptureError(_("Selected source exceeds the capture size limit. Select fewer functions."))
	return PreparedCapture(tuple(resolved), picks_json, source_json)


def start_line_profile_pass(
	session_uuid: str, run_uuid: str, user: str, picks: list[dict] | None = None,
	*, prepared: PreparedCapture | None = None,
) -> list[dict]:
	"""Atomically publish complete input and reserve an unowned active flag.

	API callers prepare before SQL admission. The legacy picks argument keeps
	internal callers compatible; no module resolution happens for prepared input.
	"""
	from redis.exceptions import WatchError

	from optimus.ai_fix import _InterruptGuard

	_require_frappe()
	if any(not isinstance(value, str) or not value or len(value) > 140 for value in (session_uuid, run_uuid, user)):
		raise CaptureError(_("Invalid capture identity."))
	if prepared is None:
		prepared = prepare_line_profile_picks(picks)
	elif picks is not None or not isinstance(prepared, PreparedCapture):
		raise CaptureError(_("Invalid prepared capture."))
	active, phase1, pick_key, source_key = [frappe.cache.make_key(key) for key in (
		_redis_keys.lp_active(user), _redis_keys.session_active(user), _redis_keys.lp_picks(run_uuid), _redis_keys.lp_source(run_uuid))]
	guard = _InterruptGuard(base=True)
	conflict = False
	try:
		with guard:
			with frappe.cache.pipeline() as pipe:
				pipe.watch(active, phase1, pick_key, source_key)
				if pipe.exists(active, phase1, pick_key, source_key):
					conflict = True
				else:
					pipe.multi()
					pipe.set(pick_key, pickle.dumps(prepared.picks_json, protocol=4), ex=DATA_TTL_SECONDS)
					pipe.set(source_key, pickle.dumps(prepared.source_json, protocol=4), ex=DATA_TTL_SECONDS)
					pipe.set(active, pickle.dumps(run_uuid, protocol=4), ex=SESSION_TTL_SECONDS)
					pipe.execute()
	except WatchError:
		conflict = True
	finally:
		frappe.local._lp_active = None
		frappe.local._lp_active_user = None
		frappe.local.cache.pop(active, None)
	if guard.pending():
		prepared = picks = None
		raise guard.interrupt()
	if conflict:
		raise CaptureError(_("Capture state changed or a run is already active. Reload and retry."))
	return list(prepared.resolved)


def stop_line_profile_pass(run_uuid: str, user: str) -> bool:
	"""Clear only the expected generation, including Frappe 15/16 byte formats.

	WATCH compares the raw value read from Redis, not the request-local cache.
	A concurrent start/expiry changes the watched key and cannot be deleted by
	this stop. No legacy Redis value is unpickled by Optimus.
	"""
	from redis.exceptions import WatchError

	from optimus.ai_fix import _InterruptGuard

	_require_frappe()
	if not isinstance(run_uuid, str) or not run_uuid or not isinstance(user, str) or not user:
		return False
	key = frappe.cache.make_key(_redis_keys.lp_active(user))
	# Frappe 15 uses pickle's default protocol, 16 explicitly uses protocol 5.
	expected = {pickle.dumps(run_uuid, protocol=protocol) for protocol in (4, 5)}
	guard = _InterruptGuard(base=True)
	cleared = False
	try:
		with guard:
			with frappe.cache.pipeline() as pipe:
				pipe.watch(key)
				if pipe.get(key) in expected:
					pipe.multi()
					pipe.delete(key)
					pipe.execute()
					cleared = True
	except WatchError:
		pass  # Ownership changed; a later explicit stop may target that run.
	finally:
		frappe.local._lp_active = None
		frappe.local._lp_active_user = None
		frappe.local.cache.pop(key, None)
	if guard.pending():
		raise guard.interrupt()
	return cleared


def is_active(user: str, *, fresh: bool = False) -> str | None:
	"""Return the active phase-2 run_uuid for the user, or None.

	Hot-path predicate from the phase-2 request hook must be cheap. The
	value is cached on ``frappe.local._lp_active`` for the request lifetime
	to avoid repeated Redis hits inside one request.
	"""
	_require_frappe()
	if not user or user == "Guest":
		return None
	cached = getattr(frappe.local, "_lp_active", None)
	if not fresh and cached is not None and getattr(frappe.local, "_lp_active_user", None) == user:
		return cached if cached != "" else None
	if fresh:
		frappe.local.cache.pop(frappe.cache.make_key(_redis_keys.lp_active(user)), None)
	value = frappe.cache.get_value(_redis_keys.lp_active(user))
	if isinstance(value, bytes):
		value = value.decode()
	frappe.local._lp_active = value or ""  # cache empty string for misses
	frappe.local._lp_active_user = user
	return value or None


# ---------------------------------------------------------------------------
# Per-request enable/disable cycle
# ---------------------------------------------------------------------------


def _get_or_resolve_picks(run_uuid: str) -> list:
	"""Return the worker-resident list of resolved function objects for a
	run, populating the cache from Redis on first access in this worker."""
	_require_frappe()
	cached = _resolved_fns_by_run.get(run_uuid)
	if cached is not None:
		return cached

	raw = frappe.cache.get_value(_redis_keys.lp_picks(run_uuid))
	if not raw:
		_resolved_fns_by_run[run_uuid] = []
		return []
	if isinstance(raw, bytes):
		raw = raw.decode()
	pick_metas = json.loads(raw)

	fns = []
	for meta in pick_metas:
		fn = _resolve_attr(meta["dotted_path"])
		if fn is not None:
			fns.append(fn)
	_resolved_fns_by_run[run_uuid] = fns
	return fns


# ---------------------------------------------------------------------------
# Process-wide active-profiler registry.
# ---------------------------------------------------------------------------
# ``sys.monitoring`` tool 2 is PROCESS-global, but each request/job thread holds
# its own thread-local ``_lp_profiler``. Under a multi-threaded (gunicorn
# ``gthread``) worker, two requests can profile concurrently and co-own tool 2.
# The forcible ``release_monitoring_tool`` (free_tool_id) and the before-hook's
# orphan self-heal therefore must NOT key on the calling thread's local freeing
# tool 2 while a sibling thread is still enabled desyncs line_profiler's shared
# manager (the tool-2 leak class that froze production). This counter is the
# process-wide truth: reclaim / force-free only when it reads 0.
#
# A thread killed mid-flight (without running its after-hook) would leak its
# increment but a gunicorn timeout recycles the whole worker, resetting this
# global, so that path self-corrects on the next request.
_active_profiler_lock = threading.Lock()
_active_profiler_count = 0


def incr_active_profilers() -> int:
	"""Register an enabled phase-2 profiler; return the new process-wide count."""
	global _active_profiler_count
	with _active_profiler_lock:
		_active_profiler_count = _active_profiler_count + 1
		return _active_profiler_count


def decr_active_profilers() -> int:
	"""Unregister a profiler; return the new count (floored at 0)."""
	global _active_profiler_count
	with _active_profiler_lock:
		_active_profiler_count = max(0, _active_profiler_count - 1)
		return _active_profiler_count


def active_profiler_count() -> int:
	with _active_profiler_lock:
		return _active_profiler_count


@_interrupt_boundary
def release_monitoring_tool() -> None:
	"""Guarantee phase-2 leaves no ``sys.monitoring`` line-trace hook behind.

	On Python 3.12+ line_profiler drives the process-global ``PROFILER_ID``
	(tool id 2). If a per-request teardown fails, tool 2's line events stay
	registered and every subsequent request in the worker is line-traced (CPU
	saturation, frozen UI). This forcibly clears and frees tool 2. Idempotent
	and version-safe: a no-op on Python < 3.12 and when tool 2 isn't ours (only
	reclaims the tool when it's registered to ``line_profiler``)."""
	mon = getattr(sys, "monitoring", None)
	if mon is None:
		return
	try:
		pid = mon.PROFILER_ID
		if mon.get_tool(pid) != "line_profiler":
			return
		mon.set_events(pid, 0)
		mon.free_tool_id(pid)
	except Exception as exc:
		_raise_if_interrupt(exc)
		pass


@_interrupt_boundary
def disengage_monitoring() -> None:
	"""Zero tool 2's line events but leave the tool registered: stop line-trace
	overhead without unseating line_profiler.

	The watchdog's disengage (vs ``release_monitoring_tool``'s full free). The
	distinction is load-bearing: calling ``free_tool_id`` from the watchdog's
	timer thread while the request thread's profiler is still active yanks tool
	2 out from under line_profiler's shared manager, so its ``disable_by_count``
	raises ``ValueError: tool 2 is not in use`` and orphans a ``LineProfiler``
	whose finalizer later crashes at teardown. Zeroing events keeps the manager
	consistent so the request thread's own ``disable_by_count`` does the real
	teardown. Idempotent and version-safe: no-op on Python < 3.12 and when tool
	2 isn't ours."""
	mon = getattr(sys, "monitoring", None)
	if mon is None:
		return
	try:
		pid = mon.PROFILER_ID
		if mon.get_tool(pid) != "line_profiler":
			return
		mon.set_events(pid, 0)
	except Exception as exc:
		_raise_if_interrupt(exc)
		_raise_if_interrupt(exc)
		pass


# ---------------------------------------------------------------------------
# Overhead budget observe without spoiling the flow
# ---------------------------------------------------------------------------
# line_profiler does deterministic per-line tracing, so instrumenting a hot
# loop multiplies its runtime and would freeze the user's request. A watchdog
# timer disengages tracing after a wall-clock budget so a profiled request can
# never take more than ~budget longer than its natural time; the partial line
# data still pinpoints the hot line. See feedback_observe_dont_spoil_flow.

def mark_budget_hit(run_uuid: str) -> None:
	"""Record that this run's profiling was cut short by the overhead budget,
	so analyze can flag the line data as partial. Best-effort."""
	if not _FRAPPE_AVAILABLE or not run_uuid:
		return
	from optimus.ai_fix import _InterruptGuard

	guard = _InterruptGuard(base=True)
	try:
		with guard:
			frappe.cache.set_value(_redis_keys.lp_budget_hit(run_uuid), "1", expires_in_sec=3600)
	except Exception:
		pass

	if guard.pending():
		raise guard.interrupt()


def budget_was_hit(run_uuid: str) -> bool:
	if not _FRAPPE_AVAILABLE or not run_uuid:
		return False
	from optimus.ai_fix import _InterruptGuard

	guard = _InterruptGuard(base=True)
	try:
		with guard:
			return bool(frappe.cache.get_value(_redis_keys.lp_budget_hit(run_uuid)))
	except Exception:
		return False

	if guard.pending():
		raise guard.interrupt()


def clear_budget_hit(run_uuid: str) -> None:
	if not _FRAPPE_AVAILABLE or not run_uuid:
		return
	from optimus.ai_fix import _InterruptGuard

	guard = _InterruptGuard(base=True)
	try:
		with guard:
			frappe.cache.delete_value(_redis_keys.lp_budget_hit(run_uuid))
	except Exception:
		pass

	if guard.pending():
		raise guard.interrupt()


def _disengage_run(run_uuid: str) -> None:
	"""Watchdog callback: stop line tracing (so the request finishes at natural
	speed) and flag the run as budget-truncated. Runs on a timer thread, so it
	uses ``disengage_monitoring`` (zero events) NOT ``release_monitoring_tool``:
	freeing the tool from under the request thread's active profiler would
	desync line_profiler's manager. The request thread's own ``disable_by_count``
	does the real teardown."""
	disengage_monitoring()
	mark_budget_hit(run_uuid)


def start_overhead_watchdog(run_uuid: str, budget_seconds):
	"""Arm a one-shot timer that disengages line tracing after ``budget_seconds``
	of wall time. Returns the started ``threading.Timer`` (cancel it in the
	after_* hook when the request finishes within budget), or None when the
	budget is disabled (``<= 0``)."""
	try:
		budget = float(budget_seconds or 0)
	except (TypeError, ValueError):
		budget = 0.0
	if budget <= 0:
		return None
	timer = threading.Timer(budget, _disengage_run, args=(run_uuid,))
	timer.daemon = True
	timer.start()
	return timer


@_interrupt_boundary
def make_profiler(run_uuid: str):
	"""Build a fresh ``LineProfiler`` with the run's picks attached. Returns
	None if line_profiler is unavailable, the run has no resolvable picks,
	or any other defensive failure phase 2 then becomes a no-op for this
	request rather than breaking the host flow."""
	if not _LP_AVAILABLE:
		return None
	try:
		fns = _get_or_resolve_picks(run_uuid)
	except Exception as exc:
		_raise_if_interrupt(exc)
		return None
	if not fns:
		return None
	profiler = LineProfiler()
	for fn in fns:
		try:
			profiler.add_function(fn)
		except Exception as exc:
			_raise_if_interrupt(exc)
			# A single bad pick shouldn't sink the whole request.
			continue
	return profiler

def serialize_stats(profiler) -> list[dict]:
	"""Extract per-line records from a ``LineProfiler`` instance.

	Output shape matches the ``samples`` batch element expected by
	``aggregate_samples``: one dict per timed line with file/qualname/lineno
	and (hits, total_us). Returns ``[]`` for None or empty profilers.
	"""
	if profiler is None:
		return []
	stats = profiler.get_stats()
	# line_profiler ≥4.x: stats.timings is a dict keyed by
	# (filename, start_lineno, function_name) → list[(lineno, hits, time)]
	# where ``time`` is in microseconds when stats.unit == 1e-6 (default).
	unit = getattr(stats, "unit", 1e-6) or 1e-6
	# Convert whatever unit `time` is in to microseconds.
	us_factor = unit / 1e-6  # 1.0 when unit is microseconds
	samples: list[dict] = []
	for (filename, _start_lineno, qualname), entries in (stats.timings or {}).items():
		for entry in entries:
			# line_profiler tuples vary across versions; first three fields
			# are always (lineno, hits, time).
			lineno, hits, time_value = entry[0], entry[1], entry[2]
			samples.append({
				"file": filename,
				"qualname": qualname,
				"lineno": int(lineno),
				"hits": int(hits),
				"total_us": int(round(float(time_value) * us_factor)),
			})
	return samples


def flush_samples(run_uuid: str, samples: list[dict]) -> None:
	"""Bound memory atomically across concurrent request/job completions.

	Overflow or repeated contention marks the entire input incomplete; it must
	not produce a successful partial report. Never recreate a deleted run.
	"""
	if not samples:
		return
	_require_frappe()
	from redis.exceptions import WatchError

	from optimus.ai_fix import _InterruptGuard

	_validate_samples([samples])
	payload = json.dumps(samples).encode("utf-8")
	sample_key, state_key, source_key = [frappe.cache.make_key(key) for key in (
		_redis_keys.lp_samples(run_uuid), _redis_keys.lp_sample_state(run_uuid), _redis_keys.lp_source(run_uuid))]
	guard = _InterruptGuard(base=True)
	complete = False
	with guard:
		for _attempt in range(3):
			try:
				with frappe.cache.pipeline() as pipe:
					pipe.watch(sample_key, state_key, source_key)
					if not pipe.exists(source_key):
						return  # expired/deleted input; late flush owns no capture
					count, raw_bytes = pipe.llen(sample_key), pipe.get(state_key)
					try:
						used = int(raw_bytes) if raw_bytes is not None else (0 if count == 0 else -1)
					except (ValueError, TypeError):
						used = -1
					complete = 0 <= used <= MAX_CAPTURE_BYTES - len(payload) and count < MAX_SAMPLE_BATCHES
					pipe.multi()
					pipe.set(state_key, str(used + len(payload) if complete else -1).encode(), ex=DATA_TTL_SECONDS)
					if complete:
						pipe.rpush(sample_key, payload)
						pipe.expire(sample_key, DATA_TTL_SECONDS)
					pipe.execute()
					break
			except WatchError:
				complete = False
		else:
			# Raw counter, not a pickled cache value; state_key already uses make_key.
			frappe.cache.set(state_key, b"-1", ex=DATA_TTL_SECONDS)  # nosemgrep: frappe-cache-breaks-multitenancy
	if guard.pending():
		samples = payload = None
		raise guard.interrupt()
	if not complete:
		raise CaptureInputError("Phase 2 sample capture is incomplete or exceeds its limit")


# ---------------------------------------------------------------------------
# Read-side helpers (analyze.run_analyze + janitor)
# ---------------------------------------------------------------------------


@_interrupt_boundary
def read_all_samples(run_uuid: str) -> list[list[dict]]:
	"""Read a bounded snapshot; malformed input is never an empty successful pass."""
	_require_frappe()
	# Raw byte accounting requires GET; make_key retains Frappe's site boundary.
	state = frappe.cache.get(frappe.cache.make_key(_redis_keys.lp_sample_state(run_uuid)))  # nosemgrep: frappe-cache-breaks-multitenancy
	if state is not None:
		try:
			valid = 0 <= int(state) <= MAX_CAPTURE_BYTES
		except (ValueError, TypeError):
			valid = False
		if not valid:
			raise CaptureInputError("Phase 2 sample capture is incomplete")
	batches, total_bytes = [], 0
	for start in range(0, MAX_SAMPLE_BATCHES + 1, 8):
		end = min(start + 7, MAX_SAMPLE_BATCHES)
		raw_list = frappe.cache.lrange(_redis_keys.lp_samples(run_uuid), start, end) or []
		if len(batches) + len(raw_list) > MAX_SAMPLE_BATCHES:
			raise CaptureInputError("Phase 2 sample batches exceed their limit")
		for raw in raw_list:
			if not isinstance(raw, (str, bytes)):
				raise CaptureInputError("Phase 2 sample batch is malformed")
			total_bytes += len(raw.encode("utf-8") if isinstance(raw, str) else raw)
			if total_bytes > MAX_CAPTURE_BYTES:
				raise CaptureInputError("Phase 2 samples exceed their size limit")
			batches.append(_decode_capture_json(raw))
		if len(raw_list) < end - start + 1:
			break
	if state is not None and (total_bytes != int(state)
		or frappe.cache.get(frappe.cache.make_key(_redis_keys.lp_sample_state(run_uuid))) != state):  # nosemgrep: frappe-cache-breaks-multitenancy
		raise CaptureInputError("Phase 2 samples changed or are incomplete. Retry after capture ends")
	_validate_samples(batches)
	return batches


@_interrupt_boundary
def read_picks_meta(run_uuid: str) -> list[dict]:
	"""A complete source snapshot is required, including for zero samples."""
	_require_frappe()
	picks = _decode_capture_json(frappe.cache.get_value(_redis_keys.lp_picks(run_uuid)))
	sources = _decode_capture_json(frappe.cache.get_value(_redis_keys.lp_source(run_uuid)))
	if not isinstance(picks, list) or not isinstance(sources, dict) or len(sources) > MAX_PICKS:
		raise CaptureInputError("Phase 2 source metadata is malformed")
	out = []
	for pick in picks:
		if not isinstance(pick, dict) or not isinstance(pick.get("dotted_path"), str):
			raise CaptureInputError("Phase 2 captured function metadata is malformed")
		out.append({**pick, "source_lines": sources.get(pick["dotted_path"])})
	_validate_captured_picks(out)
	return out


@_interrupt_boundary
def cleanup_run(run_uuid: str) -> None:
	"""Delete exact-run input; recovery callers must observe a Redis failure."""
	_require_frappe()
	keys = [frappe.cache.make_key(key_fn(run_uuid)) for key_fn in (
		_redis_keys.lp_picks, _redis_keys.lp_source, _redis_keys.lp_samples,
		_redis_keys.lp_sample_state, _redis_keys.lp_budget_hit,
	)]
	try:
		# delete_value suppresses connection errors in Frappe. Use raw DEL so
		# the caller can report/recover a failure instead of claiming cleanup.
		frappe.cache.delete(*keys)
	finally:
		_resolved_fns_by_run.pop(run_uuid, None)
		local_cache = getattr(frappe.local, "cache", None)
		if isinstance(local_cache, dict):
			for key in keys:
				local_cache.pop(key, None)
