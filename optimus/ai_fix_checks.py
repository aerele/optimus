"""Bounded checks for visible writes, permission changes and read replacements.

Pure text/AST inspection: proposed code is never run. A parsed fragment supplies
evidence only about that fragment, not the surrounding application or runtime data.
"""

import ast
import io
import logging
import re
import textwrap
import tokenize
from collections import Counter, defaultdict

import sqlparse

from optimus.ai_guardrails import _dotted, _is_diff, _norm, _parse, _v, diff_rows

_DB = {"frappe.db", "frappe.local.db"}
_READS = {"frappe.get_doc", "frappe.get_last_doc", "frappe.get_value", "frappe.get_all", "frappe.get_list"}
_READS |= {f"{db}.{method}" for db in _DB for method in ("get_value", "get_values", "get_all", "get_list", "count", "exists")}
_SQL_READS = {f"{db}.{method}" for db in _DB for method in ("sql", "sql_list", "multisql")}
_WRITES = {f"{db}.{method}" for db in _DB for method in ("delete", "set_value", "truncate")}
_MISSING = object()


def _key(node):
	return ast.dump(node, include_attributes=False) if isinstance(node, ast.AST) else repr(node)


def _arg(call, index, names, default=_MISSING):
	for kw in call.keywords:
		if kw.arg in names:
			return kw.value
	return call.args[index] if len(call.args) > index else default


def _literal(node):
	return node.value if isinstance(node, ast.Constant) else _MISSING


