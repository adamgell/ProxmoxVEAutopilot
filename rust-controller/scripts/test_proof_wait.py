import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from proof_wait import wait_for


class WaitTests(unittest.TestCase):
    def server(self, responses):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status, body = responses.pop(0) if len(responses) > 1 else responses[0]
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def read(self, url):
        with urllib.request.urlopen(url, timeout=1) as response:
            return json.load(response)

    def test_503_then_success_retries_actual_http(self):
        url = self.server([(503, b"starting"), (200, b'{"ready":true}')])
        self.assertEqual(wait_for(lambda: self.read(url), seconds=1), {"ready": True})

    def test_connection_refusal_retries_until_bounded_deadline(self):
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        calls = []

        def read():
            calls.append(time.monotonic())
            return self.read(f"http://127.0.0.1:{port}")

        began = time.monotonic()
        with self.assertRaisesRegex(AssertionError, "deadline"):
            wait_for(read, seconds=0.25)
        self.assertGreaterEqual(len(calls), 2)
        self.assertLess(time.monotonic() - began, 0.75)

    def test_persistent_503_obeys_deadline(self):
        url = self.server([(503, b"starting")])
        with self.assertRaisesRegex(AssertionError, "deadline"):
            wait_for(lambda: self.read(url), seconds=0.15)

    def test_malformed_response_and_non_retryable_http_fail_immediately(self):
        url = self.server([(200, b"not-json"), (200, b'{"ready":true}')])
        with self.assertRaises(json.JSONDecodeError):
            wait_for(lambda: self.read(url), seconds=1)
        url = self.server([(401, b"denied"), (200, b'{"ready":true}')])
        with self.assertRaises(urllib.error.HTTPError) as failure:
            wait_for(lambda: self.read(url), seconds=1)
        self.assertEqual(failure.exception.code, 401)

    def test_safety_assertion_is_not_retried(self):
        calls = []

        def unsafe():
            calls.append(1)
            raise AssertionError("workflow cap exceeded")

        with self.assertRaisesRegex(AssertionError, "cap exceeded"):
            wait_for(unsafe, seconds=1)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
