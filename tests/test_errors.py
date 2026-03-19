#!/usr/bin/env python3
"""
HTTP client error handling tests
"""
import unittest
import threading
import time
from uhttp import server as uhttp_server
from uhttp import client as uhttp_client


class TestClientConnectionErrors(unittest.TestCase):
    """Test connection error handling"""

    def test_connection_refused(self):
        """Test connection to closed port raises error"""
        client = uhttp_client.HttpClient('127.0.0.1', port=59999)

        with self.assertRaises(uhttp_client.HttpConnectionError):
            client.get('/test').wait()

        client.close()

    def test_invalid_host(self):
        """Test connection to invalid host raises error"""
        client = uhttp_client.HttpClient('invalid.host.that.does.not.exist.example')

        with self.assertRaises(uhttp_client.HttpConnectionError):
            client.get('/test').wait()

        client.close()


class TestClientTimeoutErrors(unittest.TestCase):
    """Test timeout handling"""

    server = None
    server_thread = None
    PORT = 9904

    @classmethod
    def setUpClass(cls):
        cls.server = uhttp_server.HttpServer(port=cls.PORT)

        def run_server():
            try:
                while cls.server:
                    client = cls.server.wait(timeout=0.1)
                    if client:
                        if client.path == '/slow':
                            time.sleep(2)
                        client.respond({'status': 'ok'})
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

    def test_wait_timeout_raises_timeout_error(self):
        """Test wait timeout raises HttpTimeoutError"""
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=self.PORT,
            timeout=10  # long request timeout
        )
        try:
            with self.assertRaises(uhttp_client.HttpTimeoutError):
                client.get('/slow').wait(timeout=0.3)
        finally:
            client.close()

    def test_request_timeout_raises(self):
        """Test request timeout raises HttpTimeoutError"""
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=self.PORT,
            timeout=0.3  # short request timeout
        )
        try:
            with self.assertRaises(uhttp_client.HttpTimeoutError):
                client.get('/slow').wait()
        finally:
            client.close()

    def test_normal_request_succeeds(self):
        """Test normal request with timeout succeeds"""
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=self.PORT,
            timeout=5
        )
        response = client.get('/fast').wait(timeout=5)

        self.assertIsNotNone(response)
        self.assertEqual(response.status, 200)
        client.close()


class TestClientRequestErrors(unittest.TestCase):
    """Test request-related errors"""

    server = None
    server_thread = None
    PORT = 9907

    @classmethod
    def setUpClass(cls):
        cls.server = uhttp_server.HttpServer(port=cls.PORT)

        def run_server():
            try:
                while cls.server:
                    client = cls.server.wait(timeout=0.1)
                    if client:
                        client.respond({'status': 'ok'})
            except Exception:
                pass

        cls.server_thread = threading.Thread(target=run_server, daemon=True)
        cls.server_thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            time.sleep(0.1)  # Let server finish processing
            for conn in list(cls.server._waiting_connections):
                conn.close()
            cls.server.close()
            cls.server = None

    def test_request_while_busy(self):
        """Test starting new request while one is in progress"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        # Start first request
        client.get('/test')

        # Try to start another - should raise
        with self.assertRaises(uhttp_client.HttpClientError):
            client.get('/another')

        # Complete first request
        client.wait()
        client.close()

    def test_wait_without_request(self):
        """Test wait without active request raises error"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        with self.assertRaises(uhttp_client.HttpClientError):
            client.wait()

        client.close()

    def test_unsupported_data_type(self):
        """Test unsupported data type raises error"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        with self.assertRaises(uhttp_client.HttpClientError):
            client.post('/test', data=12345)  # int not supported

        client.close()


class TestClientResponseErrors(unittest.TestCase):
    """Test response parsing errors"""

    def test_invalid_json_response(self):
        """Test invalid JSON in response.json() raises error"""
        response = uhttp_client.HttpResponse(
            200, 'OK', {}, b'not valid json'
        )

        with self.assertRaises(uhttp_client.HttpResponseError):
            response.json()


if __name__ == '__main__':
    unittest.main()
