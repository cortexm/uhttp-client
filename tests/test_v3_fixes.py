#!/usr/bin/env python3
"""Regression tests for the v3 review findings (items 1-7).

Each class pins one defect the selectors migration introduced or left open;
the explanations live in CLAUDE.md ("Selector Event Loop" / "Keep-alive
lifecycle").
"""
import selectors
import socket
import time
import unittest

from tests.helpers import (
    SSL_AVAILABLE, HangingServer, KeepAliveServer, _ListenerThread,
    client_ssl_context, server_ssl_context)
from uhttp import client as uhttp_client
from uhttp.client import EVENT_ERROR


class BulkTlsServer(_ListenerThread):
    """TLS server answering with a body large enough to sit in the SSL buffer."""

    def __init__(self, body_size=12000):
        self._body = b'x' * body_size
        self._ctx = server_ssl_context()
        super().__init__(backlog=1)

    def _serve(self):
        try:
            raw, _ = self._sock.accept()
            conn = self._ctx.wrap_socket(raw, server_side=True)
            conn.recv(4096)
            # Chunked: the reader recvs in BODY_CHUNK_SIZE steps, so the
            # rest of the single TLS record stays in the SSL buffer where
            # the kernel selector cannot see it. Keep-alive and no close,
            # so no FIN comes along to wake the selector and hide the stall.
            conn.sendall(
                b'HTTP/1.1 200 OK\r\n'
                b'Transfer-Encoding: chunked\r\n'
                b'Connection: keep-alive\r\n\r\n'
                + b'%x\r\n' % len(self._body) + self._body
                + b'\r\n0\r\n\r\n')
            time.sleep(5.0)
            conn.close()
        except OSError:
            pass
        finally:
            self.stop()


@unittest.skipUnless(SSL_AVAILABLE, "test cert/key not available")
class TestSslBufferDrain(unittest.TestCase):
    """(1) Bytes sitting in the SSL buffer are invisible to the selector."""

    def test_large_tls_body_does_not_stall(self):
        server = BulkTlsServer(body_size=12000)
        try:
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port,
                ssl_context=client_ssl_context())
            started = time.time()
            response = client.get('/bulk').wait(timeout=3)
            self.assertEqual(len(response.data), 12000)
            # Stalling on the SSL buffer showed up as the full wait timeout.
            self.assertLess(time.time() - started, 2.0)
            client.close()
        finally:
            server.stop()


class TestWaitOnSharedSelector(unittest.TestCase):
    """(2) Blocking wait() cannot service foreign keys, so it must not spin."""

    def test_wait_refuses_a_shared_selector(self):
        selector = selectors.DefaultSelector()
        server = HangingServer()
        a, b = socket.socketpair()
        try:
            selector.register(a, selectors.EVENT_READ, object())
            b.send(b'x')  # foreign key is permanently ready
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port, selector=selector)
            client.get('/')
            started = time.time()
            with self.assertRaises(uhttp_client.HttpClientError) as ctx:
                client.wait(timeout=0.5)
            self.assertNotIsInstance(
                ctx.exception, uhttp_client.HttpTimeoutError)
            self.assertLess(time.time() - started, 0.4)  # no busy spin
            client.close()
        finally:
            a.close()
            b.close()
            selector.close()
            server.stop()


class TestStaleRetryErrors(unittest.TestCase):
    """(3)(4) The replay must not break the event-mode contract or wedge."""

    def test_failed_replay_is_event_error_not_exception(self):
        server = KeepAliveServer()
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=server.port, event_mode=True)
        try:
            client.get('/one')
            deadline = time.time() + 3.0
            while client.state != uhttp_client.STATE_IDLE:
                if time.time() > deadline:
                    self.fail("first request did not complete")
                client.wait(0.1)
            server.drop_idle()
            # DNS dies with the connection (4G modem drop): the replay's
            # getaddrinfo raises synchronously inside the except handler.
            client._host = 'uhttp-no-such-host.invalid'

            client.get('/two')
            event = None
            deadline = time.time() + 3.0
            while event is None and time.time() < deadline:
                event = client.wait(0.1)
            self.assertEqual(event, EVENT_ERROR)
            self.assertIsNotNone(client.error)
        finally:
            client.close()

    def test_send_failure_on_reused_connection_is_retried(self):
        server = KeepAliveServer()
        original = uhttp_client.HttpClient._try_send
        calls = []

        def failing_send(self):
            calls.append(1)
            if len(calls) == 1:
                raise uhttp_client.HttpConnectionError("Send failed: EPIPE")
            return original(self)

        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            self.assertEqual(client.get('/one').wait().status, 200)
            uhttp_client.HttpClient._try_send = failing_send
            try:
                # The synchronous send on the reused socket blows up; the
                # request must be replayed on a fresh connection.
                self.assertEqual(client.get('/two').wait().status, 200)
            finally:
                uhttp_client.HttpClient._try_send = original
            client.close()
        finally:
            uhttp_client.HttpClient._try_send = original
            server.stop()


class TestKeepAliveHintOnReuse(unittest.TestCase):
    """(5) The Keep-Alive hint must apply to the plain get().wait() API."""

    def test_expired_connection_is_not_reused(self):
        server = KeepAliveServer(keep_alive='timeout=1')
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            client.get('/one').wait()
            self.assertIsNotNone(client._socket)
            client._idle_since -= 10  # past the advertised idle limit

            # No maintenance() call: reuse itself must honour the hint.
            self.assertEqual(client.get('/two').wait().status, 200)
            self.assertFalse(client._connection_reused)
            client.close()
        finally:
            server.stop()


class TestSelectorFailuresAreReported(unittest.TestCase):
    """(6)(7) A dead selector is not a timeout, and arming failures surface."""

    def test_closed_selector_is_not_reported_as_timeout(self):
        server = HangingServer()
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            client.get('/')
            client.selector.close()
            with self.assertRaises(uhttp_client.HttpClientError) as ctx:
                client.wait(timeout=1)
            self.assertNotIsInstance(
                ctx.exception, uhttp_client.HttpTimeoutError)
            client.close()
        finally:
            server.stop()

    def test_arming_failure_raises_in_classic_mode(self):
        server = HangingServer()
        selector = selectors.DefaultSelector()
        try:
            def boom(*args, **kwargs):
                raise OSError("register failed")
            selector.register = boom
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port, selector=selector)
            with self.assertRaises(uhttp_client.HttpConnectionError):
                client.get('/')
            self.assertIsNone(client._socket)
        finally:
            selector.close()
            server.stop()

    def test_arming_failure_is_event_error_in_event_mode(self):
        server = HangingServer()
        selector = selectors.DefaultSelector()
        try:
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port, event_mode=True,
                selector=selector)
            client.get('/')

            def boom(*args, **kwargs):
                raise OSError("modify failed")
            selector.modify = boom
            # Re-arming happens on the next dispatched event.
            deadline = time.time() + 2.0
            while client.event != EVENT_ERROR and time.time() < deadline:
                for key, mask in selector.select(0.05):
                    key.data.handle_event(key.fileobj, mask)
                client.maintenance()
            self.assertEqual(client.event, EVENT_ERROR)
            self.assertIsNotNone(client.error)
        finally:
            selector.close()
            server.stop()


if __name__ == '__main__':
    unittest.main()
