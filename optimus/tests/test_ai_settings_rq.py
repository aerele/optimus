"""A settings fallback must not swallow the refresh worker's hard timeout."""

import sys
from types import SimpleNamespace

import pytest

from optimus import settings

pytestmark = pytest.mark.rq


@pytest.mark.parametrize("stage", ["cache", "exists", "doctype", "resolve", "cache_write"])
def test_settings_layers_propagate_a_fresh_job_timeout(monkeypatch, stage):
	JobTimeoutException = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = JobTimeoutException("fake settings timeout")
	def fail(*a, **kw):
		raise original
	cache = SimpleNamespace(get_value=lambda *a: None, set_value=lambda *a: None)
	db = SimpleNamespace(exists=lambda *a: True)
	fake = SimpleNamespace(cache=cache, db=db, get_cached_doc=lambda *a: {}, conf={})
	if stage == "cache":
		cache.get_value = fail
	elif stage == "exists":
		db.exists = fail
	elif stage == "doctype":
		fake.get_cached_doc = fail
	elif stage == "resolve":
		monkeypatch.setattr(settings, "_resolve", fail)
	else:
		cache.set_value = fail
	monkeypatch.setitem(sys.modules, "frappe", fake)
	with pytest.raises(JobTimeoutException) as caught:
		settings.get_config()
	assert caught.value is not original and caught.value.__context__ is None


@pytest.mark.parametrize("name", ["is_enabled", "display_threshold_ms", "get_tracked_apps", "get_ignored_apps"])
def test_settings_convenience_fallbacks_also_honor_job_timeout(monkeypatch, name):
	JobTimeoutException = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	def fail():
		raise JobTimeoutException("fake settings timeout")
	monkeypatch.setattr(settings, "get_config", fail)
	with pytest.raises(JobTimeoutException) as caught:
		getattr(settings, name)()
	assert caught.value.__context__ is None
