# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Periodic cleanup of stale profiler sessions (scheduler_event, every 5 min).

Catches two failure modes: a session left in ``Recording`` after the user
walked away (Redis active pointer expired) and one stuck in ``Analyzing``
after a worker crashed. Both are force-stopped: clear Redis state, mark the
row ``Stopping`` and enqueue analyze.run (which retries once, then ``Failed``).
"""

import frappe
from frappe.utils import add_to_date, now_datetime

from optimus import safe_call, safe_commit, session
from optimus.timing import STALE_ANALYZING_MINUTES, STALE_RECORDING_MINUTES

# Default retention for successfully-analyzed sessions. After this many
# days, sessions in Ready or Failed state are deleted by the daily
# janitor. Overridable via site_config.json: optimus_session_retention_days
DEFAULT_RETENTION_DAYS = 90

# Hard cap on how many sessions we delete per run. Prevents a single
# janitor call from locking up the DB on a site with a huge backlog.
MAX_DELETIONS_PER_RUN = 100
# The sweep runs daily, so the backlog marker must outlive it.
RETENTION_BACKLOG_TTL_SECONDS = 25 * 3600

# A Ready session's own Redis cleanup runs within this long of it turning Ready.
READY_CLEANUP_GRACE_HOURS = 1

# A line about the orphan sweep is written at most this often per worker.
_SWEEP_LOG_DEDUPE_SECONDS = 6 * 60 * 60


def sweep_stale_sessions():
	"""Run from scheduler every 5 minutes. Force-stop or mark-failed any stuck sessions."""
	try:
		_sweep_stale_recording()
	except Exception:
		frappe.log_error(title="optimus janitor sweep_stale_recording")

	try:
		_sweep_stuck_analyzing()
	except Exception:
		frappe.log_error(title="optimus janitor sweep_stuck_analyzing")

	# v0.7.x: sessions stranded in "Stopping" (analyze never ran no worker,
	# backlog, or an OOM-killed worker left a zombie job) re-enqueue analyze.
	try:
		_sweep_stale_stopping()
	except Exception:
		frappe.log_error(title="optimus janitor sweep_stale_stopping")

	# v0.6.0: line-profile runs follow the same staleness model
	# but live on the Optimus Phase Two Run child rows. Reuses the same
	# thresholds (recording → 11min, analyzing → 30min).
	try:
		_sweep_stale_phase2_runs()
	except Exception:
		frappe.log_error(title="optimus janitor sweep_stale_phase2_runs")


def sweep_old_sessions():
	"""Run daily from scheduler. Delete terminal (Ready/Failed) sessions older
	than the configured retention (default 90 days), their attached report
	files and orphaned profiler:session:* Redis keys. Active sessions are left
	to the 5-minute janitor.
	"""
	try:
		_sweep_failed_snapshot_inputs()
	except Exception:
		frappe.log_error(title="optimus janitor failed recording inputs")
	try:
		_sweep_old_sessions()
	except Exception:
		frappe.log_error(title="optimus janitor sweep_old_sessions")

	try:
		_sweep_orphan_redis_state()
	except Exception:
		frappe.log_error(title="optimus janitor sweep_orphan_redis_state")


def _sweep_failed_snapshot_inputs():
	"""Bound retained Recorder input to a seven-day manual recovery window.

	Only terminal snapshot failures qualify; session retention may keep the
	report longer. Recheck under the parent lock so Retry Analyze cannot race
	Redis deletion. Work in batches, independently of normal session retention.
	"""
	from optimus import analyze
	from optimus.recording_bundle import SNAPSHOT_FAILURE_WARNING
	cutoff = add_to_date(now_datetime(), days=-7)
	rows = frappe.get_all("Optimus Session", filters={"status": "Failed", "modified": ["<", cutoff],
		"analyzer_warnings": SNAPSHOT_FAILURE_WARNING}, fields=["name"], limit_page_length=100)
	for row in rows:
		def expire():
			try:
				doc = frappe.get_doc("Optimus Session", row["name"], for_update=True)
				if doc.status != "Failed" or doc.modified >= cutoff or doc.analyzer_warnings != SNAPSHOT_FAILURE_WARNING:
					return
				uuids = sorted({a.recording_uuid for a in doc.actions or [] if a.recording_uuid}
					| set(session.get_recordings(doc.session_uuid)))
				marker = "Recording snapshot recovery window expired. Record a new session."
				doc.db_set("analyzer_warnings", marker)
				safe_commit()  # Expiration must be durable before irreversible deletion.
				doc = frappe.get_doc("Optimus Session", row["name"], for_update=True)
				if doc.status == "Failed" and doc.analyzer_warnings == marker:
					analyze._cleanup_redis(doc.session_uuid, uuids)
			finally:
				frappe.db.rollback()
		analyze._run_ai_step(expire, title="optimus recording expiry")


def _sweep_orphan_redis_state():
	"""Clean the Redis state of sessions that no longer need it: those with no
	Optimus Session row (leaked by analyzes that failed and never retried, or a
	deleted Failed row) and Ready ones a late background job left keys on.

	Each goes through ``analyze._cleanup_redis``, which removes its recordings'
	entries too, not just the session keys. Safe to run repeatedly. Uses SCAN
	with small batches so large keyspaces don't block Redis.
	"""
	from optimus import redis_keys

	# RedisWrapper subclasses redis.Redis, so it is the raw connection.
	redis_conn = frappe.cache

	# Get the site-qualified prefix so we only scan our own keys, not
	# other sites sharing the same Redis instance.
	try:
		site_prefix_bytes = frappe.cache.make_key("")
		site_prefix = (
			site_prefix_bytes.decode()
			if isinstance(site_prefix_bytes, bytes)
			else site_prefix_bytes
		)
	except Exception:
		site_prefix = ""

	# Scan BOTH per-session key families so a session whose only surviving keys
	# are frontend metrics is still discovered. (profiler:session:<uuid>:* and
	# profiler:frontend:<uuid>:*). Per-RECORDING keys profiler:tree/sidecar/
	# infra:<recording_uuid> carry a recording uuid (no session linkage in the
	# key), so they can't be orphan-checked here; they expire after
	# RECORDING_DATA_TTL_SECONDS (optimus/timing.py) or go with their session's cleanup.
	markers = ("profiler:session:", "profiler:frontend:")

	# Collect session UUIDs from the Redis keyspace
	uuids_in_redis: set[str] = set()
	try:
		for marker in markers:
			cursor = 0
			while True:
				cursor, keys = redis_conn.scan(cursor, match=f"{site_prefix}{marker}*", count=100)
				for key in keys:
					key_str = key.decode() if isinstance(key, bytes) else key
					# Shapes: "<prefix><marker><uuid>:<suffix>" or (frontend
					# legacy) "<prefix>profiler:frontend:<uuid>".
					parts = key_str.split(marker, 1)
					if len(parts) < 2:
						continue
					uuid = parts[1].split(":", 1)[0]
					if uuid:
						uuids_in_redis.add(uuid)
				if cursor == 0:
					break
	except Exception:
		return

	if not uuids_in_redis:
		return

	rows = {
		row["session_uuid"]: row
		for row in frappe.db.get_all(
			"Optimus Session",
			filters={"session_uuid": ["in", list(uuids_in_redis)]},
			fields=["session_uuid", "status", "modified"],
		)
	}
	# A Ready session no longer needs its Redis state; a late background job re-creates
	# some after the session's own cleanup ran. Give a session that only just finished
	# time to run that cleanup, which still reads Redis, before removing it here.
	ready_cutoff = add_to_date(now_datetime(), hours=-READY_CLEANUP_GRACE_HOURS)
	orphans = sorted(uuids_in_redis - rows.keys())
	leftovers = sorted(
		uuid for uuid, row in rows.items() if row["status"] == "Ready" and row["modified"] < ready_cutoff
	)
	cleaned_orphans = orphans[:MAX_DELETIONS_PER_RUN]
	cleaned_leftovers = leftovers[: MAX_DELETIONS_PER_RUN - len(cleaned_orphans)]

	cleaned_orphans = [uuid for uuid in cleaned_orphans if _cleaned_up(uuid)]
	cleaned_leftovers = [uuid for uuid in cleaned_leftovers if _cleaned_up(uuid)]

	# frappe.logger() is silent at the production level, so use the helper that writes at ERROR.
	if cleaned_orphans:
		safe_call.log_error_line(
			f"optimus orphan sweep cleaned Redis state of {len(cleaned_orphans)} sessions with no row",
			dedupe_seconds=_SWEEP_LOG_DEDUPE_SECONDS,
		)
	if cleaned_leftovers:
		safe_call.log_error_line(
			f"optimus orphan sweep cleaned Redis state left on {len(cleaned_leftovers)} Ready sessions "
			"(expected after a background job finished late)",
			dedupe_seconds=_SWEEP_LOG_DEDUPE_SECONDS,
		)


def _cleaned_up(uuid: str) -> bool:
	"""Clean one session's Redis state; True only when its recordings set is really gone."""
	from optimus import analyze

	analyze._cleanup_redis(uuid, [])  # lists the registered recordings itself
	return not session.get_recordings(uuid)


