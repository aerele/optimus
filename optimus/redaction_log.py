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


_logged_capture: set[tuple[str, str]] = set()


def log_capture_failure(field: str, error: BaseException) -> None:
	"""One ERROR line per (field, error class) per process: a recorded request field could not be
	masked, so a placeholder was stored instead. The class only, never the value."""
	key = (field, type(error).__name__)
	if key in _logged_capture:
		return
	_logged_capture.add(key)
	logging.getLogger(__name__).error(
		"Optimus could not redact the recorded request %s (%s); a placeholder was stored", field, key[1]
	)
