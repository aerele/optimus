# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The single-flight helpers read the flag from Redis, never from the
job's ``frappe.local.cache``.

Frappe's ``RedisWrapper.get_value`` answers from ``frappe.local.cache`` once a key is in it,
and that dict lives for the whole RQ job (``frappe.init`` makes one per job). The helpers
run here through Frappe v16's REAL ``RedisWrapper`` (bench runs only) and through a
transcription of v15's value API (every run), over an in-memory Redis, with one
``frappe.local.cache`` dict per simulated job. No real Redis is touched.
"""

import pickle
import threading
from types import SimpleNamespace

import frappe
import pytest

from optimus import analyze
from optimus.tests.singleflight_fakes import SITE, FakeRedis, connection_error, flag_key

TTL = analyze._SINGLEFLIGHT_TTL_SECONDS


class _Conf(dict):
	"""site config: ``conf.get(...)`` for analyze, ``conf.db_name`` for v15's make_key."""

	__getattr__ = dict.get


def _real_v16(server):
	"""Frappe v16's real ``RedisWrapper``; the server commands it and analyze send go to
	``server`` (no connection pool)."""
	try:
		from frappe.utils.redis_wrapper import RedisWrapper
	except ImportError:
		RedisWrapper = None
	if RedisWrapper is None or not hasattr(RedisWrapper, "set_value"):
		pytest.skip("needs Frappe's real RedisWrapper (the CI stub has none)")

	class _V16(RedisWrapper):
		def __init__(self, server):
			self.server = server

		def __repr__(self):
			return "<RedisWrapper over FakeRedis>"

		def __del__(self):
			pass

		def get(self, name):
			return self.server.get(name)

		def set(self, name, value, ex=None, nx=False, **kwargs):
			return self.server.set(name, value, ex=ex, nx=nx)

		def setex(self, name, time, value):
			return self.server.setex(name, time, value)

		def expire(self, name, time, *args, **kwargs):
			return self.server.expire(name, time)

		def unlink(self, *names):
			return self.server.unlink(*names)

		def delete(self, *names):
			return self.server.delete(*names)

	return _V16(server)


class _V15Shaped:
	"""Frappe v15's ``RedisWrapper`` value API, transcribed from v15's
	frappe/utils/redis_wrapper.py (make_key, set_value, get_value, delete_value) without
	its ConnectionError guards. v15 fills ``frappe.local.cache`` from ``get_value`` and from
	``set_value`` without an expiry only, and its ``get_value`` has no ``use_local_cache``,
	so a cached flag goes stale and an expiring write never refreshes it."""

	def __init__(self, server):
		self.server = server

	def get(self, name):
		return self.server.get(name)

	def set(self, name, value, ex=None, nx=False, **kwargs):
		return self.server.set(name, value, ex=ex, nx=nx)

	def setex(self, name, time, value):
		return self.server.setex(name, time, value)

	def expire(self, name, time):
		return self.server.expire(name, time)

	def delete(self, *names):
		return self.server.delete(*names)

	def make_key(self, key, user=None, shared=False):
		if shared:
			return key
		return f"{frappe.conf.db_name}|{key}".encode()

	def set_value(self, key, val, user=None, expires_in_sec=None, shared=False):
		key = self.make_key(key, user, shared)
		if not expires_in_sec:
			frappe.local.cache[key] = val
		if expires_in_sec:
			self.setex(name=key, time=expires_in_sec, value=pickle.dumps(val))
		else:
			self.set(key, pickle.dumps(val))

	def get_value(self, key, generator=None, user=None, expires=False, shared=False):
		original_key = key
		key = self.make_key(key, user, shared)
		local_cache = frappe.local.cache
		if key in local_cache:
			val = local_cache[key]
		else:
			val = self.get(key)
			if val is not None:
				val = pickle.loads(val)
			if not expires:
				if val is None and generator:
					val = generator()
					self.set_value(original_key, val, user=user, shared=shared)
				else:
					local_cache[key] = val
		return val

	def delete_value(self, keys, user=None, make_keys=True, shared=False):
		if not keys:
			return
		if not isinstance(keys, list | tuple):
			keys = (keys,)
		if make_keys:
			keys = [self.make_key(k, shared=shared, user=user) for k in keys]
		local_cache = frappe.local.cache
		for key in keys:
			local_cache.pop(key, None)
		self.delete(*keys)


