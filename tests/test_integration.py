#!/usr/bin/env python3
"""
Integration tests using local uhttp server (HTTP and HTTPS).

HTTPS tests require test_cert.pem and test_key.pem in tests/ directory.

Optional httpbin.org tests (require internet):
    UHTTP_HTTPBIN_INTEGRATION=1  python -m unittest tests.test_integration
"""
import base64
import json
import os
import ssl
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


PNG_DATA = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100


def _handle_request(client):
    """Handle request in test server"""
    if client.path == '/get' or client.path == '/':
        client.respond({
            'method': client.method,
            'headers': {k: v for k, v in client._headers.items()},
            'args': client.query or {},
            'url': client.path,
        })
    elif client.path in ('/post', '/put', '/patch', '/delete'):
        data = client.data
        data_str = None
        json_data = None
        if isinstance(data, dict):
            json_data = data
        elif isinstance(data, (bytes, bytearray)):
            data_str = data.decode()
            try:
                json_data = json.loads(data)
                data_str = ''
            except (json.JSONDecodeError, ValueError):
                pass
        client.respond({
            'method': client.method,
            'data': data_str or '',
            'json': json_data,
            'headers': {k: v for k, v in client._headers.items()},
            'args': client.query or {},
        })
    elif client.path == '/image/png':
        client.respond(
            PNG_DATA, headers={'content-type': 'image/png'})
    elif client.path.startswith('/basic-auth/'):
        parts = client.path.split('/')
        expected_user = parts[2]
        expected_pass = parts[3]
        auth_header = client._headers.get('authorization', '')
        if auth_header.startswith('Basic '):
            try:
                credentials = base64.b64decode(
                    auth_header[6:]).decode('utf-8')
                user, password = credentials.split(':', 1)
                if user == expected_user and password == expected_pass:
                    client.respond({
                        'authenticated': True,
                        'user': user,
                    })
                    return
            except Exception:
                pass
        client.respond(
            {'authenticated': False}, status=401,
            headers={'WWW-Authenticate': 'Basic realm="test"'})
    elif client.path.startswith('/status/'):
        code = int(client.path.split('/')[-1])
        client.respond('', status=code)
    elif client.path.startswith('/redirect/'):
        count = int(client.path.split('/')[-1])
        if count > 0:
            client.respond_redirect(f'/redirect/{count - 1}')
        else:
            client.respond({'redirected': True})
    elif client.path == '/headers':
        client.respond({
            'headers': {k: v for k, v in client._headers.items()},
        })
    else:
        client.respond({'status': 'ok', 'path': client.path})


def _run_server(cls):
    while cls.server:
        try:
            client = cls.server.wait(timeout=0.1)
            if client:
                _handle_request(client)
        except Exception:
            pass


