#!/usr/bin/env python3
"""
Event-mode tests: wait()/process_events() returning EVENT_* constants,
accept_body*() body delivery, and EVENT_ERROR surfacing.

Uses raw TCP servers so we can produce exact framing and timing.
"""
import os
import socket
import tempfile
import threading
import time
import unittest

from uhttp import client as uhttp_client
from uhttp.client import (
    EVENT_RESPONSE, EVENT_HEADERS, EVENT_DATA, EVENT_COMPLETE, EVENT_ERROR)


class RawServer:
    """Single connection, sends response fragments (optionally with delay)."""

    def __init__(self, fragments, delay=0.0, requests=1, close=True):
        self._fragments = fragments
        self._delay = delay
        self._requests = requests
        self._close = close
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(('127.0.0.1', 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
            for _ in range(self._requests):
                conn.recv(4096)
                for frag in self._fragments:
                    conn.sendall(frag)
                    if self._delay:
                        time.sleep(self._delay)
            if self._close:
                conn.close()
            else:
                time.sleep(0.5)
                conn.close()
        except OSError:
            pass
        finally:
            self._sock.close()

    def stop(self):
        try:
            self._sock.close()
        except OSError:
            pass


def drive(client, on_headers=None, on_data=None, max_time=5.0):
    """Run the event loop until a terminal event; collect event sequence."""
    events = []
    start = time.time()
    while time.time() - start < max_time:
        event = client.wait(timeout=0.3)
        if event is None:
            continue
        events.append(event)
        if event == EVENT_HEADERS and on_headers:
            on_headers(client)
        elif event == EVENT_DATA and on_data:
            on_data(client)
        if event in (EVENT_RESPONSE, EVENT_COMPLETE, EVENT_ERROR):
            break
    return events


class TestEventModeBasic(unittest.TestCase):
    """Small responses arrive as a single EVENT_RESPONSE"""

    def test_small_response_event(self):
        server = RawServer([
            b'HTTP/1.1 200 OK\r\n'
            b'Content-Type: application/json\r\n'
            b'Content-Length: 17\r\n'
            b'Connection: close\r\n\r\n'
            b'{"key": "value"}\n'])
        try:
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port, event_mode=True)
            client.get('/')
            events = drive(client)
            self.assertEqual(events, [EVENT_RESPONSE])
            self.assertEqual(client.status, 200)
            self.assertEqual(client.content_type, 'application/json')
            self.assertEqual(client.response.json(), {'key': 'value'})
            client.close()
        finally:
            server.stop()

    def test_keep_alive_two_requests(self):
        body = b'{"n": 1}'
        resp = (b'HTTP/1.1 200 OK\r\n'
                b'Content-Type: application/json\r\n'
                b'Content-Length: %d\r\n'
                b'Connection: keep-alive\r\n\r\n' % len(body)) + body
        server = RawServer([resp], requests=2, close=False)
        try:
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port, event_mode=True)
            client.get('/a')
            self.assertEqual(drive(client), [EVENT_RESPONSE])
            self.assertTrue(client.is_connected)   # kept alive
            self.assertEqual(client.response.json(), {'n': 1})
            # second request reuses the same socket
            client.get('/b')
            self.assertEqual(drive(client), [EVENT_RESPONSE])
            self.assertEqual(client.response.json(), {'n': 1})
            client.close()
        finally:
            server.stop()


