"""Linear admission checks before optional SQL parsing of captured/model text."""

MAX_SQL_CHARS = 16_384
MAX_SQL_DEPTH = 100
MAX_SQL_TOKENS = 10_000
MAX_SQL_NESTED_WORK = 50_000
MAX_SQL_COMMAS = 1_000
MAX_SQL_GROUPS = 512


def within_budget(query, *, max_chars=MAX_SQL_CHARS):
	"""Conservatively count nesting and rough tokens, including inside literals.

	This is a cost guard, not SQL validation. Callers retain their own parsing
	and fail-closed fallbacks. Counting quoted parentheses can omit advisory
	output but cannot let an expensive nested expression bypass the bound.
	"""
	if not isinstance(query, str) or len(query) > max_chars:
		return False
	depth = tokens = nested_work = commas = groups = 0
	word = False
	for char in query:
		commas += char == ","
		if commas > MAX_SQL_COMMAS:
			return False
		if char == "(":
			groups += 1
			if groups > MAX_SQL_GROUPS:
				return False
			depth += 1
			if depth > MAX_SQL_DEPTH:
				return False
		elif char == ")":
			depth = max(0, depth - 1)
		nested_work += depth
		if nested_work > MAX_SQL_NESTED_WORK:
			return False
		if char.isspace():
			word = False
		elif char.isalnum() or char in "_$":
			tokens += not word
			word = True
		else:
			tokens += 1
			word = False
		if tokens > MAX_SQL_TOKENS:
			return False
	return True