class TestHTTPIntegration(unittest.TestCase):
    """Test HTTP requests with local uhttp server"""

    server = None
    server_thread = None
    PORT = 9910

    @classmethod
    def setUpClass(cls):
        cls.server = uhttp_server.HttpServer(port=cls.PORT)
        cls.server_thread = threading.Thread(
            target=_run_server, args=(cls,), daemon=True)
        cls.server_thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            for conn in list(cls.server._waiting_connections):
                conn.close()
            cls.server.close()
            cls.server = None

    def test_http_get(self):
        """Test basic HTTP GET request"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        try:
            response = client.get('/get').wait()
            self.assertEqual(response.status, 200)
            data = response.json()
            self.assertEqual(data['method'], 'GET')
            self.assertIn('host', data['headers'])
        finally:
            client.close()

    def test_http_post_json(self):
        """Test HTTP POST with JSON data"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        try:
            payload = {'name': 'test', 'value': 123}
            response = client.post('/post', json=payload).wait()
            self.assertEqual(response.status, 200)
            data = response.json()
            self.assertEqual(data['json'], payload)
        finally:
            client.close()

    def test_http_query_params(self):
        """Test HTTP GET with query parameters"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        try:
            response = client.get(
                '/get', query={'foo': 'bar', 'num': '42'}).wait()
            self.assertEqual(response.status, 200)
            data = response.json()
            self.assertEqual(data['args']['foo'], 'bar')
            self.assertEqual(data['args']['num'], '42')
        finally:
            client.close()

    def test_http_custom_headers(self):
        """Test HTTP GET with custom headers"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        try:
            response = client.get('/headers', headers={
                'X-Custom-Header': 'test-value'
            }).wait()
            self.assertEqual(response.status, 200)
            data = response.json()
            self.assertEqual(
                data['headers'].get('x-custom-header'), 'test-value')
        finally:
            client.close()

    def test_http_put(self):
        """Test PUT request"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        try:
            response = client.put(
                '/put', json={'key': 'value'}).wait()
            self.assertEqual(response.status, 200)
            data = response.json()
            self.assertEqual(data['json'], {'key': 'value'})
            self.assertEqual(data['method'], 'PUT')
        finally:
            client.close()

    def test_http_delete(self):
        """Test DELETE request"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        try:
            response = client.delete('/delete').wait()
            self.assertEqual(response.status, 200)
            data = response.json()
            self.assertEqual(data['method'], 'DELETE')
        finally:
            client.close()

    def test_http_patch(self):
        """Test PATCH request"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        try:
            response = client.patch(
                '/patch', json={'update': True}).wait()
            self.assertEqual(response.status, 200)
            data = response.json()
            self.assertEqual(data['json'], {'update': True})
            self.assertEqual(data['method'], 'PATCH')
        finally:
            client.close()

    def test_status_404(self):
        """Test 404 Not Found"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        try:
            response = client.get('/status/404').wait()
            self.assertEqual(response.status, 404)
        finally:
            client.close()

    def test_status_500(self):
        """Test 500 Internal Server Error"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        try:
            response = client.get('/status/500').wait()
            self.assertEqual(response.status, 500)
        finally:
            client.close()

    def test_redirect(self):
        """Test that redirects are NOT automatically followed"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        try:
            response = client.get('/redirect/1').wait()
            self.assertEqual(response.status, 302)
        finally:
            client.close()

    def test_binary_response(self):
        """Test binary response (PNG image)"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        try:
            response = client.get('/image/png').wait()
            self.assertEqual(response.status, 200)
            self.assertIn('image/png', response.content_type)
            self.assertTrue(response.data.startswith(b'\x89PNG'))
        finally:
            client.close()

    def test_basic_auth_success(self):
        """Test successful basic auth"""
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=self.PORT,
            auth=('testuser', 'testpass'))
        try:
            response = client.get(
                '/basic-auth/testuser/testpass').wait()
            self.assertEqual(response.status, 200)
            data = response.json()
            self.assertTrue(data['authenticated'])
            self.assertEqual(data['user'], 'testuser')
        finally:
            client.close()

    def test_basic_auth_failure(self):
        """Test failed basic auth"""
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=self.PORT,
            auth=('wrong', 'credentials'))
        try:
            response = client.get(
                '/basic-auth/testuser/testpass').wait()
            self.assertEqual(response.status, 401)
        finally:
            client.close()

    def test_keep_alive(self):
        """Test multiple requests on same connection"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        try:
            response1 = client.get(
                '/get', query={'req': '1'}).wait()
            self.assertEqual(response1.status, 200)

            response2 = client.get(
                '/get', query={'req': '2'}).wait()
            self.assertEqual(response2.status, 200)

            response3 = client.get(
                '/get', query={'req': '3'}).wait()
            self.assertEqual(response3.status, 200)
        finally:
            client.close()


@unittest.skipIf(not SSL_AVAILABLE, "Test SSL certificates not found")
class TestHTTPSIntegration(unittest.TestCase):
    """Test HTTPS requests with local uhttp server"""

    server = None
    server_thread = None
    PORT = 9911

    @classmethod
    def setUpClass(cls):
        ctx = _create_server_ssl_context()
        cls.server = uhttp_server.HttpServer(
            port=cls.PORT, ssl_context=ctx)
        cls.server_thread = threading.Thread(
            target=_run_server, args=(cls,), daemon=True)
        cls.server_thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            for conn in list(cls.server._waiting_connections):
                conn.close()
            cls.server.close()
            cls.server = None

    def _client(self, **kwargs):
        return uhttp_client.HttpClient(
            '127.0.0.1', port=self.PORT,
            ssl_context=_create_client_ssl_context(), **kwargs)

    def test_https_get(self):
        """Test basic HTTPS GET request"""
        client = self._client()
        try:
            response = client.get('/get').wait()
            self.assertEqual(response.status, 200)
            data = response.json()
            self.assertEqual(data['method'], 'GET')
        finally:
            client.close()

    def test_https_post_json(self):
        """Test HTTPS POST with JSON data"""
        client = self._client()
        try:
            payload = {'secure': True, 'data': 'test'}
            response = client.post('/post', json=payload).wait()
            self.assertEqual(response.status, 200)
            data = response.json()
            self.assertEqual(data['json'], payload)
        finally:
            client.close()

    def test_https_keep_alive(self):
        """Test HTTPS with multiple requests on same connection"""
        client = self._client()
        try:
            response1 = client.get(
                '/get', query={'req': '1'}).wait()
            self.assertEqual(response1.status, 200)

            response2 = client.get(
                '/get', query={'req': '2'}).wait()
            self.assertEqual(response2.status, 200)

            response3 = client.get(
                '/get', query={'req': '3'}).wait()
            self.assertEqual(response3.status, 200)
        finally:
            client.close()

    def test_https_binary_response(self):
        """Test HTTPS binary response (PNG image)"""
        client = self._client()
        try:
            response = client.get('/image/png').wait()
            self.assertEqual(response.status, 200)
            self.assertIn('image/png', response.content_type)
            self.assertTrue(response.data.startswith(b'\x89PNG'))
        finally:
            client.close()

    def test_https_basic_auth(self):
        """Test basic auth over HTTPS"""
        client = self._client(auth=('testuser', 'testpass'))
        try:
            response = client.get(
                '/basic-auth/testuser/testpass').wait()
            self.assertEqual(response.status, 200)
            data = response.json()
            self.assertTrue(data['authenticated'])
        finally:
            client.close()

    def test_https_status_codes(self):
        """Test status codes over HTTPS"""
        client = self._client()
        try:
            response = client.get('/status/404').wait()
            self.assertEqual(response.status, 404)

            response = client.get('/status/500').wait()
            self.assertEqual(response.status, 500)
        finally:
            client.close()

    def test_https_put(self):
        """Test PUT over HTTPS"""
        client = self._client()
        try:
            response = client.put(
                '/put', json={'key': 'value'}).wait()
            self.assertEqual(response.status, 200)
            data = response.json()
            self.assertEqual(data['json'], {'key': 'value'})
        finally:
            client.close()

    def test_https_redirect(self):
        """Test redirect over HTTPS"""
        client = self._client()
        try:
            response = client.get('/redirect/1').wait()
            self.assertEqual(response.status, 302)
        finally:
            client.close()


