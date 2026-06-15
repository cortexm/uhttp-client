#!/usr/bin/env python3
"""
Body framing integration tests using a raw TCP server.

These exercise the response body readers (chunked transfer encoding, fixed
Content-Length, and the no-framing fallback) end-to-end through HttpClient,
which the uhttp test server cannot easily produce.
"""
import socket
import threading
import unittest

from uhttp import client as uhttp_client


class RawResponseServer:
    """Tiny single-shot TCP server that replies with a fixed raw response."""

    def __init__(self, raw_response):
        self._raw_response = raw_response
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
            conn.recv(4096)
            conn.sendall(self._raw_response)
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


class TestChunkedResponse(unittest.TestCase):
    """End-to-end chunked transfer encoding decoding"""

    def _request(self, raw_response):
        server = RawResponseServer(raw_response)
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            response = client.get('/').wait()
            client.close()
            return response
        finally:
            server.stop()

    def test_single_chunk(self):
        response = self._request(
            b'HTTP/1.1 200 OK\r\n'
            b'Transfer-Encoding: chunked\r\n'
            b'Connection: close\r\n\r\n'
            b'5\r\nhello\r\n0\r\n\r\n')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.data, b'hello')

    def test_multiple_chunks(self):
        response = self._request(
            b'HTTP/1.1 200 OK\r\n'
            b'Transfer-Encoding: chunked\r\n'
            b'Connection: close\r\n\r\n'
            b'5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n')
        self.assertEqual(response.data, b'hello world')

    def test_chunk_with_trailers(self):
        response = self._request(
            b'HTTP/1.1 200 OK\r\n'
            b'Transfer-Encoding: chunked\r\n'
            b'Connection: close\r\n\r\n'
            b'4\r\ndata\r\n0\r\nX-Checksum: abc123\r\n\r\n')
        self.assertEqual(response.data, b'data')

    def test_large_chunked_body(self):
        payload = b'x' * 5000
        response = self._request(
            b'HTTP/1.1 200 OK\r\n'
            b'Transfer-Encoding: chunked\r\n'
            b'Connection: close\r\n\r\n'
            + b'%x\r\n' % len(payload) + payload + b'\r\n0\r\n\r\n')
        self.assertEqual(response.data, payload)

    def test_chunked_json(self):
        response = self._request(
            b'HTTP/1.1 200 OK\r\n'
            b'Content-Type: application/json\r\n'
            b'Transfer-Encoding: chunked\r\n'
            b'Connection: close\r\n\r\n'
            b'10\r\n{"key": "value"}\r\n0\r\n\r\n')
        self.assertEqual(response.json(), {'key': 'value'})


class TestFixedLengthResponse(unittest.TestCase):
    """Content-Length framing still works after the reader refactor"""

    def _request(self, raw_response):
        server = RawResponseServer(raw_response)
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            response = client.get('/').wait()
            client.close()
            return response
        finally:
            server.stop()

    def test_content_length_body(self):
        response = self._request(
            b'HTTP/1.1 200 OK\r\n'
            b'Content-Length: 5\r\n'
            b'Connection: close\r\n\r\n'
            b'hello')
        self.assertEqual(response.data, b'hello')

    def test_empty_body_no_framing(self):
        """No Content-Length and no chunked: body is empty (historical)."""
        response = self._request(
            b'HTTP/1.1 204 No Content\r\n'
            b'Connection: close\r\n\r\n')
        self.assertEqual(response.status, 204)
        self.assertEqual(response.data, b'')


if __name__ == '__main__':
    unittest.main()