class TestEventModeBody(unittest.TestCase):
    """EVENT_HEADERS + accept_body*() variants"""

    def _headers(self, extra):
        return (b'HTTP/1.1 200 OK\r\n' + extra + b'Connection: close\r\n\r\n')

    def test_accept_body_buffer(self):
        # Body delivered in fragments so headers land alone first.
        server = RawServer([
            self._headers(b'Content-Length: 11\r\n'),
            b'hello',
            b' world',
        ], delay=0.05)
        try:
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port, event_mode=True)
            client.get('/')
            events = drive(client, on_headers=lambda c: c.accept_body())
            self.assertEqual(events[0], EVENT_HEADERS)
            self.assertEqual(events[-1], EVENT_COMPLETE)
            self.assertEqual(client.response.data, b'hello world')
            client.close()
        finally:
            server.stop()

    def test_accept_body_streaming(self):
        server = RawServer([
            self._headers(b'Transfer-Encoding: chunked\r\n'),
            b'5\r\nhello\r\n',
            b'6\r\n world\r\n',
            b'0\r\n\r\n',
        ], delay=0.05)
        chunks = []
        try:
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port, event_mode=True)
            client.get('/')
            events = drive(
                client,
                on_headers=lambda c: c.accept_body_streaming(),
                on_data=lambda c: chunks.append(c.read_buffer()))
            self.assertEqual(events[0], EVENT_HEADERS)
            self.assertEqual(events[-1], EVENT_COMPLETE)
            self.assertIn(EVENT_DATA, events)
            self.assertEqual(b''.join(chunks), b'hello world')
            self.assertEqual(client.bytes_received, 11)
            client.close()
        finally:
            server.stop()

    def test_accept_body_to_file(self):
        payload = b'x' * 3000
        server = RawServer([
            self._headers(b'Content-Length: %d\r\n' % len(payload)),
            payload[:1000], payload[1000:],
        ], delay=0.03)
        fd, path = tempfile.mkstemp()
        os.close(fd)
        try:
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port, event_mode=True)
            client.get('/')
            events = drive(
                client, on_headers=lambda c: c.accept_body_to_file(path))
            self.assertEqual(events[-1], EVENT_COMPLETE)
            with open(path, 'rb') as fh:
                self.assertEqual(fh.read(), payload)
            self.assertEqual(client.bytes_received, len(payload))
            client.close()
        finally:
            server.stop()
            os.remove(path)

    def test_stream_eof_reader(self):
        # No Content-Length, no chunked: stream=True reads until close.
        server = RawServer([
            self._headers(b'Content-Type: text/plain\r\n'),
            b'line one\n', b'line two\n',
        ], delay=0.03, close=True)
        chunks = []
        try:
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port, event_mode=True)
            client.get('/', stream=True)
            events = drive(
                client,
                on_headers=lambda c: c.accept_body_streaming(),
                on_data=lambda c: chunks.append(c.read_buffer()))
            self.assertEqual(events[-1], EVENT_COMPLETE)
            self.assertEqual(b''.join(chunks), b'line one\nline two\n')
            self.assertFalse(client.is_connected)  # EOF cannot keep alive
            client.close()
        finally:
            server.stop()


class TestEventModeNdjson(unittest.TestCase):
    """accept_ndjson() delivers one decoded record per EVENT_DATA"""

    def _headers(self, extra=b''):
        return (b'HTTP/1.1 200 OK\r\n'
                b'Content-Type: application/x-ndjson\r\n'
                + extra + b'Connection: close\r\n\r\n')

    def test_ndjson_records(self):
        # Records split awkwardly across fragments (mid-line boundaries).
        server = RawServer([
            self._headers(),
            b'{"a": 1}\n{"b":',
            b' 2}\n{"c": 3}\n',
        ], delay=0.04, close=True)
        records = []
        try:
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port, event_mode=True)
            client.get('/', stream=True)
            events = drive(
                client,
                on_headers=lambda c: c.accept_ndjson(),
                on_data=lambda c: records.append(c.read_record()))
            self.assertEqual(events[-1], EVENT_COMPLETE)
            self.assertEqual(records, [{'a': 1}, {'b': 2}, {'c': 3}])
            client.close()
        finally:
            server.stop()

    def test_ndjson_trailing_line_without_newline(self):
        server = RawServer([
            self._headers(),
            b'{"x": 1}\n{"y": 2}',   # last line has no trailing newline
        ], delay=0.0, close=True)
        records = []
        try:
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port, event_mode=True)
            client.get('/', stream=True)
            drive(
                client,
                on_headers=lambda c: c.accept_ndjson(),
                on_data=lambda c: records.append(c.read_record()))
            self.assertEqual(records, [{'x': 1}, {'y': 2}])
            client.close()
        finally:
            server.stop()

    def test_ndjson_bad_line_is_event_error(self):
        server = RawServer([
            self._headers(),
            b'{"ok": 1}\nnot json at all\n',
        ], delay=0.04, close=True)
        records = []
        try:
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port, event_mode=True)
            client.get('/', stream=True)
            events = drive(
                client,
                on_headers=lambda c: c.accept_ndjson(),
                on_data=lambda c: records.append(c.read_record()))
            self.assertEqual(events[-1], EVENT_ERROR)
            self.assertIsNotNone(client.error)
            self.assertEqual(records, [{'ok': 1}])  # good record delivered first
            client.close()
        finally:
            server.stop()


