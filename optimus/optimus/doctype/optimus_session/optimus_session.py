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