def _empty(node):
	if node is _MISSING:
		return True
	if isinstance(node, ast.Constant):
		return node.value is None or node.value == ""
	if isinstance(node, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
		return not (node.keys if isinstance(node, ast.Dict) else node.elts)
	return isinstance(node, ast.Call) and _dotted(node.func) in {"dict", "list", "tuple"} and not node.args and not node.keywords


class View:
	def __init__(self, rows, moved=()):
		self.rows, self.moved = rows, set(moved)
		text = "\n".join(line for line, _ in rows)
		self.tree, self.parsed, self.off = _parse(text)
		self.repaired = False
		if self.tree is None:
			# Repair only clause/opening indentation for inspection. Original rows
			# remain authoritative for change spans and control-boundary identities.
			lines = textwrap.dedent(text.expandtabs(4)).split("\n")
			lines = [re.sub(r"^(\s*)(?:else|except(?:\s+[^:]*)?|finally):", r"\1if True:", s) for s in lines]
			lines = [re.sub(r"^(\s*)elif\s", r"\1if ", s) for s in lines]
			first = next((len(s) - len(s.lstrip()) for s in lines if s.strip()), 0)
			levels = sorted({len(s) - len(s.lstrip()) for s in lines if s.strip() and len(s) - len(s.lstrip()) < first})
			if len(levels) <= 16:
				prefix = [" " * n + "if True:" for n in levels]
				self.tree, self.parsed, extra = _parse("\n".join(prefix + lines))
				self.off = len(prefix) + extra
				self.repaired = self.tree is not None
		self.parents = {child: node for node in ast.walk(self.tree) for child in ast.iter_child_nodes(node)} if self.tree else {}
		self.calls = [n for n in ast.walk(self.tree) if isinstance(n, ast.Call)] if self.tree else []
		from optimus.ai_aliases import Aliases
		self.aliases = Aliases(self.tree) if self.tree else None


	def ancestors(self, node):
		while node in self.parents:
			node = self.parents[node]
			yield node


	def scope(self, node):
		return tuple(reversed([n.name for n in self.ancestors(node)
		                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.lineno > self.off]))


	def name(self, call):
		return self.aliases.call_name(call, _dotted) if self.aliases else _dotted(call.func)


	def changed(self, node, include_moved=False):
		start = max(0, node.lineno - 1 - self.off)
		end = min(len(self.rows), node.end_lineno - self.off)
		return any(new and (include_moved or _norm(line) not in self.moved) for line, new in self.rows[start:end])


	def boundary(self, node):
		out, child = [], node
		for parent in self.ancestors(node):
			if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
				break
			if getattr(parent, "lineno", 0) <= self.off:
				child = parent
				continue
			kind = type(parent).__name__
			if isinstance(parent, (ast.For, ast.AsyncFor)):
				identity = (_key(parent.target), _key(parent.iter))
			elif isinstance(parent, (ast.If, ast.While)):
				identity = (_key(parent.test),)
			elif isinstance(parent, (ast.With, ast.AsyncWith)):
				identity = tuple(_key(i) for i in parent.items)
			elif isinstance(parent, ast.ExceptHandler):
				identity = (_key(parent.type),)
			elif isinstance(parent, ast.Try):
				identity = tuple(_key(h.type) for h in parent.handlers)
			else:
				child = parent
				continue
			branch = next((field for field in ("body", "orelse", "finalbody", "handlers") if child in getattr(parent, field, [])), "")
			# A repaired orphan clause must not masquerade as the real condition.
			index = parent.lineno - 1 - self.off
			row = self.rows[index][0].strip() if 0 <= index < len(self.rows) else ""
			if row.startswith(("else:", "except", "finally:")):
				identity = (row,)
			out.append((kind, identity, branch))
			child = parent
		return tuple(reversed(out))


def _write_check(call, name):
	if name in _WRITES:
		method = name.rsplit(".", 1)[-1]
		selector = _arg(call, 1, {"filters", "conditions"} if method == "delete" else {"dn"})
		false_selector = isinstance(selector, ast.Constant) and not selector.value
		if method == "truncate" or (method == "delete" and (_empty(selector) or false_selector)) or (
			method == "set_value" and selector is not _MISSING and _literal(selector) is not None and _empty(selector)
		):
			return _v("unbounded-write", name)
	if name.startswith("frappe.qb.") and name.endswith(".run"):
		methods = name.replace("()", "").split(".")
		if {"update", "delete"}.intersection(methods) and "where" not in methods:
			return _v("unbounded-write", "Query Builder write without a WHERE clause")
	return None


def _permission(view, call):
	# Local Frappe v16: doctype, ptype, doc, user, throw (fifth positional).
	values = [_arg(call, i, {name}, ast.Constant(value=default))
	          for i, (name, default) in enumerate((("doctype", None), ("ptype", "read"), ("doc", None), ("user", None), ("throw", False)))]
	used = not isinstance(view.parents.get(call), ast.Expr)
	throw = _literal(values[4])
	if throw is None or type(throw) in (bool, int) and throw in (0, 1):
		values[4] = ast.Constant(value=bool(throw))
	mode = "throw" if throw is True or type(throw) is int and throw == 1 else "used" if used else "discarded"
	extra = tuple(sorted((kw.arg, _key(kw.value)) for kw in call.keywords
	                     if kw.arg and kw.arg not in {"doctype", "ptype", "doc", "user", "throw"}))
	return (*[_key(v) for v in values[:4]], _key(values[4]), extra, mode)


def _fragment_checks(view, *, compare_permissions=True):
	"""Inspect complete call spans even when surrounding Python is incomplete."""
	text = "\n".join(line.lstrip() for line, _ in view.rows)
	tokens = []
	try:
		for token in tokenize.generate_tokens(io.StringIO(text).readline):
			if token.type in (tokenize.NAME, tokenize.OP, tokenize.STRING, tokenize.NUMBER):
				tokens.append(token)
	except (tokenize.TokenError, IndentationError, SyntaxError):
		pass
	out, spans = [], 0
	for i, token in enumerate(tokens):
		if token.type != tokenize.NAME or i and tokens[i - 1].string == ".":
			continue
		name, j = token.string, i + 1
		while j + 1 < len(tokens) and tokens[j].string == "." and tokens[j + 1].type == tokenize.NAME:
			name += "." + tokens[j + 1].string
			j += 2
		if name not in _WRITES | {"frappe.has_permission"} or j >= len(tokens) or tokens[j].string != "(":
			continue
		spans += 1
		if spans > 256:
			out.append(_v("unverified-structure"))
			break
		depth, end = 0, j
		for end in range(j, len(tokens)):
			if tokens[end].type == tokenize.OP:
				depth += (tokens[end].string == "(") - (tokens[end].string == ")")
				if not depth:
					break
		start_line, end_line = token.start[0] - 1, tokens[end].end[0]
		if not any(new for _, new in view.rows[start_line:end_line]):
			continue
		code = tokenize.untokenize([(t.type, t.string) for t in tokens[i:end + 1]])
		try:
			call = ast.parse(code, mode="eval").body if len(code) <= 4096 else None
		except (SyntaxError, RecursionError):
			call = None
		if name == "frappe.has_permission":
			if compare_permissions:
				out.append(_v("permission-behavior-change", "unverifiable permission fragment"))
		elif isinstance(call, ast.Call):
			violation = _write_check(call, name)
			if violation:
				out.append(violation)
		else:
			out.append(_v("unbounded-write", "unverifiable destructive call"))
	return out


def _permission_changes(before, after):
	out = []
	old = defaultdict(list)
	new = defaultdict(list)
	for view, groups in ((before, old), (after, new)):
		for call in view.calls:
			if view.name(call) == "frappe.has_permission":
				if view.changed(call, True) and (any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords) or len(call.args) > 5):
					out.append(_v("unverified-safety", "dynamic or unsupported permission arguments"))
				values = _permission(view, call)
				groups[(view.scope(call), values[:2])].append((values, view.boundary(call), call))
	for key, calls in new.items():
		prior = old.get(key)
		if not prior:
			if any(k[0] == key[0] for k in old) and any(after.changed(c, True) for _, _, c in calls):
				out.append(_v("permission-behavior-change", "changes the checks performed"))
			continue
		for values, boundary, call in calls:
			matches = [(v, b) for v, b, _ in prior if values == v]
			if not matches and after.changed(call, True):
				out.append(_v("permission-behavior-change", "changes permission arguments or denial behavior"))
			elif matches and values[-1] != "discarded" and all(boundary != b for _, b in matches):
				out.append(_v("permission-behavior-change", "moves an enforced check across an execution boundary"))
	if any(key not in new for key in old):
		out.append(_v("permission-behavior-change", "removes an individual permission check"))
	for key in old.keys() & new.keys():
		# Distinct documents/users under the same permission type are separate
		# checks. Identical repeated checks can still be moved verbatim.
		remaining = {values for values, _, _ in new[key]}
		if any(values not in remaining for values, _, _ in old[key]):
			out.append(_v("permission-behavior-change", "removes or changes an individual permission check"))
	out.extend(_denial_changes(before, after))
	return out


