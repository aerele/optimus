"""Capture ownership and corrupt inputs must not damage another run."""

import json
import pickle
import threading
from types import SimpleNamespace

import pytest

from optimus import redis_keys
from optimus.line_profile import capture
from optimus.tests.phase2_cache_fake import Cache

pytestmark = pytest.mark.rq




@pytest.fixture
def env(monkeypatch):
	cache = Cache()
	local = SimpleNamespace(cache=cache.local)
	monkeypatch.setattr(capture, "frappe", SimpleNamespace(cache=cache, local=local))
	monkeypatch.setattr(capture, "_FRAPPE_AVAILABLE", True)
	monkeypatch.setattr(capture, "_LP_AVAILABLE", True)
	from optimus.renderer import source
	monkeypatch.setattr(source, "_installed_apps", lambda: {"optimus"})
	return cache, local


@pytest.mark.parametrize("protocol", [4, 5])
def test_stop_clears_only_matching_serialized_generation(env, protocol):
	cache, local = env
	key = cache.make_key(redis_keys.lp_active("fake-user"))
	cache.data[key] = pickle.dumps("new-run", protocol=protocol)
	capture.stop_line_profile_pass("old-run", "fake-user")
	assert cache.get_value(redis_keys.lp_active("fake-user")) == "new-run"
	capture.stop_line_profile_pass("new-run", "fake-user")
	assert cache.get_value(redis_keys.lp_active("fake-user")) is None
	assert local._lp_active is None


def test_stop_loses_a_race_without_deleting_new_capture(env):
	cache, _local = env
	cache.set_value(redis_keys.lp_active("fake-user"), "old-run")
	cache.before_execute = lambda: cache.set_value(redis_keys.lp_active("fake-user"), "new-run")
	capture.stop_line_profile_pass("old-run", "fake-user")
	assert cache.get_value(redis_keys.lp_active("fake-user")) == "new-run"


def test_missing_source_snapshot_is_an_error(env):
	cache, _local = env
	cache.set_value(redis_keys.lp_picks("fake-run"), json.dumps([{
		"dotted_path": "fake_app.api.example", "qualname": "example", "file": "fake.py", "first_lineno": 1}]))
	with pytest.raises(ValueError):
		capture.read_picks_meta("fake-run")


def test_sample_read_is_bounded_and_rejects_instead_of_silently_truncating(env, monkeypatch):
	cache, _ = env
	monkeypatch.setattr(capture, "MAX_SAMPLE_BATCHES", 2)
	seen = []
	def read(key, start, end):
		seen.append((start, end))
		return ["[]"] * 3
	monkeypatch.setattr(cache, "lrange", read)
	monkeypatch.setattr(capture, "_decode_capture_json", lambda *a: pytest.fail("parsed an excessive batch list"))
	with pytest.raises(capture.CaptureInputError):
		capture.read_all_samples("fake-run")
	assert seen == [(0, 2)]


def test_samples_have_a_total_byte_budget(env, monkeypatch):
	cache, _ = env
	monkeypatch.setattr(capture, "MAX_CAPTURE_BYTES", 9)
	cache.data[cache.make_key(redis_keys.lp_samples("fake-run"))] = ["[    ]"] * 2
	with pytest.raises(capture.CaptureInputError):
		capture.read_all_samples("fake-run")


@pytest.mark.parametrize("data", ["[NaN]", '[{"hits":1,"hits":2}]', "not json", '{}'])
def test_corrupt_sample_batches_are_rejected(env, data):
	cache, _ = env
	cache.data[cache.make_key(redis_keys.lp_samples("fake-run"))] = [data]
	with pytest.raises(capture.CaptureInputError):
		capture.read_all_samples("fake-run")


def test_capture_preparation_rejects_uninstalled_import_before_picker(env, monkeypatch):
	from optimus.line_profile import picker
	from optimus.renderer import source
	monkeypatch.setattr(source, "_installed_apps", lambda: {"frappe", "optimus"})
	seen = []
	monkeypatch.setattr(picker, "resolve_freeform", lambda dotted: seen.append(dotted))
	with pytest.raises(capture.CaptureError):
		capture.prepare_line_profile_picks([{"dotted_path": "uninstalled_app.dangerous_import.run"}])
	assert not seen


