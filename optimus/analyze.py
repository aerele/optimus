# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Background-job entry point: analyze a finished session.

Triggered by api.stop() via frappe.enqueue("optimus.analyze.run", ...). Reads
the session's recordings from Redis, runs the analyzers, persists results into
the Optimus Session DocType and publishes a realtime notification for the UI.
Status goes Stopping -> Analyzing -> Ready (or Failed on an uncaught
exception). A successful run deletes the session's Redis state; a failed run
leaves it so analyze can be retried.
"""

import html
import json
import math
import os
import pickle
import re
import time
from collections import Counter, OrderedDict
from dataclasses import asdict
from datetime import datetime, timezone

import frappe
import sqlparse
from frappe.database.utils import is_query_type
from frappe.recorder import (
	RECORDER_REQUEST_HASH,
	RECORDER_REQUEST_SPARSE_HASH,
	mark_duplicates,
)
from frappe.utils.scheduler import is_scheduler_disabled

from optimus import ai_grounding, ai_privacy, ai_prompts, renderer, safe_call, safe_commit, session
from optimus.analyzers import (
	call_tree,
	explain_flags,
	frontend_timings,  # v0.5.0
	index_suggestions,
	infra_pressure,  # v0.5.0
	n_plus_one,
	per_action,
	redundant_calls,
	table_breakdown,
	top_queries,
)
from optimus.analyzers.base import (
	_DUR_SEP,
	INDEX_FINDING_TYPES,
	SEVERITY_ORDER,
	UNKNOWN_SEVERITY_RANK,
	AnalyzeContext,
	dur,
	is_error_log_hook_query,
	row_get,
	strip_profiler_own_calls,
)
from optimus.dbdialect import get_dialect

# v0.3.0: per-analyzer wall-clock budget. If the cumulative analyze
# elapsed time crosses this threshold, remaining analyzers are skipped
# and the session is finalized as Ready with a partial-completion
# warning. 5 minutes of headroom under the RQ long-queue 25-min timeout.
ANALYZE_TOTAL_BUDGET_SECONDS = 20 * 60

# Per-individual-analyzer soft cap: an analyzer that exceeds this is
# logged as a warning but doesn't halt the pipeline.
ANALYZE_PER_ANALYZER_SOFT_CAP_SECONDS = 60

# v0.6.0: when Optimus Settings ▸ AI Fix Suggestions ▸ "Suggest AI fixes
# by default" is on, the analyze pipeline calls the LLM for the top
# findings. Cap the total wall time spent on those calls so a slow
# provider can't push the analyze job past the RQ 25-min timeout.
AI_AUTO_SUGGEST_TIME_BUDGET_SECONDS = 240

# Time budget of one synchronous AI > Refresh AI suggestions run
# (api.refill_ai_suggestions, a web request), so it stays well under the
# gunicorn worker timeout (~120s). Findings not reached in this window are
# left for another Refresh.
AI_BACKFILL_TIME_BUDGET_SECONDS = 60

# v0.3.0: persistence size limits for call_tree_json on Optimus Action.
CALL_TREE_OVERFLOW_THRESHOLD_BYTES = 200_000   # write to file above this
CALL_TREE_HARD_MAX_BYTES = 16_000_000          # last-line sanity guard
CALL_TREE_HARD_TRUNCATE_KEEP_FRAMES = 300      # frames kept on fallback (Phase K: 100 was too aggressive for typical Frappe + middleware stacks)


# per_action is first because it builds the Optimus Action rows that the
# rest of the analyzers reference via action_ref. The remainder are
# independent and could in principle be parallelized.
_BUILTIN_ANALYZERS = [
	per_action.analyze,
	top_queries.analyze,
	n_plus_one.analyze,
	explain_flags.analyze,
	index_suggestions.analyze,
	table_breakdown.analyze,
	call_tree.analyze,        # v0.3.0 must run after per_action
	redundant_calls.analyze,  # v0.3.0 independent
	infra_pressure.analyze,   # v0.5.0 reads rec["infra"]
	frontend_timings.analyze, # v0.5.0 reads context.frontend_data
]

# Backward-compat alias: the old name is still the public-facing list
# for code that references `analyze.ANALYZERS` directly.
ANALYZERS = _BUILTIN_ANALYZERS


def _get_analyzers() -> list:
	"""Return the analyzer pipeline: builtins plus custom hooks.

	Third-party apps add analyzers via the ``optimus_analyzers`` hook (a list of
	dotted paths). Custom analyzers run AFTER the builtins so they can read
	context.actions / context.findings. A failing one is logged, not fatal.
	"""
	analyzers = list(_BUILTIN_ANALYZERS)
	try:
		hook_paths = frappe.get_hooks("optimus_analyzers") or []
	except Exception:
		hook_paths = []

	for dotted in hook_paths:
		if not dotted:
			continue
		try:
			fn = frappe.get_attr(dotted)
			if callable(fn):
				analyzers.append(fn)
			else:
				frappe.log_error(
					title="optimus analyzer hook",
					message=f"Custom analyzer {dotted} is not callable",
				)
		except Exception:
			frappe.log_error(
				title="optimus analyzer hook",
				message=f"Failed to load custom analyzer {dotted}",
			)

	return analyzers


def _publish_progress(percent: float, description: str, session_uuid: str):
	"""Emit an optimus_progress update for the floating widget and form UI.
	Best-effort, never raises."""
	try:
		from optimus.realtime import publish_session_event
		publish_session_event(
			"optimus_progress",
			{
				"session_uuid": session_uuid,
				"percent": round(percent, 1),
				"description": description,
			},
		)
	except Exception:
		pass


def _publish_session_event(
	event_name: str,
	*,
	session_uuid: str,
	docname: str | None,
	**extra,
) -> None:
	"""Publish a state transition to the recording user and authorized open forms.

	Runs in the background analyze job (no request-scoped user), so the user is
	looked up from the Optimus Session row. Events: optimus_session_analyzing,
	optimus_session_ready, optimus_session_failed. Best-effort and isolated: a
	publish failure never derails analyze (the state is durable on the row).
	"""
	try:
		from optimus.realtime import publish_session_event
		payload = {"session_uuid": session_uuid, "docname": docname}
		payload.update(extra)
		publish_session_event(event_name, payload)
	except Exception:
		pass


# v0.6.0: hard ceiling on how long analyze waits for the flow's background
# jobs, regardless of the configured `background_job_wait_seconds`.
_MAX_BG_JOB_WAIT_SECONDS = 300
# Throttle between re-enqueue cycles while waiting keeps a wedged ("deferred"
# forever) job from busy-looping our re-enqueues.
_BG_WAIT_THROTTLE_SECONDS = 2.0

# v0.7.x (M2): global single-flight for the heavy analyze phase. Two analyze
# jobs running at once roughly DOUBLE peak RAM the OOM trigger on long flows.
# A best-effort Redis flag (NOT a held lock) lets only one session do the heavy
# work at a time; others re-enqueue themselves to yield the worker (mirrors the
# bg-job wait pattern), so a single-worker bench still makes progress.
# v0.12.0: key centralized in optimus.redis_keys. The constant is kept
# for code-local readability every internal use that references the
# key still does so via this module-level alias.
from optimus import redis_keys as _redis_keys
from optimus import redis_schema as _redis_schema

_SINGLEFLIGHT_KEY = _redis_keys.analyze_inflight()
# Flag TTL. Heartbeated at each progress milestone in run(); if a worker dies
# mid-analyze the flag self-heals within this window (no permanent strand).
_SINGLEFLIGHT_TTL_SECONDS = 300
# Throttle between polite re-enqueue cycles while another session holds the flag.
_SINGLEFLIGHT_THROTTLE_SECONDS = 5.0
# Hard ceiling on how long a session waits for the flag before degrading to the
# pre-M2 behavior (proceed anyway) rather than waiting forever.
_SINGLEFLIGHT_MAX_WAIT_SECONDS = 600
# The flag is written with a raw SET NX EX, pickled the way RedisWrapper.get_value
# reads it back. 5 is frappe v16's DEFAULT_PICKLE_PROTOCOL; v15's get_value loads any protocol.
_SINGLEFLIGHT_PICKLE_PROTOCOL = 5
# Sessions whose current run already logged a failed or yielding heartbeat: one
# optimus log line per run. _release_singleflight, which every run ends with, clears it.
_heartbeat_noted: set[str] = set()

# Every provider call of the analyze-time AI step, and the humanize call in
# _persist, is capped here, and the single-flight flag is touched before each call, so
# two heartbeats are never further apart than one call: 240 s < the 300 s TTL.
AI_CALL_TIMEOUT_CAP_SECONDS = _SINGLEFLIGHT_TTL_SECONDS - 60


def _ai_call_timeout() -> int:
	"""The configured AI request timeout, capped at ``AI_CALL_TIMEOUT_CAP_SECONDS``."""
	from optimus import ai_fix

	return min(int(ai_fix.request_timeout_seconds()), AI_CALL_TIMEOUT_CAP_SECONDS)


def _apply_nice() -> None:
	"""Best-effort lower this process's CPU priority so a heavy analyze yields to
	live web traffic. No-op when configured to 0 or without ``os.nice``.

	Caller must gate this to the async (RQ-worker) path: ``os.nice`` is sticky
	per-process, so renicing the shared gunicorn worker would persist for
	unrelated later requests."""
	try:
		inc = int(frappe.conf.get("optimus_analyze_nice", 5) or 0)
	except Exception:
		inc = 0
	if inc and hasattr(os, "nice"):
		try:
			os.nice(inc)
		except (OSError, ValueError):
			pass


def _singleflight_redis_key():
	"""The flag's site-prefixed Redis key, for the raw SET and EXPIRE that the wrapper does
	not prefix itself."""
	return frappe.cache.make_key(_SINGLEFLIGHT_KEY)


def _read_singleflight_holder():
	"""The flag's holder as Redis has it now. ``RedisWrapper.get_value``
	answers from ``frappe.local.cache`` once the key is in it, and that dict lives for the
	whole RQ job, so a second read in one analyze returned the first read's value: a run
	whose flag lapsed still saw itself as the holder, and on v15 a run never saw its own
	flag and never released it. Drop the key from the job's cache, then read with
	``expires=True``, which does not store the answer back (portable: only v16 has
	``use_local_cache=False``). Frappe sets ``frappe.local.cache`` for every request and
	job, and its own ``get_value`` needs it; the ``getattr`` guard only helps test doubles."""
	local_cache = getattr(frappe.local, "cache", None)
	if local_cache is not None:
		local_cache.pop(_singleflight_redis_key(), None)
	return frappe.cache.get_value(_SINGLEFLIGHT_KEY, expires=True)


def _take_singleflight(session_uuid: str) -> bool:
	"""Take the flag only if it is free, in one atomic ``SET NX EX``: of two
	sessions taking it at once, exactly one gets it. Like every flag write here it skips
	``frappe.local.cache``; ``_read_singleflight_holder`` never trusts that cache."""
	value = pickle.dumps(session_uuid, protocol=_SINGLEFLIGHT_PICKLE_PROTOCOL)
	# A raw SET is the only way to get NX; the key comes from frappe.cache.make_key, so it
	# carries the site prefix that set_value would add, and the flag stays per site.
	return bool(
		frappe.cache.set(  # nosemgrep: frappe-cache-breaks-multitenancy
			_singleflight_redis_key(), value, nx=True, ex=_SINGLEFLIGHT_TTL_SECONDS
		)
	)


def _note_heartbeat_problem(session_uuid: str, problem: str) -> None:
	"""One ``optimus`` log line per analyze run when a heartbeat fails or finds another
	session's flag. Such a run does not hold the flag, so the janitor can fail its
	Analyzing row while it still runs; this line says why. Logged at ERROR because
	Frappe's loggers drop lower levels on a production site. Never raises, except an RQ
	job timeout."""
	if session_uuid in _heartbeat_noted:
		return
	_heartbeat_noted.add(session_uuid)

	safe_call.log_error_line(f"optimus: analyze {session_uuid}: {problem}")


def _touch_singleflight(session_uuid: str) -> bool:
	"""Refresh the single-flight flag's TTL while this session holds it, or take it back
	when it lapsed and nobody took it. Never writes over another session's flag (taken
	after a lapse, or held while this run degraded past its wait deadline): that session
	is the one the janitor and the next waiter must see, so a degraded run leaves it
	alone. Every check reads Redis, not the job-local cache. The refresh is an
	EXPIRE and the take-back a SET NX, so neither can replace another session's value. One
	window is left: if this flag lapses and another session takes it between the read and
	the EXPIRE, the EXPIRE renews that session's TTL (never its value). A touch that fails
	or finds another session's flag logs one line per run. Returns True only when
	this session holds the flag after the touch. Best-effort: a cache hiccup never fails analyze, it returns False; an RQ job
	timeout escapes as a fresh instance."""

	def _touch() -> bool:
		if _read_singleflight_holder() == session_uuid and frappe.cache.expire(
			_singleflight_redis_key(), _SINGLEFLIGHT_TTL_SECONDS,
		):
			return True
		# Free, ours but lapsed before the EXPIRE, or another session's: SET NX takes
		# only a free flag.
		return _take_singleflight(session_uuid)

	held = safe_call.best_effort(_touch, None, on_error=lambda error_type: _note_heartbeat_problem(
		session_uuid, f"the single-flight heartbeat failed ({error_type}); the flag may lapse",
	))
	if held is False:
		_note_heartbeat_problem(
			session_uuid,
			"another session holds the single-flight flag, so this run's heartbeat left it "
			"alone and this run does not hold it",
		)
	return held is True


def is_singleflight_holder(session_uuid: str) -> bool:
	"""True iff ``session_uuid`` currently holds the single-flight flag, read from Redis. The flag is heartbeated throughout analyze, so its presence is a reliable
	liveness signal (the janitor consults it before failing a long-running Analyzing row,
	since no DB write happens mid-analyze). A cache error reads as False; an RQ job
	timeout escapes as a fresh instance."""
	return safe_call.best_effort(lambda: _read_singleflight_holder() == session_uuid, False)


def _release_singleflight(session_uuid: str) -> None:
	"""Release the flag, but only if Redis says we still hold it (compare-then-delete),
	so a TTL-expired-then-reacquired flag belonging to another session isn't clobbered.
	Not atomic: a flag that lapses and is taken in the instant between the read and the
	delete is still deleted, which needs this run to have gone a whole TTL without a
	heartbeat first. Also ends the run's one-line heartbeat note. Best-effort: a
	cache error is ignored; an RQ job timeout escapes as a fresh instance."""
	_heartbeat_noted.discard(session_uuid)

	def _release() -> None:
		if _read_singleflight_holder() == session_uuid:
			frappe.cache.delete_value(_SINGLEFLIGHT_KEY)

	safe_call.best_effort(_release, None)


def _acquire_singleflight(session_uuid: str, docname: str, deadline) -> bool:
	"""Global single-flight gate for the heavy (memory-hungry) analyze phase, so
	two sessions can't analyze at once and roughly double peak RAM.

	Returns True to proceed (flag acquired, single-flight not applicable, or the
	wait deadline passed so we degrade rather than strand). Returns False when
	another session holds the flag and we re-enqueued ourselves to yield the
	worker: the caller must ``return`` now. Re-enqueue + yield, never a held
	lock. Skipped when the scheduler is disabled (analyze runs inline)."""
	# Inline path: no worker to yield to proceed.
	try:
		if is_scheduler_disabled():
			return True
	except Exception:
		return True

	try:
		max_wait = int(frappe.conf.get("optimus_singleflight_max_wait_seconds")
			or _SINGLEFLIGHT_MAX_WAIT_SECONDS)
	except Exception:
		max_wait = _SINGLEFLIGHT_MAX_WAIT_SECONDS
	if max_wait <= 0:
		return True  # single-flight disabled by config

	guard = safe_call.InterruptGuard()
	try:
		with guard:
			if _take_singleflight(session_uuid):
				return True  # it was free; SET NX lets only one of two racing sessions take it
			holder = _read_singleflight_holder()
	except Exception:
		return True  # cache unavailable degrade to pre-M2 behavior
	if guard.pending():
		raise guard.interrupt()  # an RQ job timeout still stops the job, fresh

	if holder == session_uuid and _touch_singleflight(session_uuid):
		# Already ours (our own self-re-enqueue) and still ours after the refresh: go. If
		# the touch found another holder (the flag lapsed and was taken since the read) or
		# failed, wait like any busy session.
		return True

	# Busy with a different session (a flag freed since the SET NX is taken on the next
	# try). Respect the wait deadline, else degrade.
	if deadline is None:
		deadline = time.time() + max_wait
	if time.time() >= deadline:
		return True  # waited long enough proceed rather than strand

	# Keep the UI honest, throttle and re-enqueue ourselves to yield the worker.
	try:
		frappe.db.set_value("Optimus Session", {"name": docname, "status": ["in", list(_ANALYZE_PENDING)]}, "status", "Analyzing")
		safe_commit()
	except Exception:
		pass
	_publish_progress(3, "Waiting for another analysis to finish…", session_uuid)
	time.sleep(min(_SINGLEFLIGHT_THROTTLE_SECONDS, max(0.0, deadline - time.time())))
	try:
		frappe.enqueue(
			"optimus.analyze.run",
			queue="long",
			session_uuid=session_uuid,
			_singleflight_deadline=deadline,
		)
	except Exception:
		frappe.log_error(title="optimus single-flight re-enqueue")
		return True  # couldn't re-enqueue just proceed
	return False


_RQ_ACTIVE = ("queued", "started", "deferred", "scheduled")
_RQ_WAITING = ("queued", "deferred", "scheduled")


def _rq_status(job_id: str) -> str | None:
	"""RQ status of ``job_id``, "missing" if RQ has no record of it, None on any other error."""
	try:
		from frappe.utils.background_jobs import get_redis_conn
		from rq.exceptions import NoSuchJobError
		from rq.job import Job

		try:
			return Job.fetch(job_id, connection=get_redis_conn()).get_status(refresh=True)
		except NoSuchJobError:
			return "missing"
	except Exception:
		return None


def _rq_job_active(job_id: str) -> bool:
	"""True if RQ job ``job_id`` is still queued / started / deferred / scheduled.
	False if terminal or no longer fetchable. Any error counts as not active (so
	analyze never blocks on it)."""
	return _rq_status(job_id) in _RQ_ACTIVE


def _short_exc(exc_info) -> str | None:
	"""Last non-empty line of an RQ ``exc_info`` traceback (the one that names the
	exception, e.g. ``ValueError: bad doc_name``), truncated to 500 chars."""
	if not exc_info:
		return None
	lines = [ln.strip() for ln in str(exc_info).splitlines() if ln.strip()]
	return lines[-1][:500] if lines else None


def _rq_dt_to_db(dt):
	"""Convert an RQ tz-aware UTC datetime to a naive system-timezone string that
	MariaDB's DATETIME column accepts (a tz offset triggers err 1292). Falls back
	to a tz-stripped string if the system timezone can't be resolved."""
	if not dt:
		return None
	try:
		from frappe.utils import convert_utc_to_system_timezone, get_datetime_str

		return get_datetime_str(convert_utc_to_system_timezone(dt))
	except Exception:
		try:
			return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")
		except Exception:
			return None


