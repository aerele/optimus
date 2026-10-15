"""Long-lived workers must observe permission changes from another process."""

from types import SimpleNamespace

import pytest

from optimus import ai_jobs
from optimus.line_profile import jobs as phase2


@pytest.mark.parametrize("worker", ["ai", "phase2"])
@pytest.mark.parametrize("revoke", ["roles", "user_permissions", "role_permissions"])
def test_worker_authorization_discards_only_requester_local_permissions(monkeypatch, worker, revoke):
	user = "requester@example.test"
	parent = dict(name="session", session_uuid="uuid", status="Ready", owner="someone-else")
	run = dict(session_name="session", session_uuid="uuid", requested_by=user)
	shared = {"roles": ["Optimus User", "System Manager"], "user_permissions": {"allowed": True}}
	local = SimpleNamespace(cache={"unrelated": "keep"}, role_permissions={})
	def make_key(name):
		return ("site:" + name).encode()
	def hget(name):
		return local.cache.setdefault(make_key(name), {}).setdefault(user, shared[name])
	def has_permission(*args, **kwargs):
		return local.role_permissions.setdefault(("Optimus Session", user, False),
			"System Manager" in hget("roles") and hget("user_permissions")["allowed"])
	fake = SimpleNamespace(local=local, cache=SimpleNamespace(make_key=make_key),
		db=SimpleNamespace(get_value=lambda table, *a, **kw: parent if table == "Optimus Session" else 1,
			get_values=lambda *a, **kw: []), get_roles=lambda user: hget("roles"),
		get_doc=lambda values: values, has_permission=has_permission)
	monkeypatch.setattr(ai_jobs, "frappe", fake)
	monkeypatch.setattr(phase2, "frappe", fake)
	monkeypatch.setattr(ai_jobs, "_the_row_answers_as_the_name", lambda user: True)
	def allowed():
		if worker == "phase2":
			return phase2._authorized(parent, user)
		try:
			ai_jobs._check_run(run, locked=True)
		except ai_jobs.StopRefresh as exc:
			assert exc.reason == "permission"
			return False
		return True
	assert allowed()
	local.cache[make_key("roles")]["other"] = ["keep"]
	local.role_permissions[("Optimus Session", "other", False)] = {"keep": True}
	if revoke == "roles":
		shared["roles"] = ["All"]
	elif revoke == "user_permissions":
		shared["user_permissions"] = {"allowed": False}
	else:
		# The user keeps the gate role but loses write on someone else's session.
		shared["roles"] = ["Optimus User"]
	assert not allowed()
	assert local.cache["unrelated"] == "keep"
	assert local.cache[make_key("roles")]["other"] == ["keep"]
	assert local.role_permissions[("Optimus Session", "other", False)] == {"keep": True}


@pytest.mark.parametrize("worker", ["ai", "phase2"])
def test_real_frappe_role_cache_observes_cross_process_revocation(monkeypatch, worker):
	import pickle

	import frappe
	if not getattr(frappe, "__file__", None):
		pytest.skip("Requires the real Frappe role cache")
	redis_wrapper = pytest.importorskip("frappe.utils.redis_wrapper")
	import redis

	user = "requester@example.test"
	monkeypatch.setattr(frappe.local, "cache", {}, raising=False)
	monkeypatch.setattr(frappe.local, "role_permissions", {}, raising=False)
	monkeypatch.setattr(frappe.local, "conf", frappe._dict(db_name="disposable_test"), raising=False)
	monkeypatch.setattr(frappe.local, "session", frappe._dict(user=user), raising=False)
	cache = redis_wrapper.RedisWrapper()
	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	roles = ["Optimus User", "All"]
	reads = []
	def read(_self, name, key):
		reads.append((name, key))
		return pickle.dumps(roles)
	monkeypatch.setattr(redis.Redis, "hget", read)
	parent = dict(name="session", session_uuid="uuid", status="Ready", owner=user)
	fake = SimpleNamespace(local=frappe.local, cache=cache, get_roles=frappe.get_roles,
		db=SimpleNamespace(get_value=lambda table, *a, **kw: parent if table == "Optimus Session" else 1,
			get_values=lambda *a, **kw: []), get_doc=lambda values: values, has_permission=lambda *a, **kw: True)
	monkeypatch.setattr(ai_jobs, "frappe", fake)
	monkeypatch.setattr(phase2, "frappe", fake)
	monkeypatch.setattr(ai_jobs, "_the_row_answers_as_the_name", lambda user: True)
	run = dict(session_name="session", session_uuid="uuid", requested_by=user)
	if worker == "ai":
		assert ai_jobs._check_run(run) == parent
	else:
		assert phase2._authorized(parent, user)
	roles[:] = ["All"]  # Another process has rebuilt shared Redis after revocation.
	if worker == "ai":
		with pytest.raises(ai_jobs.StopRefresh, match="permission"):
			ai_jobs._check_run(run, locked=True)
	else:
		assert not phase2._authorized(parent, user)
	assert len(reads) == 2
