"""Actual local HTTP failures distinguish unsent from potentially billed calls."""

import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from optimus import ai_fix, ai_transport


@contextmanager
def server(mode):
	received = []

	class Handler(BaseHTTPRequestHandler):
		def do_POST(self):
			received.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
			if mode == "close":
				self.connection.shutdown(socket.SHUT_RDWR)
				self.connection.close()
				return
			if mode == "stall":
				time.sleep(0.4)
				return
			self.send_response(mode)
			self.send_header("Content-Type", "application/json")
			self.end_headers()
			self.wfile.write(b'{"error":{"message":"synthetic"}}')

		def log_message(self, *args):
			pass

	http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
	thread = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
	thread.start()
	try:
		yield "http://127.0.0.1:" + str(http.server_port), received
	finally:
		http.shutdown()
		http.server_close()
		thread.join(timeout=2)


@pytest.fixture
def wire(monkeypatch):
	client = requests.Session()
	client.trust_env = False
	monkeypatch.setattr(ai_fix.requests, "post", client.post)
	monkeypatch.setattr(ai_fix, "_allow_key_over_http", lambda: False)
	logs = []
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **kw: logs.append((a, kw)))
	yield logs
	client.close()


def send(url):
	with ai_transport.attempt() as state:
		with pytest.raises(ai_fix.AiFixError):
			ai_fix._http_post(url, {}, {"prompt": "synthetic"}, provider="openai", where="test", timeout=0.15)
	return state.certainty


@pytest.mark.parametrize("mode", ["close", "stall", 502, 504, 520, 521, 522, 523, 524])
def test_receipt_followed_by_unknown_outcome_is_not_resendable(wire, mode):
	with server(mode) as (url, received):
		assert send(url) == "sent_unknown"
		assert len(received) == 1
	assert wire == []  # The journal, rather than HTTP, writes the one error row.


def test_connection_refused_is_known_unsent(wire):
	with socket.socket() as sock:
		sock.bind(("127.0.0.1", 0))
		port = sock.getsockname()[1]
	assert send("http://127.0.0.1:" + str(port)) == "not_sent"


def test_unresolvable_domain_is_known_unsent(wire):
	assert send("http://optimus-certainty.invalid") == "not_sent"


def test_known_http_response_is_not_an_uncertain_gateway(wire):
	with server(400) as (url, received):
		assert send(url) == "known_response"
		assert len(received) == 1


def test_an_attempt_never_downgrades_prior_uncertainty():
	with ai_transport.attempt() as state:
		ai_transport.observe("sent_unknown")
		ai_transport.observe("not_sent")
		ai_transport.observe("known_response")
		assert state.certainty == "sent_unknown"
	assert not ai_transport.journaled()