@pytest.fixture(params=["v16", "v15"])
def env(request, monkeypatch):
	server = FakeRedis()
	cache = _real_v16(server) if request.param == "v16" else _V15Shaped(server)
	local = SimpleNamespace(cache={}, conf=_Conf(db_name=SITE))
	jobs: dict = {}
	lines: list = []
	enqueued: list = []
	sleeps: list = []

	def logger(name=None, *args, **kwargs):
		def at(level):
			return lambda message: lines.append((name, level, message))
		return SimpleNamespace(**{level: at(level) for level in ("debug", "info", "warning", "error")})

	monkeypatch.setattr(frappe, "local", local, raising=False)
	monkeypatch.setattr(frappe, "conf", _Conf(db_name=SITE), raising=False)
	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	monkeypatch.setattr(frappe, "db", SimpleNamespace(set_value=lambda *a, **k: None), raising=False)
	monkeypatch.setattr(frappe, "enqueue", lambda *a, **k: enqueued.append(k), raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda *a, **k: None, raising=False)
	monkeypatch.setattr(frappe, "logger", logger, raising=False)
	monkeypatch.setattr(analyze, "is_scheduler_disabled", lambda: False)
	monkeypatch.setattr(analyze, "safe_commit", lambda: None)
	monkeypatch.setattr(analyze, "_publish_progress", lambda *a, **k: None)
	monkeypatch.setattr(analyze, "time", SimpleNamespace(
		time=lambda: server.now, monotonic=lambda: server.now, sleep=sleeps.append,
	))
	monkeypatch.setattr(analyze, "_heartbeat_noted", set(), raising=False)

	def in_job(name):
		"""Switch to session ``name``'s RQ job: its own ``frappe.local.cache``."""
		local.cache = jobs.setdefault(name, {})

	return SimpleNamespace(
		server=server, local=local, in_job=in_job, lines=lines, enqueued=enqueued, sleeps=sleeps,
		holder=lambda: server.peek(flag_key()), ttl=lambda: server.ttl(flag_key()),
	)


def test_a_run_whose_flag_lapsed_leaves_the_new_holder_alone(env):
	"""S1: A's flag lapses and B takes it. A's heartbeat must not write over B's flag or
	refresh its TTL, A must not count as the holder, and A's release must not delete it."""
	env.in_job("A")
	assert analyze._acquire_singleflight("A", "PS-A", None) is True
	analyze._touch_singleflight("A")
	assert analyze.is_singleflight_holder("A") is True
	env.server.advance(TTL + 1)  # A went a whole TTL without a heartbeat
	env.in_job("B")
	assert analyze._acquire_singleflight("B", "PS-B", None) is True
	assert env.holder() == "B"
	env.server.advance(10)
	env.in_job("A")
	assert analyze.is_singleflight_holder("A") is False
	analyze._touch_singleflight("A")
	assert env.holder() == "B" and env.ttl() == TTL - 10
	analyze._release_singleflight("A")
	assert env.holder() == "B"
	env.in_job("B")
	assert analyze.is_singleflight_holder("B") is True
	analyze._release_singleflight("B")
	assert env.holder() is None


