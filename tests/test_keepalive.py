#!/usr/bin/env python3
"""
HTTP client keep-alive connection tests
"""
import unittest
import threading
import time
from uhttp import server as uhttp_server
from uhttp import client as uhttp_client


class TestClientKeepalive(unittest.TestCase):
    """Test keep-alive connection handling"""

    server = None
    server_thread = None
    PORT = 9905

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

    def test_connection_reuse(self):
        """Test connection is reused for multiple requests"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        for i in range(5):
            response = client.get('/test', query={'n': str(i)}).wait()
            self.assertIsNotNone(response)
            self.assertEqual(response.status, 200)
            self.assertTrue(client.is_connected)

        client.close()
        self.assertFalse(client.is_connected)

    def test_is_connected_property(self):
        """Test is_connected property"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        # Before any request
        self.assertFalse(client.is_connected)

        # After request
        client.get('/test').wait()
        self.assertTrue(client.is_connected)

        # After close
        client.close()
        self.assertFalse(client.is_connected)

    def test_multiple_requests_same_client(self):
        """Test multiple sequential requests on same client"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        response1 = client.get('/path1').wait()
        self.assertEqual(response1.json()['path'], '/path1')

        response2 = client.get('/path2').wait()
        self.assertEqual(response2.json()['path'], '/path2')

        response3 = client.post('/path3', json={'data': 'test'}).wait()
        self.assertEqual(response3.json()['path'], '/path3')

        client.close()

    def test_close_and_reconnect(self):
        """Test closing and making new request reconnects"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        # First request
        response1 = client.get('/test1').wait()
        self.assertEqual(response1.status, 200)
        self.assertTrue(client.is_connected)

        # Close connection
        client.close()
        self.assertFalse(client.is_connected)

        # New request should reconnect
        response2 = client.get('/test2').wait()
        self.assertEqual(response2.status, 200)
        self.assertTrue(client.is_connected)

        client.close()


if __name__ == '__main__':
    unittest.main()