def _sweep_old_sessions():
	# v0.13.x: settings precedence DocType (via get_config) first,
	# legacy site_config fallback second, hardcoded default last. The
	# DocType wins when both are set so the operator's UI choice always
	# takes precedence over a stale site_config knob.
	try:
		from optimus.settings import get_config
		retention_days = int(get_config().session_retention_days)
	except Exception:
		retention_days = int(
			frappe.conf.get("optimus_session_retention_days") or DEFAULT_RETENTION_DAYS
		)
	# v0.13.x: 0 = forever (Strict-as-unlimited semantics). The daily
	# sweep becomes a no-op; sessions accumulate indefinitely. Honored
	# here so the field description's "Set to 0 to keep forever" promise
	# matches the runtime pre-v0.13.x this was silently overridden by
	# the legacy ``or DEFAULT_RETENTION_DAYS`` fallback.
	if retention_days <= 0:
		return
	cutoff = add_to_date(now_datetime(), days=-retention_days)

	old = frappe.db.get_all(
		"Optimus Session",
		filters={
			"status": ["in", ["Ready", "Failed"]],
			"started_at": ["<", cutoff],
		},
		fields=[
			"name", "title",
			"raw_report_file", "raw_report_pdf_file", "recordings_file",
		],
		limit=MAX_DELETIONS_PER_RUN,
		order_by="started_at asc",
	)

	# Phase K hardening: if this batch is at the per-run cap, the
	# retention sweep may be falling behind the recording rate. Count
	# the remaining stale sessions and stash an operator-visible
	# backlog metric so monitoring catches the drift before the
	# Optimus Session table grows unbounded.
	if len(old) >= MAX_DELETIONS_PER_RUN:
		try:
			remaining = frappe.db.count(
				"Optimus Session",
				filters={
					"status": ["in", ["Ready", "Failed"]],
					"started_at": ["<", cutoff],
				},
			)
		except Exception:
			remaining = None
		backlog = max(0, (remaining or 0) - len(old))
		try:
			from optimus import redis_keys, redis_schema
			frappe.cache.set_value(
				redis_keys.retention_backlog(),
				redis_schema.wrap_value(backlog),
				expires_in_sec=RETENTION_BACKLOG_TTL_SECONDS,
			)
		except Exception:
			pass
		safe_call.log_error_line(
			f"optimus janitor: MAX_DELETIONS_PER_RUN ({MAX_DELETIONS_PER_RUN}) "
			f"hit; ~{backlog} stale session(s) remain after this batch",
			dedupe_key="janitor deletion cap hit",
		)
	else:
		# Backlog cleared - reset the counter so monitoring sees the
		# recovery on the next pass.
		try:
			from optimus import redis_keys, redis_schema
			frappe.cache.set_value(
				redis_keys.retention_backlog(),
				redis_schema.wrap_value(0),
				expires_in_sec=RETENTION_BACKLOG_TTL_SECONDS,
			)
		except Exception:
			pass

	# End candidate selection's snapshot. Each parent owns its transaction;
	# normal deletion checks links before core removes attached files.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- end selection snapshot before per-parent transactions
	deleted = 0
	for row in old:
		failure = False
		try:
			from optimus import sql_work

			def remove():
				frappe.delete_doc(
					"Optimus Session", row["name"], ignore_permissions=True, delete_permanently=True,
				)

			sql_work.transaction(remove, commit=frappe.db.commit, rollback=frappe.db.rollback)
			deleted += 1
		except sql_work.TransactionFailed:
			raise  # A failed rollback cannot safely share this connection with a sibling.
		except Exception:
			failure = True
		if failure:
			# Rollback completed, including clearing after_commit callbacks. Persist
			# only a fixed diagnostic, outside the exception's traceback context.
			sql_work.transaction(
				lambda: frappe.log_error(title="optimus retention delete",
					message="Session retention failed; rolled back the parent transaction."),
				commit=frappe.db.commit, rollback=frappe.db.rollback,
			)

	if deleted:
		frappe.logger().info(
			f"optimus retention janitor deleted {deleted} session(s) "
			f"older than {retention_days} days"
		)


