# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Static scanner for the repo-read lint gate (``test_lint_repo_reads``). Not a test module.

It counts the calls in a test module that read a file of the repository: a path built from
``__file__`` that reaches ``.read_text()``, ``.read_bytes()``, ``.open()`` or ``open()``, and
every ``inspect.getsource`` call. It uses ``ast``, one pass per module, and never imports the
scanned code, runs git or looks at the environment.

Which files: every ``*.py`` under ``optimus/tests/`` (test files, ``conftest.py``, support
modules, ``__init__.py``), except the ``EXEMPT`` files and the ``fixtures/`` and ``data/``
folders. A file that pytest collects (``test_*.py``, ``*_test.py``, ``conftest.py``) is scanned
even inside those two folders. A file that cannot be read or parsed is a problem line.

Rules:

- Sources are fixed: the name ``__file__``, any ``<expr>.__file__``, a call to a source helper
  (a function whose ``return`` value is tainted) and a tainted module constant imported from an
  ``optimus.tests`` module in any import form (``import *`` too), at any scope.
- A name is tainted in a scope when it is bound there from a tainted expression (assignment,
  augmented or annotated assignment, walrus, ``for`` or comprehension target, ``with ... as``,
  a parameter default). A local binding or parameter hides a module name; inner scopes see the
  taint of the enclosing function and module scopes, not of a class body.
- Sinks: ``.read_text()``, ``.read_bytes()`` and ``.open()`` on a tainted path (also built with
  ``getattr``, stored as a bound method, or called unbound as ``Path.read_text(path)``), the
  ``open`` functions of ``io``, ``codecs``, ``gzip``, ``bz2``, ``lzma`` and ``tarfile``, and
  ``inspect.getsource``, ``getsourcelines`` and ``getsourcefile`` with any argument. An ``open``
  with a constant mode that has ``w``, ``a`` or ``x`` is a write and is not counted.
- A function that reads a path it gets as a parameter is a sink helper: each call with a tainted
  argument is one site and the read inside is not counted. A read whose path is already tainted
  in the function's own scope is an ordinary site, counted once where it is.
- A helper is found by the scope that defines it: a call name resolves through the calling
  scope and the enclosing function and module scopes, and a parameter or local of that name
  hides the helper. When one scope defines a name twice, the last definition wins.

Blind spots. Nothing here catches them, so review and the rule in CLAUDE.md must:

- A path passed through the arguments of a function that is not a sink helper; a helper that
  only calls another helper; a closure whose read uses a parameter of the outer function; lambda
  and generator helpers; helpers imported from another module.
- A read in a method reached through ``self`` (including a class attribute used as
  ``self.ROOT``), and a class-body comprehension.
- A path with no ``__file__`` in it: ``Path.cwd()``, ``os.getcwd()``, a literal such as
  ``"optimus/..."`` or an environment value. A name that holds the path in a container
  (``d[k] = ROOT / x``), after ``global`` rebinding, in a ``match`` capture or after ``*args``.
- ``getattr(x, "__file__")``, and ``__file__`` spelled with non-ASCII look-alike characters
  (Python folds them to the real name, the source hint does not).
- Pytest fixture parameters that hand a repo path to a test, reads inside production code, and
  reads through a module that is not ``pathlib``, ``open`` or one of the modules above
  (``shutil``, ``from gzip import open``).

