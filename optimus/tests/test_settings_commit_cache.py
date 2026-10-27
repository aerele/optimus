"""Commit, rollback, lost invalidation and failure-cache timelines."""
from types import SimpleNamespace

from optimus import settings
from optimus.tests.test_profiler_settings_validation import _fresh_controller


def environment(monkeypatch):
	Controller, fake = _fresh_controller(monkeypatch)
	state = SimpleNamespace(now=0, committed={"enabled": 1}, value=None, until=0, callbacks=[], writes=[])
	def get(key, *, expires=False):
		assert expires, "TTL cache misses must not be retained in the v15 request cache"
		return state.value if state.now < state.until else None
	def put(key, value, *, expires_in_sec):
		state.value, state.until = value, state.now + expires_in_sec
		state.writes.append(expires_in_sec)
	fake.cache = SimpleNamespace(get_value=get, set_value=put, delete_value=lambda key: setattr(state, "value", None))
	fake.conf = {}
	fake.db = SimpleNamespace(exists=lambda *a: True, after_commit=SimpleNamespace(add=state.callbacks.append))
	fake.get_doc = lambda *a: dict(state.committed)
	return Controller(), fake, state


def test_invalidation_waits_for_commit_and_rollback_keeps_valid_config(monkeypatch):
	doc, _, state = environment(monkeypatch)
	assert settings.get_config().enabled
	doc.on_update()
	assert settings.get_config().enabled and state.value is not None
	# Rollback discards transaction callbacks and must not evict committed config.
	state.callbacks.clear()
	assert settings.get_config().enabled
	doc.on_update()
	state.committed["enabled"] = 0
	for callback in state.callbacks:
		callback()
	assert not settings.get_config().enabled


def test_lost_after_commit_callback_is_bounded_by_ttl(monkeypatch):
	_, _, state = environment(monkeypatch)
	assert settings.get_config().enabled
	state.committed["enabled"] = 0
	assert settings.get_config().enabled
	state.now += settings.CONFIG_CACHE_SECONDS + 1
	assert not settings.get_config().enabled
	assert state.writes and all(0 < ttl <= 60 for ttl in state.writes)


def test_read_failure_defaults_are_never_cached(monkeypatch):
	_, fake, state = environment(monkeypatch)
	fake.get_doc = lambda *a: (_ for _ in ()).throw(ConnectionError("temporary read failure"))
	assert settings.get_config().enabled
	assert state.value is None and state.writes == []



def test_uncommitted_settings_reads_cannot_populate_shared_cache(monkeypatch):
	Controller, fake, state = environment(monkeypatch)
	fake.db.transaction_writes = 1
	state.committed = {"enabled": 0}  # This connection can read its pending write.
	assert not settings.get_config().enabled
	assert not state.writes
	fake.db.transaction_writes = 0
	state.committed = {"enabled": 1}  # Rollback restored the committed value.
	assert settings.get_config().enabled
	assert state.writes == [60]