# --- Optional httpbin.org tests (opt-in) ---

HTTPBIN_INTEGRATION = os.environ.get(
    'UHTTP_HTTPBIN_INTEGRATION', '').lower() in ('1', 'true', 'yes')
HTTPBIN_SKIP_REASON = (
    "httpbin.org tests disabled (set UHTTP_HTTPBIN_INTEGRATION=1)")


@unittest.skipIf(not HTTPBIN_INTEGRATION, HTTPBIN_SKIP_REASON)
class TestHttpbinHTTP(unittest.TestCase):
    """Test HTTP requests to httpbin.org (optional, requires internet)"""

    def test_http_get(self):
        """Test basic HTTP GET to httpbin.org"""
        client = uhttp_client.HttpClient('http://httpbin.org')
        try:
            response = client.get('/get').wait()
            self.assertEqual(response.status, 200)
            data = response.json()
            self.assertIn('headers', data)
        finally:
            client.close()

    def test_http_post_json(self):
        """Test HTTP POST JSON to httpbin.org"""
        client = uhttp_client.HttpClient('http://httpbin.org')
        try:
            payload = {'name': 'test', 'value': 123}
            response = client.post('/post', json=payload).wait()
            self.assertEqual(response.status, 200)
            data = response.json()
            self.assertEqual(data['json'], payload)
        finally:
            client.close()


@unittest.skipIf(not HTTPBIN_INTEGRATION, HTTPBIN_SKIP_REASON)
class TestHttpbinHTTPS(unittest.TestCase):
    """Test HTTPS requests to httpbin.org (optional, requires internet)"""

    def test_https_get(self):
        """Test basic HTTPS GET to httpbin.org"""
        client = uhttp_client.HttpClient('https://httpbin.org')
        try:
            response = client.get('/get').wait()
            self.assertEqual(response.status, 200)
            data = response.json()
            self.assertIn('headers', data)
        finally:
            client.close()

    def test_https_post_json(self):
        """Test HTTPS POST JSON to httpbin.org"""
        client = uhttp_client.HttpClient('https://httpbin.org')
        try:
            payload = {'secure': True, 'data': 'test'}
            response = client.post('/post', json=payload).wait()
            self.assertEqual(response.status, 200)
            data = response.json()
            self.assertEqual(data['json'], payload)
        finally:
            client.close()

    def test_https_binary_response(self):
        """Test HTTPS binary response from httpbin.org"""
        client = uhttp_client.HttpClient('https://httpbin.org')
        try:
            response = client.get('/image/png').wait()
            self.assertEqual(response.status, 200)
            self.assertIn('image/png', response.content_type)
            self.assertTrue(response.data.startswith(b'\x89PNG'))
        finally:
            client.close()

    def test_https_expect_100_continue(self):
        """Test Expect: 100-continue with httpbin.org"""
        client = uhttp_client.HttpClient('https://httpbin.org')
        try:
            body = b'test data for 100 continue'
            response = client.post(
                '/post', data=body, expect_continue=True).wait()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.json()['data'], body.decode())
        finally:
            client.close()


if __name__ == '__main__':
    unittest.main()
