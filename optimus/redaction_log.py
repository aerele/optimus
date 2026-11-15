# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Operator-visible trace of a statement that redaction had to omit. Only the exception class
is logged, never the SQL, and each class once per process so a bad query cannot flood the log."""

import logging

from optimus.safe_call import best_effort

_logged: set[str] = set()


def log_redaction_failure(error: BaseException) -> None:
	name = type(error).__name__
	if name in _logged:
		return
	_logged.add(name)
	logging.getLogger(__name__).warning("Optimus SQL redaction failed, statement omitted: %s", name)


_logged_capture: set[tuple[str, str]] = set()


def log_capture_failure(field: str, error_name: str) -> None:
	"""One ERROR line per (field, error class) per process: a recorded request field could not be
	masked, so a placeholder was stored instead. The error class name only, never the value. Never
	raises (an RQ job timeout excepted)."""
	key = (field, error_name)
	if key in _logged_capture:
		return
	_logged_capture.add(key)

	def _write() -> None:
		logging.getLogger(__name__).error(
			"Optimus could not redact the recorded request %s (%s); a placeholder was stored", field, error_name
		)

	best_effort(_write, None)


# A report that omitted or masked whole at least this share of its statements is logged.
DRIFT_MIN_STATEMENTS = 10
DRIFT_SHARE = 0.5


def log_redaction_drift(counts: dict[str, int]) -> None:
	"""Log, once per process, a report where most statements were omitted or masked whole: the
	redactor has drifted, or the site sends SQL it cannot read. Counts only, never SQL."""
	if not isinstance(counts, dict):
		return
	total = counts.get("statements", 0)
	unread = counts.get("omitted", 0) + counts.get("masked_whole", 0)
	if total < DRIFT_MIN_STATEMENTS or unread < total * DRIFT_SHARE or "drift" in _logged:
		return
	_logged.add("drift")
	logging.getLogger(__name__).warning(
		"Optimus SQL redaction could not read %d of %d statements (%d omitted)",
		unread,
		total,
		counts.get("omitted", 0),
	)