A marker comment ``# test-lint: repo-read-ok <reason>`` (found in tokenize COMMENT tokens only,
never in a string; it may follow another comment) waives the counted sites it covers. The reason
has at least 3 words that hold a letter. A marker that sits after code on its line covers a site
that spans that line. A marker on a line of its own covers a site that starts on the next line,
or a site that spans its line (a comment inside a multi-line call). A marker with a short reason,
or one that covers no site, is a problem.
"""

import ast
import fnmatch
import io
import re
import tokenize
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NamedTuple

PACKAGE = "optimus.tests"
BASELINE_FILE = "optimus/tests/repo_reads_baseline.py"
MARKER_RE = re.compile(r"#\s*test-lint:\s*repo-read-ok(?:\s+(.*\S))?\s*$")
MIN_REASON_WORDS = 3
MAX_PROBLEM_LINES = 40
READ_METHODS = frozenset({"read_text", "read_bytes"})
BOUND_METHODS = READ_METHODS | {"open"}  # a bare reference to one of these on a repo path is a read
SOURCE_FUNCS = frozenset({"getsource", "getsourcelines", "getsourcefile"})
OPEN_MODULES = frozenset({"io", "codecs", "gzip", "bz2", "lzma", "tarfile"})  # <module>.open(path, mode)
PATH_CLASSES = frozenset({"Path", "PurePath", "PosixPath", "PureWindowsPath", "WindowsPath"})
WRITE_MODE_CHARS = "wax"
SKIPPED_FOLDERS = frozenset({"fixtures", "data"})
CACHE_FOLDERS = frozenset({"__pycache__"})  # never scanned, even for a file pytest would collect
VENV_MARKER = "pyvenv.cfg"  # a folder holding this file is a virtual environment, whatever its name
COLLECTED_PATTERNS = ("test_*.py", "*_test.py", "conftest.py")  # what pytest collects by default
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

# A file without any of these byte patterns cannot taint a path, so the scan skips collecting it.
HINT_RE = re.compile(
	rb"__file__"  # the root of every repo path
	rb"|getsource"  # inspect.getsource, getsourcelines and getsourcefile need no path
	rb"|optimus[\s\\]*\.[\s\\]*tests"  # a constant imported by its absolute name
	rb"|from[\s\\]*\."  # a constant imported by a relative name, whatever the spacing
)

SHORT_REASON = "marker reason is too short: write a reason of 3+ words"
STALE_TRAILING = "stale marker: no repo read on this line, delete this comment"
STALE_STANDALONE = "stale marker: no repo read on the next line, delete this comment"
TITLE_NOTE = "A test must not read a repo file. See CONTRIBUTING.md, 'Test gate'."
RENAME_HINT = "Renamed a file? Move its BASELINE key to the new name, keep its count."
REMEDIES = """\
To fix a new repo read, pick one:
  - Test the behaviour, not the text of a repo file.
  - Put a docs, CHANGELOG or wording pin in test_docs_truth.py, the one home for those.
  - If the read is correct (a data file the code reads, a contract between a data file and the
    code), add `# test-lint: repo-read-ok <reason of 3 or more words>` after the read on its
    line, or on the line above it, and raise MARKED_TOTAL in optimus/tests/repo_reads_baseline.py
    so the reviewer sees the new waiver."""


class Site(NamedTuple):
	line: int
	kind: str
	end_line: int


class Problem(NamedTuple):
	line: int
	text: str


class Marker(NamedTuple):
	line: int
	reason: str  # "" when missing
	standalone: bool  # nothing but the comment on its line


class FileScan(NamedTuple):
	sites: list[Site]  # every counted read
	unmarked: list[Site]  # the sites no valid marker covers
	markers: int  # valid markers (reason accepted and at least one site covered)
	problems: list[Problem]  # bad or stale markers, or the reason the file failed
	failed: bool = False  # the file could not be read, parsed or analysed
	marker_lines: tuple[int, ...] = ()  # the lines of the valid markers


class Source(NamedTuple):
	data: bytes
	is_package: bool


class Feat(NamedTuple):
	"""What an expression is made of, computed once, so taint is set arithmetic afterwards."""

	names: frozenset = frozenset()
	chains: frozenset = frozenset()  # "a.b.c" attribute chains that start at a name
	calls: frozenset = frozenset()  # names of functions called
	has_file: bool = False  # mentions __file__
	consts: tuple = ()  # (module, name) pairs bound by an import


class Ref(NamedTuple):
	"""A bare reference to a read method, such as ``r = path.read_text``."""

	node: ast.AST
	scope: "Scope"
	value: ast.expr
	attr: str


class Scope:
	def __init__(self, kind: str, parent: "Scope | None", name: str = ""):
		self.kind = kind  # module, function, lambda, class or comp
		self.parent = parent
		self.name = name
		self.children: list[Scope] = []
		self.locals: set[str] = set()
		self.bindings: list[tuple[str, Feat, Scope]] = []  # (name, Feat, scope the Feat is read in)
		self.aliases: dict[str, str] = {}  # local name -> dotted module it stands for
		self.stars: list[str] = []  # modules imported with `import *`
		self.funcs: dict[str, Scope] = {}  # functions defined in this scope, by name
		self.positional: list[str] = []
		self.params: list[str] = []
		self.vararg: str | None = None
		self.returns: list[Feat] = []
		self.tainted: set[str] = set()


# ---------------------------------------------------------------------------
# Expression features
# ---------------------------------------------------------------------------


def _dotted(node: ast.AST) -> str | None:
	parts = []
	while isinstance(node, ast.Attribute):
		parts.append(node.attr)
		node = node.value
	if not isinstance(node, ast.Name):
		return None
	parts.append(node.id)
	return ".".join(reversed(parts))


def _feat_of(expr: ast.AST) -> Feat:
	names, chains, calls, has_file = set(), set(), set(), False
	for node in ast.walk(expr):
		if isinstance(node, ast.Name):
			names.add(node.id)
			has_file = has_file or node.id == "__file__"
		elif isinstance(node, ast.Attribute):
			has_file = has_file or node.attr == "__file__"
			dotted = _dotted(node)
			if dotted:
				chains.add(dotted)
		elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
			calls.add(node.func.id)
	return Feat(frozenset(names), frozenset(chains), frozenset(calls), has_file)


def _stored_names(target: ast.AST) -> list[str]:
	return [n.id for n in ast.walk(target) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)]


def _getattr_name(call: ast.Call) -> str | None:
	"""The method name of ``getattr(x, "read_text")``, else ``None``."""
	if not (isinstance(call.func, ast.Name) and call.func.id == "getattr" and len(call.args) >= 2):
		return None
	name = call.args[1]
	if isinstance(name, ast.Constant) and isinstance(name.value, str) and name.value in BOUND_METHODS:
		return name.value
	return None


# ---------------------------------------------------------------------------
# Collection: one walk of the tree builds the scopes
# ---------------------------------------------------------------------------


class Collector:
	def __init__(self, dotted: str, is_package: bool, resolver: "Resolver"):
		self.dotted = dotted
		self.is_package = is_package
		self.resolver = resolver
		self.scopes: list[Scope] = []
		self.helpers: list[Scope] = []  # every function that is not a method
		self.calls: list[tuple[ast.Call, Scope]] = []
		self.refs: list[Ref] = []
		self.inspect_modules = {"inspect"}
		self.inspect_funcs: dict[str, str] = {}
		self.open_modules = set(OPEN_MODULES)
		self._called: set[int] = set()  # ids of the nodes that are the function of a call
		self.module = self._new("module", None)

	def _new(self, kind: str, parent: Scope | None, name: str = "") -> Scope:
		scope = Scope(kind, parent, name)
		self.scopes.append(scope)
		if parent is not None:
			parent.children.append(scope)
		return scope

	def run(self, tree: ast.Module) -> "Collector":
		for stmt in tree.body:
			self.visit(stmt, self.module)
		return self

	def visit(self, node: ast.AST, scope: Scope) -> None:
		handler = _HANDLERS.get(type(node))
		if handler is not None:
			handler(self, node, scope)
			if type(node) in _OWN_CHILDREN:
				return
		for child in ast.iter_child_nodes(node):
			self.visit(child, scope)

	def bind(self, target: ast.AST, feat: Feat, scope: Scope, where: Scope | None = None) -> None:
		for name in _stored_names(target):
			scope.locals.add(name)
			scope.bindings.append((name, feat, where or scope))

	# -- statements --------------------------------------------------------

	def on_assign(self, node: ast.Assign, scope: Scope) -> None:
		feat = _feat_of(node.value)
		for target in node.targets:
			self.bind(target, feat, scope)

	def on_valued(self, node: ast.AnnAssign | ast.AugAssign, scope: Scope) -> None:
		if node.value is not None:
			self.bind(node.target, _feat_of(node.value), scope)

	def on_for(self, node: ast.For | ast.AsyncFor, scope: Scope) -> None:
		self.bind(node.target, _feat_of(node.iter), scope)

	def on_with(self, node: ast.With | ast.AsyncWith, scope: Scope) -> None:
		for item in node.items:
			if item.optional_vars is not None:
				self.bind(item.optional_vars, _feat_of(item.context_expr), scope)

	def on_walrus(self, node: ast.NamedExpr, scope: Scope) -> None:
		while scope.kind == "comp":
			scope = scope.parent
		self.bind(node.target, _feat_of(node.value), scope)

	def on_except(self, node: ast.ExceptHandler, scope: Scope) -> None:
		if node.name:
			scope.locals.add(node.name)

	def on_call(self, node: ast.Call, scope: Scope) -> None:
		self.calls.append((node, scope))
		self._called.add(id(node.func))
		attr = _getattr_name(node)
		if attr is not None and id(node) not in self._called:
			self.refs.append(Ref(node, scope, node.args[0], attr))

	def on_attribute(self, node: ast.Attribute, scope: Scope) -> None:
		if node.attr in BOUND_METHODS and isinstance(node.ctx, ast.Load) and id(node) not in self._called:
			self.refs.append(Ref(node, scope, node.value, node.attr))

	def on_return(self, node: ast.Return, scope: Scope) -> None:
		if node.value is not None and scope.kind == "function":
			scope.returns.append(_feat_of(node.value))

	def on_import(self, node: ast.Import, scope: Scope) -> None:
		for alias in node.names:
			local = alias.asname or alias.name.split(".")[0]
			scope.locals.add(local)
			scope.aliases[local] = alias.name if alias.asname else local
			if alias.name == "inspect":
				self.inspect_modules.add(local)
			if alias.name in OPEN_MODULES:
				self.open_modules.add(local)

	def on_import_from(self, node: ast.ImportFrom, scope: Scope) -> None:
		module = self._absolute(node)
		in_package = module is not None and module.startswith(PACKAGE)
		for alias in node.names:
			if alias.name == "*":
				if in_package:
					scope.stars.append(module)
				continue
			local = alias.asname or alias.name
			scope.locals.add(local)
			if module == "inspect" and alias.name in SOURCE_FUNCS:
				self.inspect_funcs[local] = alias.name
			if not in_package:
				continue
			full = f"{module}.{alias.name}"
			if self.resolver.exists(full):
				scope.aliases[local] = full
			else:
				scope.bindings.append((local, Feat(consts=((module, alias.name),)), scope))

	def _absolute(self, node: ast.ImportFrom) -> str | None:
		if not node.level:
			return node.module or ""
		parts = self.dotted.split(".")
		package = parts if self.is_package else parts[:-1]
		keep = len(package) - (node.level - 1)
		if keep <= 0:
			return None
		return ".".join(package[:keep] + ([node.module] if node.module else []))

	# -- scopes ------------------------------------------------------------

	def on_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, scope: Scope) -> None:
		scope.locals.add(node.name)
		for decorator in node.decorator_list:
			self.visit(decorator, scope)
		inner = self._signature(node.args, "function", scope, node.name)
		if scope.kind != "class":
			scope.funcs[node.name] = inner
			self.helpers.append(inner)
		for stmt in node.body:
			self.visit(stmt, inner)

	def on_lambda(self, node: ast.Lambda, scope: Scope) -> None:
		inner = self._signature(node.args, "lambda", scope)
		self.visit(node.body, inner)

	def _signature(self, args: ast.arguments, kind: str, outer: Scope, name: str = "") -> Scope:
		defaults = list(args.defaults) + [d for d in args.kw_defaults if d is not None]
		for default in defaults:
			self.visit(default, outer)
		inner = self._new(kind, outer, name)
		positional = args.posonlyargs + args.args
		first_default = len(positional) - len(args.defaults)
		for index, arg in enumerate(positional):
			inner.positional.append(arg.arg)
			if index >= first_default:
				inner.bindings.append((arg.arg, _feat_of(args.defaults[index - first_default]), outer))
		for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
			if default is not None:
				inner.bindings.append((arg.arg, _feat_of(default), outer))
		inner.params = inner.positional + [a.arg for a in args.kwonlyargs]
		if args.vararg:
			inner.vararg = args.vararg.arg
			inner.params.append(args.vararg.arg)
		if args.kwarg:
			inner.params.append(args.kwarg.arg)
		inner.locals.update(inner.params)
		return inner

	def on_class(self, node: ast.ClassDef, scope: Scope) -> None:
		scope.locals.add(node.name)
		for outer in node.decorator_list + node.bases + [k.value for k in node.keywords]:
			self.visit(outer, scope)
		inner = self._new("class", scope, node.name)
		for stmt in node.body:
			self.visit(stmt, inner)

	def on_comprehension(self, node: ast.expr, scope: Scope) -> None:
		inner = self._new("comp", scope)
		for generator in node.generators:
			self.bind(generator.target, _feat_of(generator.iter), inner)
			self.visit(generator.iter, inner)
			for condition in generator.ifs:
				self.visit(condition, inner)
		parts = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
		for part in parts:
			self.visit(part, inner)


_HANDLERS = {
	ast.Assign: Collector.on_assign,
	ast.AnnAssign: Collector.on_valued,
	ast.AugAssign: Collector.on_valued,
	ast.For: Collector.on_for,
	ast.AsyncFor: Collector.on_for,
	ast.With: Collector.on_with,
	ast.AsyncWith: Collector.on_with,
	ast.NamedExpr: Collector.on_walrus,
	ast.ExceptHandler: Collector.on_except,
	ast.Call: Collector.on_call,
	ast.Attribute: Collector.on_attribute,
	ast.Return: Collector.on_return,
	ast.Import: Collector.on_import,
	ast.ImportFrom: Collector.on_import_from,
	ast.FunctionDef: Collector.on_function,
	ast.AsyncFunctionDef: Collector.on_function,
	ast.Lambda: Collector.on_lambda,
	ast.ClassDef: Collector.on_class,
	**{kind: Collector.on_comprehension for kind in COMPREHENSIONS},
}
# These handlers walk their own children (in the scope they open), so the generic walk skips them.
_OWN_CHILDREN = frozenset({ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, *COMPREHENSIONS})


# ---------------------------------------------------------------------------
# Analysis: taint to a fixed point, then the sites
# ---------------------------------------------------------------------------


def _visible(scope: Scope) -> Iterator[Scope]:
	"""The scopes a name is looked up in: this one, then the enclosing non-class scopes."""
	yield scope
	outer = scope.parent
	while outer is not None:
		if outer.kind != "class":
			yield outer
		outer = outer.parent


def _is_write_mode(call: ast.Call, index: int) -> bool:
	mode = call.args[index] if len(call.args) > index else None
	for keyword in call.keywords:
		if keyword.arg == "mode":
			mode = keyword.value
	return (
		isinstance(mode, ast.Constant)
		and isinstance(mode.value, str)
		and any(char in mode.value for char in WRITE_MODE_CHARS)
	)


def _builtin_open(call: ast.Call) -> tuple[str, Feat] | None:
	path = call.args[0] if call.args else None
	for keyword in call.keywords:
		if keyword.arg == "file":
			path = keyword.value
	if path is None or _is_write_mode(call, 1):
		return None
	return "open", _feat_of(path)


def _site(node: ast.AST, kind: str) -> Site:
	return Site(node.lineno, kind, getattr(node, "end_lineno", None) or node.lineno)


def _subtree(function: Scope) -> list[Scope]:
	found, queue = [], [function]
	while queue:
		scope = queue.pop()
		found.append(scope)
		queue.extend(scope.children)
	return found


def _reach(param: str, bindings: list[tuple[str, Feat]]) -> set[str]:
	"""The names a parameter's value can flow into through the bindings of a function."""
	reach = {param}
	grew = True
	while grew:
		grew = False
		for name, feat in bindings:
			if name not in reach and feat.names & reach:
				reach.add(name)
				grew = True
	return reach


