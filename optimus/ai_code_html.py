"""Recover visible code text without treating decoded source as HTML."""

from html.parser import HTMLParser

_VOID = frozenset(
	{
		"area",
		"base",
		"br",
		"col",
		"embed",
		"hr",
		"img",
		"input",
		"link",
		"meta",
		"param",
		"source",
		"track",
		"wbr",
	}
)


class InvalidCodeHTML(ValueError):
	pass


class CodeText(HTMLParser):
	def __init__(self):
		super().__init__(convert_charrefs=True)
		self.blocks = []
		self.stack = []
		self.parts = []
		self.line = None
		self.line_depth = None

	def handle_starttag(self, tag, attrs):
		if tag == "pre":
			if self.stack:
				raise InvalidCodeHTML("nested code container")
			self.parts = []
			self.stack = ["pre"]
		elif self.stack:
			if tag == "br":
				self.handle_data("\n")
			if tag not in _VOID:
				self.stack.append(tag)
			if tag == "span" and "dh-line" in (dict(attrs).get("class") or "").split():
				if self.line is not None:
					raise InvalidCodeHTML("nested code line")
				self.line, self.line_depth = [], len(self.stack)

	def handle_startendtag(self, tag, attrs):
		self.handle_starttag(tag, attrs)
		if tag not in _VOID:
			self.handle_endtag(tag)

	def handle_endtag(self, tag):
		if not self.stack:
			return
		if tag != self.stack[-1]:
			raise InvalidCodeHTML("unbalanced code markup")
		if self.line is not None and self.line_depth == len(self.stack):
			text = "".join(self.line)
			self.parts.append(("" if text == "\u200b" else text) + "\n")
			self.line = self.line_depth = None
		self.stack.pop()
		if tag == "pre":
			self.blocks.append("".join(self.parts).strip("\n"))

	def handle_data(self, data):
		if self.stack:
			(self.line if self.line is not None else self.parts).append(data)

	def finish(self, content):
		self.feed(content)
		self.close()
		if self.stack:
			raise InvalidCodeHTML("unclosed code container")
		return self.blocks


def rendered_code(content):
	return CodeText().finish(content)


class LineText(HTMLParser):
	"""Ignore raw code-container markup; preserve source-like angle brackets."""

	def __init__(self):
		super().__init__(convert_charrefs=True)
		self.parts = []

	def handle_starttag(self, tag, attrs):
		if tag not in {"pre", "code", "span", "details"}:
			self.parts.append(self.get_starttag_text())

	def handle_endtag(self, tag):
		if tag not in {"pre", "code", "span", "details"}:
			self.parts.append(f"</{tag}>")

	def handle_data(self, data):
		self.parts.append(data)


def code_key(line):
	import re

	parser = LineText()
	parser.feed(line)
	parser.close()
	return re.sub(r"[\s`*_]", "", "".join(parser.parts))
