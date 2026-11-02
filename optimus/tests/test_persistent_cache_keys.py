# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""frappe.clear_cache() must keep live sessions and recordings."""

from optimus import hooks, redis_keys


def test_profiler_keys_survive_clear_cache():
	assert f"{redis_keys.KEY_PREFIX}*" in hooks.persistent_cache_keys


def test_settings_cache_is_still_cleared():
	assert not redis_keys.settings_cache().startswith(redis_keys.KEY_PREFIX)
