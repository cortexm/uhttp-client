#!/usr/bin/env python3
"""Regression tests for the second v3 review of the selectors client."""
import errno
import selectors
import socket
import tempfile
import time
import unittest

from tests.helpers import HangingServer, KeepAliveServer, RawServer
from tests.test_mpy_resources import FakeSocket
from uhttp import client as uhttp_client
from uhttp.client import (
    EVENT_DATA, EVENT_ERROR, EVENT_HEADERS,
    STATE_CONNECTING, STATE_IDLE, STATE_RECEIVING_BODY)

OK_RESPONSE = (
    b'HTTP/1.1 200 OK\r\n'
    b'Content-Length: 16\r\n'
    b'Connection: close\r\n\r\n'
    b'{"key": "value"}')


def pump(client, until, timeout=2.0):
    """Dispatch this client's selector events until until() holds."""
    deadline = time.time() + timeout
    while not until() and time.time() < deadline:
        for key, mask in client.selector.select(0.05):
            key.data.handle_event(key.fileobj, mask)
    return until()


class TestNoBusySpin(unittest.TestCase):

    def test_partial_chunk_frame_blocks_instead_of_spinning(self):
        server = RawServer([
            b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'
            b'5\r\nhello\r\n1'], close=False)
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=server.port, timeout=0.3)
        try:
            client.get('/')
            self.assertTrue(pump(client, lambda: (
                client.state == STATE_RECEIVING_BODY and client._buffer)))
            self.assertEqual(bytes(client._buffer), b'1')
            self.assertFalse(client._has_pending_body())
            cpu = time.process_time()
            with self.assertRaises(uhttp_client.HttpTimeoutError):
                client.wait(0.3)
            self.assertLess(time.process_time() - cpu, 0.05)
        finally:
            client.close()
            server.stop()


class TestSingleSendOnImmediateConnect(unittest.TestCase):

    def test_request_is_sent_once(self):
        server = RawServer([OK_RESPONSE])
        original = uhttp_client.HttpClient._open_socket

        def immediate(family, socktype, proto, addr):
            sock = socket.socket(family, socktype, proto)
            sock.connect(addr)
            sock.setblocking(False)
            return sock, False
        uhttp_client.HttpClient._open_socket = staticmethod(immediate)
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            self.assertEqual(client.get('/once').wait().status, 200)
            client.close()
            self.assertEqual(server.received.count(b'GET /once HTTP/1.1'), 1)
        finally:
            uhttp_client.HttpClient._open_socket = staticmethod(original)
            server.stop()


class TestBodyFileClosedOnClose(unittest.TestCase):

    def test_close_releases_body_file(self):
        client = uhttp_client.HttpClient('127.0.0.1', port=1, event_mode=True)
        path = tempfile.mktemp()
        try:
            client._body_file_handle = open(path, 'wb')
            client._accept_mode = 'file'
            client._fail_event('boom')
            self.assertIsNone(client._body_file_handle)
        finally:
            client.close()


class TestKeepAliveHintOnce(unittest.TestCase):

    def test_hint_survives_responses_without_the_header(self):
        server = KeepAliveServer(keep_alive=['timeout=5, max=100', None])
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            client.get('/one').wait()
            self.assertEqual(client._keep_alive_timeout, 5)
            client.get('/two').wait()
            self.assertEqual(client._keep_alive_timeout, 5)
            self.assertEqual(client._keep_alive_max, 100)
            client.close()
        finally:
            server.stop()


class TestReadyResultWinsOverArmFailure(unittest.TestCase):

    def test_complete_event_is_not_overwritten(self):
        server = KeepAliveServer()
        selector = selectors.DefaultSelector()
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=server.port, event_mode=True, selector=selector)
        original_want = client._wanted_interest
        original_modify = selector.modify

        def want():
            if client._state == STATE_IDLE:
                return selectors.EVENT_WRITE  # force a modify() at completion
            return original_want()

        def modify(fileobj, events, data=None):
            if client._state == STATE_IDLE:
                raise OSError('boom')
            return original_modify(fileobj, events, data)
        client._wanted_interest = want
        selector.modify = modify
        try:
            client.get('/')
            self.assertTrue(pump(client, lambda: client.event is not None))
            self.assertNotEqual(client.event, EVENT_ERROR)
            self.assertEqual(client.response.status, 200)
        finally:
            client.close()
            selector.close()
            server.stop()


class TestForeignFileobjIgnored(unittest.TestCase):

    def test_stale_key_does_not_drive_the_new_socket(self):
        server = HangingServer()
        client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
        a, b = socket.socketpair()
        calls = []
        try:
            client.get('/')
            if client.state != STATE_CONNECTING:
                self.skipTest('connect completed synchronously')
            client._process_connecting = lambda: calls.append(1)
            self.assertIsNone(client.handle_event(a, selectors.EVENT_WRITE))
            self.assertEqual(calls, [])
        finally:
            a.close()
            b.close()
            client.close()
            server.stop()


class TestStaleRetryMethodCase(unittest.TestCase):

    def test_lowercase_idempotent_method_is_retried(self):
        client = uhttp_client.HttpClient('127.0.0.1', port=1)
        try:
            client._connection_reused = True
            client._request_method = 'get'
            self.assertTrue(client._can_retry_stale())
            client._request_method = 'post'
            self.assertFalse(client._can_retry_stale())
        finally:
            client.close()


