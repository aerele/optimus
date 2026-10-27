# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The facts the index advisor may rely on for one table: frozen dataclasses with no
Frappe import. They live here, not in ``recipe_enrichment`` (which reads Frappe), so the
pure ``index_recipes`` and ``recipe_enrichment`` both import them without a cycle.
``recipe_enrichment`` re-exports the three names."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class FieldEvidence:
	"""One DocField's index-relevant flags (Custom Fields included)."""

	fieldtype: str
	length: int
	search_index: bool
	unique: bool
	is_custom_field: bool


@dataclass(frozen=True)
class IndexEvidence:
	"""One existing index: its name, its columns in key order, and uniqueness."""

	name: str
	columns: tuple[str, ...]
	unique: bool
	eligible: bool = True
	reason: str = ""
	usable_prefix: tuple[str, ...] = ()


@dataclass(frozen=True)
class TableEvidence:
	"""What the index advisor may rely on for one ``tab*`` table:
	DocField flags by fieldname, the DocType's app, the real column types and the
	indexes the database already has."""

	table: str
	doctype: str
	app: str
	is_custom_doctype: bool
	dialect: str
	fields: Mapping[str, FieldEvidence]
	column_types: Mapping[str, str]
	text_columns: frozenset[str]
	unindexable_columns: frozenset[str]
	indexes: tuple[IndexEvidence, ...]

	@property
	def usable_indexes(self):
		# A partial usable prefix is evidence for coverage, never for uniqueness
		# of that prefix or the unusable trailing part of the physical index.
		return tuple(ix if ix.eligible else replace(ix, columns=ix.usable_prefix, unique=False, eligible=True)
			for ix in self.indexes if ix.eligible or ix.usable_prefix)