def test_capture_source_snapshot_respects_source_boundary(env, monkeypatch):
	from optimus.renderer import source
	monkeypatch.setattr(source, "_resolve_source_path", lambda filename: None)
	monkeypatch.setattr(capture.inspect, "getsourcelines", lambda fn: (["secret source"], 1))
	monkeypatch.setattr(capture.inspect, "unwrap", lambda fn: SimpleNamespace(
		__code__=SimpleNamespace(co_filename="/fake/private.py", co_firstlineno=1)))
	assert capture._capture_source_lines(test_capture_source_snapshot_respects_source_boundary) == []


def test_append_exceeding_total_budget_marks_input_incomplete(env, monkeypatch):
	cache, _ = env
	batch = [{"file": "fake.py", "qualname": "example", "lineno": 1, "hits": 1, "total_us": 1}]
	monkeypatch.setattr(capture, "MAX_CAPTURE_BYTES", len(json.dumps(batch).encode()) + 1)
	cache.set_value(redis_keys.lp_source("fake-run"), "{}")
	capture.flush_samples("fake-run", batch)
	with pytest.raises(capture.CaptureInputError):
		capture.flush_samples("fake-run", batch)
	assert len(cache.lrange(redis_keys.lp_samples("fake-run"), 0, -1)) == 1
	with pytest.raises(capture.CaptureInputError):
		capture.read_all_samples("fake-run")


def test_late_flush_cannot_recreate_deleted_capture(env):
	cache, _ = env
	batch = [{"file": "fake.py", "qualname": "example", "lineno": 1, "hits": 1, "total_us": 1}]
	capture.flush_samples("deleted-run", batch)
	assert cache.lrange(redis_keys.lp_samples("deleted-run"), 0, -1) == []


def test_evicted_sample_list_is_not_a_successful_empty_capture(env):
	cache, _ = env
	cache.set_value(redis_keys.lp_source("fake-run"), "{}")
	capture.flush_samples("fake-run", [{"file": "fake.py", "qualname": "example", "lineno": 1, "hits": 1, "total_us": 1}])
	cache.delete_value(redis_keys.lp_samples("fake-run"))
	with pytest.raises(capture.CaptureInputError):
		capture.read_all_samples("fake-run")


@pytest.mark.parametrize("method", ["budget_was_hit", "mark_budget_hit", "clear_budget_hit", "cleanup_run"])
def test_capture_helpers_never_swallow_job_timeout(env, monkeypatch, method):
	cache, _local = env
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	interrupt = Timeout("fake timeout")
	def fail(*a, **kw):
		raise interrupt
	for name in ("get_value", "set_value", "delete_value", "delete"):
		monkeypatch.setattr(cache, name, fail)
	with pytest.raises(Timeout) as caught:
		getattr(capture, method)("fake-run")
	assert caught.value is not interrupt


def test_cleanup_reports_redis_failure_to_its_recovery_caller(env, monkeypatch):
	cache, _ = env
	def fail(*a, **kw):
		raise ConnectionError("fake Redis outage")
	monkeypatch.setattr(cache, "delete", fail)
	monkeypatch.setattr(cache, "delete_value", fail)
	with pytest.raises(ConnectionError, match="fake Redis outage"):
		capture.cleanup_run("fake-run")


def test_sample_counters_and_cleanup_stay_in_the_current_site(env, monkeypatch):
	cache, _ = env
	cache.data[redis_keys.lp_sample_state("same-run")] = b"-1"
	batch = [{"file": "fake.py", "qualname": "example", "lineno": 1, "hits": 1, "total_us": 1}]
	cache.set_value(redis_keys.lp_source("same-run"), "{}")
	capture.flush_samples("same-run", batch)
	first = dict(cache.data)
	monkeypatch.setattr(cache, "make_key", lambda key: ("other-site|" + key).encode())
	cache.set_value(redis_keys.lp_source("same-run"), "{}")
	capture.flush_samples("same-run", batch + batch)
	assert capture.read_all_samples("same-run") == [batch + batch]
	capture.cleanup_run("same-run")
	assert cache.data == first
	monkeypatch.setattr(cache, "make_key", lambda key: ("fake-site|" + key).encode())
	assert capture.read_all_samples("same-run") == [batch]