def _denial_changes(before, after):
	"""Compare visible denial consumers of direct and assigned permission results."""
	ambiguous = []
	def branches(view):
		groups = defaultdict(set)
		for call in view.calls:
			if view.name(call) != "frappe.has_permission":
				continue
			assigned = set()
			parent = view.parents.get(call)
			if isinstance(parent, ast.Assign) and parent.value is call:
				assigned = {target.id for target in parent.targets if isinstance(target, ast.Name)}
			elif isinstance(parent, ast.AnnAssign) and parent.value is call and isinstance(parent.target, ast.Name):
				assigned = {parent.target.id}
			if view is after and assigned:
				bindings = [n for n in ast.walk(view.tree) if isinstance(n, ast.Name)
					and isinstance(n.ctx, (ast.Store, ast.Del)) and n.id in assigned and view.scope(n) == view.scope(call)]
				if len(bindings) > len(assigned) and any(view.changed(n, True) for n in bindings):
					ambiguous.append(_v("unverified-safety", "a permission result has mixed bindings"))
			# Local result aliases are bounded and scope-local. Any ambiguous reuse
			# still retains the old consumer as a comparison obligation.
			for _ in range(16):
				prior = set(assigned)
				for node in ast.walk(view.tree):
					if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Name)
						and node.value.id in assigned and view.scope(node) == view.scope(call)):
						assigned.update(t.id for t in node.targets if isinstance(t, ast.Name))
				if prior == assigned:
					break
			for node in ast.walk(view.tree):
				if not isinstance(node, (ast.If, ast.While)) or view.scope(node) != view.scope(call):
					continue
				test_nodes = list(ast.walk(node.test))
				if call not in test_nodes and not any(isinstance(n, ast.Name) and n.id in assigned for n in test_nodes):
					continue
				for branch in ("body", "orelse"):
					denials = frozenset(_key(n) for statement in getattr(node, branch) for n in ast.walk(statement)
						if isinstance(n, (ast.Raise, ast.Return)) or isinstance(n, ast.Call) and view.name(n) == "frappe.throw")
					groups[(view.scope(call), _permission(view, call)[:4], _key(node.test), branch)].add(denials)
		return groups
	old, new = branches(before), branches(after)
	if any(not any(denials <= remaining for remaining in new.get(key, ()))
	       for key, choices in old.items() for denials in choices if denials):
		return [_v("permission-behavior-change", "removes or changes a visible permission denial branch")]
	return ambiguous


