#!/usr/bin/env python3
"""
HTTP client async (non-blocking) tests
"""
import unittest
import threading
import time
import select
from uhttp import server as uhttp_server
from uhttp import client as uhttp_client


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

    def test_process_events(self):
        """Test async processing with process_events"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        client.get('/test')

        response = None
        for _ in range(100):
            r, w, _ = select.select(
                client.read_sockets,
                client.write_sockets,
                [], 0.1
            )
            response = client.process_events(r, w)
            if response:
                break

        self.assertIsNotNone(response)
        self.assertEqual(response.status, 200)
        client.close()

    def test_read_write_sockets_before_request(self):
        """Test socket lists are empty before request"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        self.assertEqual(client.read_sockets, [])
        self.assertEqual(client.write_sockets, [])

        client.close()

    def test_state_transitions(self):
        """Test client state transitions during request"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        # Initial state
        self.assertEqual(client.state, uhttp_client.STATE_IDLE)

        # Start request
        client.get('/test')

        # Should be sending or receiving (depending on how fast)
        self.assertIn(client.state, [
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

    def test_multiple_clients_select(self):
        """Test multiple clients in single select loop"""
        clients = [
            uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
            for _ in range(3)
        ]

        # Start all requests
        for i, client in enumerate(clients):
            client.get(f'/path{i}')

        # Collect responses
        responses = [None] * len(clients)
        for _ in range(100):
            read_socks = []
            write_socks = []
            for c in clients:
                read_socks.extend(c.read_sockets)
                write_socks.extend(c.write_sockets)

            if not read_socks and not write_socks:
                break

            r, w, _ = select.select(read_socks, write_socks, [], 0.1)

            for i, client in enumerate(clients):
                if responses[i] is None:
                    resp = client.process_events(r, w)
                    if resp:
                        responses[i] = resp

            if all(r is not None for r in responses):
                break

        for i, resp in enumerate(responses):
            self.assertIsNotNone(resp)
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.json()['path'], f'/path{i}')

        for client in clients:
            client.close()

    def test_process_events_idle_returns_none(self):
        """Test process_events returns None when idle"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        result = client.process_events([], [])
        self.assertIsNone(result)

        client.close()

    def test_process_events_timeout(self):
        """Test process_events raises HttpTimeoutError on timeout"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT, timeout=0.1)
        client.get('/slow')  # Server sleeps 0.2s

        # Wait until timeout expires
        time.sleep(0.2)

        # process_events should raise timeout
        with self.assertRaises(uhttp_client.HttpTimeoutError):
            client.process_events([], [])

        client.close()

    def test_per_request_timeout(self):
        """Test per-request timeout overrides client timeout"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT, timeout=10)

        # Use short per-request timeout
        client.get('/slow', timeout=0.1)

        # Wait until timeout expires
        time.sleep(0.2)

        # process_events should raise timeout
        with self.assertRaises(uhttp_client.HttpTimeoutError):
            client.process_events([], [])

        client.close()


if __name__ == '__main__':
    unittest.main()
