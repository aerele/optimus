# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Operator-visible trace of a statement that redaction had to omit. Only the exception class
is logged, never the SQL, and each class once per process so a bad query cannot flood the log."""

import logging

_logged: set[str] = set()


def log_redaction_failure(error: BaseException) -> None:
	name = type(error).__name__
	if name in _logged:
		return
	_logged.add(name)
	logging.getLogger(__name__).warning("Optimus SQL redaction failed, statement omitted: %s", name)


def log_extras_unreadable(error: BaseException) -> None:
	"""The Optimus Settings lists could not be read, so only the default names are masked."""
	name = "extras:" + type(error).__name__
	if name in _logged:
		return
	_logged.add(name)
	logging.getLogger(__name__).warning(
		"Optimus could not read its sensitive-name settings, masking the default names only: %s",
		type(error).__name__,
	)


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