def _param_uses(function: Scope, feat: Feat) -> set[str]:
	"""The parameters of ``function`` whose value can reach the names in ``feat``."""
	bindings = [(name, bound) for scope in _subtree(function) for name, bound, _ in scope.bindings]
	return {param for param in function.params if feat.names & _reach(param, bindings)}


def _enclosing_function(scope: Scope | None) -> Scope | None:
	while scope is not None and scope.kind != "function":
		scope = scope.parent
	return scope


class Analysis:
	"""What one module holds: its tainted constants and, when it can be scanned, its read sites."""

	def __init__(self, resolver: "Resolver", data: bytes, collected: Collector | None, error: Problem | None):
		self.resolver = resolver
		self.data = data
		self.collected = collected
		self.error = error
		self.constants: frozenset[str] = frozenset()
		self.source_helpers: set[Scope] = set()

	@classmethod
	def build(cls, dotted: str, source: Source, resolver: "Resolver") -> "Analysis":
		try:
			tree = ast.parse(source.data)
		except (SyntaxError, ValueError) as error:
			problem = Problem(
				getattr(error, "lineno", None) or 1, f"cannot parse: {getattr(error, 'msg', error)}"
			)
			return cls(resolver, source.data, None, problem)
		except (RecursionError, MemoryError):
			return cls._failed(resolver, source, "cannot parse: the file is nested too deeply")
		if not HINT_RE.search(source.data):
			return cls(resolver, source.data, None, None)  # nothing to collect, so nothing to count
		try:
			analysis = cls(
				resolver, source.data, Collector(dotted, source.is_package, resolver).run(tree), None
			)
			analysis._propagate()
		except (RecursionError, MemoryError):
			return cls._failed(resolver, source, "cannot analyse: the file is nested too deeply")
		analysis.constants = frozenset(analysis.collected.module.tainted)
		return analysis

	@classmethod
	def _failed(cls, resolver: "Resolver", source: Source, text: str) -> "Analysis":
		return cls(resolver, source.data, None, Problem(1, text))

	# -- taint -------------------------------------------------------------

	def _function(self, name: str, scope: Scope) -> Scope | None:
		"""The function ``name`` stands for in ``scope``; a parameter or local of that name hides it."""
		for each in _visible(scope):
			if name in each.funcs:
				return each.funcs[name]
			if name in each.locals:
				return None
		return None

	def _alias(self, name: str, scope: Scope) -> str | None:
		for each in _visible(scope):
			if name in each.aliases:
				return each.aliases[name]
			if name in each.locals:
				return None
		return None

	def _name_tainted(self, name: str, scope: Scope) -> bool:
		for each in _visible(scope):
			if name in each.locals:
				return name in each.tainted
			if any(name in self.resolver.constants(module) for module in each.stars):
				return True
		return False

	def _chain_tainted(self, chain: str, scope: Scope) -> bool:
		parts = chain.split(".")
		module = self._alias(parts[0], scope)
		if module is None or len(parts) < 2:
			return False
		return parts[-1] in self.resolver.constants(".".join([module, *parts[1:-1]]))

	def tainted(self, feat: Feat, scope: Scope) -> bool:
		if feat.has_file or any(self._function(name, scope) in self.source_helpers for name in feat.calls):
			return True
		if any(self._name_tainted(name, scope) for name in feat.names):
			return True
		if any(name in self.resolver.constants(module) for module, name in feat.consts):
			return True
		return any(self._chain_tainted(chain, scope) for chain in feat.chains)

	def _propagate(self) -> None:
		scopes = self.collected.scopes
		changed = True
		while changed:
			changed = False
			for scope in scopes:
				for name, feat, where in scope.bindings:
					if name not in scope.tainted and self.tainted(feat, where):
						scope.tainted.add(name)
						changed = True
			for function in self.collected.helpers:
				if function not in self.source_helpers and any(
					self.tainted(feat, function) for feat in function.returns
				):
					self.source_helpers.add(function)
					changed = True

	# -- sites -------------------------------------------------------------

	def _classify(self, call: ast.Call) -> tuple[str, Feat | None] | None:
		"""``(kind, path Feat or None)`` when the call is a read sink, else ``None``."""
		func = call.func
		if isinstance(func, ast.Name):
			if func.id == "open":
				return _builtin_open(call)
			if func.id in self.collected.inspect_funcs:
				return self.collected.inspect_funcs[func.id], None
			return None
		if isinstance(func, ast.Call):  # getattr(path, "read_text")()
			attr = _getattr_name(func)
			if attr is None or (attr == "open" and _is_write_mode(call, 0)):
				return None
			return attr, _feat_of(func.args[0])
		if isinstance(func, ast.Attribute):
			return self._classify_method(call, func)
		return None

	def _classify_method(self, call: ast.Call, func: ast.Attribute) -> tuple[str, Feat | None] | None:
		attr, value = func.attr, func.value
		unbound = (
			bool(call.args) and _dotted(value) is not None and _dotted(value).split(".")[-1] in PATH_CLASSES
		)
		path = _feat_of(call.args[0] if unbound else value)
		if attr in READ_METHODS:
			return attr, path
		if attr == "open":
			if isinstance(value, ast.Name) and value.id in self.collected.open_modules:
				return _builtin_open(call)
			return None if _is_write_mode(call, 1 if unbound else 0) else ("open", path)
		if (
			attr in SOURCE_FUNCS
			and isinstance(value, ast.Name)
			and value.id in self.collected.inspect_modules
		):
			return attr, None
		return None

	def _helper_call_hits(self, call: ast.Call, function: Scope, used: set[str], scope: Scope) -> bool:
		for index, arg in enumerate(call.args):
			if isinstance(arg, ast.Starred):
				if self.tainted(_feat_of(arg.value), scope):
					return True
				break
			param = function.positional[index] if index < len(function.positional) else function.vararg
			if param in used and self.tainted(_feat_of(arg), scope):
				return True
		return any(
			(kw.arg is None or kw.arg in used) and self.tainted(_feat_of(kw.value), scope)
			for kw in call.keywords
		)

	def sites(self) -> list[Site]:
		if self.collected is None:
			return []
		found: list[Site] = []
		helpers: dict[Scope, set[str]] = {}  # sink helper -> the parameters its read uses

		def consider(node: ast.AST, kind: str, path: Feat | None, scope: Scope) -> None:
			if path is None or self.tainted(path, scope):
				found.append(_site(node, kind))
				return
			function = _enclosing_function(scope)
			used = _param_uses(function, path) if function is not None else set()
			if used:
				helpers.setdefault(function, set()).update(used)

		for call, scope in self.collected.calls:
			sink = self._classify(call)
			if sink is not None:
				consider(call, sink[0], sink[1], scope)
		for ref in self.collected.refs:
			consider(ref.node, f"{ref.attr} reference", _feat_of(ref.value), ref.scope)
		for call, scope in self.collected.calls:
			if not isinstance(call.func, ast.Name):
				continue
			function = self._function(call.func.id, scope)
			used = helpers.get(function)
			if used and self._helper_call_hits(call, function, used, scope):
				found.append(_site(call, f"{call.func.id}() helper"))
		return sorted(found)

	def markers(self) -> list[Marker]:
		if b"test-lint" not in self.data:
			return []
		found = []
		try:
			for token in tokenize.tokenize(io.BytesIO(self.data).readline):
				match = token.type == tokenize.COMMENT and MARKER_RE.search(token.string)
				if match:
					standalone = not token.line[: token.start[1]].strip()
					found.append(Marker(token.start[0], match.group(1) or "", standalone))
		except (tokenize.TokenError, SyntaxError):
			pass
		return found