_TZ_OFFSET_RE = re.compile(r"([+-]\d{2}:?\d{2}|Z)$")


def _db_datetime_str(val):
	"""Make a datetime *string* MariaDB-safe by stripping any trailing tz offset
	(``+00:00`` / ``+0000`` / ``-05:30`` / ``Z``), which the DATETIME column
	rejects (err 1292). A naive string passes through unchanged; an empty value
	returns None."""
	if not val:
		return None
	s = str(val).strip()
	if not s:
		return None
	return _TZ_OFFSET_RE.sub("", s) or None


def _capture_job_terminal_status(session_uuid: str, job_id: str) -> None:
	"""Read a now-inactive RQ job's terminal status + timing and record it on the
	session's job-meta hash (Completed / Failed / Timeout / Stopped) so analyze
	can persist it. Best-effort, never blocks the wait."""
	try:
		from frappe.utils.background_jobs import get_redis_conn
		from rq.exceptions import NoSuchJobError
		from rq.job import Job

		try:
			job = Job.fetch(job_id, connection=get_redis_conn())
		except NoSuchJobError:
			_mark_vanished_job(session_uuid, job_id)
			return
		rq_status = job.get_status(refresh=True)
		exc = job.exc_info or ""
		if rq_status == "finished":
			status, error = "Completed", None
		elif rq_status == "failed":
			# An RQ JobTimeoutException in the traceback means the worker killed
			# it for exceeding its timeout report that distinctly.
			status = "Timeout" if "JobTimeoutException" in exc else "Failed"
			error = _short_exc(exc)
		elif rq_status in ("stopped", "canceled"):
			status, error = "Stopped", _short_exc(exc)
		else:
			return  # still active leave for the running-mark path
		started = getattr(job, "started_at", None)
		ended = getattr(job, "ended_at", None)
		duration_ms = None
		if started and ended:
			try:
				duration_ms = round((ended - started).total_seconds() * 1000, 2)
			except Exception:
				duration_ms = None
		session.set_job_status(
			session_uuid, job_id,
			status=status,
			error=error,
			started_at=_rq_dt_to_db(started),
			ended_at=_rq_dt_to_db(ended),
			duration_ms=duration_ms,
		)
	except Exception:
		pass


# An after-commit job has no RQ record until its transaction commits.
_COMMIT_GRACE_SECONDS = 120


def _within_commit_grace(meta: dict) -> bool:
	"""True for a still-Queued job enqueued so recently it may be waiting on a commit."""
	if meta.get("status") not in (None, "", "Queued") or not meta.get("enqueued_at"):
		return False
	try:
		from frappe.utils import get_datetime, now_datetime

		return (now_datetime() - get_datetime(meta["enqueued_at"])).total_seconds() < _COMMIT_GRACE_SECONDS
	except Exception:
		return False


def _awaiting_commit(session_uuid: str, job_id: str, status: str | None = None) -> bool:
	"""True if RQ has no record of the job yet but it was enqueued within the commit grace."""
	status = status or _rq_status(job_id)
	if status != "missing":
		return False
	try:
		return _within_commit_grace(session._read_job(session_uuid, job_id) or {})
	except Exception:
		return False


def _job_in_flight(session_uuid: str, job_id: str, status: str | None = None) -> bool:
	"""Active in RQ, or not yet visible because its enqueueing transaction has not committed."""
	status = status or _rq_status(job_id)
	return status in _RQ_ACTIVE or _awaiting_commit(session_uuid, job_id, status)


def _mark_vanished_job(session_uuid: str, job_id: str) -> None:
	"""Mark a never-started job that RQ no longer knows as Stopped."""
	meta = session._read_job(session_uuid, job_id) or {}
	if _within_commit_grace(meta):
		return
	if meta.get("status") in (None, "", "Queued"):
		session.set_job_status(
			session_uuid,
			job_id,
			status="Stopped",
			error="Never ran: the queue has no record of this job (the transaction that "
			"queued it may have rolled back, or the job expired before a worker took it).",
		)


def _finalize_pending_statuses(session_uuid: str, job_ids) -> None:
	"""At the wait ceiling: record finished jobs' status, label active ones Running or Queued."""
	for jid in job_ids:
		status = _rq_status(jid)
		if _job_in_flight(session_uuid, jid, status):
			session.set_job_status(session_uuid, jid, status=_active_job_label(status))
		else:
			_capture_job_terminal_status(session_uuid, jid)


def _active_job_label(status: str | None) -> str:
	""""Queued" for a job still waiting or not yet visible in RQ, else "Running"."""
	return "Queued" if status in _RQ_WAITING or status == "missing" else "Running"


def _bg_wait_for_pending_jobs(session_uuid: str, docname: str, deadline):
	"""Wait for the background jobs the profiled flow enqueued to finish before we
	gather recordings.

	Returns None when jobs are still running and we re-enqueued ``analyze.run``
	to yield the worker (caller must ``return`` now); 0 when there's nothing to
	wait for or the wait is disabled/inline (proceed); N > 0 when the wait cap
	was hit with N jobs still running (proceed, but caller should warn).
	Best-effort: any failure returns 0.
	"""
	try:
		pending = session.get_pending_jobs(session_uuid)
	except Exception:
		return 0
	if not pending:
		return 0

	try:
		from optimus.settings import get_config

		wait_seconds = int(getattr(get_config(), "background_job_wait_seconds", 0) or 0)
	except Exception:
		wait_seconds = 0
	if wait_seconds <= 0:
		return 0
	wait_seconds = min(wait_seconds, _MAX_BG_JOB_WAIT_SECONDS)

	# When the scheduler is disabled, analyze is running inline in a web
	# request and there's no worker to run the pending jobs (or our
	# re-enqueued self) better to ship the report now than hang.
	try:
		if is_scheduler_disabled():
			return 0
	except Exception:
		pass

	if deadline is None:
		deadline = time.time() + wait_seconds

	# Prune finished / expired ids so the wait can end. As each job goes
	# inactive, capture its terminal status (Completed / Failed / Timeout /
	# Stopped) before dropping it, so it's reported even if it failed or timed
	# out and produced no recording.
	still_running: list[str] = []
	for jid in pending:
		if _job_in_flight(session_uuid, jid):
			still_running.append(jid)
		else:
			_capture_job_terminal_status(session_uuid, jid)
			try:
				session.clear_pending_job(session_uuid, jid)
			except Exception:
				pass

	if not still_running:
		return 0  # everything finished proceed
	if time.time() >= deadline:
		_finalize_pending_statuses(session_uuid, still_running)
		return len(still_running)  # cap hit proceed; caller warns

	# v0.13: keep the UI honest while we wait stay on "Capturing Background
	# Jobs" (set at stop) for the whole drain rather than flipping to
	# "Analyzing" here; the real "Analyzing" is set below once the jobs finish
	# and we start gathering recordings. The janitor's stale-stopping sweep
	# self-heals this status too, so a stranded drain can't get stuck.
	try:
		frappe.db.set_value(
			"Optimus Session", {"name": docname, "status": ["in", list(_ANALYZE_PENDING)]}, "status", "Capturing Background Jobs"
		)
		safe_commit()
	except Exception:
		pass
	_publish_progress(
		2, f"Waiting for {len(still_running)} RQ Job(s) to finish…", session_uuid
	)

	# Throttle, then re-enqueue ourselves so the worker can run the pending
	# job(s) meanwhile.
	time.sleep(min(_BG_WAIT_THROTTLE_SECONDS, max(0.0, deadline - time.time())))
	if time.time() >= deadline:
		_finalize_pending_statuses(session_uuid, still_running)
		return sum(1 for jid in still_running if _job_in_flight(session_uuid, jid))

	try:
		frappe.enqueue(
			"optimus.analyze.run",
			queue="long",
			session_uuid=session_uuid,
			_bg_wait_until=deadline,
		)
	except Exception:
		frappe.log_error(title="optimus bg-job wait re-enqueue")
		return 0  # couldn't re-enqueue just proceed
	return None


def _auto_arm_phase2(docname: str, context) -> None:
	"""Optional capture after the Ready commit. Worker interrupts still stop work."""
	from optimus.ai_fix import _InterruptGuard, log_ai_failure

	guard = _InterruptGuard(base=True)
	failure = None
	try:
		with guard:
			_arm_phase2(docname, context)
	except Exception as exc:
		failure = exc
	if failure is not None or guard.pending():
		with guard:
			frappe.db.rollback()
	if guard.pending():
		failure = context = None
		raise guard.interrupt()
	if failure is not None:
		log_ai_failure("optimus ai auto-arm phase 2", failure)


def _arm_phase2(docname: str, context) -> None:
	if not frappe.conf.get("optimus_phase2_auto_arm"):
		return

	import uuid as _uuid

	from optimus.line_profile import capture as _lp_capture
	from optimus.line_profile import picker as _lp_picker
	from optimus.settings import get_config

	doc = frappe.get_doc("Optimus Session", docname)
	user = getattr(doc, "user", None)
	if not user:
		return

	# Guard: respect the per-session run cap. v0.13.x: 0 = no cap
	# (Strict-as-unlimited). Pre-v0.13.x the ``or 10`` swallowed 0
	# and silently re-applied the default cap.
	cap = int(getattr(get_config(), "phase2_max_runs_per_session", 10))
	if cap > 0 and len(doc.get("phase_2_runs") or []) >= cap:
		return

	# Redis failure refuses optional arming; atomic start rechecks ownership.
	if (frappe.cache.get_value(_redis_keys.lp_active(user))
		or frappe.cache.get_value(_redis_keys.session_active(user))):
		return

	# Recommended hot paths, derived from the persisted call trees (same
	# candidate builder the picker uses).
	trees: list = []
	for action in (doc.get("actions") or []):
		raw = getattr(action, "call_tree_json", None)
		if not raw:
			continue
		try:
			tree = json.loads(raw)
		except (TypeError, ValueError):
			continue
		if isinstance(tree, dict) and "root" in tree:
			tree = tree["root"]
		trees.append(tree)

	candidates = _lp_picker._build_tree_indented_candidates(trees)
	# Don't auto-arm Ignored-App functions (same filter as the picker).
	from optimus.settings import get_ignored_apps
	_ignored_apps = get_ignored_apps()
	candidates, _ = _lp_picker.filter_out_ignored_apps(candidates, _ignored_apps)
	picks = [
		{"dotted_path": c["dotted_path"], "source": "curated"}
		for c in candidates
		if c.get("recommended") and c.get("dotted_path")
	]
	if not picks:
		return

	prepared = _lp_capture.prepare_line_profile_picks(picks)
	run_uuid = _uuid.uuid4().hex
	from optimus.api import _lock_phase2_for_write, _persist_phase2_capture
	from optimus.line_profile.jobs import _authorized
	_lock_phase2_for_write(docname)
	doc = frappe.get_doc("Optimus Session", docname)
	if cap > 0 and len(doc.get("phase_2_runs") or []) >= cap:
		frappe.db.rollback()
		return
	if (getattr(doc, "user", None) != user or getattr(doc, "session_uuid", None) != context.session_uuid
		or not _authorized({"name": docname, "owner": getattr(doc, "owner", None)}, user)):
		frappe.db.rollback()
		return
	resolved = _persist_phase2_capture(doc, docname, context.session_uuid, run_uuid, user, prepared)
	eligible = [r for r in resolved if r.get("eligible")]

	# Auto-arm runs server-side during analyze, when the user isn't on the
	# form tell them a pass is armed and what to do next (re-run + Stop),
	# since arming alone does nothing until the flow re-executes.
	functions = [r["dotted_path"].rsplit(".", 1)[-1] for r in eligible][:5]
	from optimus.realtime import publish_session_event
	publish_session_event(
		"optimus_phase2_armed",
		{
			"docname": docname,
			"run_uuid": run_uuid,
			"count": len(eligible),
			"functions": functions,
		},
	)


_ANALYZE_PENDING = frozenset({"Stopping", "Capturing Background Jobs", "Analyzing"})


