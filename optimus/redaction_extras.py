# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The one place every redaction caller reads the operator's ``sensitive_form_keys``.

Reading them can fail (the settings store is down, a timeout). A caller that swallowed that
itself dropped the operator's own names without a word, so the read is made here, once, and a
failure is logged at ERROR (the error class only, once per class per process, never a value)
before the built-in names alone are used.
"""

import logging

_logged: set[str] = set()


def form_key_extras() -> tuple[str, ...]:
	"""The extra sensitive form, header and URL parameter names from Optimus Settings, or ``()``
	when they cannot be read (logged)."""
	try:
		from optimus.settings import get_config

		return tuple(get_config().sensitive_form_keys or ())
	except Exception as error:
		log_extras_failure(error)
		return ()


def log_extras_failure(error: BaseException) -> None:
	"""One ERROR line per error class: the operator's own sensitive names were not applied."""
	name = type(error).__name__
	if name in _logged:
		return
	_logged.add(name)
	logging.getLogger(__name__).error(
		"Optimus could not read sensitive_form_keys (%s); only the built-in names were applied", name
	)