def test_repeated_append_contention_marks_only_this_site_incomplete(env):
	cache, _ = env
	cache.set_value(redis_keys.lp_source("fake-run"), "{}")
	key = cache.make_key(redis_keys.lp_source("fake-run"))
	conflicts = []
	def conflict():
		conflicts.append(True)
		cache.versions[key] += 1
	cache.before_execute = conflict
	with pytest.raises(capture.CaptureInputError):
		capture.flush_samples("fake-run", [{"file": "fake.py", "qualname": "example", "lineno": 1, "hits": 1, "total_us": 1}])
	assert len(conflicts) == 3
	assert cache.data[cache.make_key(redis_keys.lp_sample_state("fake-run"))] == b"-1"
	assert all(key.startswith(b"fake-site|") for key in cache.data)
	with pytest.raises(capture.CaptureInputError):
		capture.read_all_samples("fake-run")


def sample_target():
	return 1


def test_competing_starts_publish_only_one_complete_capture(env):
	cache, _local = env
	picks = [{"dotted_path": __name__ + ".sample_target", "source": "freeform"}]
	prepared = capture.prepare_line_profile_picks(picks)
	barrier = threading.Barrier(2)
	cache.before_execute = lambda: barrier.wait(timeout=3)
	results = []
	def start(run):
		try:
			capture.start_line_profile_pass("fake-session", run, "fake-user", prepared=prepared)
			results.append((run, True))
		except capture.CaptureError:
			results.append((run, False))
	threads = [threading.Thread(target=start, args=(run,)) for run in ("one", "two")]
	for thread in threads:
		thread.start()
	for thread in threads:
		thread.join(timeout=5)
	assert len(results) == 2 and sum(success for _, success in results) == 1
	winner = next(run for run, success in results if success)
	loser = next(run for run, success in results if not success)
	assert cache.get_value(redis_keys.lp_active("fake-user")) == winner
	assert capture.read_picks_meta(winner)[0]["source_lines"]
	assert cache.get_value(redis_keys.lp_picks(loser)) is None
	assert cache.get_value(redis_keys.lp_source(loser)) is None


def test_prepared_start_does_not_resolve_functions_inside_admission(env, monkeypatch):
	prepared = capture.prepare_line_profile_picks([{"dotted_path": __name__ + ".sample_target"}])
	monkeypatch.setattr(capture, "prepare_line_profile_picks", lambda *a: pytest.fail("resolved inside admission"))
	assert capture.start_line_profile_pass("fake-session", "fake-run", "fake-user", prepared=prepared)


@pytest.mark.parametrize("key", ["phase1", "phase2"])
def test_start_refuses_existing_ownership_without_publishing_inputs(env, key):
	cache, _ = env
	prepared = capture.prepare_line_profile_picks([{"dotted_path": __name__ + ".sample_target"}])
	active = redis_keys.lp_active("fake-user") if key == "phase2" else redis_keys.session_active("fake-user")
	cache.set_value(active, "existing-run")
	with pytest.raises(capture.CaptureError):
		capture.start_line_profile_pass("fake-session", "new-run", "fake-user", prepared=prepared)
	assert cache.get_value(active) == "existing-run"
	assert cache.get_value(redis_keys.lp_picks("new-run")) is None
	assert cache.get_value(redis_keys.lp_source("new-run")) is None


@pytest.mark.parametrize("picks", [None, {}, [], [None], [{"dotted_path": {}}], [{"dotted_path": "a" * 1000}],
	[{"dotted_path": "fake_app.fn"}] * 101])
