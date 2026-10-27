# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Session updates for the recording user's widget and authorized open forms."""

from uuid import uuid4

import frappe

from optimus import safe_call


def publish_session_event(event: str, payload: dict) -> None:
	"""Resolve the recording user; missing sessions/principals never broadcast.

	Explicit rooms avoid Frappe's ambient task_id routing. Document subscriptions
	check read permission in Frappe, preserving updates for shared open forms.
	Phase 2 callers supply their authorized capture/analysis actor in ``user``;
	that actor also receives updates in their other Desk tabs.
	"""
	docname = payload.get("docname") or payload.get("parent")
	session_uuid = payload.get("session_uuid")
	if not docname and not session_uuid:
		return
	filters = {"name": docname} if docname else {"session_uuid": session_uuid}
	if session_uuid:
		filters["session_uuid"] = session_uuid
	row = safe_call.best_effort(
		lambda: frappe.db.get_value("Optimus Session", filters, ["name", "user"], as_dict=True), None
	)
	if not row or not row.get("name") or not row.get("user") or row["user"] == "Guest":
		return
	message = {**payload, "recording_user": row["user"], "event_id": uuid4().hex}
	rooms = [f"user:{row['user']}", f"doc:Optimus Session/{row['name']}"]
	actor = payload.get("user")
	if event.startswith("phase_2_") and actor and actor not in {row["user"], "Guest"}:
		rooms.append(f"user:{actor}")
	for room in rooms:
		safe_call.best_effort(lambda: frappe.publish_realtime(event, message, room=room), None)