def run(session_uuid: str, _bg_wait_until: float | None = None,
	_singleflight_deadline: float | None = None):
	"""Background-job entry point. Called from api.stop() via frappe.enqueue.

	``_bg_wait_until`` / ``_singleflight_deadline`` are set only when ``run``
	re-enqueues itself while waiting; external callers never pass them."""
	# Round 2 fix #6: mark this request-context as "analyzing" so our
	# before_request / before_job hooks don't recursively activate the
	# recorder on the DocType writes we're about to do. Without this,
	# if the recording user also has an active profiler session (e.g.
	# multiple sessions started in sequence) we could recurse.
	frappe.local.optimus_analyzing = True

	# v0.7.x (M5): de-prioritize this analyze on the async (worker) path so it
	# doesn't starve live web traffic. Skipped inline (scheduler disabled)
	# os.nice is sticky per-process and would de-prioritize the shared gunicorn
	# worker for unrelated later requests.
	try:
		if not is_scheduler_disabled():
			_apply_nice()
	except Exception:
		pass

	docname = frappe.db.get_value("Optimus Session", {"session_uuid": session_uuid}, "name")
	if not docname:
		frappe.log_error(
			title="optimus ai analyze",
			message=f"No Optimus Session found for uuid {session_uuid}",
		)
		return

	analyze_start = time.monotonic()
	bg_jobs_unfinished = 0
	failure = None
	completed_elsewhere = False

	try:
		if frappe.db.get_value("Optimus Session", docname, "status") not in _ANALYZE_PENDING:
			return  # Late queue/janitor delivery must not reopen a completed session.
		# v0.6.0: wait for the background jobs the profiled flow enqueued to
		# finish before gathering recordings so jobs a worker picks up
		# shortly after Stop aren't lost. Re-enqueues self (yielding the
		# worker) between checks; no-op when nothing's pending / the wait is
		# disabled / no async worker is available.
		bg_jobs_unfinished = _bg_wait_for_pending_jobs(session_uuid, docname, _bg_wait_until)
		if bg_jobs_unfinished is None:
			return  # re-enqueued this invocation is done

		# v0.7.x (M2): global single-flight only one session does the heavy,
		# memory-hungry analyze at a time so two stops can't stack to ~2× RAM
		# and OOM the box. If another session holds the flag we re-enqueue
		# ourselves and yield (skipped on the inline path). Acquired here, before
		# the big materialization below; heartbeated at the progress milestones;
		# released in the `finally`.
		if not _acquire_singleflight(session_uuid, docname, _singleflight_deadline):
			return  # re-enqueued this invocation is done

		# Phase: Analyzing
		if not _begin_analysis(docname):
			return  # A preceding delivery completed while this job was waiting.
		# v0.5.1: push "analyzing" to any open widgets on this user's
		# session. Without this the widget would either have to poll
		# status() to learn about the transition, or rely on the inline
		# path (which only applies when scheduler is disabled). Pushing
		# from the background analyze worker covers the enqueued path too.
		_publish_session_event(
			"optimus_session_analyzing",
			session_uuid=session_uuid,
			docname=docname,
		)
		_publish_progress(5, "Fetching recordings", session_uuid)

		recording_uuids = session.get_recordings(session_uuid)
		# v0.3.0: _fetch_recordings is now a generator. Materialize here
		# for the analyzer pipeline (which makes multiple passes), but
		# each recording's pyi_session is dropped after call_tree finishes
		# with it (handled in the analyzer itself).
		recordings = list(_fetch_recordings(recording_uuids))
		from optimus.recording_bundle import SnapshotUnavailable
		if set(recording_uuids) - {rec.get("uuid") for rec in recordings}:
			raise SnapshotUnavailable("snapshot_recording_missing")

		if not recordings:
			if session.get_session_meta(session_uuid) is None:
				raise SnapshotUnavailable("snapshot_session_missing")
			previous_queries = frappe.db.get_value("Optimus Session", docname, "total_queries")
			try:
				had_queries = float(previous_queries or 0) > 0
			except (TypeError, ValueError):
				had_queries = False
			if had_queries:
				raise SnapshotUnavailable("snapshot_inputs_missing")
			if not _with_active_analysis(docname, lambda: _finalize_with_empty_session(docname)):
				return
			if not _with_active_analysis(docname, lambda: _render_and_attach_reports(docname, [])):
				return
			if not _with_active_analysis(docname, lambda: frappe.db.set_value("Optimus Session", docname, "status", "Ready")):
				return
			_run_ai_step(lambda: session.delete_session_state(session_uuid), title="optimus ai empty recording cleanup", session_uuid=session_uuid)
			_publish_progress(100, "Complete (no data)", session_uuid)
			_run_ai_step(lambda: _publish_session_event("optimus_session_ready", session_uuid=session_uuid, docname=docname),
				title="optimus ai ready notification", session_uuid=session_uuid)
			return

		_touch_singleflight(session_uuid)  # M2 heartbeat before the EXPLAIN burst

		_publish_progress(20, "Running EXPLAIN on queries", session_uuid)
		enrichment_warnings = _enrich_recordings(recordings)

		context = AnalyzeContext(session_uuid=session_uuid, docname=docname)
		context.warnings.extend(enrichment_warnings)

		# v0.6.0: if we hit the wait cap with jobs still running, say so.
		if bg_jobs_unfinished:
			context.warnings.append(
				f"{bg_jobs_unfinished} RQ Job(s) the flow enqueued were still "
				"running when analysis started - they aren't included. Click Retry "
				"Analyze once they finish, or raise 'Wait for RQ Jobs' in "
				"Optimus Settings."
			)

		# Surface any caps the session hit during recording
		meta = session.get_session_meta(session_uuid) or {}
		if meta.get("cap_warning"):
			context.warnings.append(meta["cap_warning"])

		# v0.5.0: load the frontend metrics posted by optimus_frontend.js
		# for consumption by the frontend_timings analyzer. Reads from the
		# two atomic Redis lists written by api.submit_frontend_metrics
		# (v0.5.1+). Falls back to the pre-v0.5.1 single-blob format if
		# that's what's in Redis for upgrade safety on sessions
		# captured just before the update.
		try:
			from optimus import api as _api
			context.frontend_data = _api._read_frontend_data(session_uuid)
			if (
				not context.frontend_data.get("xhr")
				and not context.frontend_data.get("vitals")
			):
				legacy = frappe.cache.get_value(
					_redis_keys.frontend_legacy(session_uuid)
				)
				if legacy and isinstance(legacy, dict):
					context.frontend_data = legacy
		except Exception:
			context.frontend_data = {"xhr": [], "vitals": []}

		# v0.5.0: attach per-recording infra dicts from profiler:infra:<uuid>
		# so infra_pressure.analyze can read them as rec["infra"] without a
		# Redis hop inside the analyzer.
		for rec in recordings:
			rec_uuid = rec.get("uuid")
			if not rec_uuid:
				continue
			infra_blob = frappe.cache.get_value(_redis_keys.infra(rec_uuid))
			if infra_blob:
				rec["infra"] = infra_blob
			# Merge the resolved target-doc sidecar so the renderer can swap a "new-…" placeholder for the real doc name.
			resolved_doc = frappe.cache.get_value(_redis_keys.resolved_doc(rec_uuid))
			if resolved_doc:
				rec["resolved_target_doc"] = resolved_doc

		_publish_progress(50, "Running analyzers", session_uuid)
		analyzers = _get_analyzers()
		for i, analyzer in enumerate(analyzers):
			# v0.3.0: total wall-clock budget check. If we're over budget,
			# skip remaining analyzers and finalize partial.
			if time.monotonic() - analyze_start > ANALYZE_TOTAL_BUDGET_SECONDS:
				skipped = len(analyzers) - i
				context.warnings.append(
					f"Analyze partially completed (timeout) "
					f"{skipped} analyzer(s) skipped"
				)
				break

			# M2: heartbeat the single-flight flag each iteration. The analyzer
			# loop can run up to ANALYZE_TOTAL_BUDGET_SECONDS (1200s) well past
			# the flag's 300s TTL. Without this, the flag would lapse mid-analyze,
			# a second queued session could acquire it and two heavy analyzes
			# would run concurrently at ~2× peak RAM exactly the OOM the
			# single-flight guard exists to prevent. Redis-only, so it also keeps
			# is_singleflight_holder() truthy for the janitor's liveness check.
			_touch_singleflight(session_uuid)

			analyzer_name = getattr(analyzer, "__module__", "<unknown>")
			analyzer_start = time.monotonic()
			try:
				result = analyzer(recordings, context)
				context.merge(result)
			except Exception:
				context.warnings.append(f"Analyzer {analyzer_name} failed (see error log)")
				frappe.log_error(title=f"optimus analyzer {analyzer_name}")

			# v0.3.0: per-analyzer soft cap warning (logged, not fatal)
			analyzer_elapsed = time.monotonic() - analyzer_start
			if analyzer_elapsed > ANALYZE_PER_ANALYZER_SOFT_CAP_SECONDS:
				context.warnings.append(
					f"Analyzer {analyzer_name} took {analyzer_elapsed:.0f}s "
					f"(soft cap is {ANALYZE_PER_ANALYZER_SOFT_CAP_SECONDS}s)"
				)

			# Progress between 50-75% spread across analyzers
			pct = 50 + (25 * (i + 1) / len(analyzers))
			_publish_progress(pct, f"Ran {analyzer_name.split('.')[-1]}", session_uuid)

		# v0.7.x (M1): call_tree freed each recording's raw pyinstrument tree as
		# it consumed it. Force a collection now so that memory is returned to
		# the allocator BEFORE the persist + render phase (the next RAM peak),
		# instead of waiting for a generational GC cycle. Conf-gated so it can be
		# turned off if it ever shows up as latency on small sessions.
		if frappe.conf.get("optimus_analyze_gc_collect", True):
			import gc
			gc.collect()

		# How long did analyze take so far (before report rendering)?
		analyze_elapsed_ms = (time.monotonic() - analyze_start) * 1000

		# v0.6.0: attach ±1-line source snippets to each finding's callsite
		# before persisting, so finding cards can show the offending line
		# without requiring a per-render file read.
		owner = frappe.db.get_value("Optimus Session", docname, "owner")
		_persist_representative_callsites(context.findings, recordings, owner=owner)
		_enrich_findings_with_source_snippets(context.findings, owner=owner)

		_publish_progress(80, "Writing session data", session_uuid)
		_touch_singleflight(session_uuid)  # _persist's humanize call is capped below the TTL too
		if not _with_active_analysis(docname, lambda: _persist(docname, context, recordings, analyze_elapsed_ms)):
			return
		# Commit the complete recording snapshot before rendering and deleting Redis.
		if not _with_active_analysis(docname, lambda: _persist_recordings_file(docname, session_uuid, recording_uuids)):
			return

		_touch_singleflight(session_uuid)  # M2 heartbeat before the render phase
		_publish_progress(90, "Rendering reports", session_uuid)
		# Render and attach the HTML report to the DocType.
		# IMPORTANT: this must run BEFORE _cleanup_redis, because raw mode
		# reads raw SQL, headers, form_dict and full stack traces from the
		# in-memory recordings list (not from the DocType, which only has
		# normalized data).
		if not _with_active_analysis(docname, lambda: _render_and_attach_reports(docname, recordings)):
			return

		# Phase: Ready
		if not _with_active_analysis(docname, lambda: frappe.db.set_value("Optimus Session", docname, "status", "Ready")):
			return

	except Exception as exc:
		failure = exc
		frappe.db.rollback()
		changed = None
		try:
			from optimus.recording_bundle import SNAPSHOT_FAILURE_WARNING, SnapshotUnavailable
			snapshot_missing = isinstance(exc, SnapshotUnavailable)
			def fail():
				frappe.db.set_value("Optimus Session", docname, "status", "Failed")
				if snapshot_missing:
					frappe.db.set_value("Optimus Session", docname, "analyzer_warnings", SNAPSHOT_FAILURE_WARNING)
			changed = _with_active_analysis(docname, fail, statuses=_ANALYZE_PENDING)
		except Exception:
			pass
		if changed is False:
			completed_elsewhere = True
			if not isinstance(exc, safe_call.job_timeout_types()):
				failure = None
		# v0.5.1: push "failed" to any open widgets so they transition
		# out of "Analyzing…" immediately instead of hanging forever.
		# Best-effort and isolated so a publish failure can't mask the
		# original exception, logged and raised after this handler.
		if changed is not False:
			try:
				_publish_session_event(
					"optimus_session_failed",
					session_uuid=session_uuid,
					docname=docname,
				)
			except Exception:
				pass
	finally:
		# v0.7.x (M2): always release the single-flight flag so the next
		# session can analyze. Compare-then-delete (only if we still hold it);
		# a no-op on the early-return paths where we never acquired.
		_release_singleflight(session_uuid)
		# Round 2 fix #6: always clear the analyzing flag so subsequent
		# requests in the same worker process can profile normally.
		if hasattr(frappe.local, "optimus_analyzing"):
			del frappe.local.optimus_analyzing

	if failure is not None:
		_log_ai_step_failure("optimus ai analyze", failure, session_uuid=session_uuid)
		raise failure
	if completed_elsewhere:
		return

	# The report and Ready status are durable. Cleanup/notification outages
	# cannot demote the result or skip independent optional admission.
	_run_ai_step(lambda: _cleanup_redis(session_uuid, recording_uuids),
		title="optimus ai recording cleanup", session_uuid=session_uuid)
	_run_ai_step(lambda: _publish_progress(100, "Report ready", session_uuid),
		title="optimus ai ready progress", session_uuid=session_uuid)
	_run_ai_step(lambda: _publish_session_event("optimus_session_ready", session_uuid=session_uuid, docname=docname),
		title="optimus ai ready notification", session_uuid=session_uuid)
	# This is outside the profiling failure handler. Optional admission may
	# fail or time out, but the committed Ready result must stay usable.
	_auto_arm_phase2(docname, context)
	queue_analyze_time_ai(docname)


def _begin_analysis(docname):
	"""Recheck late deliveries in a fresh, short parent-lock transaction."""
	from optimus import sql_work
	def begin():
		status = frappe.db.get_value("Optimus Session", docname, "status", for_update=True)
		if status not in _ANALYZE_PENDING:
			return False
		frappe.db.set_value("Optimus Session", docname, "status", "Analyzing")
		return True
	return sql_work.retry(begin, framework=frappe,
		transaction=lambda operation: sql_work.transaction(operation, commit=safe_commit, rollback=frappe.db.rollback))


def _with_active_analysis(docname, operation, *, statuses=("Analyzing",)):
	"""Fence writes against a terminal result, even without Redis single-flight."""
	from optimus import sql_work
	def guarded():
		if frappe.db.get_value("Optimus Session", docname, "status", for_update=True) not in statuses:
			return False
		operation()
		return True
	# Persistence mutates its in-memory trees and registers rollback File cleanup.
	# A conflict must fail this delivery, preserving input for an explicit retry;
	# replaying this callback could persist a marker whose File was rolled back.
	sql_work.transaction(lambda: None, commit=safe_commit, rollback=frappe.db.rollback)
	return sql_work.transaction(guarded, commit=safe_commit, rollback=frappe.db.rollback)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def queue_analyze_time_ai(docname: str) -> None:
	"""Admit optional work only after the profiling result and report commit.

	Ordinary admission failures are logged; job interrupts still stop this
	worker. Neither path enters run's handler that marks profiling Failed.
	"""
	from optimus import ai_fix, ai_jobs
	from optimus.settings import get_config

	guard = safe_call.InterruptGuard(base=True)
	failure = None
	try:
		with guard:
			cfg = get_config()
			fixes = bool(cfg.ai_enabled and cfg.ai_auto_suggest and cfg.ai_suggest_findings)
			steps = bool(cfg.ai_enabled and cfg.ai_auto_suggest and cfg.ai_humanize_steps)
			if not (fixes or steps):
				return
			row = frappe.db.get_value("Optimus Session", docname, "*", as_dict=True)
			if not row or row.get("status") != "Ready":
				return
			out = ai_jobs.start_refresh(
				docname=docname, session_uuid=row["session_uuid"], requested_by=row["owner"],
				scope="fixes_missing", cap=cfg.ai_auto_suggest_max,
				include_fixes=fixes, include_steps=steps,
			)
			if out["status"] == "refused":
				ai_jobs.record_admission_notice(docname, reason=out.get("reason", "unknown"))
	except Exception as exc:
		failure = exc
	if guard.pending():
		raise guard.interrupt()
	if failure is not None:
		frappe.db.rollback()
		try:
			ai_fix.log_ai_failure("optimus ai admission after analyze", failure, docname=docname)
		finally:
			failure = None
		safe_commit()




def _run_ai_step(fn, *, title: str, session_uuid: str | None = None, **context):
	"""Run one AI step, ``fn()``, and return ``(result, failed)``; when it raises
	an ``Exception``, return ``(None, True)`` once the error is logged through
	``_log_ai_step_failure`` (``ai_fix.log_ai_failure``, with ``title``, the
	session and ``context`` as ``k=v`` lines), AFTER the ``try``: never
	inside the ``except`` block, where Sentry would ship the active
	exception's frame locals. The one capture-then-log skeleton of the AI
	steps of this module and ``optimus.api``. Callers receive only a boolean
	failure flag, so a later non-AI failure cannot log the exception's frames
	or text through their locals. Never raises, except an RQ job timeout, which ``log_ai_failure``
	raises again as a fresh instance so the job still stops. It references
	no ``ai_fix`` name itself, so ``run()`` can use it."""
	try:
		return fn(), False
	except Exception as e:
		error = e
	try:
		_log_ai_step_failure(title, error, session_uuid, **context)
	finally:
		error = None
	return None, True


def _log_ai_step_failure(title: str, exc: BaseException, session_uuid: str | None, **context) -> None:
	"""Log a failed AI step through ``ai_fix.log_ai_failure`` (``context`` as
	its ``k=v`` lines).

	``run()`` keeps plain ``frappe.log_error`` calls for its own non-AI
	failures, so it must not reference ``ai_fix`` names itself
	(``test_ai_log_audit.py`` treats a function that does as AI code). Call
	it outside any ``except`` block. Never raises, except an RQ job timeout:
	``log_ai_failure`` lets that through as a fresh instance so the job
	still stops, and ``run()``'s outer handler re-raises it."""
	from optimus.ai_fix import log_ai_failure

	log_ai_failure(title, exc, session_uuid=session_uuid, **context)


def _ai_selection(items, ai_fix, *, type_of, gate, cfg=None) -> tuple[list, int, int]:
	"""(eligible items, gated count, excluded count). ``cfg`` is the Optimus Settings
	object to read the exclusion list from (``None`` reads Settings). The operator's per-type exclusion
	runs first and counts only a type that could reach the AI; any other excluded type
	falls through to the eligibility gate, which refuses it anyway. Only types that can
	reach the AI and Framework N+1 count as gated; infrastructure types never do, and neither
	do index findings: every session has them, each shows Optimus's own advice, and counting
	them made the note fire on every session."""
	counted = ai_fix.AI_ELIGIBLE_FINDING_TYPES | {"Framework N+1"}
	eligible, gated, excluded = [], 0, 0
	for item in items:
		ftype = type_of(item)
		if ftype in ai_fix.AI_ELIGIBLE_FINDING_TYPES and ai_fix.is_finding_type_excluded(ftype, cfg=cfg):
			excluded += 1
			continue
		if gate(item) is not None:
			gated += int(ftype in counted)
			continue
		eligible.append(item)
	return eligible, gated, excluded


def _note_ai_selection(context, gated: int, excluded: int) -> None:
	"""Analyzer notes for the auto-suggest step's gated and excluded findings. Stored on
	the session and rendered into the report, so plain prose like the other warnings."""
	if gated:
		context.warnings.append(
			f"AI auto-suggest: {gated} finding(s) were not sent to the AI: the report shows "
			"Optimus's own advice or a note on each."
		)
	if excluded:
		context.warnings.append(
			f"AI auto-suggest: {excluded} finding(s) skipped because their type is listed under "
			"Excluded finding types in Optimus Settings."
		)


def _allow_unsigned_pickles() -> bool:
	"""Legacy Redis opt-in; signing-capable sites require verification by default."""
	from optimus.ai_fix import _InterruptGuard

	guard = _InterruptGuard(base=True)
	try:
		with guard:
			value = frappe.conf.get("optimus_allow_unsigned_pickles")
			if value is not None:
				return value is True or (type(value) is int and value == 1) or (
					isinstance(value, str) and value.lower() in {"1", "true"}
				)
			return not bool(frappe.conf.get("encryption_key"))
	except Exception:
		return False
	if guard.pending():
		raise guard.interrupt()


def _deserialize_tree(uuid: str, tree_blob, *, allow_unsigned: bool = False):
	"""Verify and unpickle a live Redis tree. Persisted bundles never use this.

	Only an explicit legacy override (or a site without a signing secret)
	permits the old raw/stripped-signature formats. Failure logs contain fixed
	reasons, never the blob or an active exception's locals.
	"""
	import pickle

	from optimus.ai_fix import _InterruptGuard, log_ai_failure
	from optimus.session import unsign_blob

	if not tree_blob:
		return None
	problem = None
	guard = _InterruptGuard(base=True)
	try:
		with guard:
			verified = unsign_blob(tree_blob)
			if verified is not None:
				return pickle.loads(verified)
			problem = "signature_verification_failed"
			if allow_unsigned and isinstance(tree_blob, (bytes, bytearray)):
				for payload in (bytes(tree_blob), bytes(tree_blob[32:])):
					try:
						with guard:
							result = pickle.loads(payload)
					except Exception:
						continue
					if guard.pending():
						break
					return result
				problem = "invalid_legacy_pickle"
	except Exception:
		problem = "invalid_tree"
	if guard.pending():
		tree_blob = verified = payload = None
		raise guard.interrupt()
	log_ai_failure("optimus ai recording tree load", reason=problem)
	return None


def _bundle_entries(bundle) -> dict:
	"""The ``uuid -> entry`` map of a persisted recordings bundle: the full bundle
	(``{"recordings": {...}}``) or the legacy bare map. ``{}`` for anything else. The one
	reader of the bundle's shape, shared by :func:`_rehydrate_from_bundle` and
	:func:`load_recordings_light`."""
	if not isinstance(bundle, dict):
		return {}
	entries = bundle.get("recordings")
	return entries if isinstance(entries, dict) else bundle


def _fetch_recordings(recording_uuids: list[str]):
	"""Stream recording dicts from Redis one at a time (a generator, so the
	pipeline can free each pyi_session between recordings instead of holding all
	in RAM).

	For each recording also loads the per-recording pyi tree pickle and sidecar
	log (best-effort; a missing piece yields None). Persisted JSON consumers
	use load_recordings_light instead, with no pickle or sidecar loading.
	"""
	allow_unsigned = _allow_unsigned_pickles()
	for uuid in recording_uuids:
		rec = frappe.cache.hget(RECORDER_REQUEST_HASH, uuid)
		if not rec:
			continue

		# Load the pyinstrument tree pickle (best-effort) via the shared,
		# HMAC-verified loader (see _deserialize_tree).
		from optimus.ai_fix import _InterruptGuard, log_ai_failure
		guard = _InterruptGuard(base=True)
		pyi_session = None
		failed = False
		try:
			with guard:
				tree_blob = frappe.cache.get_value(_redis_keys.tree(uuid))
				pyi_session = _deserialize_tree(uuid, tree_blob, allow_unsigned=allow_unsigned)
		except Exception:
			failed = True
		if guard.pending():
			tree_blob = None
			raise guard.interrupt()
		if failed:
			log_ai_failure("optimus ai recording tree read", reason="tree_read_failed")

		# Load the sidecar argument log (best-effort)
		sidecar = []
		failed = False
		try:
			with guard:
				loaded = frappe.cache.get_value(_redis_keys.sidecar(uuid))
				if isinstance(loaded, list):
					sidecar = loaded
		except Exception:
			failed = True
		if guard.pending():
			loaded = sidecar = rec = tree_blob = None
			raise guard.interrupt()
		if failed:
			log_ai_failure("optimus ai recording sidecar read", reason="sidecar_read_failed")

		rec["pyi_session"] = pyi_session
		rec["sidecar"] = sidecar
		strip_profiler_own_calls(rec)
		yield rec