def _acceptable(reason: str) -> bool:
	words = [word for word in reason.split() if any(char.isalpha() for char in word)]
	return len(words) >= MIN_REASON_WORDS


def _covers(marker: Marker, site: Site) -> bool:
	if site.line <= marker.line <= site.end_line:
		return True
	return marker.standalone and site.line == marker.line + 1


def apply_markers(sites: list[Site], markers: list[Marker]) -> tuple[list[Site], list[int], list[Problem]]:
	"""Split ``sites`` into the unmarked ones; return ``(unmarked, lines of valid markers, problems)``."""
	problems, valid, covered = [], [], set()
	for marker in markers:
		if not _acceptable(marker.reason):
			problems.append(Problem(marker.line, SHORT_REASON))
			continue
		hit = [site for site in sites if _covers(marker, site)]
		if not hit:
			problems.append(Problem(marker.line, STALE_STANDALONE if marker.standalone else STALE_TRAILING))
			continue
		valid.append(marker.line)
		covered.update(hit)
	return [site for site in sites if site not in covered], valid, problems


def _failed_scan(text: str) -> FileScan:
	return FileScan([], [], 0, [Problem(1, text)], failed=True)


class Resolver:
	"""Parses each optimus.tests module once and answers which of its constants are tainted."""

	def __init__(self, loader: Callable[[str], Source | None]):
		self._loader = loader  # dotted name -> Source or None; used for imports only
		self._sources: dict[str, Source | None] = {}
		self._analyses: dict[str, Analysis | None] = {}
		self._building: set[str] = set()
		self._cuts = 0  # import-cycle edges cut so far

	def preload(self, dotted: str, source: Source) -> None:
		"""Register the bytes of a scanned file, so the dotted-name loader is not used for it."""
		self._sources.setdefault(dotted, source)

	def source(self, dotted: str) -> Source | None:
		if dotted not in self._sources:
			self._sources[dotted] = self._loader(dotted)
		return self._sources[dotted]

	def exists(self, dotted: str) -> bool:
		return self.source(dotted) is not None

	def analysis(self, dotted: str) -> Analysis | None:
		if dotted in self._analyses:
			return self._analyses[dotted]
		source = self.source(dotted)
		if source is None:
			self._analyses[dotted] = None
			return None
		cuts_before = self._cuts
		self._building.add(dotted)
		try:
			analysis = Analysis.build(dotted, source, self)
		finally:
			self._building.discard(dotted)
		if self._cuts == cuts_before:  # a result computed past a cut cycle edge depends on the scan order
			self._analyses[dotted] = analysis
		return analysis

	def constants(self, dotted: str) -> frozenset[str]:
		"""Tainted module-level names of ``dotted``; empty for an unknown module or an import cycle."""
		if dotted in self._building:
			self._cuts += 1
			return frozenset()
		if not dotted.startswith(PACKAGE):
			return frozenset()
		analysis = self.analysis(dotted)
		return analysis.constants if analysis is not None else frozenset()

	def scan(self, dotted: str) -> FileScan:
		analysis = self.analysis(dotted)
		if analysis is None:
			return _failed_scan("cannot read: no such module")
		if analysis.error is not None:
			return FileScan([], [], 0, [analysis.error], failed=True)
		try:
			sites = analysis.sites()
		except (RecursionError, MemoryError):
			return _failed_scan("cannot analyse: the file is nested too deeply")
		unmarked, valid, problems = apply_markers(sites, analysis.markers())
		return FileScan(sites, unmarked, len(valid), problems, marker_lines=tuple(valid))


