#!/usr/bin/env python3
"""
Tests for Expect: 100-continue support in client
"""
import unittest
import socket
import threading
import time
from uhttp import client as uhttp_client
from uhttp import server as uhttp_server
from uhttp.server import EVENT_HEADERS, EVENT_COMPLETE


class Test100ContinueBasic(unittest.TestCase):
    """Test 100-continue with real server"""

    server = None
    server_thread = None
    PORT = 9970
    received_data = []

    @classmethod
    def setUpClass(cls):
        """Start server in non-event mode (auto 100 Continue)"""
        cls.server = uhttp_server.HttpServer(port=cls.PORT)

        def run_server():
            try:
                while True:
                    server = cls.server
                    if server is None:
                        break
                    client = server.wait(timeout=0.5)
                    if client:
                        cls.received_data.append({
                            'path': client.path,
                            'data': client.data,
                            'headers': dict(client._headers) if client._headers else {},
                        })
                        client.respond({
                            'status': 'ok',
                            'received': len(client.data) if client.data else 0
                        })
            except Exception as e:
                print(f"Server error: {e}")

        cls.server_thread = threading.Thread(target=run_server, daemon=True)
        cls.server_thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.close()
            cls.server = None

    def setUp(self):
        Test100ContinueBasic.received_data = []

    def test_post_with_expect_continue(self):
        """Test POST with Expect: 100-continue header"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        body = b'x' * 1000
        response = client.post('/upload', data=body, expect_continue=True).wait()

        self.assertIsNotNone(response)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()['received'], len(body))

        # Verify server received the data
        self.assertEqual(len(self.received_data), 1)
        self.assertEqual(self.received_data[0]['data'], body)

        client.close()

    def test_put_with_expect_continue(self):
        """Test PUT with Expect: 100-continue header"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        body = b'updated content'
        response = client.put('/resource', data=body, expect_continue=True).wait()

        self.assertIsNotNone(response)
        self.assertEqual(response.status, 200)
        client.close()


class Test100ContinueEventMode(unittest.TestCase):
    """Test 100-continue with server in event mode"""

    server = None
    server_thread = None
    PORT = 9971
    received_data = []

    @classmethod
    def setUpClass(cls):
        """Start server in event mode"""
        cls.server = uhttp_server.HttpServer(port=cls.PORT, event_mode=True)

        def run_server():
            try:
                while True:
                    server = cls.server
                    if server is None:
                        break
                    client = server.wait(timeout=0.5)
                    if client:
                        if client.event == EVENT_HEADERS:
                            # Accept body - this sends 100 Continue
                            client.accept_body()
                        elif client.event == EVENT_COMPLETE:
                            data = client.read_buffer()
                            cls.received_data.append({
                                'path': client.path,
                                'data': data,
                            })
                            client.respond({
                                'status': 'ok',
                                'received': len(data) if data else 0
                            })
                        else:
                            # EVENT_REQUEST - small request
                            client.respond({'status': 'ok'})
            except Exception as e:
                print(f"Server error: {e}")

        cls.server_thread = threading.Thread(target=run_server, daemon=True)
        cls.server_thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.close()
            cls.server = None

    def setUp(self):
        Test100ContinueEventMode.received_data = []

    def test_expect_continue_with_event_mode_server(self):
        """Test client with event mode server that controls 100 Continue"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        body = b'event mode test data'
        response = client.post('/upload', data=body, expect_continue=True).wait()

        self.assertIsNotNone(response)
        self.assertEqual(response.status, 200)
        client.close()


class Test100ContinueReject(unittest.TestCase):
    """Test server rejecting request without sending 100 Continue"""

    server = None
    server_thread = None
    PORT = 9972

    @classmethod
    def setUpClass(cls):
        """Start server in event mode that rejects large uploads"""
        cls.server = uhttp_server.HttpServer(port=cls.PORT, event_mode=True)

        def run_server():
            try:
                while True:
                    server = cls.server
                    if server is None:
                        break
                    client = server.wait(timeout=0.5)
                    if client:
                        if client.event == EVENT_HEADERS:
                            # Reject without sending 100 Continue
                            client.respond(
                                {'error': 'too large'},
                                status=413,
                                headers={'connection': 'close'})
                        else:
                            client.respond({'status': 'ok'})
            except Exception as e:
                print(f"Server error: {e}")

        cls.server_thread = threading.Thread(target=run_server, daemon=True)
        cls.server_thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.close()
            cls.server = None

    def test_server_rejects_without_100_continue(self):
        """Test that client handles rejection without 100 Continue"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        body = b'this should not be sent'
        response = client.post('/upload', data=body, expect_continue=True).wait()

        # Client should get 413 response
        self.assertIsNotNone(response)
        self.assertEqual(response.status, 413)

        client.close()


class Test100ContinueTimeout(unittest.TestCase):
    """Test timeout when server doesn't respond to Expect header"""

    def test_timeout_waiting_for_100_continue(self):
        """Test client times out if server doesn't send 100 Continue"""
        # Create a server socket that accepts but never responds
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(('127.0.0.1', 9973))
        server_sock.listen(1)

        def accept_and_ignore():
            conn, _ = server_sock.accept()
            # Read headers but never respond
            conn.recv(4096)
            time.sleep(5)  # Hold connection
            conn.close()

        thread = threading.Thread(target=accept_and_ignore, daemon=True)
        thread.start()

        try:
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=9973, timeout=1)

            body = b'test data'
            # Should timeout waiting for 100 Continue
            with self.assertRaises(uhttp_client.HttpTimeoutError):
                client.post('/upload', data=body, expect_continue=True).wait()

            client.close()
        finally:
            server_sock.close()


if __name__ == '__main__':
    unittest.main()
