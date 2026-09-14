#!/usr/bin/env python3
"""
HTTP client async (non-blocking) tests
"""
import selectors
import threading
import time
import unittest

from uhttp import client as uhttp_client
from uhttp import server as uhttp_server


class TestClientAsync(unittest.TestCase):
    """Test async (non-blocking) client usage with select loop"""

    server = None
    server_thread = None
    PORT = 9903

    @classmethod
    def setUpClass(cls):
        cls.server = uhttp_server.HttpServer(port=cls.PORT)

        def run_server():
            try:
                while cls.server:
                    client = cls.server.wait(timeout=0.1)
                    if client:
                        if client.path == '/slow':
                            time.sleep(0.2)
                        client.respond({'status': 'ok', 'path': client.path})
            except Exception:
                pass

        cls.server_thread = threading.Thread(target=run_server, daemon=True)
        cls.server_thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            # Close all waiting connections first
            for conn in list(cls.server._waiting_connections):
                conn.close()
            cls.server.close()
            cls.server = None

    def test_handle_event(self):
        """Test async processing via selector + handle_event()"""
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
        client.close()

    def test_nothing_registered_before_request(self):
        """Test the selector is empty before a request starts"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        self.assertIsNone(client._interest)
        self.assertEqual(client.selector.get_map(), {})

        client.close()

    def test_state_transitions(self):
        """Test client state transitions during request"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        # Initial state
        self.assertEqual(client.state, uhttp_client.STATE_IDLE)

        # Start request
        client.get('/test')

        # Should be connecting, sending or receiving (depending on how fast)
        self.assertIn(client.state, [
            uhttp_client.STATE_CONNECTING,
            uhttp_client.STATE_SSL_HANDSHAKE,
            uhttp_client.STATE_SENDING,
            uhttp_client.STATE_RECEIVING_HEADERS,
            uhttp_client.STATE_RECEIVING_BODY,
            uhttp_client.STATE_COMPLETE
        ])

        # Complete request
        client.wait()

        # Back to idle
        self.assertEqual(client.state, uhttp_client.STATE_IDLE)

        client.close()

    def test_multiple_clients_shared_selector(self):
        """Test multiple clients driven by one shared selector"""
        selector = selectors.DefaultSelector()
        clients = [
            uhttp_client.HttpClient(
                '127.0.0.1', port=self.PORT, selector=selector)
            for _ in range(3)
        ]

        # Start all requests
        for i, client in enumerate(clients):
            client.get(f'/path{i}')

        # One loop collects every response
        responses = [None] * len(clients)
        deadline = time.time() + 10
        while None in responses and time.time() < deadline:
            for key, mask in selector.select(0.1):
                ready = key.data.handle_event(key.fileobj, mask)
                if ready is not None:
                    responses[clients.index(ready)] = ready.response

        for i, resp in enumerate(responses):
            self.assertIsNotNone(resp)
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.json()['path'], f'/path{i}')

        for client in clients:
            client.close()
        selector.close()

    def test_handle_event_idle_returns_none(self):
        """Test handle_event returns None when idle"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        self.assertIsNone(client.handle_event(None, 0))

        client.close()

    def test_handle_event_timeout(self):
        """Test handle_event raises HttpTimeoutError on timeout"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT, timeout=0.1)
        client.get('/slow')  # Server sleeps 0.2s

        # Wait until timeout expires
        time.sleep(0.2)

        # handle_event should raise timeout
        with self.assertRaises(uhttp_client.HttpTimeoutError):
            client.handle_event(client._socket, 0)

        client.close()

    def test_per_request_timeout(self):
        """Test per-request timeout overrides client timeout"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT, timeout=10)

        # Use short per-request timeout
        client.get('/slow', timeout=0.1)

        # Wait until timeout expires
        time.sleep(0.2)

        # handle_event should raise timeout
        with self.assertRaises(uhttp_client.HttpTimeoutError):
            client.handle_event(client._socket, 0)

        client.close()


if __name__ == '__main__':
    unittest.main()
