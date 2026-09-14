#!/usr/bin/env python3
"""
Tests for non-blocking connect (TCP and SSL handshake via select)
"""
import os
import selectors
import ssl
import sys
import threading
import time
import unittest

from uhttp import client as uhttp_client
from uhttp import server as uhttp_server


TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
CERT_FILE = os.path.join(TESTS_DIR, 'test_cert.pem')
KEY_FILE = os.path.join(TESTS_DIR, 'test_key.pem')
SSL_AVAILABLE = os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)


def _create_server_ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT_FILE, KEY_FILE)
    return ctx


def _create_client_ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class TestNonBlockingConnect(unittest.TestCase):
    """Test non-blocking TCP connect via select"""

    server = None
    server_thread = None
    PORT = 9920

    @classmethod
    def setUpClass(cls):
        cls.server = uhttp_server.HttpServer(port=cls.PORT)

        def run_server():
            try:
                while cls.server:
                    client = cls.server.wait(timeout=0.1)
                    if client:
                        client.respond({'status': 'ok', 'path': client.path})
            except Exception:
                pass

        cls.server_thread = threading.Thread(target=run_server, daemon=True)
        cls.server_thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            for conn in list(cls.server._waiting_connections):
                conn.close()
            cls.server.close()
            cls.server = None

    def test_state_connecting_after_request(self):
        """Test that state is CONNECTING or later after request start"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        client.get('/test')

        self.assertIn(client.state, [
            uhttp_client.STATE_CONNECTING,
            uhttp_client.STATE_SENDING,
            uhttp_client.STATE_RECEIVING_HEADERS,
            uhttp_client.STATE_RECEIVING_BODY,
            uhttp_client.STATE_COMPLETE,
        ])

        response = client.wait()
        self.assertEqual(response.status, 200)
        client.close()

    def test_is_connected_false_during_connecting(self):
        """Test is_connected is False during connect phase"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        self.assertFalse(client.is_connected)

        client.get('/test')

        # If still connecting, is_connected should be False
        if client.state == uhttp_client.STATE_CONNECTING:
            self.assertFalse(client.is_connected)

        client.wait()
        client.close()

    def test_interest_is_write_during_connecting(self):
        """Test the selector interest is WRITE during CONNECTING state"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        client.get('/test')

        if client.state == uhttp_client.STATE_CONNECTING:
            self.assertEqual(client._interest, selectors.EVENT_WRITE)

        client.wait()
        client.close()

    def test_handle_event_completes_connect(self):
        """Test that handle_event handles connect completion"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        client.get('/test')

        response = None
        for _ in range(100):
            for key, mask in client.selector.select(0.1):
                if key.data.handle_event(key.fileobj, mask) is not None:
                    response = client.response
            if response:
                break

        self.assertIsNotNone(response)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()['path'], '/test')
        client.close()

    def test_multiple_clients_nonblocking(self):
        """Test multiple clients connecting non-blocking on one selector"""
        selector = selectors.DefaultSelector()
        self.addCleanup(selector.close)
        clients = [
            uhttp_client.HttpClient(
                '127.0.0.1', port=self.PORT, selector=selector)
            for _ in range(3)
        ]

        for i, client in enumerate(clients):
            client.get(f'/path{i}')

        responses = [None] * len(clients)
        deadline = time.time() + 20
        while None in responses and time.time() < deadline:
            for key, mask in selector.select(0.1):
                ready = key.data.handle_event(key.fileobj, mask)
                if ready is not None:
                    responses[clients.index(ready)] = ready.response

        for i, resp in enumerate(responses):
            self.assertIsNotNone(resp, f"Client {i} got no response")
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.json()['path'], f'/path{i}')

        for client in clients:
            client.close()

    def test_keep_alive_reuses_connection(self):
        """Test keep-alive skips connect on second request"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        # First request - goes through connect
        resp1 = client.get('/first').wait()
        self.assertEqual(resp1.status, 200)
        self.assertTrue(client.is_connected)

        # Second request - socket already connected, no CONNECTING state
        client.get('/second')
        self.assertNotEqual(client.state, uhttp_client.STATE_CONNECTING)

        resp2 = client.wait()
        self.assertEqual(resp2.json()['path'], '/second')
        client.close()

    def test_reconnect_after_close(self):
        """Test reconnect after explicit close"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        resp1 = client.get('/first').wait()
        self.assertEqual(resp1.status, 200)

        client.close()
        self.assertFalse(client.is_connected)

        # New request should reconnect
        resp2 = client.get('/second').wait()
        self.assertEqual(resp2.status, 200)
        self.assertEqual(resp2.json()['path'], '/second')
        client.close()


