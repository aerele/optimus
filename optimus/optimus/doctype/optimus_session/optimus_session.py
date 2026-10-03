# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

from frappe.model.document import Document


class OptimusSession(Document):
	def validate(self):
		from optimus.ai_jobs import validate_session_accounting
		from optimus.line_profile.jobs import validate_parent_journal

		validate_session_accounting(self)
		validate_parent_journal(self)

	def on_trash(self):
		from optimus.ai_jobs import delete_session_state

		delete_session_state(self)