def _sweep_stale_recording():
	"""Find Recording rows older than STALE_RECORDING_MINUTES and force-stop them."""
	cutoff = add_to_date(now_datetime(), minutes=-STALE_RECORDING_MINUTES)
	stale = frappe.db.get_all(
		"Optimus Session",
		filters={"status": "Recording", "started_at": ["<", cutoff]},
		fields=["name", "session_uuid", "user"],
	)
	# ``started_at`` is fixed at session creation, so a genuinely LIVE long flow
	# (45min of real traffic) crosses the cutoff and would be wrongly stopped. The
	# real liveness signal is the Redis active pointer, whose TTL is refreshed on
	# every captured request so only force-stop rows whose pointer is gone or
	# has moved to a different session (the true "user walked away" condition).
	def _pointer_gone(row) -> bool:
		try:
			return session.get_active_session_for(row["user"]) != row["session_uuid"]
		except Exception:
			# Can't read the pointer → fall back to the old started_at behavior
			# (treat as stale) rather than leave a possibly-dead row recording.
			return True

	stale = [r for r in stale if _pointer_gone(r)]
	if not stale:
		return

	from optimus.api import claim_for_stop

	for row in stale:
		# Locked per-row transition: a Stop click may have taken this row since the
		# query above, and it has queued analyze already. Analyze only what we changed.
		if not claim_for_stop(row["name"]):
			continue

		# Clear the pointer only while it still names this session; the user may have started a new one.
		try:
			if session.get_active_session_for(row["user"]) == row["session_uuid"]:
				session.clear_active_session(row["user"])
		except Exception:
			pass

		# Enqueue analyze for whatever recordings did get captured before
		# the user walked away. The analyze job handles empty sessions
		# gracefully it will mark the session Ready with a "no traffic
		# was recorded" summary.
		try:
			frappe.enqueue(
				"optimus.analyze.run",
				queue="long",
				session_uuid=row["session_uuid"],
			)
		except Exception:
			frappe.log_error(title=f"optimus janitor enqueue {row['session_uuid']}")