def test_invalid_picks_are_refused_before_import(env, monkeypatch, picks):
	from optimus.line_profile import picker

	monkeypatch.setattr(picker, "resolve_freeform", lambda *a: pytest.fail("invalid pick was resolved"))
	with pytest.raises(capture.CaptureError):
		capture.prepare_line_profile_picks(picks)


@pytest.mark.parametrize("source", [None, {}, [], "untrusted"])
def test_pick_source_is_a_known_label(env, source):
	with pytest.raises(capture.CaptureError):
		capture.validate_picks([{"dotted_path": "optimus.api.start", "source": source}])


def test_preparation_stops_reading_when_cumulative_source_exceeds_budget(env, monkeypatch):
	from optimus.line_profile import picker
	monkeypatch.setattr(capture, "MAX_CAPTURE_BYTES", 2000)
	monkeypatch.setattr(picker, "resolve_freeform", lambda dotted: {
		"dotted_path": dotted, "eligible": True, "qualname": "example", "file": "fake.py", "lineno": 1})
	monkeypatch.setattr(capture, "_resolve_attr", lambda dotted: sample_target)
	read = []
	monkeypatch.setattr(capture, "_capture_source_lines", lambda fn: read.append(True) or [{"lineno": 1, "content": "x" * 800}])
	with pytest.raises(capture.CaptureError):
		capture.prepare_line_profile_picks([{"dotted_path": f"optimus.fn{i}"} for i in range(20)])
	assert len(read) <= 3


def test_active_cache_is_scoped_to_the_recording_user(env):
	cache, _local = env
	cache.set_value(redis_keys.lp_active("first"), "first-run")
	cache.set_value(redis_keys.lp_active("second"), "second-run")
	assert capture.is_active("first") == "first-run"
	assert capture.is_active("second") == "second-run"


def stored_input(cache, *, lines=None, pick=None):
	pick = pick if pick is not None else {"dotted_path": "fake_app.api.example", "qualname": "example", "file": "fake.py", "first_lineno": 1}
	cache.set_value(redis_keys.lp_picks("fake-run"), json.dumps([pick]))
	cache.set_value(redis_keys.lp_source("fake-run"), json.dumps({"fake_app.api.example": lines if lines is not None else [{"lineno": 1, "content": "def example(): pass"}]}))


@pytest.mark.parametrize("lines", [[], [None], [{"lineno": -1, "content": "x"}], [{"lineno": True, "content": "x"}],
	[{"lineno": 1, "content": {}}], [{"lineno": 1, "content": "x"}, {"lineno": 1, "content": "duplicate"}]])
def test_corrupt_source_snapshot_is_not_an_uninvoked_function(env, lines):
	cache, _local = env
	stored_input(cache, lines=lines)
	with pytest.raises(ValueError):
		capture.read_picks_meta("fake-run")


def test_missing_source_for_one_pick_is_refused(env):
	cache, _local = env
	stored_input(cache)
	cache.set_value(redis_keys.lp_source("fake-run"), "{}")
	with pytest.raises(ValueError):
		capture.read_picks_meta("fake-run")


def test_valid_source_with_no_samples_keeps_uninvoked_result(env):
	cache, _local = env
	stored_input(cache)
	result = capture.aggregate_samples([], capture.read_picks_meta("fake-run"))
	assert result[0]["lines"][0]["hits"] == 0
	assert result[0]["lines"][0]["content"] == "def example(): pass"


@pytest.mark.parametrize("sample", [None, {}, {"file": [], "qualname": "example", "lineno": 1},
	{"file": "fake.py", "qualname": "example", "lineno": 1, "hits": -1, "total_us": 1},
	{"file": "fake.py", "qualname": "example", "lineno": 1, "hits": 1, "total_us": float("inf")},
	{"file": "fake.py", "qualname": "example", "lineno": True, "hits": 1, "total_us": 1}])
def test_corrupt_samples_fail_before_aggregation(env, sample):
	cache, _local = env
	stored_input(cache)
	with pytest.raises(ValueError):
		capture.aggregate_samples([[sample]], capture.read_picks_meta("fake-run"))
