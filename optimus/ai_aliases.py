"""Bounded, scope-uniform Frappe identities for static suggestion checks.

All bindings in a lexical scope must agree. This deliberately does not infer
runtime order or execute proposed code. Mixed Frappe/unrelated bindings are
ambiguous; callers withhold policy-shaped uses rather than guessing.
"""

import ast
from collections import defaultdict
from dataclasses import dataclass, field


class AnalysisLimitExceeded(ValueError):
	"""The untrusted code exceeds the static resolver's work/stack budget."""


@dataclass(eq=False)
class Scope:
	node: ast.AST
	parent: object = None
	bindings: dict = field(default_factory=lambda: defaultdict(list))
	globals: set = field(default_factory=set)
	nonlocals: set = field(default_factory=set)


class Aliases(ast.NodeVisitor):
	def __init__(self, tree):
		pending, count = [(tree, 0)], 0
		while pending:
			node, depth = pending.pop()
			count += 1
			if count > 4000 or depth > 100:
				raise AnalysisLimitExceeded("Static alias analysis budget exceeded")
			pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
		self.root = self.current = Scope(tree)
		self.scopes = [self.root]
		self.at = {}
		self.cache = {}
		self.resolution_work = 0
		self.identity_chars = 0
		self.unsupported = []
		self.visit(tree)
		# Redirect global/nonlocal writes only after all scopes' locals exist.
		for scope in self.scopes:
			for name in scope.globals | scope.nonlocals:
				values = scope.bindings.pop(name, [])
				if values:
					self._binding_scope(scope, name).bindings[name].extend(values)

	def visit(self, node):
		self.at[node] = self.current
		return super().visit(node)

	def _binding_scope(self, scope, name):
		if name in scope.globals:
			return self.root
		if name in scope.nonlocals:
			parent = scope.parent
			while parent:
				if not isinstance(parent.node, ast.ClassDef) and name in parent.bindings:
					return parent
				parent = parent.parent
			return self.root
		return scope

	def _bind(self, target, value=None):
		if isinstance(target, ast.Name):
			self.at[target] = self.current
			self.current.bindings[target.id].append(value)
		elif (
			isinstance(target, (ast.Tuple, ast.List))
			and isinstance(value, (ast.Tuple, ast.List))
			and len(target.elts) == len(value.elts)
		):
			for item, part in zip(target.elts, value.elts, strict=True):
				self._bind(item, part)
		else:
			self.visit(target)

	def visit_Name(self, node):
		if isinstance(node.ctx, (ast.Store, ast.Del)):
			self.current.bindings[node.id].append(None)

	def visit_Assign(self, node):
		self.visit(node.value)
		for target in node.targets:
			self._bind(target, node.value)

	def visit_AnnAssign(self, node):
		self.visit(node.annotation)
		if node.value:
			self.visit(node.value)
		self._bind(node.target, node.value)

	def visit_NamedExpr(self, node):
		self.visit(node.value)
		scope = self.current
		while isinstance(self.current.node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
			self.current = self.current.parent
		self._bind(node.target, node.value)
		self.current = scope

	def visit_Import(self, node):
		for item in node.names:
			name = item.asname or item.name.split(".")[0]
			identity = item.name if item.asname else item.name.split(".")[0]
			self.current.bindings[name].append(
				identity if identity in {"frappe", "operator"} or identity.startswith("frappe.") else None
			)

	def visit_ImportFrom(self, node):
		for item in node.names:
			if item.name == "*" and (node.module == "frappe" or str(node.module).startswith("frappe.")):
				self.unsupported.append(node)
			identity = f"{node.module}.{item.name}"
			self.current.bindings[item.asname or item.name].append(
				identity
				if not node.level
				and (
					node.module == "frappe"
					or str(node.module).startswith("frappe.")
					or identity == "operator.attrgetter"
				)
				else None
			)

	def visit_Global(self, node):
		self.current.globals.update(node.names)

	def visit_Nonlocal(self, node):
		self.current.nonlocals.update(node.names)

	def _scope(self, node, body, *, function=False, args=None):
		prior = self.current
		parent = prior
		if function:
			while parent and isinstance(parent.node, ast.ClassDef):
				parent = parent.parent
		self.current = Scope(node, parent)
		self.scopes.append(self.current)
		if args:
			for arg in [
				*args.posonlyargs,
				*args.args,
				*args.kwonlyargs,
				*([args.vararg] if args.vararg else []),
				*([args.kwarg] if args.kwarg else []),
			]:
				self.current.bindings[arg.arg].append(None)
			positional = [*args.posonlyargs, *args.args]
			for arg, default in zip(
				positional[-len(args.defaults) :] if args.defaults else [], args.defaults, strict=True
			):
				self.current.bindings[arg.arg].append(default)
			for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
				if default is not None:
					self.current.bindings[arg.arg].append(default)
		for child in body:
			self.visit(child)
		self.current = prior

	def visit_FunctionDef(self, node):
		self.current.bindings[node.name].append(None)
		for arg in [
			*node.args.posonlyargs,
			*node.args.args,
			*node.args.kwonlyargs,
			*([node.args.vararg] if node.args.vararg else []),
			*([node.args.kwarg] if node.args.kwarg else []),
		]:
			if arg.annotation:
				self.visit(arg.annotation)
		for item in [
			*node.decorator_list,
			*node.args.defaults,
			*[v for v in node.args.kw_defaults if v],
			*([node.returns] if node.returns else []),
		]:
			self.visit(item)
		self._scope(node, node.body, function=True, args=node.args)

	visit_AsyncFunctionDef = visit_FunctionDef

	def visit_Lambda(self, node):
		for item in [*node.args.defaults, *[v for v in node.args.kw_defaults if v]]:
			self.visit(item)
		self._scope(node, [node.body], function=True, args=node.args)

	def visit_ClassDef(self, node):
		self.current.bindings[node.name].append(None)
		for item in [*node.decorator_list, *node.bases, *node.keywords]:
			self.visit(item)
		self._scope(node, node.body)

	def visit_ExceptHandler(self, node):
		if node.name:
			self.current.bindings[node.name].append(None)
		self.generic_visit(node)

	def visit_MatchAs(self, node):
		if node.name:
			self.current.bindings[node.name].append(None)
		self.generic_visit(node)

	visit_MatchStar = visit_MatchAs

	def visit_MatchMapping(self, node):
		if node.rest:
			self.current.bindings[node.rest].append(None)
		self.generic_visit(node)

	def visit_ListComp(self, node):
		first, *rest = node.generators
		self.visit(first.iter)  # Python evaluates this iterator in the enclosing scope.
		body = [first.target, *first.ifs]
		for generator in rest:
			body.extend([generator.iter, generator.target, *generator.ifs])
		body.extend([node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt])
		self._scope(node, body, function=True)

	visit_SetComp = visit_ListComp
	visit_DictComp = visit_ListComp
	visit_GeneratorExp = visit_ListComp

	def values(self, node, seen=frozenset(), depth=0):
		self.resolution_work += 1
		if depth > 100 or self.resolution_work > 10000:
			raise AnalysisLimitExceeded("Static alias resolution budget exceeded")
		if isinstance(node, ast.IfExp):
			return self._bounded(self.values(node.body, seen, depth + 1) | self.values(node.orelse, seen, depth + 1))
		if isinstance(node, ast.BoolOp):
			out = set()
			for value in node.values:
				out.update(self.values(value, seen, depth + 1))
				self._bounded(out)
			return frozenset(out)
		if isinstance(node, ast.Name):
			scope = self.at.get(node, self.root)
			while scope:
				scope = self._binding_scope(scope, node.id)
				if node.id in scope.bindings:
					key = (scope, node.id)
					if key in seen:
						return frozenset({None})
					if key not in self.cache:
						out = set()
						for value in scope.bindings[node.id]:
							out.update(
								{value}
								if isinstance(value, str) or value is None
								else self.values(value, seen | {key}, depth + 1)
							)
							self._bounded(out)
						self.cache[key] = frozenset(out or {None})
					return self.cache[key]
				scope = scope.parent
			return frozenset({node.id if node.id == "frappe" else None})
		if isinstance(node, ast.Attribute):
			return frozenset(
				self._extend(value, "." + node.attr) if value else None
				for value in self.values(node.value, seen, depth + 1)
			)
		if isinstance(node, ast.Call):
			return frozenset(
				self._extend(value, "()") if value and value.startswith("frappe.qb.") else None
				for value in self.values(node.func, seen, depth + 1)
			)
		return frozenset({None})

	def _extend(self, value, suffix):
		length = len(value) + len(suffix)
		self.identity_chars += length
		# Bound allocations before concatenating; cached prefixes can otherwise
		# grow quadratically even when depth and identity counts stay small.
		if length > 1024 or self.identity_chars > 1048576:
			raise AnalysisLimitExceeded("Static alias identity size budget exceeded")
		return value + suffix

	@staticmethod
	def _bounded(values):
		# Repeated alternative bindings can double the identity set at every
		# level even in a tiny AST. Never cache or combine an unbounded set.
		if len(values) > 128 or any(value is not None and len(value) > 1024 for value in values):
			raise AnalysisLimitExceeded("Static alias identity budget exceeded")
		return frozenset(values)

	def name(self, node):
		values = self.values(node)
		return next(iter(values)) if len(values) == 1 and None not in values else ""

	def call_name(self, node, dotted):
		known = self.name(node.func)
		raw = dotted(node.func)
		return known or ("@unrelated." + raw if raw == "frappe" or raw.startswith("frappe.") else raw)

	def ambiguous_policy(self, node):
		values = self.values(node)
		return len(values) > 1 and any(
			value
			and (
				value.startswith("frappe.qb.")
				or value.rsplit(".", 1)[-1]
				in {
					"sql",
					"multisql",
					"sql_list",
					"sql_ddl",
					"delete",
					"set_value",
					"truncate",
					"has_permission",
					"get_doc",
					"get_list",
					"get_all",
					"get_value",
					"get_values",
					"count",
					"exists",
				}
			)
			for value in values
		)

	def dynamic(self, node):
		def root(value):
			return any(
				name and (name == "frappe" or name.startswith("frappe.")) for name in self.values(value)
			)

		if isinstance(node, ast.Attribute) and node.attr == "__dict__":
			return root(node.value)
		if isinstance(node, ast.Call) and node.args:
			if isinstance(node.func, ast.Name) and node.func.id in {"getattr", "setattr", "vars"}:
				return root(node.args[0])
			if isinstance(node.func, ast.Call) and (
				self.name(node.func.func) == "operator.attrgetter"
				or isinstance(node.func.func, ast.Attribute)
				and node.func.func.attr == "attrgetter"
			):
				return root(node.args[0])
		return False