def scan_source(
	text: str, others: dict[str, str] | None = None, module: str = f"{PACKAGE}.test_sample"
) -> FileScan:
	"""Scan inline source; ``others`` maps dotted optimus.tests names to the source they import."""
	files = {module: text, **(others or {})}

	def loader(dotted: str) -> Source | None:
		return Source(files[dotted].encode(), False) if dotted in files else None

	return Resolver(loader).scan(module)


# ---------------------------------------------------------------------------
# The tree and the gate
# ---------------------------------------------------------------------------


def _dotted_name(relative: str) -> str:
	"""The module name of a scanned file; a dot in a file stem (``test_a.old.py``) becomes ``_``."""
	parts = relative[: -len(".py")].split("/")
	if parts[-1] == "__init__":
		parts.pop()
	else:
		parts[-1] = parts[-1].replace(".", "_")
	return ".".join([PACKAGE, *parts])


def _tree_loader(root: Path) -> Callable[[str], Source | None]:
	def candidates(parts: list[str]) -> list[tuple[Path, bool]]:
		if not parts:
			return [(root / "__init__.py", True)]
		base = root.joinpath(*parts)
		return [(base.with_suffix(".py"), False), (base / "__init__.py", True)]

	def loader(dotted: str) -> Source | None:
		if dotted != PACKAGE and not dotted.startswith(PACKAGE + "."):
			return None
		for path, is_package in candidates(dotted[len(PACKAGE) :].split(".")[1:]):
			try:
				return Source(path.read_bytes(), is_package)
			except OSError:
				continue
		return None

	return loader


