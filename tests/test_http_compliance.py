#!/usr/bin/env python3
"""HTTP/1.1 conformance regressions found reviewing the client.

Self-contained fixture on purpose: this file is meant to rebase cleanly onto
branches that reorganise the shared test helpers.
"""
import errno
import socket
import threading
import unittest

from uhttp import client as uhttp_client


class RawServer:
    """Answers one connection with a fixed byte string."""

    def __init__(self, response, host='127.0.0.1'):
        self._response = response
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
            conn.recv(4096)
            conn.sendall(self._response)
            try:
                conn.shutdown(socket.SHUT_WR)
                conn.settimeout(2.0)
                while conn.recv(4096):
                    pass
            except OSError:
                pass
            conn.close()
        except OSError:
            pass
        finally:
            self.stop()

    def stop(self):
        try:
            self._sock.close()
        except OSError:
            pass


class TestBodylessResponses(unittest.TestCase):
    """(9) HEAD / 204 / 304 never carry a body, whatever Content-Length says.

    RFC 7230 3.3.3: the framing of those responses is fixed by the status or
    the request method, so a Content-Length header must not be believed.
    """

    def _request(self, method, raw_response):
        server = RawServer(raw_response)
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            try:
                return client.request(method, '/').wait(timeout=1)
            finally:
                client.close()
        finally:
            server.stop()

    def test_head_with_content_length(self):
        response = self._request(
            'HEAD',
            b'HTTP/1.1 200 OK\r\nContent-Length: 5\r\n'
            b'Connection: close\r\n\r\n')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.data, b'')

    def test_204_with_content_length(self):
        response = self._request(
            'GET',
            b'HTTP/1.1 204 No Content\r\nContent-Length: 5\r\n'
            b'Connection: close\r\n\r\n')
        self.assertEqual(response.status, 204)
        self.assertEqual(response.data, b'')

    def test_304_with_content_length(self):
        response = self._request(
            'GET',
            b'HTTP/1.1 304 Not Modified\r\nContent-Length: 5\r\n'
            b'Connection: close\r\n\r\n')
        self.assertEqual(response.status, 304)
        self.assertEqual(response.data, b'')


class TestAddressFallback(unittest.TestCase):
    """(10) getaddrinfo returns several addresses; all must be tried."""

    def test_unusable_first_address_falls_back(self):
        server = RawServer(
            b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n'
            b'Connection: close\r\n\r\nok')
        original = uhttp_client._socket.getaddrinfo

        def two_addresses(host, port, *args, **kwargs):
            real = original('127.0.0.1', port, *args, **kwargs)
            # A family socket() rejects outright, then the working address.
            return [(-1, socket.SOCK_STREAM, 0, '', ('::1', port))] + list(real)

        uhttp_client._socket.getaddrinfo = two_addresses
        try:
            client = uhttp_client.HttpClient('localhost', port=server.port)
            response = client.get('/').wait(timeout=2)
            self.assertEqual(response.data, b'ok')
            client.close()
        finally:
            uhttp_client._socket.getaddrinfo = original
            server.stop()

    def test_all_addresses_failing_raises_connection_error(self):
        original = uhttp_client._socket.getaddrinfo

        def only_bad(host, port, *args, **kwargs):
            return [(-1, socket.SOCK_STREAM, 0, '', ('::1', port)),
                    (-1, socket.SOCK_STREAM, 0, '', ('127.0.0.1', port))]

        uhttp_client._socket.getaddrinfo = only_bad
        try:
            client = uhttp_client.HttpClient('localhost', port=9)
            with self.assertRaises(uhttp_client.HttpConnectionError):
                client.get('/').wait(timeout=2)
            client.close()
        finally:
            uhttp_client._socket.getaddrinfo = original