class TestSelectFailure(unittest.TestCase):

    def _boom(self, *args, **kwargs):
        raise OSError(errno.EPERM, 'poll failed')

    def test_event_mode_reports_event_error(self):
        server = HangingServer()
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=server.port, event_mode=True)
        try:
            client.get('/')
            client.selector.select = self._boom
            self.assertEqual(client.wait(0.1), EVENT_ERROR)
            self.assertIsNotNone(client.error)
        finally:
            client.close()
            server.stop()

    def test_classic_mode_raises_connection_error(self):
        server = HangingServer()
        client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
        try:
            client.get('/')
            client.selector.select = self._boom
            with self.assertRaises(uhttp_client.HttpConnectionError):
                client.wait(0.1)
        finally:
            client.close()
            server.stop()


class TestIdleClearsEvent(unittest.TestCase):

    def test_peer_close_while_idle_clears_stale_event(self):
        server = KeepAliveServer()
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=server.port, event_mode=True)
        try:
            client.get('/')
            self.assertTrue(pump(client, lambda: client.event is not None))
            server.drop_idle()
            self.assertTrue(pump(client, lambda: client._socket is None))
            self.assertIsNone(client.event)
        finally:
            client.close()
            server.stop()


class TestAcceptBodyArmFailure(unittest.TestCase):

    def test_event_mode_reports_event_error(self):
        server = RawServer([
            b'HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n', b'hello'],
            delay=0.3)
        selector = selectors.DefaultSelector()
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=server.port, event_mode=True, selector=selector)
        try:
            client.get('/')
            self.assertTrue(pump(client, lambda: client.event == EVENT_HEADERS))

            def boom(*args, **kwargs):
                raise OSError('register failed')
            selector.register = boom
            client.accept_body_streaming()  # must not raise
            self.assertEqual(client.event, EVENT_ERROR)
            self.assertIsNotNone(client.error)
        finally:
            client.close()
            selector.close()
            server.stop()


class TestDeadIdleSocketProbe(unittest.TestCase):

    def test_post_on_closed_idle_socket_reconnects(self):
        server = KeepAliveServer()
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            self.assertEqual(client.get('/one').wait().status, 200)
            first = client._socket
            server.drop_idle()
            time.sleep(0.05)
            response = client.post('/two', json={'a': 1}).wait()
            self.assertEqual(response.status, 200)
            self.assertIsNot(client._socket, first)
            client.close()
        finally:
            server.stop()


class TestHeaderRecvCap(unittest.TestCase):

    def test_header_recv_is_capped_to_the_chunk_size(self):
        original = uhttp_client.MAX_RESPONSE_HEADERS_LENGTH
        uhttp_client.MAX_RESPONSE_HEADERS_LENGTH = 64 * 1024
        client = uhttp_client.HttpClient('127.0.0.1', port=1)
        try:
            sock = FakeSocket()
            client._socket = sock
            client._state = uhttp_client.STATE_RECEIVING_HEADERS
            client._process_recv_headers()
            self.assertLessEqual(
                sock.recv_sizes[0], uhttp_client.BODY_CHUNK_SIZE)
        finally:
            uhttp_client.MAX_RESPONSE_HEADERS_LENGTH = original
            client._socket = None
            client.close()


class TestStreamBackPressure(unittest.TestCase):

    def test_unread_stream_data_is_not_an_overflow(self):
        server = RawServer([
            b'HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n',
            b'x' * 80, b'y' * 80, b'z' * 80], delay=0.05)
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=server.port, event_mode=True,
            max_response_length=100)
        try:
            client.get('/', stream=True)
            self.assertTrue(pump(client, lambda: client.event == EVENT_HEADERS))
            client.accept_body_streaming()
            self.assertTrue(pump(client, lambda: client.event == EVENT_DATA))
            # Never read_buffer(): the client must stop pulling, not overflow.
            pump(client, lambda: client.event == EVENT_ERROR, timeout=0.5)
            self.assertNotEqual(client.event, EVENT_ERROR)
            self.assertIsNone(client.error)
            self.assertLessEqual(len(client._body), 100)
        finally:
            client.close()
            server.stop()



class TestFixtureShutdown(unittest.TestCase):
    """stop() must leave nothing blocked in accept().

    Closing a listening socket does not wake a thread blocked in accept()
    on Linux (it does on macOS), and the freed fd number goes straight to
    the next socket() call - the zombie thread then accepts a connection
    meant for a later fixture and answers it from the stopped server.
    That is how test_retry_gives_up_when_the_server_is_gone got a valid
    200 from a server it had just stopped, but only on the Linux runner.
    """

    def test_stop_joins_the_accept_thread(self):
        for server in (KeepAliveServer(), HangingServer()):
            server.stop()
            self.assertFalse(
                server._thread.is_alive(), type(server).__name__)

    def test_stopped_port_refuses_new_connections(self):
        server = KeepAliveServer()
        port = server.port
        server.stop()
        sock = socket.socket()
        sock.settimeout(2.0)
        try:
            with self.assertRaises(OSError):
                sock.connect(('127.0.0.1', port))
        finally:
            sock.close()


if __name__ == '__main__':
    unittest.main()