# Per-recording enrichment caps. On a session that hit the 200-recording
# cap with 5000 queries each, running EXPLAIN on every query would be
# nearly a million DB calls and would exceed the RQ long-queue timeout.
# We cap at a reasonable upper bound and dedupe EXPLAIN by normalized
# query shape so we only EXPLAIN each distinct query once per session.
#
# v0.5.3: the cap is now configurable via
# ``Optimus Settings ▸ Max Queries per Recording``. The constant
# below is the fallback default used when settings can't be read.
MAX_QUERIES_ENRICHED_PER_RECORDING = 2000

# Cross-session EXPLAIN cache (Round 2 fix #12). On a stable schema, two
# consecutive analyze runs often see the same query shapes. Caching the
# EXPLAIN result for an hour lets the second run skip the DB roundtrip
# entirely. Override per site via:
#   site_config.json: optimus_explain_cache_ttl_seconds (default 3600)
# Set to 0 to disable the cross-session cache entirely (falls back to
# per-session dedup only).
DEFAULT_EXPLAIN_CACHE_TTL = 3600

# Hard cap on the in-memory EXPLAIN cache per analyze run. At ~500B per
# EXPLAIN row × N rows per query, 1000 entries is roughly 5-50 MB - a
# safe ceiling that still hits ~99% of duplicate-shape queries on a
# normal session. Overflow evicts the oldest entry (FIFO).
_EXPLAIN_CACHE_MAX_PER_ANALYZE = 1000


def _cap_explain_cache(cache: OrderedDict, key: str, value):
	"""Insert into the ordered EXPLAIN cache, evicting the oldest entry (FIFO)
	past the cap so a session with 10K+ unique queries doesn't balloon memory."""
	cache[key] = value
	if len(cache) > _EXPLAIN_CACHE_MAX_PER_ANALYZE:
		cache.popitem(last=False)


def _enrich_recordings(recordings: list[dict]) -> list[str]:
	"""Mirror frappe.recorder.post_process (sqlparse-format + EXPLAIN + mark
	duplicates) for this session's recordings only.

	Unlike the vanilla version, it doesn't touch every recording in
	RECORDER_REQUEST_HASH or start a read-only transaction that would block our
	DocType writes. Idempotent. Returns warning strings to surface in the report
	(e.g. queries truncated at the per-recording cap).
	"""
	# Flush any pending transaction state so EXPLAIN sees a consistent
	# snapshot (Round 2 fix #4). The caller commits right before invoking
	# us so this is usually a no-op, but it's a cheap defensive check
	# that protects against future refactors that might leave dirty
	# state in the transaction.
	try:
		safe_commit()
	except Exception:
		pass

	warnings: list[str] = []
	truncated_queries = 0
	total_queries_seen = 0   # for the truncation-banner percentage

	# v0.7.x (M6): optional throttle of the EXPLAIN/sqlparse burst so a long
	# flow doesn't hammer the DB/CPU continuously (and so GC gets breathing
	# room). Off by default (every=0) exact current behavior, zero overhead.
	# Counts ACTUAL EXPLAIN executions (not cache hits); sleeping changes only
	# timing, never which queries are EXPLAINed or any result.
	try:
		throttle_every = int(frappe.conf.get("optimus_enrich_throttle_every") or 0)
	except Exception:
		throttle_every = 0
	try:
		throttle_sleep_s = float(
			frappe.conf.get("optimus_enrich_throttle_sleep_ms") or 5
		) / 1000.0
	except Exception:
		throttle_sleep_s = 0.005
	explains_issued = 0
	# Cache EXPLAIN results by normalized query shape so we don't re-run
	# EXPLAIN on the same query hundreds of times within a single session.
	# This is the in-memory first tier; the second tier is the
	# cross-session frappe.cache lookup below. Bounded with FIFO
	# eviction at _EXPLAIN_CACHE_MAX_PER_ANALYZE to keep a 10K-unique-
	# query session from balloning memory.
	explain_cache: OrderedDict[str, list] = OrderedDict()

	# Cross-session EXPLAIN cache config
	cache_ttl = int(
		frappe.conf.get("optimus_explain_cache_ttl_seconds")
		if frappe.conf.get("optimus_explain_cache_ttl_seconds") is not None
		else DEFAULT_EXPLAIN_CACHE_TTL
	)
	use_shared_cache = cache_ttl > 0

	# v0.5.3: per-recording cap is admin-configurable. Fall back to
	# the hardcoded default if settings read fails for any reason
	# we must never let a settings hiccup starve the analyze pipeline.
	try:
		from optimus.settings import get_config
		cap = int(get_config().max_queries_per_recording)
	except Exception:
		cap = MAX_QUERIES_ENRICHED_PER_RECORDING
	# v0.13.x: 0 = no cap (Strict-as-unlimited semantics). Pre-v0.13.x
	# this fell back to MAX_QUERIES_ENRICHED_PER_RECORDING, silently
	# overriding the operator's "I want every query enriched" intent.
	# Negative values are still treated as a typo and fall back; only
	# exactly 0 means "unlimited".
	if cap < 0:
		cap = MAX_QUERIES_ENRICHED_PER_RECORDING

	for recording in recordings:
		# Normalize entries before enrichment and every downstream analyzer. A
		# malformed entry must neither abort the session nor consume its SQL cap.
		calls = [c for c in recording.get("calls") or [] if isinstance(c, dict)]
		recording["calls"] = calls
		total_queries_seen += len(calls)
		past_cap = []
		if cap and len(calls) > cap:
			truncated_queries += len(calls) - cap
			past_cap = calls[cap:]
			calls = calls[:cap]
		# Raw SQL before formatting, so past-cap calls compare like with like.
		enriched_raw = [c.get("query") or "" for c in calls]

		for call in calls:
			query = call.get("query", "")
			if not query:
				continue

			# sqlparse format (idempotent on already-formatted SQL)
			try:
				from optimus.sql_budget import within_budget

				if within_budget(query):
					call["query"] = sqlparse.format(
						query.strip(),
						keyword_case="upper",
						reindent=True,
						strip_comments=True,
					)
			except Exception:
				pass

			# Skip EXPLAIN if already populated
			if call.get("explain_result"):
				continue

			if not is_query_type(call["query"], ("select", "update", "delete")):
				call["explain_result"] = []
				continue

			# Check the original SQL before cache lookup or sqlparse's interpretation
			# can hide a declined compound query. The adapters enforce this too.
			from optimus.dbdialect.base import prepare_explain_query
			try:
				prepare_explain_query(query)
			except ValueError as exc:
				call["explain_result"] = []
				call["explain_warning"] = str(exc)
				if str(exc) not in warnings:
					warnings.append(str(exc))
				continue

			# Dedupe EXPLAIN by normalized query shape. The recorder
			# hasn't computed normalized_query yet (mark_duplicates runs
			# below), so compute a temporary shape key here.
			cache_key = _shape_key(call["query"])

			# First tier: in-memory cache for this analyze run
			if cache_key in explain_cache:
				call["explain_result"] = explain_cache[cache_key]
				continue

			# Second tier: cross-session frappe.cache with TTL. Two
			# analyze runs on a stable schema will hit this cache.
			# v0.12.17: explain_cache is the fourth value migrated to
			# the v0.12.0 versioned envelope. Reads handle both new
			# (wrapped) and legacy (bare-list) shapes via
			# ``unwrap_value``'s legacy-detection branch.
			shared_key = _redis_keys.explain_cache(cache_key)
			if use_shared_cache:
				raw_cached = frappe.cache.get_value(shared_key)
				cached, _version = _redis_schema.unwrap_value(raw_cached)
				if cached is not None:
					call["explain_result"] = cached
					_cap_explain_cache(explain_cache, cache_key, cached)
					continue

			try:
				# Route EXPLAIN through the dialect → normalized PlanTable
				# dicts (run_explain returns ok=False / empty tables on failure,
				# so the stored shape matches the old empty-list-on-error path).
				plan = get_dialect().run_explain(call["query"])
				if plan.reason:
					call["explain_warning"] = plan.reason
					if plan.reason not in warnings:
						warnings.append(plan.reason)
				result = [asdict(t) for t in plan.tables]
				call["explain_result"] = result
				_cap_explain_cache(explain_cache, cache_key, result)
				if use_shared_cache:
					try:
						frappe.cache.set_value(
							shared_key,
							_redis_schema.wrap_value(result),
							expires_in_sec=cache_ttl,
						)
					except Exception:
						pass  # shared cache is best-effort
			except Exception:
				call["explain_result"] = []
				_cap_explain_cache(explain_cache, cache_key, [])

			if "explain_result" not in call:
				call["explain_result"] = []

			# M6: we just issued a real EXPLAIN (cache hits / non-SELECTs
			# `continue` above and don't reach here). Yield periodically.
			explains_issued += 1
			if throttle_every and explains_issued % throttle_every == 0:
				time.sleep(throttle_sleep_s)

		# mark_duplicates adds normalized_query, exact_copies, normalized_copies, index
		try:
			if getattr(frappe.db, "db_type", None) == "postgres":
				for call in past_cap:
					call["past_enrich_cap"] = True
				_mark_postgres_duplicates(recording, calls, enriched_raw, past_cap)
			elif past_cap:
				_mark_duplicates_past_cap(recording, calls, enriched_raw, past_cap)
			else:
				mark_duplicates(recording)
		except Exception:
			pass

	if truncated_queries:
		pct = (
			round(truncated_queries / total_queries_seen * 100)
			if total_queries_seen else 0
		)
		# v0.5.3: the renderer reads this structured marker and shows
		# a prominent banner at the top of the report, in addition to
		# the Analyzer Notes entry. Users were missing truncation
		# warnings when they only appeared in the collapsed bottom
		# section "166 queries truncated" buried below stats cards
		# and findings led to developers reading an incomplete report
		# without noticing.
		warnings.append(
			f"⚠ TRUNCATED: {truncated_queries} queries "
			f"({pct}% of the flow) exceeded the {cap}-queries-per-"
			"recording enrichment cap and were analyzed without "
			"EXPLAIN or index lookups. They still count in query totals, N+1 and "
			"Top Queries, but not in index-suggestion / full-scan / "
			"filter-ratio findings. "
			"To get full coverage, raise "
			"<b>Optimus Settings ▸ Max Queries per Recording</b> "
			"(default 2000, try 5000-10000) and re-run the session "
			" OR profile a shorter flow."
		)
	return warnings


# Numeric literal forms supported by PostgreSQL, including v16 separators
# and nondecimal forms. Only tried at a numeric token boundary.
_PG_PLACEHOLDER = re.compile(r"\$[1-9][0-9]*")
_PG_NUMBER = re.compile(r"(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|(?:[0-9][0-9_]*(?:\.[0-9_]*)?|\.[0-9][0-9_]*)(?:[eE][+-]?[0-9][0-9_]*)?)")


def _postgres_literal_shape(query):
	"""Linear grouping lexer; preserve identifiers and never retain value tokens."""
	if not isinstance(query, str) or len(query) > ai_privacy.MAX_QUERY_CHARS or "\\" in query or "\x00" in query:
		return ""
	parts, index, size = [], 0, len(query)
	while index < size:
		char = query[index]
		if char in ("'", '"'):
			start, quote = index, char
			index += 1
			while True:
				end = query.find(quote, index)
				if end < 0:
					return ""
				index = end + 1
				if index < size and query[index] == quote:
					index += 1
					continue
				break
			parts.append(query[start:index] if quote == '"' else "?")
		elif query.startswith("/*", index) or query.startswith("--", index):
			block = query.startswith("/*", index)
			end = query.find("*/" if block else "\n", index + 2)
			if end < 0:
				if block:
					return ""
				end = size
			comment = query[index:end]
			if block and "/*" in comment[2:]:
				return ""  # Unsupported nested comments must not expose their tail.
			if "'" in comment or '"' in comment:
				return ""
			parts.append(" ")
			index = end + 2 if block else end
		elif char == "$":
			placeholder = _PG_PLACEHOLDER.match(query, index)
			if not placeholder:
				return ""  # Dollar strings need the full bounded parser.
			parts.append(placeholder.group())
			index = placeholder.end()
		elif (char.isascii() and char.isdigit() or char == "." and index + 1 < size and query[index + 1].isdigit()) and (
			index == 0 or not (query[index - 1].isalnum() or query[index - 1] in "_$")
		):
			number = _PG_NUMBER.match(query, index)
			if not number:
				return ""
			parts.append("?")
			index = number.end()
		else:
			parts.append(char.lower())
			index += 1
	return _collapse_pg_in_lists(re.sub(r"\s+", " ", "".join(parts)).strip())


def _collapse_pg_in_lists(query):
	chunks = re.split(r'("(?:[^"]|"")*")', query)
	for index in range(0, len(chunks), 2):
		chunks[index] = re.sub(r"\bin\s*\(\s*\?(?:\s*,\s*\?)*\s*\)", "in (?)", chunks[index], flags=re.I)
	return "".join(chunks)


def _mark_postgres_duplicates(recording, enriched, enriched_raw, past_cap):
	"""Keep PG schema identity, with parsing confined to the enrichment cap."""
	calls = recording.get("calls") or []
	raw = enriched_raw + [c.get("query") or "" for c in past_cap]
	normalized_cache = OrderedDict()
	for index, (call, query) in enumerate(zip(calls, raw, strict=True)):
		shape = _postgres_literal_shape(query)
		if shape and shape in normalized_cache:
			normalized = normalized_cache[shape]
		elif index < len(enriched):
			normalized = _collapse_pg_in_lists(ai_privacy.query_text(query, send_raw=False))
			if shape:
				normalized_cache[shape] = normalized
		else:
			normalized = shape
		# Distinct opaque omissions cannot manufacture duplicate-query findings.
		call["normalized_query"] = normalized or f"[SQL omitted: call {index + 1}]"
	exact = Counter(raw)
	normalized = Counter(c["normalized_query"] for c in calls)
	for index, (call, query) in enumerate(zip(calls, raw, strict=True)):
		call.update(index=index, exact_copies=exact[query], normalized_copies=normalized[call["normalized_query"]])


_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.)*\"|\b\d+(?:\.\d+)?\b")
_IN_LIST_RE = re.compile(r"\bin\s*\(\s*\?(?:\s*,\s*\?)*\s*\)")


def _literal_shape(query: str) -> str:
	"""Cheap literal-free key (regex only) so past-cap calls group without a sqlparse pass each."""
	key = re.sub(r"\s+", " ", _LITERAL_RE.sub("?", query)).strip().lower()
	return _IN_LIST_RE.sub("in (?)", key)


def _mark_duplicates_past_cap(recording: dict, enriched: list, enriched_raw: list, past_cap: list) -> None:
	"""mark_duplicates for a capped recording: full normalize for the enriched
	calls, then past-cap calls reuse the normalized form of their literal shape."""
	from frappe.recorder import normalize_query

	past_raw = [c.get("query") or "" for c in past_cap]
	mark_duplicates({"calls": enriched})

	normalized_by_shape: dict[str, str] = {}
	for call, q in zip(enriched, enriched_raw, strict=True):
		if call.get("normalized_query"):
			normalized_by_shape.setdefault(_literal_shape(q), call["normalized_query"])
	for call, q in zip(past_cap, past_raw, strict=True):
		call["past_enrich_cap"] = True
		shape = _literal_shape(q)
		if shape not in normalized_by_shape:
			# A shape the enriched calls never saw: pay one format + normalize for it.
			try:
				formatted = sqlparse.format(q.strip(), keyword_case="upper", reindent=True, strip_comments=True)
				normalized_by_shape[shape] = normalize_query(formatted)
			except Exception:
				normalized_by_shape[shape] = shape
		call["normalized_query"] = normalized_by_shape[shape]

	calls = enriched + past_cap
	raw = enriched_raw + past_raw
	recording["calls"] = calls
	exact = Counter(raw)
	normalized = Counter(c.get("normalized_query") for c in calls)
	for index, call in enumerate(calls):
		call["index"] = index
		call["exact_copies"] = exact[raw[index]]
		call["normalized_copies"] = normalized[call.get("normalized_query")]


def _shape_key(query: str) -> str:
	"""Cheap query-shape key for EXPLAIN dedup: lower-case, collapse whitespace,
	truncate. Not proper normalization (mark_duplicates does that later); just
	enough to tell whether two queries are shaped the same for caching."""
	import re

	return re.sub(r"\s+", " ", query.lower().strip())[:500]


# ---------------------------------------------------------------------------
# v0.3.0: call_tree_json overflow handling
# ---------------------------------------------------------------------------


def _apply_overflow_or_pass(
	tree_json: str,
	action_idx: int,
	docname: str,
	write_file=None,
	warnings_sink: list = None,
	hard_max_bytes: int = CALL_TREE_HARD_MAX_BYTES,
) -> tuple[str, str | None]:
	"""Decide whether to inline, overflow-to-file, or hard-truncate a tree blob.
	Returns ``(json_to_persist, overflow_file_url_or_None)``.

	Below CALL_TREE_OVERFLOW_THRESHOLD_BYTES: returned as-is. Between the
	threshold and hard_max_bytes: written via ``write_file`` to a private File,
	returning a marker JSON with the URL (hard-truncated on write failure). Above
	hard_max_bytes: hard-truncated immediately, no file.
	"""
	import json as _json

	size = len(tree_json)
	warnings = warnings_sink if warnings_sink is not None else []

	# Path 1: small enough to inline
	if size < CALL_TREE_OVERFLOW_THRESHOLD_BYTES:
		return tree_json, None

	# Path 2: hard sanity guard truncate immediately, never attempt file
	if size > hard_max_bytes:
		warnings.append(
			f"Action {action_idx}: tree exceeded hard guard "
			f"({size} > {hard_max_bytes}); hard-truncated"
		)
		return _hard_truncate_tree(tree_json), None

	# Path 3: try to write overflow file
	if write_file is None:
		warnings.append(
			f"Action {action_idx}: no overflow writer; tree hard-truncated"
		)
		return _hard_truncate_tree(tree_json), None

	filename = f"optimus_call_tree_{docname}_action_{action_idx}.json"
	try:
		url = write_file(filename, tree_json.encode("utf-8"))
		marker = _json.dumps({"_overflow": True, "url": url})
		return marker, url
	except Exception:
		warnings.append(
			f"Action {action_idx}: overflow file write failed; "
			"tree hard-truncated"
		)
		return _hard_truncate_tree(tree_json), None