def test_a_degraded_run_never_steals_the_flag_and_takes_it_once_free(env):
	"""S2: B ran on past its wait deadline while A held the flag. B's heartbeats leave A's
	flag alone; once A releases, B's next heartbeat takes the free flag."""
	env.in_job("A")
	assert analyze._acquire_singleflight("A", "PS-A", None) is True
	env.in_job("B")
	assert analyze._acquire_singleflight("B", "PS-B", 0.0) is True  # degraded
	assert env.enqueued == []
	analyze._touch_singleflight("B")
	assert env.holder() == "A"
	env.in_job("A")
	analyze._release_singleflight("A")
	assert env.holder() is None
	env.in_job("B")
	analyze._touch_singleflight("B")
	assert env.holder() == "B" and env.ttl() == TTL


def test_a_plain_run_releases_its_flag(env):
	"""S3: acquire, heartbeat, finish. On v15 the flag was never released, so the next
	analyze waited up to the TTL."""
	env.in_job("A")
	assert analyze._acquire_singleflight("A", "PS-A", None) is True
	analyze._touch_singleflight("A")
	assert analyze.is_singleflight_holder("A") is True
	analyze._release_singleflight("A")
	assert env.holder() is None
	assert analyze.is_singleflight_holder("A") is False


def test_a_stale_job_cache_entry_never_decides_the_holder(env):
	"""Whatever left the flag in this job's ``frappe.local.cache`` (an earlier read, another
	caller), each helper asks Redis: B holds the flag, A's job cache says A."""
	env.in_job("B")
	assert analyze._acquire_singleflight("B", "PS-B", None) is True
	env.server.advance(10)
	env.in_job("A")

	def stale():
		env.local.cache[flag_key()] = "A"

	stale()
	assert analyze.is_singleflight_holder("A") is False
	stale()
	analyze._touch_singleflight("A")
	assert env.holder() == "B" and env.ttl() == TTL - 10
	stale()
	analyze._release_singleflight("A")
	assert env.holder() == "B"
	stale()
	assert analyze._acquire_singleflight("A", "PS-A", None) is False  # B holds it: wait
	assert env.holder() == "B" and env.ttl() == TTL - 10
	# One line, from the touch above; a session waiting at the gate is not a failed or
	# yielding heartbeat (read stale, the acquire would touch, yield and log).
	assert len(env.lines) == 1


def test_the_helpers_never_leave_the_flag_in_the_job_cache(env):
	"""Reads use ``expires=True`` and writes go straight to Redis, so no later plain
	``get_value`` in the same job can be served a flag value from this run."""
	env.in_job("A")
	assert analyze._acquire_singleflight("A", "PS-A", None) is True
	analyze._touch_singleflight("A")
	assert analyze.is_singleflight_holder("A") is True
	assert flag_key() not in env.local.cache
	analyze._release_singleflight("A")
	assert flag_key() not in env.local.cache


def test_a_re_enqueued_holder_refreshes_its_own_flag(env):
	env.in_job("A")
	assert analyze._acquire_singleflight("A", "PS-A", None) is True
	env.server.advance(100)
	env.in_job("A-again")  # its own self-re-enqueue: a new job
	assert analyze._acquire_singleflight("A", "PS-A", None) is True
	assert env.holder() == "A" and env.ttl() == TTL
	assert env.enqueued == []


def test_a_busy_flag_re_enqueues_and_is_left_alone(env):
	env.in_job("A")
	assert analyze._acquire_singleflight("A", "PS-A", None) is True
	env.in_job("B")
	assert analyze._acquire_singleflight("B", "PS-B", None) is False
	assert len(env.enqueued) == 1 and env.enqueued[0]["session_uuid"] == "B"
	assert env.holder() == "A"


