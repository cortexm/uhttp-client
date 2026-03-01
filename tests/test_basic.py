#!/usr/bin/env python3
"""
Basic HTTP client tests - GET, POST, PUT, DELETE, etc.
"""
import unittest
import threading
import time
from uhttp import server as uhttp_server
from uhttp import client as uhttp_client


class TestClientBasicRequests(unittest.TestCase):
    """Test basic HTTP methods and data handling"""

    server = None
    server_thread = None
    last_request = None
    PORT = 9900

    @classmethod
    def setUpClass(cls):
        cls.server = uhttp_server.HttpServer(port=cls.PORT)

        def run_server():
            try:
                while cls.server:
                    client = cls.server.wait(timeout=0.1)
                    if client:
                        cls.last_request = {
                            'method': client.method,
                            'path': client.path,
                            'query': client.query,
                            'data': client.data,
                            'headers': dict(client._headers) if client._headers else {},
                        }
                        if client.path == '/json':
                            client.respond({'received': client.data, 'method': client.method})
                        elif client.path == '/echo':
                            client.respond(client.data)
                        elif client.path == '/headers':
                            client.respond({'headers': cls.last_request['headers']})
                        elif client.path == '/binary':
                            client.respond(b'\x00\x01\x02\xff\xfe\xfd')
                        elif client.path == '/large':
                            client.respond(b'x' * 10000)
                        elif client.path == '/status':
                            status = int(client.query.get('code', 200))
                            client.respond({'status': status}, status=status)
                        else:
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

    def setUp(self):
        TestClientBasicRequests.last_request = None

    def test_simple_get(self):
        """Test simple GET request"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        response = client.get('/test').wait()

        self.assertIsNotNone(response)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()['path'], '/test')
        client.close()

    def test_get_with_query(self):
        """Test GET with query parameters"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        response = client.get('/test', query={'a': '1', 'b': '2'}).wait()

        self.assertIsNotNone(response)
        self.assertEqual(self.last_request['query'], {'a': '1', 'b': '2'})
        client.close()

    def test_post_json(self):
        """Test POST with JSON body"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        response = client.post('/json', json={'key': 'value', 'num': 42}).wait()

        self.assertIsNotNone(response)
        data = response.json()
        self.assertEqual(data['received'], {'key': 'value', 'num': 42})
        self.assertEqual(data['method'], 'POST')
        client.close()

    def test_put_json(self):
        """Test PUT request"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        response = client.put('/json', json={'update': True}).wait()

        self.assertIsNotNone(response)
        self.assertEqual(response.json()['method'], 'PUT')
        client.close()

    def test_delete(self):
        """Test DELETE request"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        response = client.delete('/test').wait()

        self.assertIsNotNone(response)
        self.assertEqual(self.last_request['method'], 'DELETE')
        client.close()

    def test_head(self):
        """Test HEAD request"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        response = client.head('/test').wait()

        self.assertIsNotNone(response)
        self.assertEqual(self.last_request['method'], 'HEAD')
        client.close()

    def test_patch(self):
        """Test PATCH request"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        response = client.patch('/json', json={'patch': True}).wait()

        self.assertIsNotNone(response)
        self.assertEqual(self.last_request['method'], 'PATCH')
        client.close()

    def test_binary_response(self):
        """Test receiving binary data"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        response = client.get('/binary').wait()

        self.assertEqual(response.data, b'\x00\x01\x02\xff\xfe\xfd')
        client.close()

    def test_large_response(self):
        """Test receiving large response"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        response = client.get('/large').wait()

        self.assertEqual(len(response.data), 10000)
        client.close()

    def test_post_binary_data(self):
        """Test sending binary data"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        binary_data = b'\x00\x01\x02\xff'
        response = client.post('/echo', data=binary_data).wait()

        self.assertEqual(response.data, binary_data)
        client.close()

    def test_post_string_data(self):
        """Test sending string data"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        response = client.post('/echo', data='hello world').wait()

        self.assertEqual(response.data, b'hello world')
        client.close()

    def test_custom_headers(self):
        """Test custom headers are sent"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        response = client.get('/headers', headers={
            'X-Custom': 'custom-value',
            'X-Another': 'another-value'
        }).wait()

        headers = response.json()['headers']
        self.assertEqual(headers.get('x-custom'), 'custom-value')
        self.assertEqual(headers.get('x-another'), 'another-value')
        client.close()

    def test_user_agent_header(self):
        """Test User-Agent header is set"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        response = client.get('/headers').wait()

        headers = response.json()['headers']
        self.assertIn('uhttp-client', headers['user-agent'])
        client.close()

    def test_status_404(self):
        """Test 404 response"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        response = client.get('/status', query={'code': '404'}).wait()

        self.assertEqual(response.status, 404)
        client.close()

    def test_status_500(self):
        """Test 500 response"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        response = client.get('/status', query={'code': '500'}).wait()

        self.assertEqual(response.status, 500)
        client.close()

    def test_response_properties(self):
        """Test response properties"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        response = client.get('/json').wait()

        self.assertEqual(response.status, 200)
        self.assertEqual(response.status_message, 'OK')
        self.assertIn('content-type', response.headers)
        self.assertIsNotNone(response.content_length)
        client.close()

    def test_url_init(self):
        """Test client initialization with URL"""
        client = uhttp_client.HttpClient(f'http://127.0.0.1:{self.PORT}/api')
        response = client.get('/test').wait()

        self.assertEqual(self.last_request['path'], '/api/test')
        client.close()

    def test_base_path(self):
        """Test base_path is prepended to requests"""
        client = uhttp_client.HttpClient(f'http://127.0.0.1:{self.PORT}/v1/api')

        client.get('/users').wait()
        self.assertEqual(self.last_request['path'], '/v1/api/users')

        client.get('/items').wait()
        self.assertEqual(self.last_request['path'], '/v1/api/items')

        client.close()

    def test_context_manager(self):
        """Test client as context manager"""
        with uhttp_client.HttpClient('127.0.0.1', port=self.PORT) as client:
            response = client.get('/test').wait()
            self.assertEqual(response.status, 200)

    def test_fluent_api(self):
        """Test fluent API (method chaining)"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        result = client.get('/test')
        self.assertIs(result, client)
        response = result.wait()
        self.assertIsNotNone(response)
        client.close()


if __name__ == '__main__':
    unittest.main()