def _read_name(name):
	return name in _READS | _SQL_READS or name.startswith("frappe.qb.") and name.endswith(".run")


def _read(view, call):
	return _read_name(view.name(call))


def _loop_contexts(view, call):
	contexts = []
	child = call
	for node in view.ancestors(call):
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
			break
		if isinstance(node, (ast.For, ast.AsyncFor)):
			# Calls in the iterator execute once for this loop.
			if child in node.body:
				contexts.append((view.scope(call), "iter", _key(node.iter)))
		if isinstance(node, ast.While):
			if child not in node.orelse:
				contexts.append((view.scope(call), "while", _key(node.test)))
		if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
			# Generator iterables run once per preceding generator, not per item
			# of themselves. Elements and filters run once for every generator.
			comp_contexts = []
			for generator in node.generators:
				if call in ast.walk(generator.iter):
					break
				comp_contexts.append((view.scope(call), "iter", _key(generator.iter)))
				if any(call in ast.walk(condition) for condition in generator.ifs):
					break
			contexts.extend(reversed(comp_contexts))
		child = node
	return tuple(reversed(contexts))


def _loop_context(view, call):
	contexts = _loop_contexts(view, call)
	return contexts[-1] if contexts else None


def _batch_or_memo(view, call):
	for n in ast.walk(call):
		if isinstance(n, (ast.Tuple, ast.List)) and n.elts and _literal(n.elts[0]) in ("in", "not in"):
			return True
		if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "isin":
			return True
	if view.name(call) == "frappe.get_doc" and len(call.args) == 1:
		return True
	assignment = next((n for n in view.ancestors(call) if isinstance(n, ast.Assign)), None)
	for node in view.ancestors(call):
		if isinstance(node, (ast.For, ast.FunctionDef, ast.AsyncFunctionDef)):
			break
		if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.NotIn):
			if assignment and any(isinstance(t, ast.Subscript) and _key(t.value) == _key(node.test.comparators[0])
			                      and _key(t.slice) == _key(node.test.left) for t in assignment.targets):
				return True
	return False


def _loop_replacements(before, after):
	old = {_loop_context(before, c) for c in before.calls if _read(before, c) and before.changed(c, True)} - {None}
	if not old:
		return []
	for call in after.calls:
		if _read(after, call) and after.changed(call) and _loop_context(after, call) in old and not _batch_or_memo(after, call):
			return [_v("loop-query", after.name(call))]
	return []


def _loop_reduction(before, after):
	"""Reject API-only N+1 rewrites; incomplete evidence needs a fuller diff.

	An IN filter in a repeated call does not establish batching. Only an actual
	move outside the shown loop, a cached read, or a memoized read can do that.
	This check runs on new N+1 answers only, never historical render selection.
	"""
	# A whole-function diff often repeats unchanged reads as +/- lines. Cancel
	# identical calls with identical loop ancestry before judging replacements.
	def signature(view, call):
		return (view.name(call), _key(call), view.scope(call), view.boundary(call), _loop_contexts(view, call))
	old = [c for c in before.calls if _read(before, c)]
	new = [c for c in after.calls if _read(after, c)]
	has_reads = bool(old or new)
	old_counts = Counter(signature(before, c) for c in old)
	new_counts = Counter(signature(after, c) for c in new)
	def changed(view, calls, counts):
		out = []
		for call in calls:
			key = signature(view, call)
			if counts[key]:
				counts[key] -= 1
			else:
				out.append(call)
		return out
	old, new = changed(before, old, new_counts), changed(after, new, old_counts)
	if not new:
		if not old and has_reads:
			return [_v("unverified-loop-fix", "the diff leaves the database reads unchanged")]
		return []
	if not old:
		return [_v("unverified-loop-fix", "show a complete grounded diff for the repeated read")]
	if before.repaired or after.repaired:
		return [_v("unverified-loop-fix", "show the enclosing loop and complete read replacement")]
	chains = {_loop_contexts(before, c) for c in old}
	contexts = {chain[-1] for chain in chains if chain}
	scopes = {before.scope(call) for call in old}
	for call in new:
		if any(isinstance(node, ast.Lambda) for node in after.ancestors(call)):
			return [_v("unverified-loop-fix", "a lambda can still execute the read once per item")]
		# Constructing a document from an explicit dict is not a database read.
		if after.name(call) == "frappe.get_doc" and len(call.args) == 1 and isinstance(call.args[0], ast.Dict):
			continue
		if after.scope(call) not in scopes:
			return [_v("unverified-loop-fix", "moving a read into a helper does not establish fewer calls")]
		# A memo guard is distinct from a mere IN-filter batch claim.
		if _memoized(after, call):
			continue
		chain = _loop_contexts(after, call)
		if contexts.intersection(chain):
			return [_v("ineffective-loop-fix", after.name(call))]
		if not contexts or not any(len(chain) < len(prior) and prior[:len(chain)] == chain for prior in chains):
			return [_v("unverified-loop-fix", "show the same enclosing loop and where its read moves")]
	return []


