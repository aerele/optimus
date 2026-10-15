# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

from frappe.model.document import Document


class OptimusSession(Document):
	def before_validate(self):
		# The AI counters (ai_tokens_spent, ai_refresh_count) only change through SQL
		# increments: keep the stored values so a save never writes back counts older than
		# the row's (analyze._keep_session_counters). Runs on every save.
		from optimus.analyze import _keep_session_counters

		_keep_session_counters(self)

	def validate(self):
		from optimus.ai_jobs import validate_session_accounting
		from optimus.line_profile.jobs import validate_parent_journal

		validate_session_accounting(self)
		validate_parent_journal(self)

	def on_trash(self):
		from optimus.ai_jobs import delete_session_state

		delete_session_state(self)