def _collected_by_pytest(name: str) -> bool:
	return any(fnmatch.fnmatchcase(name, pattern) for pattern in COLLECTED_PATTERNS)


def _outside_the_repo_code(root: Path, relative: Path) -> bool:
	"""True when a folder above the file is hidden (``.venv``, ``.idea``), a cache, or a virtual environment.

	Only the folders below ``root`` count, so a tree that itself sits in a hidden folder is still scanned.
	"""
	folders = relative.parts[:-1]
	if any(name.startswith(".") or name in CACHE_FOLDERS for name in folders):
		return True
	return any((root.joinpath(*folders[:depth]) / VENV_MARKER).is_file() for depth in range(1, len(folders) + 1))


def scanned_files(root: Path, exempt: dict[str, str]) -> dict[str, Path]:
	"""``{posix path relative to root: path}`` of every file the gate scans, in sorted order."""
	found = {}
	for path in sorted(root.rglob("*.py")):
		relative = path.relative_to(root)
		if relative.as_posix() in exempt or path.is_dir():
			continue
		if _outside_the_repo_code(root, relative):
			continue
		if SKIPPED_FOLDERS & set(relative.parts[:-1]) and not _collected_by_pytest(path.name):
			continue
		found[relative.as_posix()] = path
	return found


def scan_tree(root: Path, exempt: dict[str, str]) -> dict[str, FileScan]:
	"""``{posix path relative to root: FileScan}`` for every scanned ``*.py`` under ``root``."""
	resolver = Resolver(_tree_loader(root))
	scans = {}
	for rel, path in scanned_files(root, exempt).items():
		try:
			data = path.read_bytes()
		except OSError as error:
			scans[rel] = _failed_scan(f"cannot read: {error.strerror or type(error).__name__}")
			continue
		dotted = _dotted_name(rel)
		resolver.preload(dotted, Source(data, path.name == "__init__.py"))
		scans[rel] = resolver.scan(dotted)
	return scans


