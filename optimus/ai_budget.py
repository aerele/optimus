# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Context-window budgeting and safe user-message assembly for AI calls. Pure.

Sizes are data-driven: ``ai_fix`` passes the provider's ``context_tokens``
(``_PROVIDER_DEFAULTS`` or the ``ai_context_tokens`` Setting) and everything else
is derived here. Sizes are measured in ``text_size`` units: one per ASCII
character and 3.3 per other character, so a CJK, Arabic or Hebrew character
(often a whole token) is costed as one token. Token counts are estimated at 3.3
units per token, which is conservative against the tokenizers measured on real
Optimus prompts (3.6 to 4.05 ASCII characters per token); the re-ask fit then
self-calibrates from the usage the provider reports for the first call.
"""

from __future__ import annotations

import math
import re
import secrets

CHARS_PER_TOKEN_SAFE = 3.3
CHARS_PER_TOKEN_CENTRAL = 4.0
TEMPLATE_TOKENS = 64  # chat-template role markers and similar overhead
MAX_OUTPUT_TOKENS = 1024  # stored answers measure 194 to 366 tokens
MIN_OUTPUT_TOKENS = 512
REASK_MIN_OUTPUT_TOKENS = 400
MIN_USER_CHARS = 1200
MAX_USER_CHARS = 18000
TRUNCATION_RATIO = 0.6  # reported prompt tokens below this share of the estimate = the server cut the prompt


def text_size(text: str) -> int:
	"""Budget units of ``text``: ``len(text)`` for ASCII; every other character
	counts CHARS_PER_TOKEN_SAFE units (one estimated token)."""
	t = text or ""
	other = len(t) - len(t.encode("ascii", "ignore"))
	return len(t) + math.ceil(other * (CHARS_PER_TOKEN_SAFE - 1))


def clip(text: str, size: int) -> str:
	"""The longest prefix of ``text`` whose ``text_size`` is at most ``size``."""
	t = text or ""
	if text_size(t) <= size:
		return t
	lo, hi = 0, len(t)
	while lo < hi:
		mid = (lo + hi + 1) // 2
		if text_size(t[:mid]) <= size:
			lo = mid
		else:
			hi = mid - 1
	return t[:lo]


def estimate_tokens(text: str) -> int:
	return math.ceil(text_size(text) / CHARS_PER_TOKEN_SAFE)


def output_tokens(context_tokens: int) -> int:
	"""The completion budget: a fifth of the window, between 512 and 1024 tokens."""
	return min(MAX_OUTPUT_TOKENS, max(MIN_OUTPUT_TOKENS, int(context_tokens) // 5))


def min_context_tokens(system: str) -> int:
	"""The smallest window that holds ``system``, a minimal answer and a short user message."""
	return estimate_tokens(system) + MIN_OUTPUT_TOKENS + REASK_MIN_OUTPUT_TOKENS


def user_char_budget(context_tokens: int, system: str, *, out_tokens: int) -> int:
	"""``text_size`` units left for the user message once the system prompt, the
	completion and the chat template are reserved, clamped to [1200, 18000]."""
	free = int(context_tokens) - estimate_tokens(system) - int(out_tokens) - TEMPLATE_TOKENS
	return max(MIN_USER_CHARS, min(MAX_USER_CHARS, int(free * CHARS_PER_TOKEN_SAFE)))


def fence_for(content: str) -> str:
	"""A backtick fence longer than any backtick run inside ``content``, so captured
	source or SQL that contains a fence cannot close ours."""
	longest = max((len(m) for m in re.findall(r"`+", content or "")), default=0)
	return "`" * max(3, longest + 1)


def new_nonce() -> str:
	return secrets.token_hex(3)


def data_block(kind: str, text: str, *, lang: str = "", nonce: str | None = None) -> str:
	"""Wrap captured text in a per-call ``<data-NONCE kind="...">`` tag. With ``lang``
	the text also sits in a fence that no backtick run inside it can close. The
	caller truncates ``text`` before wrapping so a cut never lands inside the fence."""
	tag = "data-" + (nonce or new_nonce())
	body = "" if text is None else str(text)
	if lang:
		fence = fence_for(body)
		body = f"{fence}{lang}\n{body}\n{fence}"
	return f'<{tag} kind="{kind}">\n{body}\n</{tag}>'


def assemble(parts: list[tuple[int, str]], budget_chars: int) -> str:
	"""Join ``(priority, text)`` parts with blank lines, in their given order. While
	the result is over ``budget_chars`` drop whole parts, highest priority number
	first (the later part on a tie); priority 0 is never dropped. Never cuts
	inside a part, so a fenced block is never split."""
	keep = [i for i, (_, text) in enumerate(parts) if text]

	def joined() -> str:
		return "\n\n".join(parts[i][1] for i in keep)

	droppable = sorted((i for i in keep if parts[i][0] > 0), key=lambda i: (parts[i][0], i))
	while droppable and text_size(joined()) > budget_chars:
		keep.remove(droppable.pop())
	return joined()


def trim_window(window: list[dict], *, max_lines: int) -> list[dict]:
	"""Keep ``max_lines`` rows of a source window centred on the ``is_target`` row
	(the middle row when none is marked)."""
	if max_lines <= 0:
		return []
	if len(window) <= max_lines:
		return list(window)
	t = next((i for i, row in enumerate(window) if row.get("is_target")), len(window) // 2)
	lo = max(0, min(t - max_lines // 2, len(window) - max_lines))
	return list(window[lo : lo + max_lines])


def reask_output_tokens(out_tokens: int, first_completion: int) -> int:
	"""A rewrite is about as long as the answer it repairs: twice that, at least 400."""
	return min(int(out_tokens), max(REASK_MIN_OUTPUT_TOKENS, 2 * int(first_completion or 0)))


def reask_fits(context_tokens: int, usage: dict, reask_text: str, *, out_tokens: int) -> bool:
	"""Whether the re-ask (first prompt + first answer + re-ask text + rewrite) fits
	the window. ``usage`` is the first call's usage; the caller fills estimates for
	fields the provider did not report."""
	prompt = int(usage.get("prompt_tokens") or 0)
	completion = int(usage.get("completion_tokens") or 0)
	need = prompt + completion + estimate_tokens(reask_text) + reask_output_tokens(out_tokens, completion)
	return need <= int(context_tokens)


def context_truncated(usage: dict, sent_size: int) -> bool:
	"""True when the provider reports far fewer prompt tokens than were sent
	(``sent_size`` in ``text_size`` units): an Ollama server whose real num_ctx is
	smaller than the prompt drops tokens silently and reports only what it kept."""
	reported = int(usage.get("prompt_tokens") or 0)
	return bool(reported) and reported < TRUNCATION_RATIO * (int(sent_size) / CHARS_PER_TOKEN_CENTRAL)
