# Frappe data-layer idioms for AI fix suggestions

This is the performance-fix subset behind `FRAPPE_DEV_IDIOMS` in `optimus/ai_prompts.py`. It was checked against the local Frappe v16 sources. The broader upstream reference is [frappe-app-dev](https://github.com/frappe/skills/tree/main/skills/frappe-app-dev).

The prompt focuses on existing code: batching reads, preserving permissions and transactions, and avoiding state shared across sites. App scaffolding and operations are outside that scope.

## The idioms adopted into `FRAPPE_DEV_IDIOMS` (prompt v2)

1. **Caching**: `frappe.get_cached_value` / `frappe.get_cached_doc` for document data (cleared on save; never modify the result), `frappe.db.get_single_value` for Single settings, `@request_cache` / `@redis_cache(ttl=..., user=True when user-dependent)` from `frappe.utils.caching`; never `functools.lru_cache`, module dicts or hand-rolled caches on `frappe.local` / `frappe.flags`; `frappe.cache` is an object. (`caching.md`.)
2. **Single DocTypes**: `get_single_value` / `set_single_value`, not `get_value(dt, None, ...)`. (`database.md`.)
3. **Controllers**: `self.db_set(field, value)` in `on_update` / `on_submit` / `on_cancel` / `after_insert`; never add or remove child rows while iterating them. (`controllers.md`.)
4. **Background work**: `frappe.enqueue(..., queue="long", enqueue_after_commit=True)`, plus `job_id` and `deduplicate=True` per document. (`background-jobs.md`.)
5. **Query builder**: `.orderby(field, order=frappe.qb.desc)`. (`database.md`.)
6. **Large reads**: `.run(as_iterator=True, as_dict=True)` inside `frappe.db.unbuffered_cursor()`, only when the loop makes no other DB call and selects no child-table fields. (`database.md`.)
7. **Batched writes** (in `FRAPPE_REVIEW_RULES`): `frappe.db.set_value(doctype, {filters}, field, value)` / `frappe.db.bulk_update(...)`, which skip validation and hooks, so validated fields keep `doc.save()`.

## Deliberately dropped in prompt v2

- `frappe.qb.get_query` dot-notation joins: it ignores permissions by default; the prompt teaches the same joins through `frappe.get_list` / `frappe.get_all` fields (`"customer.customer_name"`, `{"items": [...]}`), which preserve the replaced call's permission semantics.
- `frappe.db.delete` versus `delete_doc` hook rules: correct, but not something a performance fix changes; dropped to keep the prompt within a 4,096-token window.
- Request memoization on `frappe.local` and `frappe.cache()`: replaced by `frappe.utils.caching`.
- App scaffolding, bench operations, site management, DocType and permission setup: not relevant to fixing a finding.
