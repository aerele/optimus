# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The generated ``<app>/<app>/optimus_indexes.py`` module: its text template
(``_ENSURE_FUNCTION``), ``ensure_indexes_code`` and the deterministic ``optimus_index_name``.
Pure string work, no Frappe import. The generated text is part of the product, so a change
here must keep ``ensure_indexes_code`` byte-identical (the docs-sync test pins it).
Split out of ``index_recipes`` unchanged; ``index_recipes`` re-exports the public names."""

from __future__ import annotations

import hashlib
import json
import re

HOOK_MODULE = "optimus_indexes"
UNKNOWN_APP = "your_app"
# The hooks.py lists ensure_indexes() is registered in: after_sync
# runs right after the install's fixture sync, so a fixture-shipped Custom Field is
# indexed on a fresh install too.
HOOK_EVENTS: tuple[str, ...] = ("after_install", "after_sync", "after_migrate")
_HOOK_EVENTS_TEXT = ", ".join(HOOK_EVENTS[:-1]) + f" and {HOOK_EVENTS[-1]}"

_APP_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_ENSURE_FUNCTION = '''def ensure_indexes():
	"""Create each index in INDEXES once. One failed entry never stops the others."""
	previous = None
	try:
		# commit what ran before, so a rollback below undoes only this function's own work
		frappe.db.commit()
		previous = _lock_wait()
	except Exception:
		_rollback()
	try:
		for entry in INDEXES:
			try:
				# commit what ran before this entry, so the rollback below undoes only this entry
				frappe.db.commit()
				_ensure_index(entry)
				frappe.db.commit()
			except Exception as error:
				_rollback()
				# a hand-edited entry can lack a "doctype" or not be a dict: the handler itself must
				# not raise, or one bad entry would fail the migrate and skip the entries after it
				title = f"ensure_indexes: an entry was not created ({type(error).__name__})"
				reference = {}
				with contextlib.suppress(Exception):
					title = _title(entry, f"was not created ({type(error).__name__})")
					reference = {"reference_doctype": "DocType", "reference_name": entry["doctype"]}
				try:
					frappe.log_error(title=title, **reference)
					frappe.db.commit()
				except Exception:
					# the Error Log row failed too: migrate's output is the only record left
					with contextlib.suppress(Exception):
						sys.stderr.write(f"optimus_indexes: {title}; the Error Log row could not be written either\\n")
				# a failed Error Log write must not leave a failed transaction for the next hook
				_rollback()
	finally:
		if previous is not None:
			try:
				_lock_wait(previous)
				# on Postgres the restore is part of the open transaction: commit it now, or a
				# later hook's rollback would undo it and the 300 s cap would stay for the session
				frappe.db.commit()
			except Exception:
				_rollback()


def _lock_wait(value=None):
	"""Cap how long this connection waits for a table lock at 300 seconds and return the old
	setting, so an index build on a busy table gives up instead of holding every later query
	on that table behind it. Called with that old setting, put it back."""
	if frappe.db.db_type == "mariadb":
		read, cap = "select @@session.lock_wait_timeout", 300
		write = "set session lock_wait_timeout = %s"
	elif frappe.db.db_type == "postgres":
		read, cap = "select current_setting('lock_timeout')", "300s"
		write = "select set_config('lock_timeout', %s, false)"
	else:
		return None
	previous = frappe.db.sql(read)[0][0] if value is None else None
	frappe.db.sql(write, (cap if value is None else value,))
	return previous


def _rollback():
	# on Postgres a failed statement aborts the transaction until it is rolled back
	with contextlib.suppress(Exception):
		frappe.db.rollback()


def _title(entry, what):
	# the key and what happened first: a cut to 140 characters (Error Log.method) can only
	# shorten the DocType, which reference_name also holds
	key = entry.get("index_name") or entry.get("search_index_field")
	return f"ensure_indexes: {key} {what} on {entry['doctype']}"[:140]


def _ensure_index(entry):
	doctype = entry["doctype"]
	if entry.get("db", frappe.db.db_type) != frappe.db.db_type:
		title = _title(entry, f"skipped on {frappe.db.db_type} (the entry is for {entry['db']})")
		# one row per entry, looked up by the reference columns: MariaDB indexes one of them,
		# never the title (method); on Postgres Frappe may not have indexed either
		reference = {"reference_doctype": "DocType", "reference_name": doctype}
		if not frappe.db.exists("Error Log", {**reference, "method": title}):
			frappe.log_error(title=title, **reference)
		return
	if not frappe.db.table_exists(doctype, cached=False):
		return
	field = entry.get("search_index_field")
	if field:
		if not frappe.db.has_column(doctype, field):
			return
		# the index first: a failed build leaves no Property Setter that a later sync of
		# this DocType would act on outside this guard
		if not frappe.db.get_column_index(f"tab{doctype}", field, unique=False):
			frappe.db.add_index(doctype, [field], index_name=f"{field}_index")
		# then Search Index on the field, so Frappe's schema sync keeps the index
		if not frappe.db.exists(
			"Property Setter",
			{"doc_type": doctype, "field_name": field, "property": "search_index", "value": "1"},
		):
			from frappe.custom.doctype.property_setter.property_setter import make_property_setter

			make_property_setter(doctype, field, "search_index", 1, "Check", validate_fields_for_doctype=False)
		return
	columns = entry["columns"]
	if not all(frappe.db.has_column(doctype, column.split("(", 1)[0]) for column in columns):
		return
	if frappe.db.has_index(f"tab{doctype}", entry["index_name"]):
		return
	frappe.db.add_index(doctype, columns, index_name=entry["index_name"])
'''


def optimus_index_name(doctype: str, base_cols) -> str:
	"""``idx_<slug>_<hash8>``: at most 53 characters (MariaDB allows 64, Postgres 63) and
	unique across the schema, because the hash covers the table and the columns."""
	slug = re.sub(r"[^a-z0-9]", "_", str(doctype).lower())[:40]
	key = "tab" + str(doctype) + "|" + ",".join(base_cols)
	return f"idx_{slug}_{hashlib.sha1(key.encode('utf-8'), usedforsecurity=False).hexdigest()[:8]}"


def _string_hook_pair(hook: str) -> str:
	"""A hooks.py value set as a string, turned into a list with ensure_indexes last:
	a list pasted under the string would replace it, or be replaced by it."""
	return f'["<the string already there>", "{hook}"]'


def ensure_indexes_code(entries: list[dict], *, app_name: str = UNKNOWN_APP) -> str:
	"""The developer's ``<app>/<app>/optimus_indexes.py``: the hooks.py lines as comments,
	then ``INDEXES`` (one JSON literal per entry) and ``ensure_indexes()``."""
	app = app_name if _APP_RE.fullmatch(app_name or "") else UNKNOWN_APP
	hook = f"{app}.{HOOK_MODULE}.ensure_indexes"
	body = "".join(f"\t{json.dumps(entry)},\n" for entry in entries)
	hook_lines = "".join(f'#   {event} = ["{hook}"]\n' for event in HOOK_EVENTS)
	return (
		f"# {app}/{app}/{HOOK_MODULE}.py\n"
		f"# In {app}/hooks.py run it after install, right after the install's fixture sync and after\n"
		"# every migrate. Add it as the last item of each list:\n"
		+ hook_lines
		+ "# When hooks.py sets one of them as a string, make it a list that keeps that string first:\n"
		f"#   after_migrate = {_string_hook_pair(hook)}\n"
		+ "import contextlib\nimport sys\n\nimport frappe\n\n"
		'# An entry with "db" runs only on that database (frappe.db.db_type).\n'
		"INDEXES = [\n" + body + "]\n\n\n" + _ENSURE_FUNCTION
	)