class TestNonBlockingConnectRefused(unittest.TestCase):
    """Test non-blocking connect error handling"""

    def test_connection_refused(self):
        """Test connection to closed port raises error"""
        client = uhttp_client.HttpClient('127.0.0.1', port=59998)

        with self.assertRaises(uhttp_client.HttpConnectionError):
            client.get('/test').wait()

        client.close()

    @unittest.skipIf(
        sys.platform == 'win32',
        'Windows signals connect errors via except fds in select()')
    def test_connection_refused_via_handle_event(self):
        """Test connection refused detected via handle_event"""
        client = uhttp_client.HttpClient('127.0.0.1', port=59998)

        with self.assertRaises(uhttp_client.HttpConnectionError):
            client.get('/test')
            for _ in range(50):
                for key, mask in client.selector.select(0.1):
                    key.data.handle_event(key.fileobj, mask)

        client.close()

    def test_connect_timeout(self):
        """Test connect timeout with non-routable address"""
        # 192.0.2.1 is TEST-NET (RFC 5737) - packets are dropped
        client = uhttp_client.HttpClient(
            '192.0.2.1', port=80, connect_timeout=0.5, timeout=1)

        with self.assertRaises(uhttp_client.HttpTimeoutError):
            client.get('/test').wait(timeout=2)

        client.close()

    def test_invalid_host(self):
        """Test DNS failure raises error immediately"""
        client = uhttp_client.HttpClient(
            'invalid.host.that.does.not.exist.example')

        with self.assertRaises(uhttp_client.HttpConnectionError):
            client.get('/test').wait()

        client.close()


@unittest.skipUnless(SSL_AVAILABLE, "SSL test certificates not available")
class TestNonBlockingSSLConnect(unittest.TestCase):
    """Test non-blocking SSL handshake via select"""

    server = None
    server_thread = None
    PORT = 9921

    @classmethod
    def setUpClass(cls):
        ssl_ctx = _create_server_ssl_context()
        cls.server = uhttp_server.HttpServer(
            port=cls.PORT, ssl_context=ssl_ctx)

        def run_server():
            try:
                while cls.server:
                    client = cls.server.wait(timeout=0.1)
                    if client:
                        client.respond({
                            'secure': client.is_secure,
                            'path': client.path,
                        })
            except Exception:
                pass

        cls.server_thread = threading.Thread(target=run_server, daemon=True)
        cls.server_thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            for conn in list(cls.server._waiting_connections):
                conn.close()
            cls.server.close()
            cls.server = None

    def test_ssl_nonblocking_handshake(self):
        """Test SSL handshake completes via select loop"""
        ssl_ctx = _create_client_ssl_context()
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=self.PORT, ssl_context=ssl_ctx)

        client.get('/ssl-test')

        response = None
        for _ in range(100):
            for key, mask in client.selector.select(0.1):
                if key.data.handle_event(key.fileobj, mask) is not None:
                    response = client.response
            if response:
                break

        self.assertIsNotNone(response)
        self.assertEqual(response.status, 200)
        self.assertTrue(response.json()['secure'])
        self.assertEqual(response.json()['path'], '/ssl-test')
        client.close()

    def test_ssl_state_transitions(self):
        """Test state passes through SSL_HANDSHAKE"""
        ssl_ctx = _create_client_ssl_context()
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=self.PORT, ssl_context=ssl_ctx)

        client.get('/test')

        # State should be CONNECTING, SSL_HANDSHAKE, SENDING, or later
        self.assertIn(client.state, [
            uhttp_client.STATE_CONNECTING,
            uhttp_client.STATE_SSL_HANDSHAKE,
            uhttp_client.STATE_SENDING,
            uhttp_client.STATE_RECEIVING_HEADERS,
            uhttp_client.STATE_RECEIVING_BODY,
            uhttp_client.STATE_COMPLETE,
        ])

        response = client.wait()
        self.assertEqual(response.status, 200)
        client.close()

    def test_ssl_blocking_wait(self):
        """Test blocking wait works with non-blocking SSL connect"""
        ssl_ctx = _create_client_ssl_context()
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=self.PORT, ssl_context=ssl_ctx)

        response = client.get('/test').wait()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.json()['secure'])
        client.close()

    def test_ssl_keep_alive(self):
        """Test SSL keep-alive reuses connection"""
        ssl_ctx = _create_client_ssl_context()
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=self.PORT, ssl_context=ssl_ctx)

        resp1 = client.get('/first').wait()
        self.assertEqual(resp1.status, 200)
        self.assertTrue(client.is_connected)

        # Second request should not reconnect
        client.get('/second')
        self.assertNotIn(client.state, [
            uhttp_client.STATE_CONNECTING,
            uhttp_client.STATE_SSL_HANDSHAKE,
        ])

        resp2 = client.wait()
        self.assertEqual(resp2.json()['path'], '/second')
        client.close()

    def test_ssl_multiple_clients(self):
        """Test multiple SSL clients on one shared selector"""
        selector = selectors.DefaultSelector()
        self.addCleanup(selector.close)
        clients = []
        for _ in range(3):
            ssl_ctx = _create_client_ssl_context()
            clients.append(uhttp_client.HttpClient(
                '127.0.0.1', port=self.PORT, ssl_context=ssl_ctx,
                selector=selector))

        for i, client in enumerate(clients):
            client.get(f'/path{i}')

        responses = [None] * len(clients)
        deadline = time.time() + 20
        while None in responses and time.time() < deadline:
            for key, mask in selector.select(0.1):
                ready = key.data.handle_event(key.fileobj, mask)
                if ready is not None:
                    responses[clients.index(ready)] = ready.response

        for i, resp in enumerate(responses):
            self.assertIsNotNone(resp, f"Client {i} got no response")
            self.assertEqual(resp.status, 200)

        for client in clients:
            client.close()


if __name__ == '__main__':
    unittest.main()
