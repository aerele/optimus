# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The request and job hooks read settings only after the cheap local gates pass."""

import pytest

from optimus import hooks_callbacks as hc
from optimus import settings
from optimus.tests.test_hooks_request_job_flow import env  # noqa: F401  (the shared fake)

frappe = hc.frappe


@pytest.fixture
def reads(env, monkeypatch):  # noqa: F811
	"""Count settings reads; both the enabled check and the skip-user read count."""
	counter = {"settings": 0}
	enabled = settings.is_enabled
	config = settings.get_config

	def counted_enabled():
		counter["settings"] += 1
		return enabled()

	def counted_config():
		counter["settings"] += 1
		return config()

	monkeypatch.setattr(settings, "is_enabled", counted_enabled)
	monkeypatch.setattr(settings, "get_config", counted_config)
	return counter


class TestBeforeRequestOrder:
	def test_a_request_without_a_session_reads_no_settings(self, env, reads):  # noqa: F811
		hc.before_request()

		assert reads["settings"] == 0
		assert env.recorded == []

	def test_a_guest_request_reads_no_settings(self, env, reads):  # noqa: F811
		monkey = frappe.session
		monkey.user = "Guest"
		hc.before_request()

		assert reads["settings"] == 0

	def test_a_disabled_site_with_a_session_never_starts_the_recorder(self, env, reads):  # noqa: F811
		env.store.active["alice@example.com"] = "s1"
		env.enabled = False

		hc.before_request()

		assert env.recorded == []
		assert not hasattr(env.local, "optimus_session_id")

	def test_a_skipped_user_with_a_session_is_not_recorded(self, env, reads):  # noqa: F811
		env.store.active["alice@example.com"] = "s1"
		env.skip_users = ["alice@example.com"]

		hc.before_request()

		assert env.recorded == []

	def test_an_enabled_request_with_a_session_still_records(self, env, reads):  # noqa: F811
		env.store.active["alice@example.com"] = "s1"
		env.store.meta["s1"] = {"capture_python_tree": False}

		hc.before_request()

		assert env.recorded == [{"force": True}]
		assert reads["settings"] >= 1


class TestBeforeJobOrder:
	def test_an_untagged_job_reads_no_settings(self, env, reads):  # noqa: F811
		kwargs = {"a": 1}

		hc.before_job(method="x.y", kwargs=kwargs)

		assert reads["settings"] == 0
		assert kwargs == {"a": 1}

	def test_a_disabled_site_still_pops_the_marker(self, env, reads):  # noqa: F811
		env.enabled = False
		kwargs = {"a": 1, "_profiler_session_id": "s1"}

		hc.before_job(method="x.y", kwargs=kwargs)

		assert kwargs == {"a": 1}

	def test_a_disabled_site_writes_no_jobs_hash_row(self, env, reads):  # noqa: F811
		env.enabled = False

		hc.before_job(method="x.y", kwargs={"_profiler_session_id": "s1"})

		assert env.bg_started == []

	def test_an_internal_job_pops_both_markers_before_any_settings_read(self, env, reads, monkeypatch):  # noqa: F811
		import optimus

		monkeypatch.setattr(optimus, "is_internal_job", lambda method: True)
		kwargs = {"_profiler_session_id": "s1", "_lp_session_id": "s2", "a": 1}

		hc.before_job(method="optimus.analyze.run", kwargs=kwargs)

		assert kwargs == {"a": 1}
		assert reads["settings"] == 0

	def test_an_enabled_tagged_job_is_tracked(self, env, reads):  # noqa: F811
		hc.before_job(method="x.y", kwargs={"_profiler_session_id": "s1"})

		assert env.bg_started == [("s1", "x.y")]
