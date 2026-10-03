# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

from frappe.model.document import Document


class OptimusPhaseTwoRun(Document):
	def validate(self):
		from optimus.line_profile.jobs import validate_child_journal

		validate_child_journal(self)

	def on_trash(self):
		from optimus.line_profile.jobs import protect_child_journal

		protect_child_journal(self)
