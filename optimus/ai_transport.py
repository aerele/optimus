"""Attempt-local send certainty; no request or response bodies are retained."""

from contextlib import contextmanager
from contextvars import ContextVar
from types import SimpleNamespace

import requests
from urllib3.exceptions import NewConnectionError

_current = ContextVar("optimus_ai_transport_attempt", default=None)
_rank = {"not_sent": 0, "known_response": 1, "sent_unknown": 2}


@contextmanager
def attempt():
	state = SimpleNamespace(certainty="not_sent")
	token = _current.set(state)
	try:
		yield state
	finally:
		_current.reset(token)


def journaled():
	return _current.get() is not None


def observe(certainty):
	state = _current.get()
	if state and _rank[certainty] > _rank[state.certainty]:
		state.certainty = certainty


def exception_certainty(error):
	if isinstance(error, requests.exceptions.ConnectTimeout):
		return "not_sent"
	# requests wraps urllib3's MaxRetryError in ConnectionError; its reason
	# distinguishes failure to establish a connection from a broken read.
	stack, seen = [error], set()
	while stack and len(seen) < 16:
		item = stack.pop()
		if not isinstance(item, BaseException) or id(item) in seen:
			continue
		seen.add(id(item))
		if isinstance(item, NewConnectionError):  # includes urllib3 2.x NameResolutionError
			return "not_sent"
		stack.extend(item.args)
		stack.extend((item.__cause__, item.__context__, getattr(item, "reason", None)))
	return "sent_unknown"


def response_certainty(status):
	return "sent_unknown" if status in {502, 504, *range(520, 525)} else "known_response"