def file_problems(
	rel: str, scan: FileScan, allowed: int, prefix: str
) -> tuple[list[list[str]], list[str], bool]:
	"""``(problems, change lines, needs the remedies)`` for one file against its baseline count.

	A problem is a list of lines. A change line is one a person may paste into the baseline module:
	a lower count or a ``delete:`` line, never a higher count.
	"""
	problems = [[f"{prefix}{rel}:{p.line}: {p.text}"] for p in scan.problems]
	found = len(scan.unmarked)
	if scan.failed or found == allowed:
		return problems, [], False
	if found > allowed:
		head = f"{prefix}{rel}: the file has {found} unmarked repo read(s), the baseline allows {allowed}"
		sites = [f"{prefix}{rel}:{s.line}: repo read ({s.kind})" for s in scan.unmarked]
		return [*problems, [head, *sites]], [], True
	if found:
		line = f"{prefix}{rel}: the file has {found} unmarked repo read(s), the baseline says {allowed}"
		return [*problems, [line]], [f'\t"{rel}": {found},'], False
	line = f"{prefix}{rel}: no repo read left, its BASELINE line says {allowed}"
	return [*problems, [line]], [_delete_line(rel, allowed)], False


def _delete_line(rel: str, allowed: int) -> str:
	return f'\tdelete: "{rel}": {allowed},'