def _memoized(view, call):
	assignment = next((n for n in view.ancestors(call) if isinstance(n, ast.Assign)), None)
	child = call
	for node in view.ancestors(call):
		if isinstance(node, (ast.For, ast.AsyncFor, ast.While, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
			break
		if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.NotIn):
			if child not in node.body:
				return False
			if assignment and any(isinstance(t, ast.Subscript) and _key(t.value) == _key(node.test.comparators[0])
			                      and _key(t.slice) == _key(node.test.left) for t in assignment.targets):
				memo = node.test.comparators[0]
				if not isinstance(memo, ast.Name):
					return False
				# Require one visible dict initialization before the repeated loop,
				# with no rebinding, deletion or method mutation inside the loop.
				bindings = [n for n in ast.walk(view.tree) if isinstance(n, ast.Name)
				            and isinstance(n.ctx, (ast.Store, ast.Del)) and n.id == memo.id
				            and view.scope(n) == view.scope(call)]
				if len(bindings) != 1:
					return False
				binding = bindings[0]
				init = view.parents.get(binding)
				if not (isinstance(init, ast.Assign) and isinstance(init.value, ast.Dict)
				        and binding.lineno < call.lineno
				        and not set(_loop_contexts(view, binding)).intersection(_loop_contexts(view, call))):
					return False
				if any(isinstance(n, ast.Subscript) and isinstance(n.ctx, (ast.Store, ast.Del))
				       and _key(n.value) == _key(memo) and n not in assignment.targets
				       or isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and _key(n.func.value) == _key(memo)
				       and n.func.attr not in {"get", "keys", "values", "items", "copy"}
				       for n in ast.walk(view.tree)):
					return False
				if any(isinstance(n, ast.Name) and n.id == memo.id and isinstance(n.ctx, ast.Load)
				       and isinstance(view.parents.get(n), (ast.Assign, ast.AnnAssign, ast.Call))
				       for n in ast.walk(view.tree)):
					return False  # an alias or unknown consumer could reset the memo
				return True
		child = node
	return False


def _sql_clauses(query):
	from optimus.sql_budget import within_budget

	if not within_budget(query, max_chars=4096):
		return None
	try:
		statements = sqlparse.parse(query)
		if len(statements) != 1 or statements[0].get_type() != "SELECT":
			return None
		tokens = [t for t in statements[0].tokens if not t.is_whitespace and not isinstance(t, sqlparse.sql.Comment)]
		words = [t.normalized.upper() if t.ttype in sqlparse.tokens.Keyword else "" for t in tokens]
		if words.count("FROM") != 1 or any("JOIN" in w or w in {"UNION", "INTERSECT", "EXCEPT", "GROUP BY"} for w in words):
			return None
		from_index = words.index("FROM") + 1
		table = tokens[from_index]
		if not isinstance(table, sqlparse.sql.Identifier) or table.get_alias() or table.get_parent_name():
			return None
		name = table.get_real_name()
		if not name or not name.startswith("tab") or any(isinstance(t, sqlparse.sql.Parenthesis) for t in table.tokens):
			return None
		result = {"doctype": name[3:]}
		for keyword in ("LIMIT", "OFFSET"):
			if keyword in words:
				value = tokens[words.index(keyword) + 1].value.strip()
				if not re.fullmatch(r"\d+(?:\s*,\s*\d+)?", value):
					return None
				numbers = [int(n) for n in value.split(",")]
				if len(numbers) == 2:
					result["offset"], result["limit"] = numbers
				else:
					result[keyword.lower()] = numbers[0]
		result["order"] = "ORDER BY" in words
		return result
	except (IndexError, ValueError, RecursionError, sqlparse.exceptions.SQLParseError):
		return None


def _assigned_calls(view, names):
	return [(node.targets[0].id, node.value) for node in ast.walk(view.tree)
	        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
	        and isinstance(node.value, ast.Call) and view.name(node.value) in names and view.changed(node.value, True)] if view.tree else []


def _sql_replacement(before, after):
	old = _assigned_calls(before, {f"{db}.sql" for db in _DB})
	new = _assigned_calls(after, _READS - {"frappe.get_doc", "frappe.get_last_doc"})
	if len(old) != 1 or len(new) != 1 or old[0][0] != new[0][0] or before.scope(old[0][1]) != after.scope(new[0][1]):
		return []
	call, replacement = old[0][1], new[0][1]
	clauses = _sql_clauses(_literal(_arg(call, 0, {"query"})))
	if not clauses or _literal(_arg(replacement, 0, {"doctype"})) != clauses["doctype"]:
		return []
	if any(k.arg is None for k in replacement.keywords):
		return []  # dynamically supplied parameters are not statically comparable
	out = []
	for clause, aliases in (("limit", ("limit", "limit_page_length", "page_length")), ("offset", ("offset", "limit_start", "start"))):
		if clause not in clauses:
			continue
		expected = clauses[clause]
		kw = {k.arg: k.value for k in replacement.keywords}
		value = next((kw[a] for i, a in enumerate(aliases) if a in kw and _literal(kw[a]) is not None
		              and (i == 0 or not isinstance(kw[a], ast.Constant) or bool(kw[a].value))), _MISSING)
		actual = _literal(value)
		if clause == "limit" and after.name(replacement).endswith("get_value"):
			actual = 1
		elif value is _MISSING:
			actual = 0
		if actual is not _MISSING and (type(actual) is not int or actual != expected or clause == "limit" and actual == 0):
			out.append(_v("sql-semantics-dropped", f"changes the query's {clause.upper()}"))
	if clauses["order"]:
		order = _arg(replacement, 99, {"order_by"})
		if _empty(order) or isinstance(order, ast.Constant) and not order.value:
			out.append(_v("sql-semantics-dropped", "drops explicit ORDER BY"))
	return out


def _check_changes(blocks, source_lines, *, stored=False):
	"""Curated checks. Stored mode uses only evidence retained in the Fix text."""
	out = []
	source = {_norm(line) for line in source_lines}
	for block in blocks:
		is_diff = _is_diff(block)
		rows = diff_rows(block) if is_diff else [("+" if _norm(line) not in source else " ", line) for line in block.lines]
		removed = {_norm(line) for tag, line in rows if tag == "-"}
		after = View([(line, tag == "+") for tag, line in rows if tag != "-"], removed)
		if after.repaired and any(after.changed(c, True) and after.name(c).startswith("frappe.") for c in after.calls):
			out.append(_v("unverified-structure"))
		if after.tree is None:
			out.extend(_fragment_checks(after, compare_permissions=is_diff or not stored))
			if any(new for _, new in after.rows) and any(re.search(
				r"\b(?:from\s+frappe|import\s+frappe)|=\s*frappe(?:\.[A-Za-z_]\w*)*\s*(?:#.*)?$|\b(?:getattr|setattr|vars)\s*\(\s*frappe|\bfrappe\.__dict__",
				line) for line, _ in after.rows):
				out.append(_v("unverified-safety", "unparseable Frappe code"))
			if any(new and re.search(r"\bfrappe\.|\bhas_permission\s*\(", line) for line, new in after.rows):
				out.append(_v("unverified-structure"))
		else:
			if any(after.changed(node, True) for node in after.aliases.unsupported):
				out.append(_v("unverified-safety", "dynamic Frappe imports"))
			for node in ast.walk(after.tree):
				if not hasattr(node, "lineno") or not after.changed(node, True):
					continue
				if after.aliases.dynamic(node) or (isinstance(node, (ast.Name, ast.Attribute)) and after.aliases.ambiguous_policy(node)):
					out.append(_v("unverified-safety", "dynamic or ambiguous Frappe access"))
			for call in after.calls:
				name = after.name(call)
				if after.changed(call, True):
					for candidate in after.aliases.names(call.func) or {name}:
						violation = _write_check(call, candidate)
						if violation:
							out.append(violation)
				if name == "frappe.has_permission" and after.changed(call) and (is_diff or not stored):
					if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords) or len(call.args) > 5:
						out.append(_v("unverified-safety", "dynamic or unsupported permission arguments"))
					elif _permission(after, call)[-1] == "discarded":
						out.append(_v("unchecked-permission"))
		if is_diff:
			before = View([(line, tag == "-") for tag, line in rows if tag != "+"])
			if before.tree is not None and after.tree is not None:
				for old in before.calls:
					if before.name(old) not in {"frappe.get_list", "frappe.db.get_list"} or not before.changed(old, True):
						continue
					for new in after.calls:
						if (after.name(new) in {"frappe.get_all", "frappe.db.get_all"} and after.changed(new, True)
							and _key(_arg(old, 0, {"doctype"})) == _key(_arg(new, 0, {"doctype"}))):
							out.append(_v("permission-downgrade", "get_list replaced by get_all"))
				out.extend(_permission_changes(before, after))
				out.extend(_sql_replacement(before, after))
				if not stored:
					out.extend(_loop_replacements(before, after))
			elif any(new and "has_permission" in line for line, new in after.rows):
				out.append(_v("permission-behavior-change", "unverifiable permission fragment"))
		elif not stored and source_lines and after.tree is not None:
			# Full replacements provide no reliable line correspondence. Require a
			# grounded diff for comparative safety rather than guessing a pairing.
			before = View([(line, True) for line in source_lines])
			permission_source = any(before.name(c) in {"frappe.has_permission", "frappe.get_list", "frappe.db.get_list"} for c in before.calls)
			sql_source = _assigned_calls(before, {f"{db}.sql" for db in _DB})
			replacements = _assigned_calls(after, _READS)
			changed_shape = [line for line, _ in before.rows] != [line for line, _ in after.rows]
			if changed_shape and (permission_source or {name for name, _ in sql_source} & {name for name, _ in replacements}):
				out.append(_v("unverified-safety", "show permission or SQL changes as a grounded diff"))
	return out


