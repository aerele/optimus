"""Scheduler readiness during the code-before-migrate deployment window."""

_WARNED = set()


def ready(framework, tables):
	from optimus.safe_call import InterruptGuard
	guard = InterruptGuard(base=True)
	with guard:
		return _ready(framework, tables)
	framework = tables = None
	raise guard.interrupt()


def _ready(framework, tables):
	"""Quietly defer only confirmed schema gaps; database failures still surface."""
	missing = any(not framework.db.table_exists(table) or any(
		not framework.db.has_column(table, field) for field in fields
	) for table, fields in tables.items())
	site = getattr(framework.local, "site", None)
	key = (site, tuple(tables))
	if missing:
		if key not in _WARNED:
			framework.logger("optimus").error(
				"Optimus background recovery is waiting for schema migration. Run bench migrate for this site, then restart workers."
			)
			_WARNED.add(key)
		return False
	_WARNED.discard(key)
	return True