def _marked_total_problems(
	scans: dict[str, FileScan], marked_total: int, prefix: str
) -> tuple[list[list[str]], list[str]]:
	"""The problem and the change line when the valid markers do not match ``marked_total``."""
	listing = [f"  {prefix}{rel}:{line}" for rel, scan in scans.items() for line in scan.marker_lines]
	if len(listing) > marked_total:
		head = (
			f"more valid markers than MARKED_TOTAL allows (it says {marked_total}), so a new waiver: "
			"raise MARKED_TOTAL by the number of markers you added, so the reviewer sees it. Valid markers:"
		)
		return [[head, *listing]], []
	if len(listing) < marked_total and not any(scan.failed for scan in scans.values()):
		head = (
			f"{len(listing)} valid marker(s), but MARKED_TOTAL says {marked_total}: lower it. Valid markers:"
		)
		return [[head, *listing]], [f"\tMARKED_TOTAL = {len(listing)}"]
	return [], []  # with a failed file the real count is unknown, so a lower total is not reported


def check_tree(
	root: Path, baseline: dict[str, int], marked_total: int, exempt: dict[str, str], prefix: str = ""
) -> str | None:
	"""The gate: ``None`` when the tree matches ``baseline`` and ``marked_total``, else the message."""
	scans = scan_tree(root, exempt)
	problems: list[list[str]] = []
	changes: list[str] = []
	remedy = False
	for rel, scan in scans.items():
		found, change, needs = file_problems(rel, scan, baseline.get(rel, 0), prefix)
		problems += found
		changes += change
		remedy = remedy or needs
	problems += [
		[f"{prefix}{rel}: in EXEMPT but no such file, delete its line"]
		for rel in sorted(exempt)
		if not (root / rel).is_file()
	]
	vanished = sorted(set(baseline) - set(scans))
	problems += [[f"{prefix}{rel}: in BASELINE but not a scanned file"] for rel in vanished]
	changes += [_delete_line(rel, baseline[rel]) for rel in vanished]
	total_problems, total_changes = _marked_total_problems(scans, marked_total, prefix)
	problems += total_problems
	changes += total_changes
	if not problems:
		return None
	renamed = bool(vanished) and any(rel not in baseline and scan.unmarked for rel, scan in scans.items())
	return _message(problems, changes, remedy, renamed)


def _message(problems: list[list[str]], changes: list[str], remedy: bool, renamed: bool) -> str:
	flat = [line for problem in problems for line in problem]
	lines = [f"repo-read lint: {len(problems)} problem(s)", TITLE_NOTE, "", *flat[:MAX_PROBLEM_LINES]]
	if len(flat) > MAX_PROBLEM_LINES:
		lines.append(f"... and {len(flat) - MAX_PROBLEM_LINES} more line(s)")
	if renamed:
		lines += ["", RENAME_HINT]
	if remedy:
		lines += ["", REMEDIES]
	if changes:
		lines += ["", f"Change {BASELINE_FILE}:", *changes]
	return "\n".join(lines)
