# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

from frappe.model.document import Document


class OptimusSession(Document):
	def validate(self):
		from optimus.line_profile.jobs import validate_parent_journal

		validate_parent_journal(self)