def test_a_holder_whose_flag_is_taken_during_its_acquire_waits(env):
	"""A's earlier job holds the flag and A's retry reaches the gate.
	Between acquire's read ("A") and the touch's read, the flag lapses and B takes it. The
	touch yields, so A must wait like any busy session instead of running beside B."""
	env.in_job("A-1")
	assert analyze._acquire_singleflight("A", "PS-A", None) is True
	env.server.advance(200)
	env.in_job("A-2")
	reads = []

	def lapse_before_the_touch_read(command):
		if command == "get":
			reads.append(command)
			if len(reads) == 2:  # the touch's read, right after acquire's read said "A"
				env.server.advance(TTL)
				env.server.put(flag_key(), "B", ex=TTL)

	env.server.before = lapse_before_the_touch_read
	proceeded = analyze._acquire_singleflight("A", "PS-A", None)
	env.server.before = None
	assert proceeded is False
	assert env.holder() == "B"
	assert [k["session_uuid"] for k in env.enqueued] == ["A"]


def test_a_holder_whose_own_refresh_fails_waits(env):
	"""The touch on acquire's own-holder path fails: A cannot confirm it holds the flag, so
	it re-enqueues and tries again instead of proceeding."""
	env.in_job("A-1")
	assert analyze._acquire_singleflight("A", "PS-A", None) is True
	env.in_job("A-2")

	def down_on_expire(command):
		if command == "expire":
			raise connection_error()("redis down")

	env.server.before = down_on_expire
	proceeded = analyze._acquire_singleflight("A", "PS-A", None)
	env.server.before = None
	assert proceeded is False
	assert env.holder() == "A"
	assert [k["session_uuid"] for k in env.enqueued] == ["A"]
	assert len(env.lines) == 1 and "ConnectionError" in env.lines[0][2]


def test_a_flag_freed_after_the_set_nx_waits_one_cycle(env):
	"""The SET NX fails while B holds the flag and B releases before A's
	read. A re-enqueues and waits one throttle cycle; it does not take the flag through
	the touch (the old "free or ours" condition did)."""
	env.server.put(flag_key(), "B", ex=TTL)
	env.in_job("A")

	def release_before_the_read(command):
		if command == "get":
			env.server.unlink(flag_key())

	env.server.before = release_before_the_read
	proceeded = analyze._acquire_singleflight("A", "PS-A", None)
	env.server.before = None
	assert proceeded is False
	assert env.holder() is None
	assert [k["session_uuid"] for k in env.enqueued] == ["A"]
	assert env.sleeps == [analyze._SINGLEFLIGHT_THROTTLE_SECONDS]


def test_a_flag_that_lapses_during_the_refresh_is_taken_back(env):
	"""The flag lapses between the heartbeat's read and its EXPIRE: the EXPIRE finds no key,
	so the heartbeat takes the free flag back."""
	env.in_job("A")
	assert analyze._acquire_singleflight("A", "PS-A", None) is True

	def lapse(command):
		if command == "expire":
			env.server.advance(TTL + 1)

	env.server.before = lapse
	analyze._touch_singleflight("A")
	env.server.before = None
	assert env.holder() == "A" and env.ttl() == TTL
	assert env.lines == []


def test_a_flag_taken_before_the_take_back_stays_with_its_new_holder(env):
	"""The flag lapses before A's EXPIRE and B takes it before A's take-back: the take-back
	is a SET NX, so B keeps the flag and A logs that it yielded."""
	env.in_job("A")
	assert analyze._acquire_singleflight("A", "PS-A", None) is True

	def lapse_then_take(command):
		if command == "expire":
			env.server.advance(TTL + 1)
		elif command == "set":
			env.server.put(flag_key(), "B", ex=TTL)

	env.server.before = lapse_then_take
	analyze._touch_singleflight("A")
	env.server.before = None
	assert env.holder() == "B" and env.ttl() == TTL
	assert len(env.lines) == 1 and "another session" in env.lines[0][2]


def test_an_expire_that_lands_on_the_new_holders_flag_keeps_its_value(env):
	"""The documented window: the flag lapses and B takes it between A's read and A's
	EXPIRE. That EXPIRE renews B's TTL; it never replaces B's value."""
	env.in_job("A")
	assert analyze._acquire_singleflight("A", "PS-A", None) is True

	def lapse_and_take(command):
		if command == "expire":
			env.server.advance(TTL + 1)
			env.server.put(flag_key(), "B", ex=10)

	env.server.before = lapse_and_take
	analyze._touch_singleflight("A")
	env.server.before = None
	assert env.holder() == "B" and env.ttl() == TTL