def _hard_truncate_tree(tree_json: str) -> str:
	"""Build a top-100-frames truncated tree JSON as a fallback."""
	import json as _json

	try:
		tree = _json.loads(tree_json)
	except Exception:
		return _json.dumps({
			"_truncated": True, "_parse_failed": True,
			"function": "<root>", "children": [],
		})

	# Walk and collect the top-N frames by cumulative_ms
	all_nodes: list = []

	def collect(n):
		all_nodes.append(n)
		for c in n.get("children", []):
			collect(c)

	collect(tree)
	all_nodes.sort(key=lambda n: n.get("cumulative_ms", 0), reverse=True)
	kept = all_nodes[:CALL_TREE_HARD_TRUNCATE_KEEP_FRAMES]

	flat_children = [
		{
			"function": n.get("function"),
			"filename": n.get("filename"),
			"lineno": n.get("lineno"),
			"self_ms": n.get("self_ms", 0),
			"cumulative_ms": n.get("cumulative_ms", 0),
			"kind": n.get("kind", "python"),
			"children": [],
		}
		for n in kept
	]
	out = {
		"_truncated": True,
		# B.DI2 preserve the original frame count so the renderer can
		# show a "captured X frames, only top N shown" banner. Without
		# these, the truncation is invisible after persistence.
		"_captured_frames": len(all_nodes),
		"_kept_frames": len(kept),
		"function": "<root>",
		"filename": "",
		"lineno": 0,
		"self_ms": 0,
		"cumulative_ms": tree.get("cumulative_ms", 0),
		"kind": "python",
		"children": flat_children,
	}
	return _json.dumps(out, default=str)


def _session_query_totals(recordings: list[dict]) -> tuple[int, float]:
	"""``(query count, query time in ms)`` over every recorded call, leaving
	out the Error Log hook's own stored-key read (``is_error_log_hook_query``,
	the analyzers' rule for it): one ``__Auth`` SELECT per Error Log insert
	that is Optimus's, not the flow's."""
	count = 0
	time_ms = 0
	for r in recordings:
		for c in r.get("calls") or []:
			if not isinstance(c, dict) or is_error_log_hook_query(c.get("stack")):
				continue
			count += 1
			time_ms += c.get("duration", 0)
	return count, time_ms


def _set_background_job_rows(doc, session_uuid: str) -> None:
	"""One Optimus Background Job row per RQ job the flow enqueued, with its status.

	A job with no status or still "Queued" gets its terminal status from RQ first
	(a job RQ no longer knows is marked Stopped); a job with none at all is stored "Queued".
	"""
	try:
		for _jm in session.get_jobs(session_uuid):
			if _jm.get("status") in (None, "", "Queued"):
				_capture_job_terminal_status(session_uuid, _jm["job_id"])
		doc.set("background_jobs", [])
		for _jm in session.get_jobs(session_uuid):
			try:
				doc.append("background_jobs", {
					# method / job_id are Data(140) and come from external sources
					# (the enqueue call, RQ): cap them so an unusually long value
					# can't raise a "Data too long" insert error and sink the whole
					# report save (the datetimes are already DB-safe via _rq_dt_to_db).
					"job_id": (_jm.get("job_id") or "")[:140],
					"method": (_jm.get("method") or "")[:140],
					"status": _jm.get("status") or "Queued",
					"error": (_jm.get("error") or "") or None,
					# DB-safety guard: strip any tz offset a pre-fix build may have
					# stored in Redis, so MariaDB's DATETIME column accepts them.
					"enqueued_at": _db_datetime_str(_jm.get("enqueued_at")),
					"started_at": _db_datetime_str(_jm.get("started_at")),
					"ended_at": _db_datetime_str(_jm.get("ended_at")),
					"duration_ms": _jm.get("duration_ms") or 0,
					"recording_uuid": _jm.get("recording_uuid") or "",
					"parent_recording_uuid": (_jm.get("parent_recording_uuid") or "")[:140],
				})
			except Exception:
				frappe.log_error(title=f"optimus persist background_job row {_jm.get('job_id')}"[:140])
	except Exception:
		frappe.log_error(title="optimus persist background_jobs")


def _persist(
	docname: str,
	context: AnalyzeContext,
	recordings: list[dict],
	analyze_elapsed_ms: float = 0,
) -> None:
	"""Write the analyzed data into the Optimus Session DocType row."""
	total_requests = len(recordings)
	total_queries, total_query_time_ms = _session_query_totals(recordings)
	total_duration_ms = sum(r.get("duration", 0) for r in recordings)

	doc = frappe.get_doc("Optimus Session", docname)

	# v0.5.1: auto-fill "Steps to Reproduce" from the captured actions,
	# but ONLY if the user hasn't already written notes on the form. The
	# start dialog no longer prompts for notes (it added friction and
	# most users just skipped it), so the common path is: field is
	# empty → we populate it here. Power users who typed something into
	# the notes field on the doc form between start and stop get their
	# text left alone.
	generated_notes = False
	doc.auto_notes_sha256 = None
	if not (doc.notes or "").strip():
		notes_html = _build_auto_notes_html(recordings)
		if notes_html:
			doc.notes = notes_html
			generated_notes = True

	doc.total_requests = total_requests
	doc.total_queries = total_queries
	doc.total_query_time_ms = round(total_query_time_ms, 2)
	doc.total_duration_ms = round(total_duration_ms, 2)
	doc.analyze_duration_ms = round(analyze_elapsed_ms, 2)
	doc.summary_html = _build_summary_html(context, total_queries, recordings)
	doc.top_queries_json = json.dumps(
		context.aggregate.get("top_queries", []), default=str
	)
	doc.table_breakdown_json = json.dumps(
		context.aggregate.get("table_breakdown", []), default=str
	)
	# v0.5.0: infra_pressure and frontend_timings aggregates. Capped to
	# prevent unbounded growth on pathological 200-recording sessions
	# without caps, v5_aggregate_json could balloon to 1 MB+ and slow
	# the form load for the Optimus Session record. Truncation is
	# tail-preferring (keep the last N entries) with a warning surfaced
	# in analyzer_warnings so operators can see the drop.
	V5_INFRA_TIMELINE_CAP = 200
	V5_FRONTEND_XHR_CAP = 500
	V5_FRONTEND_ORPHANS_CAP = 100

	infra_timeline = context.aggregate.get("infra_timeline", [])
	if len(infra_timeline) > V5_INFRA_TIMELINE_CAP:
		context.warnings.append(
			f"v5_aggregate: infra_timeline truncated from "
			f"{len(infra_timeline)} to {V5_INFRA_TIMELINE_CAP} entries (tail kept)"
		)
		infra_timeline = infra_timeline[-V5_INFRA_TIMELINE_CAP:]

	frontend_xhr = context.aggregate.get("frontend_xhr_matched", [])
	if len(frontend_xhr) > V5_FRONTEND_XHR_CAP:
		context.warnings.append(
			f"v5_aggregate: frontend_xhr_matched truncated from "
			f"{len(frontend_xhr)} to {V5_FRONTEND_XHR_CAP} entries (tail kept)"
		)
		frontend_xhr = frontend_xhr[-V5_FRONTEND_XHR_CAP:]

	frontend_orphans = context.aggregate.get("frontend_orphans", [])
	if len(frontend_orphans) > V5_FRONTEND_ORPHANS_CAP:
		context.warnings.append(
			f"v5_aggregate: frontend_orphans truncated from "
			f"{len(frontend_orphans)} to {V5_FRONTEND_ORPHANS_CAP} entries"
		)
		frontend_orphans = frontend_orphans[-V5_FRONTEND_ORPHANS_CAP:]

	doc.v5_aggregate_json = json.dumps(
		{
			"infra_timeline": infra_timeline,
			"infra_summary": context.aggregate.get("infra_summary", {}),
			"frontend_xhr_matched": frontend_xhr,
			"frontend_vitals_by_page": context.aggregate.get("frontend_vitals_by_page", {}),
			"frontend_orphans": frontend_orphans,
			"frontend_summary": context.aggregate.get("frontend_summary", {}),
		},
		default=str,
	)
	# v0.3.0: call_tree analyzer outputs
	doc.hot_frames_json = json.dumps(
		context.aggregate.get("hot_frames", []), default=str
	)
	doc.session_time_breakdown_json = json.dumps(
		context.aggregate.get("session_time_breakdown", {}), default=str
	)
	doc.total_python_ms = round(context.aggregate.get("total_python_ms", 0), 2)
	doc.total_sql_ms = round(context.aggregate.get("total_sql_ms", 0), 2)
	doc.analyzer_warnings = "\n".join(context.warnings) if context.warnings else None

	# v0.3.0: apply overflow / hard-truncate to each action's call_tree_json.
	def _writer(filename, content, _docname=docname):
		file_doc = frappe.get_doc({
			"doctype": "File",
			"file_name": filename,
			"attached_to_doctype": "Optimus Session",
			"attached_to_name": _docname,
			"is_private": 1,
			"content": content,
		})
		file_doc.insert(ignore_permissions=True)
		return file_doc.file_url

	for action in context.actions:
		tree_json = action.get("call_tree_json")
		if not tree_json:
			continue
		new_json, overflow_url = _apply_overflow_or_pass(
			tree_json,
			action_idx=action.get("idx", 0),
			docname=docname,
			write_file=_writer,
			warnings_sink=context.warnings,
		)
		action["call_tree_json"] = new_json
		action["call_tree_size_bytes"] = len(new_json)
		action["call_tree_overflow_file"] = overflow_url

	# Reset and re-populate child tables (in case of re-run)
	doc.set("actions", [])
	for action in context.actions:
		doc.append("actions", action)

	# v0.5.1: safety net for Optimus Finding.title's 140-char Frappe
	# limit. Individual analyzers already shorten filenames in titles
	# (see n_plus_one + call_tree), but pathological inputs unusual
	# function names, very high occurrence counts, unexpected formats
	# from future analyzers can still push past the limit and crash
	# the whole persist with CharacterLengthExceededError. We clamp
	# here so a single too-long title never destroys the entire
	# analyze run.
	# v0.7.x: collapse per-action duplicates of the same code path
	# (Slow Hot Path / Hook Bottleneck / Self-Time Hot Path / Repeated
	# Hot Frame). Runs BEFORE truncation so the dominant title is what
	# gets trimmed.
	_dedupe_findings_across_actions(context.findings, context.actions)
	_truncate_finding_titles(context.findings)

	doc.set("findings", [])
	for finding in context.findings:
		doc.append("findings", finding)
	doc.top_severity = _compute_top_severity(doc.findings)

	_set_background_job_rows(doc, context.session_uuid)

	from optimus.managed import save_session

	save_session(doc)
	if generated_notes:
		from optimus.auto_steps import notes_hash
		# Hash the actual saved representation, after Frappe sanitizes it.
		saved_notes = frappe.db.get_value("Optimus Session", docname, "notes")
		frappe.db.set_value("Optimus Session", docname, "auto_notes_sha256", notes_hash(saved_notes), update_modified=False)
	safe_commit()


# v0.7.x: per-action analyzers can emit the same (code-path) finding
# once per action that exercises it. A recording with N actions
# touching the same hot function used to produce N near-identical
# cards (same drill-down, same Phase 2 callout, same AI fix). We
# collapse them into a single card here.
_DEDUPE_FINDING_TYPES = frozenset({
	"Slow Hot Path",
	"Hook Bottleneck",
	"Self-Time Hot Path",
	"Repeated Hot Frame",
})


def _dedupe_findings_across_actions(
	findings: list[dict],
	actions: list[dict] | None,
) -> None:
	"""Collapse per-action duplicates of the same code path in place.

	Groups the ``_DEDUPE_FINDING_TYPES`` findings by ``(finding_type, filename,
	lineno, function)``. In each group of 2+, the highest-impact finding becomes
	dominant; the others are dropped after folding their action context into its
	``technical_detail_json`` (``merged_action_refs`` etc.) and appending an
	"Also affects ..." note. The dominant's ``estimated_impact_ms`` becomes the
	group sum and ``affected_count`` the merged count; severity / title /
	action_ref / drilldown_chain stay unchanged. Other finding types pass
	through untouched.
	"""
	if not findings:
		return

	# Build action_ref → label lookup for the "Also affects" sentence.
	action_label_by_ref: dict[str, str] = {}
	for a in (actions or []):
		if not isinstance(a, dict):
			continue
		idx = a.get("idx")
		label = a.get("action_label") or a.get("path") or ""
		if idx is not None and label:
			action_label_by_ref[str(idx)] = label

	# Bucket by dedup key. Findings outside the dedupe set get a
	# unique passthrough key so they never collide.
	buckets: dict[tuple, list[int]] = {}
	passthrough_counter = 0
	keyed_indices: list[tuple[tuple, int]] = []

	for i, f in enumerate(findings):
		ftype = f.get("finding_type") or ""
		if ftype not in _DEDUPE_FINDING_TYPES:
			# Stable unique key so this finding passes through unchanged.
			passthrough_counter += 1
			key = ("__passthrough__", passthrough_counter)
			keyed_indices.append((key, i))
			continue
		try:
			detail = json.loads(f.get("technical_detail_json") or "{}")
		except (TypeError, ValueError):
			detail = {}
		filename = detail.get("filename") or ""
		lineno = detail.get("lineno")
		function = detail.get("function") or ""
		# Findings missing any of the key parts pass through (can't
		# group what we can't identify).
		if not filename or lineno is None or not function:
			passthrough_counter += 1
			key = ("__passthrough__", passthrough_counter)
		else:
			key = (ftype, filename, int(lineno) if isinstance(lineno, int | float) else lineno, function)
		keyed_indices.append((key, i))

	for key, i in keyed_indices:
		buckets.setdefault(key, []).append(i)

	# For each group with 2+ entries: pick dominant, fold others in,
	# remove dropped indices from `findings` at the end.
	to_drop: set[int] = set()
	for _key, indices in buckets.items():
		if len(indices) < 2:
			continue
		# Sort by impact desc; the head is the dominant.
		indices_sorted = sorted(
			indices,
			key=lambda i: -(findings[i].get("estimated_impact_ms") or 0),
		)
		dom_idx = indices_sorted[0]
		dom = findings[dom_idx]
		merged_impacts: list[dict] = []
		merged_refs: list[str] = []
		merged_labels: list[str] = []
		total_impact = 0.0
		for i in indices_sorted:
			f = findings[i]
			ms = float(f.get("estimated_impact_ms") or 0)
			total_impact += ms
			ref = str(f.get("action_ref") or "")
			label = action_label_by_ref.get(ref, "")
			merged_refs.append(ref)
			merged_labels.append(label)
			merged_impacts.append({"action_ref": ref, "label": label, "ms": round(ms, 2)})
			if i != dom_idx:
				to_drop.add(i)

		# Mutate dominant in place.
		dom["estimated_impact_ms"] = round(total_impact, 2)
		dom["affected_count"] = len(indices_sorted)
		try:
			dom_detail = json.loads(dom.get("technical_detail_json") or "{}")
		except (TypeError, ValueError):
			dom_detail = {}
		dom_detail["merged_count"] = len(indices_sorted)
		dom_detail["merged_action_refs"] = merged_refs
		dom_detail["merged_action_labels"] = merged_labels
		dom_detail["merged_impact_ms"] = merged_impacts
		dom["technical_detail_json"] = json.dumps(dom_detail, default=str)

		# Append "Also affects …" note to customer_description.
		other_entries = [e for e in merged_impacts[1:] if e.get("label") or e.get("ms")]
		if other_entries:
			if all(e.get("label") for e in other_entries):
				bits = ", ".join(
					f"**{e['label']}** ({dur(e['ms'])})" for e in other_entries
				)
				suffix = (
					f" Also affects {len(other_entries)} other "
					f"action{'s' if len(other_entries) != 1 else ''}: {bits}."
				)
			else:
				# Labels missing generic count.
				suffix = (
					f" Also affects {len(other_entries)} other "
					f"action{'s' if len(other_entries) != 1 else ''}."
				)
			existing = dom.get("customer_description") or ""
			dom["customer_description"] = existing.rstrip() + "\n\n" + suffix.lstrip()

	if to_drop:
		# Compact the list in place. Iterating backwards so we don't
		# shift indices we still need to drop.
		for i in sorted(to_drop, reverse=True):
			findings.pop(i)


# Optimus Finding.title is a Frappe Data field VARCHAR(140). Titles
# that exceed this length raise CharacterLengthExceededError at save
# time, taking down the whole analyze pipeline. We clamp in-place as a
# safety net: 137 visible chars + the "..." ellipsis marker fits the
# limit exactly and the full information remains in the finding's
# technical_detail_json for navigation.
_FINDING_TITLE_MAX_CHARS = 140
_FINDING_TITLE_ELLIPSIS = "..."
# A duration token in a title ("5234ms", with the dur() marker or, for a legacy
# pre-marker title, without it). Used only to keep title truncation from slicing
# through one: a cut anywhere in the token drops it whole rather than leaving a
# bare "…523" or a unit-less "…5234m" that can't be rolled over at render.
_DUR_IN_TITLE_RE = re.compile(r"\d+(?:\.\d+)?ms" + _DUR_SEP + "?")


def _truncate_finding_titles(findings: list[dict]) -> None:
	"""Clamp every finding's title to <= _FINDING_TITLE_MAX_CHARS chars, in place.
	A defense-in-depth guard against an over-long title crashing the whole
	persist (Data field is VARCHAR(140)); the full info stays in
	technical_detail_json."""
	for finding in findings:
		title = finding.get("title") or ""
		if len(title) > _FINDING_TITLE_MAX_CHARS:
			keep = _FINDING_TITLE_MAX_CHARS - len(_FINDING_TITLE_ELLIPSIS)
			# Never slice through a duration token: a mid-number cut ("...523") or a
			# unit-less remnant ("...5234m") can't be rolled over at render (the
			# marker pass needs the whole token and the prose backstop needs the
			# "<n>ms" intact). If a "<n>ms" straddles the cut, drop it whole instead
			# (the badge and description still carry the number).
			cut = keep
			for _m in _DUR_IN_TITLE_RE.finditer(title):
				if _m.start() < keep < _m.end():
					cut = _m.start()
					break
			finding["title"] = title[:cut].rstrip() + _FINDING_TITLE_ELLIPSIS


