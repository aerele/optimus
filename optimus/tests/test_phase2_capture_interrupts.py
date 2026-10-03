"""A best-effort capture may skip ordinary errors, never worker interrupts."""

from types import SimpleNamespace

import pytest

from optimus import ai_fix
from optimus.line_profile import capture, hooks

pytestmark = pytest.mark.rq


@pytest.fixture(autouse=True)
def require_rq():
	pytest.importorskip("rq", exc_type=ImportError)


@pytest.fixture
def env(monkeypatch):
	seen = []
	profiler = SimpleNamespace(disable_by_count=lambda: seen.append("disable"))
	local = SimpleNamespace(_lp_profiler=profiler, _lp_run_uuid="fake-run", _lp_watchdog=None)
	fake = SimpleNamespace(session=SimpleNamespace(user="fake-user"), local=local,
		log_error=lambda **kw: seen.append("old-log"), conf={})
	monkeypatch.setattr(hooks, "frappe", fake)
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **kw: seen.append("log"))
	monkeypatch.setattr(capture, "is_active", lambda *a, **kw: "fake-run")
	monkeypatch.setattr(capture, "serialize_stats", lambda *a: [])
	monkeypatch.setattr(capture, "flush_samples", lambda *a: None)
	monkeypatch.setattr(capture, "decr_active_profilers", lambda: seen.append("decrement") or 0)
	monkeypatch.setattr(capture, "release_monitoring_tool", lambda: seen.append("release"))
	monkeypatch.setattr(hooks.hooks_callbacks, "_should_skip_request", lambda: False)
	return SimpleNamespace(seen=seen, profiler=profiler, local=local)


@pytest.mark.parametrize("name", ["before_request_line_profile", "before_job_line_profile", "after_request_line_profile", "after_job_line_profile"])
@pytest.mark.parametrize("kind", ["rq", "base"])
def test_hook_preserves_interrupt_and_after_hook_always_tears_down(env, monkeypatch, name, kind):
	from rq.timeouts import JobTimeoutException
	original = JobTimeoutException("fake timeout") if kind == "rq" else SystemExit("fake interrupt")
	def fail(*a, **kw):
		raise original
	monkeypatch.setattr(capture, "make_profiler" if name.startswith("before") else "flush_samples", fail)
	with pytest.raises(type(original)) as caught:
		getattr(hooks, name)(kwargs={"_lp_session_id": "fake-run"})
	assert (caught.value is original) is (kind == "base")
	assert caught.value.__context__ is None and caught.value.__cause__ is None
	tb = caught.value.__traceback__
	while tb:
		assert tb.tb_frame.f_code.co_name != "fail"
		tb = tb.tb_next
	assert not any(entry.endswith("log") for entry in env.seen)
	if name.startswith("after"):
		assert env.seen == ["disable", "decrement", "release"]
		assert env.local._lp_profiler is None


@pytest.mark.parametrize("step", ["watchdog", "disable"])
def test_interrupt_during_teardown_still_releases_the_profiler(env, monkeypatch, step):
	from rq.timeouts import JobTimeoutException
	original = JobTimeoutException("fake timeout")
	def fail():
		raise original
	if step == "watchdog":
		env.local._lp_watchdog = SimpleNamespace(cancel=fail)
	else:
		def disable():
			env.seen.append("disable")
			fail()
		env.profiler.disable_by_count = disable
	with pytest.raises(JobTimeoutException) as caught:
		hooks.after_job_line_profile()
	assert caught.value is not original
	assert env.seen == ["disable", "decrement", "release"]


def test_make_profiler_does_not_consume_timeout(monkeypatch):
	from rq.timeouts import JobTimeoutException
	monkeypatch.setattr(capture, "_LP_AVAILABLE", True)
	original = JobTimeoutException("fake timeout")
	def fail(*a):
		raise original
	monkeypatch.setattr(capture, "_get_or_resolve_picks", fail)
	with pytest.raises(JobTimeoutException) as caught:
		capture.make_profiler("fake-run")
	assert caught.value is not original


@pytest.mark.parametrize("step", ["read_picks_meta", "read_all_samples", "prepare_line_profile_picks"])
def test_input_boundary_discards_interrupted_frames(monkeypatch, step):
	from rq.timeouts import JobTimeoutException

	from optimus.line_profile import picker
	from optimus.renderer import source
	original = JobTimeoutException("fake timeout")
	def fail(*a, **kw):
		raise original
	monkeypatch.setattr(capture, "_FRAPPE_AVAILABLE", True)
	monkeypatch.setattr(capture, "_LP_AVAILABLE", True)
	monkeypatch.setattr(capture, "frappe", SimpleNamespace(cache=SimpleNamespace(get_value=fail, get=fail, make_key=lambda v: v)))
	monkeypatch.setattr(source, "_installed_apps", lambda: {"fake_app"})
	monkeypatch.setattr(picker, "resolve_freeform", fail)
	arg = [{"dotted_path": "fake_app.api.example"}] if step.startswith("prepare") else "fake-run"
	with pytest.raises(JobTimeoutException) as caught:
		getattr(capture, step)(arg)
	assert caught.value is not original
	frames = []
	tb = caught.value.__traceback__
	while tb:
		frames.append(tb.tb_frame.f_code.co_name)
		tb = tb.tb_next
	assert "fail" not in frames


@pytest.mark.parametrize("stage", ["enable", "watchdog"])
@pytest.mark.parametrize("kind", ["rq", "base", "ordinary"])
def test_interrupted_setup_releases_its_registered_profiler(env, monkeypatch, stage, kind):
	from rq.timeouts import JobTimeoutException
	original = {"rq": JobTimeoutException, "base": SystemExit, "ordinary": ValueError}[kind]("fake failure")
	def fail(*a, **kw):
		raise original
	profiler = SimpleNamespace(enable_by_count=fail if stage == "enable" else lambda: None,
		disable_by_count=lambda: env.seen.append("disable"))
	monkeypatch.setattr(capture, "make_profiler", lambda *a: profiler)
	monkeypatch.setattr(capture, "incr_active_profilers", lambda: env.seen.append("increment"))
	monkeypatch.setattr(capture, "active_profiler_count", lambda: 1)
	monkeypatch.setattr(capture, "start_overhead_watchdog", fail)
	if kind == "ordinary":
		hooks.before_job_line_profile(kwargs={"_lp_session_id": "fake-run"})
	else:
		with pytest.raises(type(original)):
			hooks.before_job_line_profile(kwargs={"_lp_session_id": "fake-run"})
	assert env.seen[:4] == ["increment", "disable", "decrement", "release"]
	assert env.local._lp_profiler is None and env.local._lp_run_uuid is None
