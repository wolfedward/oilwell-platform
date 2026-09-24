"""TCP teardown must preserve early HTTP error responses, with bounded drain."""
import socket
import threading
import time
import unittest

from backend.http_server import PlatformHTTPServer, PlatformRequestHandler


class HttpTeardownTests(unittest.TestCase):
    def setUp(self):
        self.server = PlatformHTTPServer(('127.0.0.1', 0), PlatformRequestHandler)
        self.addCleanup(self.server.server_close)
        self.incoming, self.peer = socket.socketpair()
        self.addCleanup(self.incoming.close)
        self.addCleanup(self.peer.close)
        self.peer.settimeout(2)

    def test_early_response_half_closes_then_accepts_remaining_request(self):
        finished = threading.Event()
        def close():
            try:
                self.server.shutdown_request(self.incoming)
            finally:
                finished.set()
        worker = threading.Thread(target=close)
        worker.start()
        try:
            self.assertEqual(self.peer.recv(1), b'', 'Response side must close promptly')
            self.assertFalse(finished.wait(.05), 'Full close must wait for delayed request bytes')
            self.peer.sendall(b'remaining request body')
            self.peer.shutdown(socket.SHUT_WR)
            self.assertTrue(finished.wait(.5), 'Peer EOF should finish drain immediately')
        finally:
            self.peer.close()
            worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_uncooperative_peer_cannot_hold_teardown_open(self):
        started = time.monotonic()
        worker = threading.Thread(target=self.server.shutdown_request, args=(self.incoming,))
        worker.start()
        worker.join(1.5)
        try:
            self.assertFalse(worker.is_alive(), 'Drain needs an absolute time bound')
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            self.peer.close()
            worker.join(2)