# v0.6.0: ±1-line source snippet attached to each finding's callsite so
# the report's finding card can show the actual offending line without the
# reader having to jump into the codebase. Truncate per-line so a single
# multi-kilobyte minified string can't blow the technical_detail_json blob.
_FINDING_SNIPPET_TRUNCATE_CHARS = 200


def _persist_representative_callsites(findings, recordings, *, owner=None):
	"""Keep SQL findings actionable independently of the recording File lifecycle."""
	from optimus.renderer.finding_enrichment import _attach_representative_callsites
	from optimus.renderer.source import server_script_readers
	views = []
	originals = []
	for finding in findings:
		try:
			detail = json.loads(finding.get("technical_detail_json") or "{}")
		except (ValueError, TypeError):
			continue
		if isinstance(detail, dict):
			views.append({**finding, "technical_detail": detail})
			originals.append(finding)
	with server_script_readers(owner):
		_attach_representative_callsites(views, recordings)
	for finding, view in zip(originals, views, strict=True):
		finding["technical_detail_json"] = json.dumps(view["technical_detail"], default=str)


def _enrich_findings_with_source_snippets(findings: list[dict], *, owner: str | None = None) -> None:
	"""Mutate findings in place: attach a source snippet to each finding whose
	technical_detail.callsite resolves to a readable file.

	Best-effort: missing files, decode errors, out-of-range linenos and
	malformed JSON all yield no snippet (the renderer skips the block). Files are
	cached per call so clustered findings read each file once.
	"""
	file_cache: dict[str, list[str] | None] = {}

	for finding in findings:
		if not isinstance(finding, dict):
			# Defence-in-depth: the public surface is typed as list[dict]
			# but a corrupted upstream payload (legacy session, partial
			# migration) could plant a non-dict entry. Silent skip matches
			# the function's best-effort contract.
			continue
		raw = finding.get("technical_detail_json")
		if not raw:
			continue
		try:
			detail = json.loads(raw)
		except Exception:
			continue
		if not isinstance(detail, dict):
			# Valid JSON that isn't an object e.g. a stray "string", a
			# bare ``null``, or a top-level list left by a stale persistence
			# path. ``.get()`` below would AttributeError; silent skip.
			continue
		callsite = detail.get("callsite") or {}
		if not isinstance(callsite, dict):
			# Slow Query findings (optimus/analyzers/top_queries.py) store
			# callsite as a "path:lineno" string, not the canonical dict
			# shape every other finding type uses. Skip analyze-time
			# enrichment here renderer._normalize_callsite converts the
			# string at render time and renderer._finding_to_dict attaches
			# the snippet lazily, so no functionality is lost.
			continue
		if not callsite.get("filename"):
			# call_tree (Slow Hot Path / Hook Bottleneck / Repeated Hot Frame)
			# and line_profile (Hot Line) store the location at the top level
			# synthesize a callsite dict so the snippet lands where
			# renderer._finding_to_dict expects it (and stop _finding_to_dict
			# from having to re-synthesize at render time).
			fname = detail.get("filename") or detail.get("file")
			if fname and detail.get("lineno") is not None:
				callsite = {
					"filename": fname,
					"lineno": detail.get("lineno"),
					"function": detail.get("function") or "",
				}
				detail["callsite"] = callsite
				finding["technical_detail_json"] = json.dumps(detail, default=str)
		filename = callsite.get("filename")
		try:
			lineno = int(callsite.get("lineno"))
		except (TypeError, ValueError):
			continue
		if not filename or lineno <= 0 or callsite.get("source_snippet"):
			continue
		# renderer._read_source_snippet resolves app-relative callsite paths
		# (e.g. "ugly_code/python/common.py") to real files; a bare open()
		# would fail because the worker cwd is <bench>/sites.
		from optimus.renderer.source import server_script_readers

		with server_script_readers(owner):
			snippet = renderer._read_source_snippet(filename, lineno, cache=file_cache)
		if not snippet:
			continue
		callsite["source_snippet"] = snippet
		detail["callsite"] = callsite
		finding["technical_detail_json"] = json.dumps(detail, default=str)




@ai_privacy.sql_redaction_scope()
def ai_payload_for_finding(
	child,
	file_cache: dict,
	*,
	phase2_index: dict | None = None,
	recordings_by_uuid: dict | None = None,
	actions_by_idx: dict | None = None,
	evidence_lookup=None,
	tracked_apps: tuple[str, ...] = (),
) -> dict:
	"""Build the dict ``ai_fix.suggest_fix`` expects from a finding-like object
	(an ``Optimus Finding`` child row or a ``SimpleNamespace`` shaped like one).

	It's the renderer's normalized finding dict plus a wider source-code window
	around the callsite, plus (when a Phase-2 pass instrumented the finding's
	function) its hottest line via ``phase2_index``
	(``{(basename, function): hotline}``). When ``recordings_by_uuid`` +
	``actions_by_idx`` are given and the finding has an ``action_ref``, the top-N
	slowest queries from that action's recording are attached as
	``technical_detail.example_queries`` (verbatim SQL evidence), unless already
	set by a SQL red-flag analyzer. A Slow Query also carries the deterministic
	index advice (``index_advice``) when ``evidence_lookup`` is given."""
	payload = renderer._finding_to_dict(child, file_cache=file_cache)
	callsite = (payload.get("technical_detail") or {}).get("callsite") or {}
	if callsite.get("filename") and callsite.get("lineno") is not None:
		grounding = _ai_grounding_window(callsite["filename"], callsite["lineno"], file_cache)
		if grounding is not None:
			payload["source_window"] = grounding.rows
			if (payload.get("finding_type") or "") in ai_grounding.LOOP_FACT_TYPES:
				# Facts come from the WHOLE file's tree, so a loop outside the window
				# is still seen; the prompt formats only those about shown lines.
				payload["loop_facts"] = safe_call.best_effort(
					lambda: ai_grounding.loop_facts_from_tree(
						grounding.tree, int(callsite["lineno"]), parent=grounding.parent,
					), {},
					on_error=lambda kind: safe_call.log_error_line(f"optimus: AI loop facts failed: {kind}"),
				)

	fn = (callsite.get("function") or "").strip()
	fname = (callsite.get("filename") or "").strip()
	if phase2_index and fn and fname:
		from optimus.server_script_source import extract_script_name, get_server_script_record

		base = fname.replace("\\", "/").rsplit("/", 1)[-1]
		script = extract_script_name(fname)
		# A stored Phase 2 snapshot must not restore source denied by the renderer.
		allowed = not script or get_server_script_record(script, cache=file_cache) is not None
		hot = phase2_index.get((base, fn)) if allowed else None
		if isinstance(hot, dict) and hot.get("lineno") is not None:
			payload["phase2_hotline"] = {
				"lineno": hot.get("lineno"),
				"content": hot.get("content") or "",
				"total_ms": hot.get("total_ms") or 0,
				"hits": hot.get("hits") or 0,
			}

	if (payload.get("finding_type") or "") == "Slow Query" and evidence_lookup is not None:
		_attach_index_advice(payload, evidence_lookup, tracked_apps)

	_maybe_attach_recorded_queries(
		payload,
		action_ref=getattr(child, "action_ref", None) or (child.get("action_ref") if isinstance(child, dict) else None),
		recordings_by_uuid=recordings_by_uuid,
		actions_by_idx=actions_by_idx,
	)
	return payload


def _attach_index_advice(payload: dict, evidence_lookup, tracked_apps: tuple[str, ...]) -> None:
	"""``payload["index_advice"]`` from the same advisor the report uses."""
	from optimus.renderer import index_recipes

	advice = safe_call.best_effort(
		lambda: index_recipes.advise_finding(payload, evidence_lookup=evidence_lookup, tracked_apps=tracked_apps),
		None,
		on_error=lambda kind: safe_call.log_error_line(f"optimus: AI index advice failed: {kind}"),
	)
	if advice is not None:
		payload["index_advice"] = {
			"route": advice.route,
			"doctype": advice.doctype,
			"columns": list(advice.columns),
			"text": index_recipes.finding_text(advice, install=False),
		}


def ai_evidence_scope() -> tuple:
	"""``(evidence_lookup, tracked_apps)`` for one AI run's Slow Query advice."""
	from optimus.renderer import recipe_enrichment
	from optimus.settings import read_tracked_apps

	return recipe_enrichment.make_evidence_lookup(), read_tracked_apps()


_AI_AST_MEMO_MAX = 4  # whole-file trees kept per run


def _ai_grounding_window(filename: str, lineno, file_cache: dict) -> ai_grounding.GroundingWindow | None:
	"""The prompt's source window (the enclosing function when it fits 80 lines, else 24
	lines either side) plus the whole file's parsed tree, or None when the source cannot
	be read. A job timeout escapes as a fresh timeout."""
	from optimus import ai_fix
	from optimus.renderer import source as _source

	guard = safe_call.InterruptGuard()
	grounding = None
	try:
		with guard:
			lines = _source._source_lines(filename, cache=file_cache)
			if lines and not isinstance(lineno, bool):
				# The whole-file tree and parent map are built once per file per run. A
				# small LRU keeps only the last few files' trees; an entry is valid only for
				# the very list of lines it was parsed from.
				memo_key = ("optimus_ast",)
				memo = file_cache[memo_key] if memo_key in file_cache else None
				if memo is None:
					memo = file_cache[memo_key] = OrderedDict()
				entry = memo.get(filename)
				if entry is None or entry[0] is not lines:
					entry = memo[filename] = (lines, *ai_grounding.parse_source(lines))
				memo.move_to_end(filename)
				while len(memo) > _AI_AST_MEMO_MAX:
					memo.popitem(last=False)
				grounding = ai_grounding.grounding_window(
					lines, int(lineno), ai_fix._SOURCE_LINES_BEFORE, ai_fix._SOURCE_LINES_AFTER,
					max_lines=ai_fix._MAX_SOURCE_WINDOW_LINES, max_line_chars=_source._SNIPPET_TRUNCATE_CHARS,
					parsed=(entry[1], entry[2]),
				)
	except Exception:
		grounding = None
	if guard.pending():
		raise guard.interrupt()
	return grounding if grounding is not None and grounding.rows else None


_AI_EXAMPLE_QUERIES_MAX = 3
_AI_EXAMPLE_QUERY_MIN_MS = 0.5  # drop sub-half-ms queries (cache hits, etc.)


def _maybe_attach_recorded_queries(
	payload: dict,
	*,
	action_ref,
	recordings_by_uuid: dict | None,
	actions_by_idx: dict | None,
	send_raw: bool | None = None,
) -> None:
	"""When recordings + actions are available and the finding has an
	``action_ref``, attach the top-N slowest SQL queries from that action's
	recording to ``payload.technical_detail.example_queries`` (best-effort).
	Skipped when inputs are missing or ``example_queries`` is already set by a
	SQL red-flag analyzer (those are the most relevant; don't overwrite)."""
	if not recordings_by_uuid or not actions_by_idx or action_ref in (None, ""):
		return
	try:
		idx = int(action_ref)
	except (TypeError, ValueError):
		return
	action = actions_by_idx.get(idx)
	if not action:
		return
	recording_uuid = action.get("recording_uuid") if isinstance(action, dict) else getattr(action, "recording_uuid", None)
	if not recording_uuid:
		return
	recording = recordings_by_uuid.get(recording_uuid)
	if not recording:
		return
	calls = recording.get("calls") if isinstance(recording, dict) else None
	if not calls:
		return
	detail = payload.setdefault("technical_detail", {}) or {}
	if detail.get("example_queries"):
		# Analyzer (SQL red flag) set these; respect they're the most relevant.
		return
	from optimus import ai_privacy

	send_raw = ai_privacy.raw_values_enabled() if send_raw is None else send_raw
	top = []
	seen_queries = set()
	for c in sorted(calls, key=lambda c: -(c.get("duration") or c.get("duration_ms") or 0)):
		dur_ms = c.get("duration") or c.get("duration_ms") or 0
		if dur_ms < _AI_EXAMPLE_QUERY_MIN_MS:
			continue
		# Old PG normalized fields may already have lost identifiers. Always
		# redact from the original when available; query_text owns the boundary.
		raw_query = c.get("query") or c.get("normalized_query")
		if not isinstance(raw_query, str) or raw_query in seen_queries:
			continue
		seen_queries.add(raw_query)
		q = ai_privacy.query_text(raw_query, send_raw=send_raw)
		if not q or q in top:
			continue
		top.append(q)
		if len(top) >= _AI_EXAMPLE_QUERIES_MAX:
			break
	if top:
		detail["example_queries"] = top
		payload["technical_detail"] = detail


def phase2_index_for(doc_or_docname) -> dict:
	"""``renderer._build_line_drilldown_callsite_index`` for a session doc or
	docname. An ordinary failure writes one Error Log row, with the session
	(``_run_ai_step``), and uses empty context; job timeouts stop the worker."""
	docname = doc_or_docname if isinstance(doc_or_docname, str) else getattr(doc_or_docname, "name", None)

	def _build() -> dict:
		doc = doc_or_docname
		if isinstance(doc, str):
			doc = frappe.get_doc("Optimus Session", doc)
		return renderer._build_line_drilldown_callsite_index(doc) or {}

	index, _failed = _run_ai_step(_build, title="optimus ai phase 2 context", docname=docname)
	return index or {}


_SESSION_COUNTER_FIELDS = frozenset({"ai_tokens_spent", "ai_refresh_count"})
# The columns a counter matches the session on: ``name`` for a caller that knows the docname,
# ``session_uuid`` for ai_fix's per-call recorder, which only has the worker's spend marker.
_SESSION_COUNTER_KEYS = frozenset({"name", "session_uuid"})
_COUNTER_SAVEPOINT = "optimus_ai_counter"


def _session_increment_query(key: str, fieldname: str, n: int, *, by: str = "name"):
	"""``UPDATE tabOptimus Session SET f = COALESCE(f, 0) + n WHERE <by> = key``: one portable
	(MariaDB and Postgres) statement, never a stale read-modify-write, ``modified`` untouched.
	``by`` is ``"name"`` or ``"session_uuid"``. Another field or column, an empty key or a count
	that is not a non-negative int raises ValueError."""
	if (
		fieldname not in _SESSION_COUNTER_FIELDS or by not in _SESSION_COUNTER_KEYS
		or type(n) is not int or n < 0 or not isinstance(key, str) or not key
	):
		raise ValueError("Invalid session counter increment")
	from frappe.query_builder.functions import Coalesce

	table = frappe.qb.DocType("Optimus Session")
	field = table[fieldname]
	return frappe.qb.update(table).set(field, Coalesce(field, 0) + n).where(table[by] == key)


def _increment_session_counter(
	key: str, fieldname: str, n: int, *, by: str = "name", title: str = "optimus ai counter",
) -> None:
	"""Add ``n`` to one session counter in the caller's transaction, inside a savepoint.

	It never commits: the increment commits or rolls back with the caller's own writes. A count
	of 0 or an empty ``key`` is a no-op that runs no SQL. When the UPDATE fails, only the counter
	is rolled back (to the savepoint), the failure is logged once through
	``ai_fix.log_ai_failure`` after the ``try`` (``title``, the session, the field and the amount)
	and nothing is raised, so the billed answer the caller is about to save is kept. When the
	rollback to the savepoint fails too, the transaction is already gone (a MariaDB deadlock rolls
	it back whole) and the caller's writes with it: the counter error is logged and raised again.
	An RQ job timeout always leaves as a fresh instance. The savepoint is released after a
	successful UPDATE.

	A statement-level failure (a serialization failure 40001, a lock wait timeout 1205, and on
	Postgres a deadlock too) leaves the savepoint usable, so it is absorbed here: ``_add_ai_spend``
	never raises it. The trade-off (owner decision D2): a transient lock error on the counter drops
	that call's spend, and the Error Log row is all that records it. Only an error that has already
	rolled back the whole transaction reaches the caller: a MariaDB deadlock (1213), or 1020
	("Record has changed since last read"), which MariaDB 11.8 raises by default
	(``innodb_snapshot_isolation``) when this transaction's read view is older than another
	transaction's commit of the session row, and which also ends the whole transaction. Frappe
	raises both as ``frappe.QueryDeadlockError``.

	Rules for a caller that records spend itself (``_add_ai_spend``: ``_write_ai_answer`` for
	Refresh AI suggestions, and the background refresh engine):

	- never call the AI provider inside the transaction that holds the counter: the UPDATE keeps
	  the session row locked until that transaction ends;
	- write in one short transaction: this counter first, then the Finding row, then commit (the
	  parent row before the child, the order ``Document.save`` locks them in);
	- retry only that short transaction, after a full rollback, on ``frappe.QueryDeadlockError``
	  (1213 or 1020 on MariaDB, a deadlock on Postgres) from the counter, the Finding write or the
	  commit; never retry the provider call."""
	if not key or not n:
		return
	query = _session_increment_query(key, fieldname, n, by=by)
	from optimus.ai_fix import log_ai_failure

	error = None
	guard = safe_call.InterruptGuard()
	try:
		with guard:
			frappe.db.savepoint(_COUNTER_SAVEPOINT)
			query.run()
			frappe.db.release_savepoint(_COUNTER_SAVEPOINT)
	except Exception as exc:
		error = exc
	if guard.pending():
		raise guard.interrupt()
	if error is None:
		return
	rollback_error = None
	guard = safe_call.InterruptGuard()
	try:
		with guard:
			frappe.db.rollback(save_point=_COUNTER_SAVEPOINT)
	except Exception as exc:
		rollback_error = exc
	if guard.pending():
		error = rollback_error = None
		raise guard.interrupt()
	reference = {"docname": key} if by == "name" else {"session_uuid": key}
	log_ai_failure(
		title, error, **reference, field=fieldname, amount=n,
		savepoint_rollback="done" if rollback_error is None else "failed: " + type(rollback_error).__name__,
	)
	if rollback_error is not None:
		raise error