def _sweep_stuck_analyzing():
	"""Find Analyzing rows older than STALE_ANALYZING_MINUTES and mark Failed.

	Rows still holding the heartbeated single-flight flag are skipped (a long
	analyze that hasn't bumped ``modified`` is still progressing, not wedged);
	only the mutex holder is skipped, so a crashed row is still failed promptly.
	"""
	from optimus.analyze import is_singleflight_holder

	cutoff = add_to_date(now_datetime(), minutes=-STALE_ANALYZING_MINUTES)
	# We use modified as a proxy for "when did the analyze start", since
	# analyze.run sets status to Analyzing first thing.
	stuck = frappe.db.get_all(
		"Optimus Session",
		filters={"status": "Analyzing", "modified": ["<", cutoff]},
		fields=["name", "session_uuid"],
	)
	stuck = [r for r in stuck if not is_singleflight_holder(r.get("session_uuid"))]
	if not stuck:
		return

	# v0.6.x: single batched UPDATE.
	frappe.db.set_value(
		"Optimus Session",
		{"name": ("in", [r["name"] for r in stuck]), "status": "Analyzing", "modified": ["<", cutoff]},
		{
			"status": "Failed",
			"analyzer_warnings": "Analyze job timed out, crashed, or its analyze heartbeat lapsed (the optimus log names a failed or yielded heartbeat). The session owner, or anyone who can edit it, can use Retry Analyze; saved reports and retained input are kept.",
		},
	)
	safe_commit()


