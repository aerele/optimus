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