def _add_ai_spend(docname: str, tokens) -> None:
	"""Add one AI call's ``tokens`` (a count, or a usage dict's ``total_tokens``) to the session's
	cumulative ``ai_tokens_spent``, in the caller's transaction (``_increment_session_counter``).

	Only for a caller that attributed its calls explicitly (``suggest_fix`` or ``humanize_steps``
	with ``session_uuid`` or ``docname``): ai_fix records nothing for those calls, so the caller
	adds each call's tokens here exactly once, from the result's ``tokens`` or from a failure's
	``usage``. A call without explicit attribution is already charged to the worker's ambient
	session by ``ai_fix._record_session_spend``; adding it here too would count it twice. A count
	that is not a non-negative int (``ai_fix._token_count``) or no ``docname`` is a no-op."""
	from optimus.ai_fix import _token_count

	if isinstance(tokens, dict):
		tokens = tokens.get("total_tokens")
	_increment_session_counter(docname, "ai_tokens_spent", _token_count(tokens), title="optimus ai spend")


def _bump_ai_refresh_count(docname: str) -> None:
	"""Count one AI refresh on the session (``ai_refresh_count`` + 1), atomically, in the caller's
	transaction (``_increment_session_counter``). No-op without a ``docname``. A caller commits
	the bump on its own, before the refresh calls the provider, so the bump itself does not hold
	the session row during the refresh (``api.refill_ai_suggestions``). The refresh then locks
	the row only inside each answer's short write (``_write_ai_answer``), never across a
	provider call."""
	_increment_session_counter(docname, "ai_refresh_count", 1, title="optimus ai refresh count")


def _keep_session_counters(doc) -> None:
	"""Keep the stored AI counters on ``doc`` before a session save writes its row.

	``_increment_session_counter`` adds to ``ai_tokens_spent`` and ``ai_refresh_count`` in SQL
	without touching ``modified``, so a document loaded before an increment would write the old
	counts back on save (every analyze lost its Steps to Reproduce tokens this way), and Frappe's
	modified check cannot notice. The stored values come from the copy ``Document.save`` has just
	read with ``for_update=True`` (``check_if_latest``, Frappe v15 and v16), so no increment can
	land between that read and the write. The Optimus Session controller calls this from
	``before_validate``, which runs on every save (``ignore_validate`` included), so every save
	path keeps them: analyze, Phase 2, the Desk form. A new document keeps its own values."""
	previous = doc.get_doc_before_save()
	if previous is None:
		return
	for fieldname in _SESSION_COUNTER_FIELDS:
		setattr(doc, fieldname, getattr(previous, fieldname, None))


def _system_timezone_name() -> str | None:
	"""The site's System Settings timezone through Frappe's own helper
	(``frappe.utils.get_system_timezone``; v15 and v16 both have it), or ``None`` when it
	cannot be read. A job timeout raised while reading it escapes fresh."""
	name = None
	guard = safe_call.InterruptGuard()
	try:
		with guard:
			from frappe.utils import get_system_timezone

			name = str(get_system_timezone() or "") or None
	except Exception:
		name = None
	if guard.pending():
		raise guard.interrupt()
	return name


def _system_zone():
	"""The site's timezone as a ``tzinfo``. When it cannot be read or is not a known zone,
	UTC, with one line in the bench log (written after the ``try``, never inside an ``except``)."""
	from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

	zone = None
	name = _system_timezone_name()
	if name:
		try:
			zone = ZoneInfo(name)
		except (ZoneInfoNotFoundError, ValueError, OSError):
			zone = None
	if zone is None:
		safe_call.log_error_line(
			"optimus: the System Settings timezone could not be read, so a naive requested_at is read as UTC"
		)
		return timezone.utc
	return zone


def _requested_cutoff(requested_at) -> float:
	"""The resume cutoff, as a POSIX timestamp, for the ``requested_at`` of
	:func:`eligible_findings`; ``0.0`` (no cutoff) for ``None`` or an empty string.

	A ``datetime`` or an ISO 8601 string (``Z`` and numeric offsets accepted). An aware value
	keeps its own offset. A NAIVE value is read in the site's System Settings timezone, which
	is how Frappe stores a Datetime field (a ``requested_at`` read from a document, or built
	with ``frappe.utils.now_datetime()``). Any other type (``date``, a number, bytes) raises
	``TypeError`` and an unreadable string raises ``ValueError``: a request time that
	silently became "no cutoff" would bill every answer again.

	A naive time inside the repeated hour when daylight saving ends resolves to its first
	occurrence, so the cutoff can be up to an hour early. That only skips an answer generated
	in that hour; it never asks again for one."""
	if requested_at is None or requested_at == "":
		return 0.0
	if isinstance(requested_at, str):
		try:
			parsed = datetime.fromisoformat(requested_at.strip().replace("Z", "+00:00"))
		except ValueError:
			raise ValueError(f"requested_at is not an ISO 8601 date and time: {requested_at!r}") from None
	elif isinstance(requested_at, datetime):
		parsed = requested_at
	else:
		raise TypeError(f"requested_at must be a datetime or an ISO 8601 string, not {type(requested_at).__name__}")
	if parsed.tzinfo is None:
		parsed = parsed.replace(tzinfo=_system_zone())
	try:
		return parsed.timestamp()
	except (OverflowError, OSError, ValueError):
		raise ValueError(f"requested_at is out of range: {requested_at!r}") from None


def _generated_timestamp(fix) -> float:
	"""When a stored answer was made, as a POSIX timestamp. ``suggest_fix`` writes an aware
	UTC time; a legacy NAIVE time is UTC too (not the system timezone). ``0.0`` when it is
	absent or unreadable, which sorts the answer as the oldest and never skips it."""
	value = fix.get("generated_at") if isinstance(fix, dict) else None
	if not isinstance(value, (str, datetime)):
		return 0.0
	try:
		parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
		return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
	except (ValueError, OverflowError, OSError):
		return 0.0


def _impact_ms(row) -> float:
	"""A finding's impact for sorting: ``0.0`` when it is absent, not a number or not finite."""
	try:
		impact = float(row_get(row, "estimated_impact_ms", 0) or 0)
	except (TypeError, ValueError, OverflowError):
		return 0.0
	return impact if math.isfinite(impact) else 0.0


_FIX_STATE_RANK = {
	ai_prompts.FIX_MISSING: 0, ai_prompts.FIX_INCOMPLETE: 1,
	ai_prompts.FIX_OUTDATED: 2, ai_prompts.FIX_CURRENT: 3,
	ai_prompts.FIX_BLOCKED: 4, ai_prompts.FIX_PROCESSING_LIMITED: 5,
}


class AiSelection(list):
	"""What :func:`eligible_findings` returns: the chosen rows, in order, as a plain list,
	plus ``gated`` and ``excluded``, the counts :func:`_ai_selection` found among ALL the rows
	given (not only the chosen ones). ``gated`` counts the AI-eligible findings (and Framework
	N+1) the report answers with Optimus's own advice or a note; ``excluded`` counts the
	AI-eligible findings whose type Optimus Settings excludes. Read ``.gated`` and ``.excluded``
	BEFORE slicing, sorting or filtering the result: those operations return a plain ``list``
	without them."""

	gated: int = 0
	excluded: int = 0


def eligible_findings(rows, cfg, *, regenerate_all=False, requested_at=None, include_outdated=True) -> AiSelection:
	"""The findings an AI refresh should (re)ask about, in the order to ask, each as the
	original child row, plus the gated and excluded counts (:class:`AiSelection`).

	Both Refresh paths use it (``_run_ai_backfill`` today, the background refresh later), so
	they cannot disagree. Rows are first narrowed by :func:`_ai_selection` (the Excluded
	finding types in ``cfg``, then ``ai_fix.llm_gate_note``); ``cfg=None`` reads Optimus
	Settings. Then each row's stored answer is classified by ``ai_prompts.fix_state``, the
	one definition shared with the report:

	- ``missing`` (nothing stored) is always chosen;
	- ``incomplete`` (a cut-off answer) is chosen when ``include_outdated`` or
	  ``regenerate_all``, so automatic missing-only runs never retry it;
	- ``outdated`` (unparseable, an older or non-integer ``prompt_version``, an error record or
	  no suggestion text; NOT a newer version, NOT a non-truncated guardrail fallback) is chosen when
	  ``include_outdated`` or ``regenerate_all``;
	- ``current`` is chosen only when ``regenerate_all``.

	``requested_at`` (see :func:`_requested_cutoff`) resumes an interrupted run: a stored
	answer generated at or after it is skipped, missing rows never are.

	Order: missing, incomplete, outdated, current; inside each, severity (High first; a missing
	or unknown severity last), then the larger ``estimated_impact_ms``, then the older answer.
	``_run_ai_backfill`` asks in exactly this order: it takes the list as returned, only cut to
	its cap."""
	from optimus import ai_fix

	cutoff = _requested_cutoff(requested_at)
	inputs = {}
	def gate_input(row):
		key = id(row)
		if key not in inputs:
			inputs[key] = ai_fix.gate_input(row)
		return inputs[key]
	eligible, gated, excluded = _ai_selection(
		rows or (), ai_fix,
		type_of=lambda r: gate_input(r)["finding_type"],
		gate=lambda r: ai_fix.llm_gate_note(gate_input(r)),
		cfg=cfg,
	)
	chosen = []
	for row in eligible:
		state, fix = ai_prompts.fix_state(row_get(row, "llm_fix_json"))
		if state in (ai_prompts.FIX_CURRENT, ai_prompts.FIX_BLOCKED, ai_prompts.FIX_PROCESSING_LIMITED) and not regenerate_all:
			continue
		if state in (ai_prompts.FIX_INCOMPLETE, ai_prompts.FIX_OUTDATED) and not (include_outdated or regenerate_all):
			continue
		generated = _generated_timestamp(fix)
		if state != ai_prompts.FIX_MISSING and cutoff and generated >= cutoff:
			continue
		chosen.append((_refresh_order_key(row, state, generated), row))
	chosen.sort(key=lambda pair: pair[0])
	out = AiSelection(row for _, row in chosen)
	out.gated, out.excluded = gated, excluded
	return out


def _refresh_order_key(row, state: str, generated: float) -> tuple:
	"""The order Refresh asks in: state (missing, incomplete, outdated, current), severity, larger impact,
	older answer."""
	severity = row_get(row, "severity")
	# A blank severity ranks as Low, like the report and the analyze-time order; anything else
	# unrecognised goes last.
	if severity is None or severity == "":
		severity = "Low"
	rank = SEVERITY_ORDER.get(severity, UNKNOWN_SEVERITY_RANK) if isinstance(severity, str) else UNKNOWN_SEVERITY_RANK
	return (_FIX_STATE_RANK[state], rank, -_impact_ms(row), generated)


def action_recording_map(actions) -> dict:
	"""``action_ref`` is a zero-based position, unlike Frappe's one-based idx."""
	return {i: {"recording_uuid": row_get(action, "recording_uuid")} for i, action in enumerate(actions or ())}


def _light_recording_map(doc) -> dict:
	"""``{uuid: rec}`` of the session's persisted bundle, with nothing else kept: the parsed
	bundle (96% trees and sidecars) is a local of this function, so it is freed on return."""
	entries = _bundle_entries(_load_recordings_bundle(doc))
	return {
		uuid: entry["rec"]
		for uuid, entry in entries.items()
		if isinstance(entry, dict) and isinstance(entry.get("rec"), dict)
	}


def load_recordings_light(doc, uuids=None, *, memo=None) -> list[dict]:
	"""Load only recorder JSON, without deserializing trees or sidecars.

	A caller-owned ``memo`` dict reads the persisted bundle at most once per
	session/file, however many calls share it (a refresh's slices, the Steps call and the
	re-render). The memo keeps only ``{uuid: rec}``, never the parsed bundle, so it holds
	about the size of the recordings and not the trees. The result copies each recording
	without its ``pyi_session``, ``sidecar`` and ``tree_b64``. An explicitly empty UUID list
	requests no recordings. Missing or malformed records are omitted; Redis errors fall
	back to the bundle.
	"""
	if uuids is None:
		uuids = [row_get(action, "recording_uuid") for action in (row_get(doc, "actions") or ())]
	uuids = list(dict.fromkeys(uuid for uuid in uuids if isinstance(uuid, str) and uuid))
	if not uuids:
		return []
	memo = {} if memo is None else memo
	memo_key = ("recordings", row_get(doc, "name"), row_get(doc, "session_uuid"), row_get(doc, "recordings_file"))
	result = []
	redis_failed = False
	session_uuid = row_get(doc, "session_uuid")
	for uuid in uuids:
		rec = None
		if not redis_failed:
			# a failed read is logged once (after the try) and the rest come from the bundle
			rec, redis_failed = _run_ai_step(
				lambda: frappe.cache.hget(RECORDER_REQUEST_HASH, uuid),
				title="optimus ai recording cache read", session_uuid=session_uuid,
			)
		if not isinstance(rec, dict) or not rec:
			if memo_key not in memo:
				memo[memo_key] = _light_recording_map(doc)
			rec = memo[memo_key].get(uuid)
		if isinstance(rec, dict) and rec:
			light = {key: value for key, value in rec.items() if key not in {"pyi_session", "sidecar", "tree_b64"}}
			strip_profiler_own_calls(light)
			result.append(light)
	return result


# How many times one answer's short write (its spend, the Finding row, the commit) is tried in all
# when the database rolled the whole transaction back (``frappe.QueryDeadlockError``).
AI_WRITE_ATTEMPTS = 3


def _whole_transaction_rolled_back(exc) -> bool:
	"""True when ``exc`` is ``frappe.QueryDeadlockError``: the database ended the whole transaction
	(MariaDB: a deadlock, 1213, or 1020 "Record has changed since last read" under
	``innodb_snapshot_isolation``, on by default since 11.8; Postgres: a deadlock), so a short write
	can start again from a rollback. Frappe raises it for those codes on v15 and v16."""
	deadlock = getattr(frappe, "QueryDeadlockError", None)
	return isinstance(deadlock, type) and isinstance(exc, deadlock)


# v0.5.1: auto-generated "Steps to Reproduce" from captured actions. The
# dialog no longer asks the user to type notes at start time because (a) it
# added friction to the one-click "start profiling" flow and (b) the user
# already performed the steps the profiler captured them. We synthesize
# a bullet list from the recordings and write it to the `notes` field ONLY
# when the user hasn't already provided their own text via the DocType
# form. The developer can then edit the auto-generated list to add
# business context (what the user was *trying* to do, not what endpoint
# was hit) before sharing the report. The template still runs notes_html
# through sanitize_html(always_sanitize=True), so any HTML we emit is
# re-sanitized at render time but we still escape labels here because
# the stored value is also what appears when someone edits the doc.
_AUTO_NOTES_MAX_ENTRIES = 50
_AUTO_NOTES_PREAMBLE = (
	"<p><em>Auto-generated from captured actions. Edit to add business "
	"context (what you were trying to accomplish, any steps taken before "
	"recording started, expected vs. actual behavior).</em></p>"
)

# v0.5.1: recordings whose cmd or path matches any of these is
# considered background/polling noise and excluded from the Steps to
# Reproduce list. These ARE still shown in the per-action table (the
# full picture), they just don't belong in a human-readable reproducer.
#
# Driven by a real user report whose reproducer read:
#
#   GET /api/method/frappe.realtime.has_permission 25 ms
#   POST /api/method/frappe.desk.form.save.savedocs 774.8 ms
#   GET /api/method/frappe.realtime.has_permission 6 ms
#
# Of those three, only the savedocs is a user action. The two
# has_permission entries are the Desk polling for realtime
# subscription permissions and should be filtered.
_REPRODUCER_NOISE_CMD_PREFIXES = (
	# Desk polling / realtime subscription permission checks. Fire
	# 2-3x per second while the Desk has a doctype page open.
	"frappe.realtime.",
	# Form-metadata loading issued on every form open. Useful in the
	# per-action table for timing but clutters the reproducer
	# "Load Sales Invoice form" says nothing about user intent.
	"frappe.desk.form.load.getdoctype",
	"frappe.desk.form.load.getdocinfo",
	# Background list counters.
	"frappe.client.get_count",
	# Frappe's internal doctype hooks endpoint
	"frappe.desk.notifications.get_open_count",
	# Build / reload assets
	"frappe.core.doctype.system_settings.system_settings.load",
)

_REPRODUCER_NOISE_PATH_PREFIXES = (
	# Static asset requests
	"/assets/",
	"/favicon",
	# Frappe's built-in recorder desk page (not a user action)
	"/app/recorder",
)


def _is_reproducer_noise(rec: dict) -> bool:
	"""True when a recording is background/polling noise that shouldn't appear in
	the auto-notes reproducer list. It still appears in the per-action
	breakdown."""
	cmd = (rec.get("cmd") or "").strip()
	if cmd:
		for prefix in _REPRODUCER_NOISE_CMD_PREFIXES:
			if cmd.startswith(prefix):
				return True
	path = (rec.get("path") or "").strip()
	if path:
		for prefix in _REPRODUCER_NOISE_PATH_PREFIXES:
			if path.startswith(prefix):
				return True
	return False


def _recordings_for_reproducer(recordings: list[dict]) -> list[dict]:
	"""The signal (non-noise) recordings, in order. See
	``_is_reproducer_noise``."""
	return [r for r in (recordings or []) if not _is_reproducer_noise(r)]


def _build_auto_notes_list_html(recordings: list[dict]) -> str:
	"""The ordered-list body of the "Steps to Reproduce" note (no preamble): an
	``<ol>`` of humanized action labels with timings, plus a "N background
	requests filtered" footer. Returns "" when there are no signal recordings.
	Labels come from ``per_action.humanized_label`` and are HTML-escaped."""
	if not recordings:
		return ""
	signal_recordings = _recordings_for_reproducer(recordings)
	if not signal_recordings:
		return ""

	items: list[str] = []
	for rec in signal_recordings[:_AUTO_NOTES_MAX_ENTRIES]:
		label = per_action.humanized_label(rec) or "(unnamed action)"
		duration_ms = round(rec.get("duration") or 0, 1)
		# dur() marks the timing for render-time formatting (plain digits, so a
		# >=1e6 ms step never bakes as "5e+06").
		items.append(f"<li>{html.escape(label)}: {dur(duration_ms, 1)}</li>")

	overflow = len(signal_recordings) - _AUTO_NOTES_MAX_ENTRIES
	if overflow > 0:
		items.append(f"<li><em>… and {overflow} more action(s) not shown.</em></li>")

	noise_count = len(recordings) - len(signal_recordings)
	footer = ""
	if noise_count > 0:
		footer = (
			f"<p class='muted' style='color:#6b7280;font-size:0.85rem'>"
			f"{noise_count} background / polling request(s) filtered "
			"out (permission checks, form-metadata loads, static assets)."
			"</p>"
		)
	return "<ol>" + "".join(items) + "</ol>" + footer