class _ResetSocket:
    """Fake socket whose recv() raises a given OSError (e.g. ECONNRESET)."""

    def __init__(self, err):
        self._err = err

    def recv(self, _n):
        raise self._err


class _NoneRecvSocket:
    """Fake socket: recv() returns None (MicroPython SSL would-block)."""

    def recv(self, _n):
        return None


class TestEofConnReset(unittest.TestCase):
    """A peer reset/abort (Windows) is end-of-body for close-delimited reads"""

    def test_connreset_is_eof_for_eof_reader(self):
        import errno
        client = uhttp_client.HttpClient('127.0.0.1', port=1, event_mode=True)
        client._body_reader = uhttp_client._EofBodyReader()
        client._socket = _ResetSocket(OSError(errno.ECONNRESET, 'reset'))
        # Must not raise; the reset means the body is complete.
        self.assertTrue(client._recv_to_buffer(100))
        self.assertTrue(client._body_reader.complete)

    def test_any_recv_error_is_eof_for_eof_reader(self):
        import errno
        # Windows surfaces the close with a platform-specific errno; for a
        # close-delimited body any non-would-block error means end-of-body.
        client = uhttp_client.HttpClient('127.0.0.1', port=1, event_mode=True)
        client._body_reader = uhttp_client._EofBodyReader()
        client._socket = _ResetSocket(OSError(errno.ETIMEDOUT, 'boom'))
        self.assertTrue(client._recv_to_buffer(100))
        self.assertTrue(client._body_reader.complete)

    def test_wouldblock_is_not_eof_for_eof_reader(self):
        import errno
        # Windows non-blocking recv raises EWOULDBLOCK (10035, != EAGAIN).
        # It means 'no data yet', and must NOT be mistaken for end-of-body.
        client = uhttp_client.HttpClient('127.0.0.1', port=1, event_mode=True)
        client._body_reader = uhttp_client._EofBodyReader()
        client._socket = _ResetSocket(OSError(errno.EWOULDBLOCK, 'wouldblock'))
        self.assertFalse(client._recv_to_buffer(100))
        self.assertFalse(client._body_reader.complete)

    def test_connreset_is_error_for_framed_reader(self):
        import errno
        client = uhttp_client.HttpClient('127.0.0.1', port=1, event_mode=True)
        # Length reader is keep-alive capable: a reset mid-body is a failure.
        client._body_reader = uhttp_client._LengthBodyReader(100)
        client._socket = _ResetSocket(OSError(errno.ECONNRESET, 'reset'))
        with self.assertRaises(uhttp_client.HttpConnectionError):
            client._recv_to_buffer(100)

    def test_recv_none_is_would_block_not_eof(self):
        # MicroPython SSL recv() returns None on would-block: must NOT be
        # mistaken for end-of-body by a close-delimited reader.
        client = uhttp_client.HttpClient('127.0.0.1', port=1, event_mode=True)
        client._body_reader = uhttp_client._EofBodyReader()
        client._socket = _NoneRecvSocket()
        self.assertFalse(client._recv_to_buffer(100))
        self.assertFalse(client._body_reader.complete)


class TestEventModeError(unittest.TestCase):
    """Connection errors surface as EVENT_ERROR, not exceptions"""

    def test_disconnect_mid_headers(self):
        server = RawServer([b'HTTP/1.1 200 OK\r\nContent-Len'])  # truncated
        try:
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port, event_mode=True)
            client.get('/')
            events = drive(client)
            self.assertEqual(events[-1], EVENT_ERROR)
            self.assertIsNotNone(client.error)
            client.close()
        finally:
            server.stop()


if __name__ == '__main__':
    unittest.main()