def _check_loop_changes(blocks, source_lines):
	"""N+1 quality check for the Fix section only; safety checks still cover all code."""
	out = []
	source = {_norm(line) for line in source_lines}
	for block in blocks:
		is_diff = _is_diff(block)
		rows = diff_rows(block) if is_diff else [("+" if _norm(line) not in source else " ", line) for line in block.lines]
		after = View([(line, tag == "+") for tag, line in rows if tag != "-"])
		if after.tree is not None:
			for node in ast.walk(after.tree):
				if not isinstance(node, (ast.Name, ast.Attribute)) or not after.changed(node, True):
					continue
				parent = after.parents.get(node)
				if isinstance(parent, ast.Call) and parent.func is node:
					continue
				if _read_name(after.aliases.name(node)):
					out.append(_v("unverified-loop-fix", "passing a read as a function reference does not establish fewer calls"))
		if is_diff:
			before = View([(line, tag == "-") for tag, line in rows if tag != "+"])
			if before.tree is not None and after.tree is not None:
				out.extend(_loop_reduction(before, after))
			elif any(new and line.strip() for line, new in after.rows):
				out.append(_v("unverified-loop-fix", "show a complete parseable diff with its enclosing loop"))
		elif after.tree is not None and any(_read(after, call) for call in after.calls):
			out.append(_v("unverified-loop-fix", "show the read replacement as a grounded diff with its enclosing loop"))
		elif after.tree is None and block.info in {"", "py", "python", "python3"} and any(
			new and re.search(r"\bfrappe\b|\bqb\b|\.run\s*\(", line) for line, new in after.rows
		):
			out.append(_v("unverified-loop-fix", "the proposed read replacement is incomplete or unparseable"))
	return out


def check_loop_changes(blocks, source_lines):
	try:
		return _check_loop_changes(blocks, source_lines)
	except Exception as error:
		logging.getLogger(__name__).warning("AI loop checker failed: %s", type(error).__name__)
		return [_v("unverified-loop-fix", "the proposed query-count change could not be checked")]


def check_changes(blocks, source_lines, *, stored=False):
	"""A malformed answer must not break report rendering or refresh selection."""
	try:
		return _check_changes(blocks, source_lines, stored=stored)
	except Exception as error:
		# Log only the exception class; snippets may contain private site data.
		logging.getLogger(__name__).warning("AI safety checker failed: %s", type(error).__name__)
		return [_v("unverified-safety", "the proposed code could not be checked safely")]
