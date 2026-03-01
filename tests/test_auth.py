#!/usr/bin/env python3
"""
HTTP client authentication tests - Basic and Digest auth
"""
import unittest
import threading
import time
import hashlib
import base64
from uhttp import server as uhttp_server
from uhttp import client as uhttp_client


class TestClientBasicAuth(unittest.TestCase):
    """Test HTTP Basic Authentication"""

    server = None
    server_thread = None
    PORT = 9901

    @classmethod
    def setUpClass(cls):
        cls.server = uhttp_server.HttpServer(port=cls.PORT)

        def run_server():
            try:
                while cls.server:
                    client = cls.server.wait(timeout=0.1)
                    if client:
                        auth_header = client._headers.get('authorization', '')
                        if auth_header.startswith('Basic '):
                            try:
                                credentials = base64.b64decode(auth_header[6:]).decode('utf-8')
                                user, password = credentials.split(':', 1)
                                if user == 'testuser' and password == 'testpass':
                                    client.respond({'auth': 'success', 'user': user})
                                    continue
                            except Exception:
                                pass
                        client.respond({'error': 'Unauthorized'}, status=401)
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

    def test_basic_auth_success(self):
        """Test successful basic auth"""
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=self.PORT,
            auth=('testuser', 'testpass')
        )
        response = client.get('/protected').wait()

        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()['auth'], 'success')
        client.close()

    def test_basic_auth_url(self):
        """Test basic auth from URL"""
        client = uhttp_client.HttpClient(
            f'http://testuser:testpass@127.0.0.1:{self.PORT}'
        )
        response = client.get('/protected').wait()

        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()['user'], 'testuser')
        client.close()

    def test_basic_auth_failure(self):
        """Test failed basic auth"""
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=self.PORT,
            auth=('wrong', 'credentials')
        )
        response = client.get('/protected').wait()

        self.assertEqual(response.status, 401)
        client.close()

    def test_no_auth(self):
        """Test request without auth"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        response = client.get('/protected').wait()

        self.assertEqual(response.status, 401)
        client.close()

    def test_per_request_auth(self):
        """Test auth parameter in request overrides client auth"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        # Without auth - should fail
        response = client.get('/protected').wait()
        self.assertEqual(response.status, 401)

        # With per-request auth - should succeed
        response = client.get('/protected', auth=('testuser', 'testpass')).wait()
        self.assertEqual(response.status, 200)

        client.close()

    def test_auth_property_setter(self):
        """Test setting auth after client creation"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        # No auth initially
        response = client.get('/protected').wait()
        self.assertEqual(response.status, 401)

        # Set auth
        client.auth = ('testuser', 'testpass')
        response = client.get('/protected').wait()
        self.assertEqual(response.status, 200)

        client.close()


class TestClientDigestAuth(unittest.TestCase):
    """Test HTTP Digest Authentication"""

    server = None
    server_thread = None
    PORT = 9902
    REALM = 'test-realm'
    NONCE = 'test-nonce-12345'

    @classmethod
    def setUpClass(cls):
        cls.server = uhttp_server.HttpServer(port=cls.PORT)

        def run_server():
            try:
                while cls.server:
                    client = cls.server.wait(timeout=0.1)
                    if client:
                        auth_header = client._headers.get('authorization', '')
                        if auth_header.lower().startswith('digest '):
                            params = uhttp_client._parse_www_authenticate(auth_header)
                            username = params.get('username', '')
                            uri = params.get('uri', '')
                            response_hash = params.get('response', '')

                            if username == 'digestuser':
                                ha1 = hashlib.md5(
                                    f"digestuser:{cls.REALM}:digestpass".encode()
                                ).hexdigest()
                                ha2 = hashlib.md5(
                                    f"{client.method}:{uri}".encode()
                                ).hexdigest()

                                qop = params.get('qop')
                                if qop:
                                    nc = params.get('nc', '')
                                    cnonce = params.get('cnonce', '')
                                    expected = hashlib.md5(
                                        f"{ha1}:{cls.NONCE}:{nc}:{cnonce}:{qop}:{ha2}".encode()
                                    ).hexdigest()
                                else:
                                    expected = hashlib.md5(
                                        f"{ha1}:{cls.NONCE}:{ha2}".encode()
                                    ).hexdigest()

                                if response_hash == expected:
                                    client.respond({'auth': 'digest_success'})
                                    continue

                        # Send 401 with digest challenge
                        client.respond(
                            {'error': 'Unauthorized'},
                            status=401,
                            headers={
                                'WWW-Authenticate': f'Digest realm="{cls.REALM}", nonce="{cls.NONCE}", qop="auth"'
                            }
                        )
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

    def test_digest_auth_success(self):
        """Test successful digest auth (automatic retry after 401)"""
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=self.PORT,
            auth=('digestuser', 'digestpass')
        )
        response = client.get('/protected').wait()

        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()['auth'], 'digest_success')
        client.close()

    def test_digest_auth_wrong_password(self):
        """Test digest auth with wrong password"""
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=self.PORT,
            auth=('digestuser', 'wrongpass')
        )
        response = client.get('/protected').wait()

        self.assertEqual(response.status, 401)
        client.close()

    def test_digest_auth_per_request(self):
        """Test digest auth in request parameter"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        response = client.get(
            '/protected',
            auth=('digestuser', 'digestpass')
        ).wait()

        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()['auth'], 'digest_success')
        client.close()

    def test_digest_multiple_requests(self):
        """Test digest auth works for multiple requests"""
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=self.PORT,
            auth=('digestuser', 'digestpass')
        )

        # First request triggers 401 + retry
        response1 = client.get('/path1').wait()
        self.assertEqual(response1.status, 200)

        # Second request should use cached digest params
        response2 = client.get('/path2').wait()
        self.assertEqual(response2.status, 200)

        client.close()


if __name__ == '__main__':
    unittest.main()