def _sweep_stale_stopping():
	"""Find rows stuck in ``Stopping`` / ``Capturing Background Jobs`` longer
	than STALE_RECORDING_MINUTES (the analyze job never ran) and re-enqueue
	analyze, which is idempotent. Bumps ``modified`` so a still-stuck row backs
	off ~one window between retries instead of stacking a job every sweep.
	"""
	cutoff = add_to_date(now_datetime(), minutes=-STALE_RECORDING_MINUTES)
	stale = frappe.db.get_all(
		"Optimus Session",
		filters={
			"status": ["in", ["Stopping", "Capturing Background Jobs"]],
			"modified": ["<", cutoff],
		},
		fields=["name", "session_uuid"],
	)
	if not stale:
		return

	# Re-affirm the status to bump ``modified`` → next sweep waits another
	# window before re-enqueuing the same row.
	frappe.db.set_value(
		"Optimus Session",
		{"name": ("in", [r["name"] for r in stale]), "status": ["in", ["Stopping", "Capturing Background Jobs"]], "modified": ["<", cutoff]},
		{"status": "Stopping"},
	)
	safe_commit()

	for row in stale:
		try:
			frappe.enqueue(
				"optimus.analyze.run",
				queue="long",
				session_uuid=row["session_uuid"],
			)
		except Exception:
			frappe.log_error(
				title=f"optimus janitor stopping re-enqueue {row['session_uuid']}"
			)


def _sweep_stale_phase2_runs():
	"""Expire old captures/legacy jobs with a current locked status check.

	The durable queue scheduler owns upgraded jobs. Failed analyses retain
	input for explicit retry; never erase samples merely because a job died.
	"""
	from optimus import ai_jobs
	from optimus.line_profile import capture as _lp_capture
	from optimus.line_profile import jobs as _lp_jobs

	for status, age in (("Recording", STALE_RECORDING_MINUTES), ("Analyzing", STALE_ANALYZING_MINUTES)):
		cutoff = add_to_date(now_datetime(), minutes=-age)
		rows = frappe.db.get_all(
			"Optimus Phase Two Run", filters={"status": status, "modified": ["<", cutoff]},
			fields=["name", "parent", "run_uuid"], limit_page_length=100,
		)
		ai_jobs._transaction(lambda: None)
		for row in rows:
			def expire():
				changed = ai_jobs._retry_sql(lambda: _lp_jobs.expire_legacy(
					row["parent"], row["run_uuid"], status=status, cutoff=cutoff,
				))
				if changed and status == "Recording":
					_lp_capture.cleanup_run(row["run_uuid"])
			from optimus.analyze import _run_ai_step
			_run_ai_step(expire, title="optimus ai stale line profiling recovery", docname=row["parent"])
