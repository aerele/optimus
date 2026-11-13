# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The one place every redaction caller reads the operator's ``sensitive_form_keys``.

Reading them can fail (the settings store is down, a timeout). A caller that swallowed that
itself dropped the operator's own names without a word, so the read is made here, once, and a
failure is logged at ERROR (the error class only, once per class per process, never a value)
before the built-in names alone are used.
"""

import logging

from optimus.safe_call import best_effort

_logged: set[str] = set()


def _read_extras() -> tuple[str, ...]:
	from optimus.settings import get_config

	return tuple(get_config().sensitive_form_keys or ())


def form_key_extras() -> tuple[str, ...]:
	"""The extra sensitive form, header and URL parameter names from Optimus Settings, or ``()``
	when they cannot be read (logged). Callers then mask with the built-in names, never skip the
	mask. A failing logger never raises, and an RQ job timeout still stops the job."""
	return best_effort(_read_extras, (), on_error=log_extras_failed)


def log_extras_failure(error: BaseException) -> None:
	"""One ERROR line per error class: the operator's own sensitive names were not applied."""
	log_extras_failed(type(error).__name__)


def log_extras_failed(name: str) -> None:
	"""``log_extras_failure`` from the error class name. Never raises (a job timeout excepted)."""
	if name in _logged:
		return
	_logged.add(name)

	def _write() -> None:
		logging.getLogger(__name__).error(
			"Optimus could not read sensitive_form_keys (%s); only the built-in names were applied", name
		)

	best_effort(_write, None)