def test_two_racing_acquires_let_exactly_one_session_through(env, monkeypatch):
	"""Two sessions reach the gate at the same moment: neither sends its second Redis
	command until both have sent their first (a check-then-set gate reads a free flag twice
	and lets both through). Exactly one proceeds and the other re-enqueues."""
	monkeypatch.setattr(frappe, "local", threading.local(), raising=False)
	barrier = threading.Barrier(2, timeout=5)
	sent: dict = {}
	waited: set = set()

	def wait_once():
		name = threading.current_thread().name
		if name not in waited:
			waited.add(name)
			barrier.wait()

	def line_up(command):
		name = threading.current_thread().name
		sent[name] = sent.get(name, 0) + 1
		if sent[name] == 2:
			wait_once()

	env.server.before = line_up
	results, errors = {}, []

	def run(uuid):
		frappe.local.cache = {}
		frappe.local.conf = _Conf(db_name=SITE)
		try:
			results[uuid] = analyze._acquire_singleflight(uuid, f"PS-{uuid}", None)
		except BaseException as exc:  # surfaced below
			errors.append(exc)
		finally:
			wait_once()  # a session done after one command still releases the other

	threads = [threading.Thread(target=run, args=(uuid,), name=uuid) for uuid in ("A", "B")]
	for thread in threads:
		thread.start()
	for thread in threads:
		thread.join(10)
	env.server.before = None
	assert errors == []
	assert sorted(results.values()) == [False, True]
	winner = next(uuid for uuid, proceeded in results.items() if proceeded)
	assert env.holder() == winner
	assert [k["session_uuid"] for k in env.enqueued] == [u for u in ("A", "B") if u != winner]


# --- a heartbeat that fails or yields writes one optimus log line per run -------------


def test_a_heartbeat_that_finds_another_holder_logs_one_line_per_run(env):
	env.in_job("A")
	assert analyze._acquire_singleflight("A", "PS-A", None) is True
	env.in_job("B")
	assert analyze._acquire_singleflight("B", "PS-B", 0.0) is True  # degraded
	for _ in range(3):
		analyze._touch_singleflight("B")
	assert len(env.lines) == 1
	name, level, message = env.lines[0]
	assert (name, level) == ("optimus", "error")  # Frappe drops lower levels on production
	assert "B" in message and "another session" in message
	analyze._release_singleflight("B")  # the run ends; A's flag stays
	assert env.holder() == "A"
	env.in_job("B-again")
	assert analyze._acquire_singleflight("B", "PS-B", 0.0) is True
	analyze._touch_singleflight("B")
	assert len(env.lines) == 1  # the same text within a minute is one line (log_error_line)
	from optimus import safe_call

	safe_call._RECENT_LINES.clear()  # a minute later
	analyze._heartbeat_noted.clear()
	analyze._touch_singleflight("B")
	assert len(env.lines) == 2


def test_a_failing_heartbeat_logs_one_line_naming_the_error(env):
	env.in_job("A")
	assert analyze._acquire_singleflight("A", "PS-A", None) is True

	def down(command):
		raise connection_error()("redis down")

	env.server.before = down
	analyze._touch_singleflight("A")
	analyze._touch_singleflight("A")
	env.server.before = None
	assert len(env.lines) == 1
	name, level, message = env.lines[0]
	assert (name, level) == ("optimus", "error")
	assert "A" in message and "ConnectionError" in message


def test_a_healthy_run_logs_nothing(env):
	env.in_job("A")
	assert analyze._acquire_singleflight("A", "PS-A", None) is True
	for _ in range(3):
		analyze._touch_singleflight("A")
	analyze._release_singleflight("A")
	assert env.lines == []