def _build_auto_notes_html(recordings: list[dict]) -> str:
	"""Auto-generated "Steps to Reproduce" preamble + the raw labelled action
	list. The fallback when AI humanizing is off or fails. Returns "" when
	there's nothing to list, so the caller leaves ``doc.notes`` empty."""
	body = _build_auto_notes_list_html(recordings)
	if not body:
		return ""
	return _AUTO_NOTES_PREAMBLE + body


# v0.6.0: LLM-humanized "Steps to Reproduce" just the friendly narrative.
# (The raw labelled action list isn't appended; the per-action breakdown in
# the report already shows every action with its technical label + timing.)
_HUMANIZED_NOTES_PREAMBLE = (
	"<p><em>Steps to Reproduce drafted by AI from the captured actions. "
	"Edit to add business context (what you were trying to accomplish, any "
	"steps taken before recording started, expected vs. actual behavior).</em></p>"
)


def actions_for_humanizer(recordings: list[dict], *, send_raw: bool | None = None) -> list[dict]:
	"""Compact per-action dicts (label / cmd / path / method / doctype /
	duration_ms) for ``ai_fix.humanize_steps``, noise-filtered and capped like
	the raw auto-notes list."""
	from optimus import ai_privacy

	send_raw = ai_privacy.raw_values_enabled() if send_raw is None else send_raw
	out: list[dict] = []
	for original in recordings or []:
		rec = ai_privacy.action_recording(original)
		if rec is None or _is_reproducer_noise(rec):
			continue
		if len(out) >= _AUTO_NOTES_MAX_ENTRIES:
			break
		fd = rec["form_dict"]
		doctype, is_new = per_action._extract_doc_info(fd)
		doctype = doctype or fd.get("dt") or fd.get("doc_type") or ""
		out.append({
			"label": per_action.humanized_label(rec) or "",
			"cmd": (rec.get("cmd") or "").strip(),
			"path": (rec.get("path") or "").strip(),
			"method": (rec.get("method") or "").strip(),
			"doctype": doctype,
			"operation": fd.get("action"),
			"is_new": is_new,
			"duration_ms": round(rec.get("duration") or 0, 1),
		})
	return out if send_raw else [ai_privacy.private_action(action) for action in out]


def assemble_humanized_notes(steps_markdown: str) -> str:
	"""The HTML stored in ``doc.notes`` for an AI-humanized "Steps to Reproduce":
	the preamble plus the LLM's Markdown steps, rendered and sanitized. No raw
	captured-actions appendix (the per-action breakdown already lists them)."""
	return _HUMANIZED_NOTES_PREAMBLE + renderer._markdown_to_safe_html(steps_markdown)




def _compute_top_severity(findings: list) -> str:
	"""Highest severity among the finding rows the report shows ("High"/"Medium"/"Low", else
	"None"). Populated per session so the list view can show a "Top Severity"
	column without loading child rows."""
	if not findings:
		return "None"
	file_cache: dict = {}
	severities = {
		f.severity
		for f in findings
		if renderer._has_renderable_callsite(renderer._finding_to_dict(f, file_cache=file_cache))
	}
	for level in ("High", "Medium", "Low"):
		if level in severities:
			return level
	return "None"


_PRIORITY_WORD = {"High": "high", "Medium": "medium", "Low": "low"}


def _humanize_action_label(action: dict, recordings: list[dict]) -> str:
	"""Plain-English label for an action ("Submit Sales Invoice" rather than
	"frappe.desk.form.save.savedocs:Submit"), via ``per_action.humanized_label``
	on the recording (looked up by uuid). Falls back to the raw ``action_label``
	when the recording isn't available."""
	raw = str(action.get("action_label") or "?")
	uid = action.get("recording_uuid")
	if uid:
		rec = next((r for r in recordings if r.get("uuid") == uid), None)
		if rec:
			try:
				h = per_action.humanized_label(rec)
				if h and h not in ("?", ""):
					return h
			except Exception:
				pass
	return raw


def _build_summary_html(
	context: AnalyzeContext, total_queries: int, recordings: list[dict] | None = None
) -> str:
	"""Plain-language customer summary (an HTML ``<ul>``) built from the analyzer
	findings, written for a non-developer: humanized action names, "operations"
	and "high priority" wording, raw ``cmd:action`` refs swapped for readable
	forms."""
	recordings = recordings or []
	n_actions = len(context.actions)
	findings = context.findings
	high = sum(1 for f in findings if f.get("severity") == "High")
	medium = sum(1 for f in findings if f.get("severity") == "Medium")
	low = sum(1 for f in findings if f.get("severity") == "Low")

	# v0.7.x: emit each summary point as a bullet (joined into one <ul>
	# at the end of the function). Each ``parts`` entry is the bullet's
	# inner HTML (no <li> wrapper here added in the final join).
	parts = [
		f"This session covered <strong>{n_actions} operation"
		f"{'s' if n_actions != 1 else ''}</strong> (page loads, saves and "
		f"RQ Jobs) with <strong>{total_queries} database "
		f"quer{'ies' if total_queries != 1 else 'y'}</strong>."
	]

	if context.actions:
		slowest_idx = max(
			range(len(context.actions)),
			key=lambda i: context.actions[i].get("duration_ms", 0),
		)
		slowest = context.actions[slowest_idx]
		slowest_ms = slowest.get("duration_ms", 0)
		slowest_label = _humanize_action_label(slowest, recordings)
		slowest_label_esc = html.escape(slowest_label)

		# raw cmd / "Job: x" label  ->  plain-English label, for every action,
		# so finding titles that reference an action read cleanly too.
		label_map = {
			str(a.get("action_label") or ""): _humanize_action_label(a, recordings)
			for a in context.actions if a.get("action_label")
		}

		def _finding_phrase(f: dict) -> str:
			"""Finding title with raw action references swapped for plain labels (a
			leading "In <slowest>, " trimmed since the sentence already names it),
			plus a plain-language impact/priority parenthetical."""
			title = (f.get("title") or "").strip()
			for raw, human in label_map.items():
				if raw and human and raw != human:
					title = title.replace(raw, human)
			prefix = f"In {slowest_label}, "
			if title.startswith(prefix):
				title = title[len(prefix):]
			pri = _PRIORITY_WORD.get(f.get("severity") or "", "")
			impact = f.get("estimated_impact_ms") or 0
			tail = f" (~{dur(impact)}" + (f" - {pri} priority" if pri else "") + ")"
			return f"<strong>{html.escape(title)}</strong>{tail}"

		# Prefer a finding tied to this specific action (via action_ref);
		# else the highest-impact finding overall (session-wide ones like a
		# missing index don't attribute to one action).
		tied_finding = None
		for f in findings:
			ref = f.get("action_ref")
			if ref and str(ref) == str(slowest_idx):
				if not tied_finding or (f.get("estimated_impact_ms") or 0) > (
					tied_finding.get("estimated_impact_ms") or 0
				):
					tied_finding = f
		overall_finding = max(
			findings, key=lambda f: f.get("estimated_impact_ms") or 0
		) if findings else None

		if tied_finding:
			# v0.7.x: the "See the Findings section below …" pointer lives once,
			# on the issue-count sentence below don't repeat it here.
			parts.append(
				f"The slowest one was <strong>{slowest_label_esc}</strong> at "
				f"{dur(slowest_ms)} - and most of its time went into "
				f"{_finding_phrase(tied_finding)}."
			)
		elif overall_finding:
			parts.append(
				f"The slowest one was <strong>{slowest_label_esc}</strong> at "
				f"{dur(slowest_ms)}. The biggest issue this session "
				f"(it affects several operations) was {_finding_phrase(overall_finding)}."
			)
		else:
			parts.append(
				f"The slowest one was <strong>{slowest_label_esc}</strong> at "
				f"{dur(slowest_ms)}."
			)

	if not findings:
		# Two-bullet reassurance (was one block of two <p>s pre-v0.7.x).
		parts.append(
			"We checked your flow for the usual culprits - "
			"<strong>repeated queries (N+1 patterns)</strong>, "
			"<strong>full table scans</strong>, "
			"<strong>filesort operations</strong>, "
			"<strong>temporary table creation</strong>, "
			"<strong>low filter ratios</strong>, "
			"<strong>missing indexes</strong> and "
			"<strong>individually slow queries</strong> (&gt;200ms) - "
			"and nothing significant turned up."
		)
		parts.append(
			"Either your flow is already well-optimized, or the data "
			"volume is too small to surface bottlenecks at this scale. "
			"Try running the profiler again with a larger dataset for "
			"more insight."
		)
	else:
		total_issues = high + medium + low
		bits = []
		if high:
			bits.append(f"<strong>{high} high priority</strong>")
		if medium:
			bits.append(f"{medium} medium")
		if low:
			bits.append(f"{low} minor")
		breakdown = (" - " + ", ".join(bits)) if bits else ""
		parts.append(
			f"We found <strong>{total_issues} potential issue"
			f"{'s' if total_issues != 1 else ''}</strong>{breakdown}. See the "
			"Findings section below for the ones to ask your developer to fix first."
		)

	# Wrap into a single <ul> each parts entry becomes one <li>. Inline
	# style mirrors the How-to-read list's pattern for visual consistency
	# with the rest of the report.
	bullets = "\n".join(f"<li>{p}</li>" for p in parts)
	return (
		'<ul class="small" style="margin: 4px 0 0 18px; padding: 0; '
		'line-height: 1.7;">\n' + bullets + "\n</ul>"
	)


def _finalize_with_empty_session(docname: str) -> None:
	"""Save the empty-session summary; the caller renders the report, then marks Ready."""
	doc = frappe.get_doc("Optimus Session", docname)
	doc.total_requests = 0
	doc.total_queries = 0
	# v0.7.x: same bullet shape as the populated Summary for visual
	# consistency a single <li> inside <ul> reads as a deliberate
	# summary line rather than dangling prose.
	doc.summary_html = (
		'<ul class="small" style="margin: 4px 0 0 18px; padding: 0; '
		'line-height: 1.7;">\n'
		"<li>No traffic was recorded during this session. Either no requests "
		"were made, or the session was stopped before any flow was performed.</li>\n"
		"</ul>"
	)
	from optimus.managed import save_session

	save_session(doc)
	safe_commit()


def _render_and_attach_reports(docname: str, recordings: list[dict]) -> bool:
	"""Atomically replace the private HTML report, preserving the old file on failure."""
	from optimus.report_refresh import render_report

	return bool(render_report(docname, recordings, required_status="Analyzing")["regenerated"])


def _save_report_file(*, docname: str, filename: str, attached_to_field: str, content) -> str | None:
	"""Attach generated recording bytes; ordinary failure is optional, interrupts escape."""
	from optimus.ai_fix import _InterruptGuard, log_ai_failure

	guard = _InterruptGuard(base=True)
	failed = False
	try:
		with guard:
			file_doc = frappe.get_doc({
				"doctype": "File", "file_name": filename,
				"attached_to_doctype": "Optimus Session", "attached_to_name": docname,
				"attached_to_field": attached_to_field,
				"content": content if isinstance(content, (bytes, bytearray)) else content.encode("utf-8"),
				"is_private": 1,
			})
			saved_request = getattr(frappe.local, "request", None)
			try:
				# Preserve the narrow generated-file extension exception.
				frappe.local.request = None
				from optimus.managed import artifact_writer

				with artifact_writer(docname, attached_to_field):
					file_doc.insert(ignore_permissions=True)
			finally:
				frappe.local.request = saved_request
			return file_doc.file_url
	except Exception:
		failed = True
	if guard.pending():
		content = file_doc = None
		raise guard.interrupt()
	if failed:
		log_ai_failure("optimus ai save recording File", reason="file_write_failed")
	return None


def _persist_recordings_file(docname: str, session_uuid: str, recording_uuids: list[str]) -> None:
	"""Persist recorder JSON for later refresh and report regeneration.

	Trees and argument sidecars remain transient Redis input for the first
	analysis. Neither opaque artifact is copied into persisted snapshots.
	"""
	from optimus import recording_bundle
	from optimus.ai_fix import _InterruptGuard, log_ai_failure

	guard = _InterruptGuard(base=True)
	failed = False
	try:
		with guard:
			recordings: dict = {}
			for uuid in recording_uuids:
				rec = frappe.cache.hget(RECORDER_REQUEST_HASH, uuid)
				if not isinstance(rec, dict):
					raise recording_bundle.InvalidBundle("snapshot_recording_missing")
				rec = {key: value for key, value in rec.items() if key not in {"pyi_session", "sidecar", "tree_b64"}}
				if isinstance(rec.get("calls"), list):
					rec["calls"] = [call for call in rec["calls"] if isinstance(call, dict)]
				entry: dict = {"rec": rec}
				sparse = frappe.cache.hget(RECORDER_REQUEST_SPARSE_HASH, uuid)
				if isinstance(sparse, dict):
					entry["sparse"] = sparse
				infra_blob = frappe.cache.get_value(_redis_keys.infra(uuid))
				if isinstance(infra_blob, dict):
					entry["infra"] = infra_blob
				# Carry the resolved target-doc onto the snapshotted rec so a regenerated report still shows the real doc name.
				resolved_doc = frappe.cache.get_value(_redis_keys.resolved_doc(uuid))
				if isinstance(resolved_doc, dict):
					rec["resolved_target_doc"] = resolved_doc
				recordings[uuid] = entry

			if not recordings:
				if recording_uuids:
					raise recording_bundle.InvalidBundle("snapshot_inputs_missing")
				return

			bundle = {
				"schema": 1,
				"session_uuid": session_uuid,
				"session_state": session.get_session_meta(session_uuid),
				"recordings": recordings,
			}
			payload, degraded = recording_bundle.encode_snapshot(bundle)
			if degraded:
				warnings = frappe.db.get_value("Optimus Session", docname, "analyzer_warnings") or ""
				if recording_bundle.DEGRADED_WARNING not in warnings.splitlines():
					frappe.db.set_value("Optimus Session", docname, "analyzer_warnings", (warnings + "\n" + recording_bundle.DEGRADED_WARNING).strip())
			# A lost commit acknowledgement may leave a valid snapshot already
			# bound. Reuse it instead of creating duplicate File rows/URLs.
			previous = frappe.db.get_value("Optimus Session", docname, "recordings_file")
			if previous:
				from types import SimpleNamespace
				existing = _load_recordings_bundle(SimpleNamespace(
					name=docname, session_uuid=session_uuid, recordings_file=previous))
				if existing is not None and recording_bundle.encode(existing) == payload:
					safe_commit()
					return
			url = _save_report_file(
				docname=docname,
				filename=f"optimus_recordings_{session_uuid}.json.gz",
				attached_to_field="recordings_file",
				content=payload,
			)
			if not url:
				raise recording_bundle.InvalidBundle("snapshot_file_missing")
			frappe.db.set_value("Optimus Session", docname, "recordings_file", url)
			# File row and parent binding must be durable before Redis cleanup.
			safe_commit()
	except Exception:
		failed = True
	if guard.pending():
		recordings = rec = bundle = payload = None
		raise guard.interrupt()
	if failed:
		recordings = rec = entry = sparse = infra_blob = resolved_doc = bundle = payload = existing = None
		log_ai_failure("optimus ai persist recordings", reason="snapshot_write_failed")
		raise recording_bundle.SnapshotUnavailable("snapshot_write_failed")


def _load_recordings_bundle(session_doc):
	"""Read bounded JSON only from the private File bound to this session.

	Missing snapshots are optional. Rejections/read failures are logged with
	fixed reasons, outside exception handlers, without recording content.
	"""
	from optimus import recording_bundle
	from optimus.ai_fix import _InterruptGuard, log_ai_failure

	url = getattr(session_doc, "recordings_file", None)
	if not url:
		return None
	problem = None
	guard = _InterruptGuard(base=True)
	try:
		with guard:
			docname = getattr(session_doc, "name", None)
			session_uuid = getattr(session_doc, "session_uuid", None)
			if (not docname or not session_uuid or not isinstance(url, str)
				or not url.startswith("/private/files/") or not url.removeprefix("/private/files/")
				or any(char in url.removeprefix("/private/files/") for char in ("/", "\\", "?", "#", "\0"))):
				raise recording_bundle.InvalidBundle("invalid_file_reference")
			binding = {"file_url": url, "is_private": 1, "attached_to_doctype": "Optimus Session",
				"attached_to_name": docname, "attached_to_field": "recordings_file"}
			names = frappe.get_all("File", filters=binding, pluck="name", limit_page_length=2)
			if len(names) != 1:
				raise recording_bundle.InvalidBundle("file_not_bound_to_session")
			file_doc = frappe.get_doc("File", names[0])
			if any(getattr(file_doc, key, None) != value for key, value in binding.items()):
				raise recording_bundle.InvalidBundle("file_binding_changed")
			return recording_bundle.read(file_doc.get_full_path(),
				private_root=frappe.get_site_path("private", "files"), session_uuid=session_uuid)
	except recording_bundle.InvalidBundle as exc:
		problem = str(exc)
	except Exception:
		problem = "bundle_read_failed"
	if guard.pending():
		raise guard.interrupt()
	if problem:
		log_ai_failure("optimus ai load recordings bundle", reason=problem,
			session_uuid=getattr(session_doc, "session_uuid", None))
	return None


def _cleanup_redis(session_uuid: str, recording_uuids: list[str]) -> None:
	"""Delete Redis state for this finalized session (the Optimus Session row is
	now the durable record). Best-effort: a failure does not abort analyze."""
	try:
		session.delete_session_state(session_uuid)
	except Exception:
		frappe.log_error(title="optimus cleanup session_state")

	for uuid in recording_uuids:
		try:
			frappe.cache.hdel(RECORDER_REQUEST_HASH, uuid)
		except Exception:
			pass
		try:
			frappe.cache.hdel(RECORDER_REQUEST_SPARSE_HASH, uuid)
		except Exception:
			pass
		# v0.3.0: also delete the per-recording tree and sidecar keys.
		try:
			frappe.cache.delete_value(_redis_keys.tree(uuid))
		except Exception:
			pass
		try:
			frappe.cache.delete_value(_redis_keys.sidecar(uuid))
		except Exception:
			pass
		try:
			frappe.cache.delete_value(_redis_keys.resolved_doc(uuid))
		except Exception:
			pass
