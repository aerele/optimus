"""Pre-migrate recovery defers quietly; operational database failures still surface."""
from types import SimpleNamespace

import pytest

from optimus import journal_schema


def test_schema_gap_emits_one_actionable_breadcrumb_then_recovers(monkeypatch):
	present, logs = set(), []
	framework = SimpleNamespace(local=SimpleNamespace(site='synthetic-upgrade-site'),
		db=SimpleNamespace(table_exists=lambda t: t in present, has_column=lambda t,f: (t,f) in present),
		logger=lambda *a: SimpleNamespace(error=logs.append))
	monkeypatch.setattr(journal_schema, '_WARNED', set())
	for _ in range(3):
		assert not journal_schema.ready(framework, {'Run': ('lease',)})
	assert len(logs) == 1 and 'migrate' in logs[0]
	present.update({'Run', ('Run','lease')})
	assert journal_schema.ready(framework, {'Run': ('lease',)})


def test_database_outage_is_not_misdiagnosed_as_schema_upgrade():
	def fail(*a):
		raise ConnectionError('synthetic outage')
	with pytest.raises(ConnectionError):
		journal_schema.ready(SimpleNamespace(db=SimpleNamespace(table_exists=fail)), {'Run': ()})