class TestMalformedContentLength(unittest.TestCase):
    """(11) A bad Content-Length is a response error, not a raw ValueError."""

    def test_non_numeric_content_length(self):
        server = RawServer(
            b'HTTP/1.1 200 OK\r\nContent-Length: abc\r\n'
            b'Connection: close\r\n\r\n')
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            with self.assertRaises(uhttp_client.HttpResponseError):
                client.get('/').wait(timeout=1)
            # The state machine must not be left mid-request.
            self.assertEqual(client.state, uhttp_client.STATE_IDLE)
            client.close()
        finally:
            server.stop()

    def test_client_is_reusable_after_a_bad_content_length(self):
        server = RawServer(
            b'HTTP/1.1 200 OK\r\nContent-Length: 1x\r\n'
            b'Connection: close\r\n\r\n')
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            with self.assertRaises(uhttp_client.HttpResponseError):
                client.get('/').wait(timeout=1)
            # Used to raise "Request already in progress" forever.
            try:
                client.get('/again')
            except uhttp_client.HttpConnectionError:
                pass  # server is gone; only the wedged state matters here
            client.close()
        finally:
            server.stop()


class TestHeaderOverride(unittest.TestCase):
    """(12) A caller-supplied header replaces the default, whatever its case."""

    def _header_lines(self, request, name):
        prefix = name.lower().encode('ascii') + b':'
        return [line for line in request.split(b'\r\n')
                if line.lower().startswith(prefix)]

    def test_canonical_case_host_is_not_duplicated(self):
        client = uhttp_client.HttpClient('example.com')
        try:
            request = client._build_request(
                'GET', '/', {'Host': 'vhost.example'})
            lines = self._header_lines(request, 'Host')
            self.assertEqual(len(lines), 1, b' | '.join(lines).decode())
            self.assertEqual(lines[0], b'Host: vhost.example')
        finally:
            client.close()

    def test_canonical_case_user_agent_is_not_duplicated(self):
        client = uhttp_client.HttpClient('example.com')
        try:
            request = client._build_request(
                'GET', '/', {'User-Agent': 'mine/1.0'})
            self.assertEqual(len(self._header_lines(request, 'User-Agent')), 1)
        finally:
            client.close()

    def test_canonical_case_content_type_is_not_duplicated(self):
        client = uhttp_client.HttpClient('example.com')
        try:
            request = client._build_request(
                'POST', '/', {'Content-Type': 'text/plain'}, data=b'x')
            self.assertEqual(
                len(self._header_lines(request, 'Content-Type')), 1)
        finally:
            client.close()


class TestIpv6Url(unittest.TestCase):
    """(13) Bracketed IPv6 literals are valid URLs (RFC 3986 3.2.2)."""

    def test_ipv6_with_port(self):
        host, port, path, use_ssl, auth = uhttp_client.parse_url(
            'http://[::1]:8080/x')
        self.assertEqual(host, '::1')
        self.assertEqual(port, 8080)
        self.assertEqual(path, '/x')

    def test_ipv6_without_port(self):
        host, port, path, use_ssl, auth = uhttp_client.parse_url(
            'http://[::1]/x')
        self.assertEqual(host, '::1')
        self.assertEqual(port, 80)

    def test_ipv6_https_default_port(self):
        host, port, path, use_ssl, auth = uhttp_client.parse_url(
            'https://[2001:db8::1]/')
        self.assertEqual(host, '2001:db8::1')
        self.assertEqual(port, 443)
        self.assertTrue(use_ssl)

    def test_ipv6_with_auth(self):
        host, port, path, use_ssl, auth = uhttp_client.parse_url(
            'http://user:pass@[::1]:8080/x')
        self.assertEqual(host, '::1')
        self.assertEqual(port, 8080)
        self.assertEqual(auth, ('user', 'pass'))

    def test_ipv6_client_sends_bare_host_header(self):
        client = uhttp_client.HttpClient('http://[::1]:8080/')
        try:
            self.assertEqual(client.host, '::1')
            self.assertEqual(client.port, 8080)
        finally:
            client.close()


class TestPortableErrno(unittest.TestCase):
    """(14) Every site must use the EWOULDBLOCK fallback constant."""

    def test_no_site_reads_errno_ewouldblock_directly(self):
        with open(uhttp_client.__file__) as handle:
            source = handle.read()
        below_fallback = source.split('EWOULDBLOCK = getattr', 1)[1]
        offenders = [line.strip() for line in below_fallback.splitlines()
                     if 'errno.EWOULDBLOCK' in line]
        self.assertEqual(
            offenders, [],
            "some MicroPython ports do not define errno.EWOULDBLOCK")


if __name__ == '__main__':
    unittest.main()
