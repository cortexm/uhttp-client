#!/usr/bin/env python3
"""
Unit tests for uhttp_client - no network required
"""
import unittest
from uhttp import client as uhttp_client


class TestParseUrl(unittest.TestCase):
    """Tests for parse_url function"""

    def test_http_simple(self):
        """Test simple HTTP URL"""
        host, port, path, ssl, auth = uhttp_client.parse_url('http://example.com')
        self.assertEqual(host, 'example.com')
        self.assertEqual(port, 80)
        self.assertEqual(path, '')
        self.assertFalse(ssl)
        self.assertIsNone(auth)

    def test_https_simple(self):
        """Test simple HTTPS URL"""
        host, port, path, ssl, auth = uhttp_client.parse_url('https://example.com')
        self.assertEqual(host, 'example.com')
        self.assertEqual(port, 443)
        self.assertEqual(path, '')
        self.assertTrue(ssl)
        self.assertIsNone(auth)

    def test_http_with_path(self):
        """Test HTTP URL with path"""
        host, port, path, ssl, auth = uhttp_client.parse_url('http://example.com/api/v1')
        self.assertEqual(host, 'example.com')
        self.assertEqual(port, 80)
        self.assertEqual(path, '/api/v1')
        self.assertFalse(ssl)

    def test_https_with_path(self):
        """Test HTTPS URL with path"""
        host, port, path, ssl, auth = uhttp_client.parse_url('https://api.example.com/v1/users')
        self.assertEqual(host, 'api.example.com')
        self.assertEqual(port, 443)
        self.assertEqual(path, '/v1/users')
        self.assertTrue(ssl)

    def test_custom_port(self):
        """Test URL with custom port"""
        host, port, path, ssl, auth = uhttp_client.parse_url('http://localhost:8080/test')
        self.assertEqual(host, 'localhost')
        self.assertEqual(port, 8080)
        self.assertEqual(path, '/test')

    def test_https_custom_port(self):
        """Test HTTPS with custom port"""
        host, port, path, ssl, auth = uhttp_client.parse_url('https://localhost:8443')
        self.assertEqual(host, 'localhost')
        self.assertEqual(port, 8443)
        self.assertTrue(ssl)

    def test_basic_auth_in_url(self):
        """Test URL with basic auth credentials"""
        host, port, path, ssl, auth = uhttp_client.parse_url('http://user:pass@example.com/api')
        self.assertEqual(host, 'example.com')
        self.assertEqual(port, 80)
        self.assertEqual(path, '/api')
        self.assertEqual(auth, ('user', 'pass'))

    def test_auth_without_password(self):
        """Test URL with username only"""
        host, port, path, ssl, auth = uhttp_client.parse_url('http://admin@example.com')
        self.assertEqual(host, 'example.com')
        self.assertEqual(auth, ('admin', ''))

    def test_auth_with_special_chars(self):
        """Test URL with special characters in password"""
        host, port, path, ssl, auth = uhttp_client.parse_url('http://user:p@ss:word@example.com')
        self.assertEqual(host, 'example.com')
        self.assertEqual(auth, ('user', 'p@ss:word'))

    def test_at_in_path_not_auth(self):
        """Test @ in path is not interpreted as auth"""
        host, port, path, ssl, auth = uhttp_client.parse_url('http://example.com/@username/profile')
        self.assertEqual(host, 'example.com')
        self.assertEqual(path, '/@username/profile')
        self.assertIsNone(auth)

    def test_at_in_path_with_port(self):
        """Test @ in path with custom port"""
        host, port, path, ssl, auth = uhttp_client.parse_url('http://localhost:8080/@user')
        self.assertEqual(host, 'localhost')
        self.assertEqual(port, 8080)
        self.assertEqual(path, '/@user')
        self.assertIsNone(auth)

    def test_no_protocol(self):
        """Test URL without protocol (defaults to http)"""
        host, port, path, ssl, auth = uhttp_client.parse_url('example.com')
        self.assertEqual(host, 'example.com')
        self.assertEqual(port, 80)
        self.assertFalse(ssl)

    def test_no_protocol_with_path(self):
        """Test hostname with path, no protocol"""
        host, port, path, ssl, auth = uhttp_client.parse_url('example.com/api')
        self.assertEqual(host, 'example.com')
        self.assertEqual(path, '/api')


class TestEncodeQuery(unittest.TestCase):
    """Tests for _encode_query function"""

    def test_empty_query(self):
        """Test empty query returns empty string"""
        self.assertEqual(uhttp_client._encode_query(None), '')
        self.assertEqual(uhttp_client._encode_query({}), '')

    def test_simple_query(self):
        """Test simple query parameters"""
        result = uhttp_client._encode_query({'a': '1', 'b': '2'})
        self.assertIn('a=1', result)
        self.assertIn('b=2', result)
        self.assertTrue(result.startswith('?'))

    def test_query_with_list(self):
        """Test query with list values"""
        result = uhttp_client._encode_query({'tags': ['a', 'b', 'c']})
        self.assertEqual(result.count('tags='), 3)

    def test_query_with_none_value(self):
        """Test query with None value (flag)"""
        result = uhttp_client._encode_query({'flag': None})
        self.assertEqual(result, '?flag')


class TestEncodeRequestData(unittest.TestCase):
    """Tests for _encode_request_data function"""

    def test_none_data(self):
        """Test None data returns None"""
        headers = {}
        result = uhttp_client._encode_request_data(None, headers)
        self.assertIsNone(result)

    def test_dict_to_json(self):
        """Test dict is encoded as JSON"""
        headers = {}
        result = uhttp_client._encode_request_data({'key': 'value'}, headers)
        self.assertEqual(result, b'{"key": "value"}')
        self.assertEqual(headers['content-type'], 'application/json')

    def test_list_to_json(self):
        """Test list is encoded as JSON"""
        headers = {}
        result = uhttp_client._encode_request_data([1, 2, 3], headers)
        self.assertEqual(result, b'[1, 2, 3]')
        self.assertEqual(headers['content-type'], 'application/json')

    def test_string_to_bytes(self):
        """Test string is encoded to UTF-8 bytes"""
        headers = {}
        result = uhttp_client._encode_request_data('hello', headers)
        self.assertEqual(result, b'hello')

    def test_bytes_passthrough(self):
        """Test bytes pass through unchanged"""
        headers = {}
        data = b'\x00\x01\x02'
        result = uhttp_client._encode_request_data(data, headers)
        self.assertEqual(result, data)
        self.assertEqual(headers['content-type'], 'application/octet-stream')

    def test_existing_content_type_preserved(self):
        """Test existing content-type is not overwritten"""
        headers = {'content-type': 'text/plain'}
        uhttp_client._encode_request_data(b'data', headers)
        self.assertEqual(headers['content-type'], 'text/plain')


class TestParseWwwAuthenticate(unittest.TestCase):
    """Tests for _parse_www_authenticate function"""

    def test_digest_basic(self):
        """Test basic Digest header parsing"""
        header = 'Digest realm="test", nonce="abc123"'
        result = uhttp_client._parse_www_authenticate(header)
        self.assertEqual(result['realm'], 'test')
        self.assertEqual(result['nonce'], 'abc123')

    def test_digest_with_qop(self):
        """Test Digest header with qop"""
        header = 'Digest realm="api", nonce="xyz", qop="auth"'
        result = uhttp_client._parse_www_authenticate(header)
        self.assertEqual(result['realm'], 'api')
        self.assertEqual(result['qop'], 'auth')

    def test_digest_full(self):
        """Test full Digest header"""
        header = 'Digest realm="api@example.com", nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", qop="auth,auth-int", opaque="5ccc069c403ebaf9f0171e9517f40e41"'
        result = uhttp_client._parse_www_authenticate(header)
        self.assertEqual(result['realm'], 'api@example.com')
        self.assertEqual(result['nonce'], 'dcd98b7102dd2f0e8b11d0f600bfb0c093')
        self.assertEqual(result['qop'], 'auth,auth-int')
        self.assertEqual(result['opaque'], '5ccc069c403ebaf9f0171e9517f40e41')

    def test_basic_header(self):
        """Test Basic header parsing"""
        header = 'Basic realm="Secure Area"'
        result = uhttp_client._parse_www_authenticate(header)
        self.assertEqual(result['realm'], 'Secure Area')


class TestMd5Hex(unittest.TestCase):
    """Tests for _md5_hex function"""

    def test_string_input(self):
        """Test MD5 of string"""
        result = uhttp_client._md5_hex('hello')
        self.assertEqual(result, '5d41402abc4b2a76b9719d911017c592')

    def test_bytes_input(self):
        """Test MD5 of bytes"""
        result = uhttp_client._md5_hex(b'hello')
        self.assertEqual(result, '5d41402abc4b2a76b9719d911017c592')

    def test_empty_string(self):
        """Test MD5 of empty string"""
        result = uhttp_client._md5_hex('')
        self.assertEqual(result, 'd41d8cd98f00b204e9800998ecf8427e')


class TestBuildDigestAuth(unittest.TestCase):
    """Tests for _build_digest_auth function"""

    def test_basic_digest(self):
        """Test basic digest auth generation"""
        auth_params = {
            'realm': 'test',
            'nonce': 'abc123',
        }
        result = uhttp_client._build_digest_auth(
            'user', 'pass', 'GET', '/api', auth_params, nc=1)

        self.assertTrue(result.startswith('Digest '))
        self.assertIn('username="user"', result)
        self.assertIn('realm="test"', result)
        self.assertIn('nonce="abc123"', result)
        self.assertIn('uri="/api"', result)
        self.assertIn('response="', result)

    def test_digest_with_qop(self):
        """Test digest auth with qop"""
        auth_params = {
            'realm': 'test',
            'nonce': 'abc123',
            'qop': 'auth',
        }
        result = uhttp_client._build_digest_auth(
            'user', 'pass', 'GET', '/api', auth_params, nc=1)

        self.assertIn('qop=auth', result)
        self.assertIn('nc=00000001', result)
        self.assertIn('cnonce="', result)

    def test_digest_with_opaque(self):
        """Test digest auth preserves opaque"""
        auth_params = {
            'realm': 'test',
            'nonce': 'abc',
            'opaque': 'xyz789',
        }
        result = uhttp_client._build_digest_auth(
            'user', 'pass', 'GET', '/', auth_params, nc=1)

        self.assertIn('opaque="xyz789"', result)


class TestParseHeaderLine(unittest.TestCase):
    """Tests for _parse_header_line function"""

    def test_simple_header(self):
        """Test simple header parsing"""
        key, val = uhttp_client._parse_header_line(b'Content-Type: application/json')
        self.assertEqual(key, 'content-type')
        self.assertEqual(val, 'application/json')

    def test_header_with_spaces(self):
        """Test header with extra spaces"""
        key, val = uhttp_client._parse_header_line(b'  Content-Length  :  123  ')
        self.assertEqual(key, 'content-length')
        self.assertEqual(val, '123')

    def test_header_with_colon_in_value(self):
        """Test header with colon in value"""
        key, val = uhttp_client._parse_header_line(b'Location: http://example.com:8080/path')
        self.assertEqual(key, 'location')
        self.assertEqual(val, 'http://example.com:8080/path')

    def test_invalid_header_no_colon(self):
        """Test invalid header without colon raises error"""
        with self.assertRaises(uhttp_client.HttpResponseError):
            uhttp_client._parse_header_line(b'InvalidHeader')


class TestHttpResponse(unittest.TestCase):
    """Tests for HttpResponse class"""

    def test_basic_response(self):
        """Test basic response properties"""
        response = uhttp_client.HttpResponse(
            200, 'OK',
            {'content-type': 'text/plain', 'content-length': '5'},
            b'hello'
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.status_message, 'OK')
        self.assertEqual(response.data, b'hello')
        self.assertEqual(response.content_type, 'text/plain')
        self.assertEqual(response.content_length, 5)

    def test_json_parsing(self):
        """Test JSON response parsing"""
        response = uhttp_client.HttpResponse(
            200, 'OK',
            {'content-type': 'application/json'},
            b'{"key": "value"}'
        )
        self.assertEqual(response.json(), {'key': 'value'})

    def test_json_cached(self):
        """Test JSON parsing is cached"""
        response = uhttp_client.HttpResponse(
            200, 'OK', {}, b'{"a": 1}'
        )
        result1 = response.json()
        result2 = response.json()
        self.assertIs(result1, result2)

    def test_json_invalid(self):
        """Test invalid JSON raises error"""
        response = uhttp_client.HttpResponse(
            200, 'OK', {}, b'not json'
        )
        with self.assertRaises(uhttp_client.HttpResponseError):
            response.json()

    def test_repr(self):
        """Test response repr"""
        response = uhttp_client.HttpResponse(404, 'Not Found', {}, b'')
        self.assertEqual(repr(response), 'HttpResponse(404 Not Found)')


class TestHttpClientInit(unittest.TestCase):
    """Tests for HttpClient initialization"""

    def test_host_port_init(self):
        """Test initialization with host and port"""
        client = uhttp_client.HttpClient('example.com', port=8080)
        self.assertEqual(client.host, 'example.com')
        self.assertEqual(client.port, 8080)
        self.assertEqual(client.base_path, '')
        client.close()

    def test_url_init_http(self):
        """Test initialization with HTTP URL"""
        client = uhttp_client.HttpClient('http://example.com/api')
        self.assertEqual(client.host, 'example.com')
        self.assertEqual(client.port, 80)
        self.assertEqual(client.base_path, '/api')
        client.close()

    def test_url_init_https(self):
        """Test initialization with HTTPS URL"""
        client = uhttp_client.HttpClient('https://api.example.com/v1')
        self.assertEqual(client.host, 'api.example.com')
        self.assertEqual(client.port, 443)
        self.assertEqual(client.base_path, '/v1')
        client.close()

    def test_url_with_auth(self):
        """Test initialization with auth in URL"""
        client = uhttp_client.HttpClient('http://user:pass@example.com')
        self.assertEqual(client.auth, ('user', 'pass'))
        client.close()

    def test_auth_parameter_override(self):
        """Test auth parameter overrides URL auth"""
        client = uhttp_client.HttpClient(
            'http://user1:pass1@example.com',
            auth=('user2', 'pass2')
        )
        self.assertEqual(client.auth, ('user2', 'pass2'))
        client.close()

    def test_default_port_http(self):
        """Test default port for HTTP"""
        client = uhttp_client.HttpClient('example.com')
        self.assertEqual(client.port, 80)
        client.close()

    def test_initial_state(self):
        """Test initial client state"""
        client = uhttp_client.HttpClient('example.com')
        self.assertEqual(client.state, uhttp_client.STATE_IDLE)
        self.assertFalse(client.is_connected)
        self.assertEqual(client.cookies, {})
        self.assertEqual(client.read_sockets, [])
        self.assertEqual(client.write_sockets, [])
        client.close()

    def test_context_manager(self):
        """Test client as context manager"""
        with uhttp_client.HttpClient('example.com') as client:
            self.assertEqual(client.host, 'example.com')
        # After exiting, client should be closed


class TestHttpClientAuth(unittest.TestCase):
    """Tests for HttpClient authentication"""

    def test_auth_property_setter(self):
        """Test auth property setter"""
        client = uhttp_client.HttpClient('example.com')
        client.auth = ('new_user', 'new_pass')
        self.assertEqual(client.auth, ('new_user', 'new_pass'))
        client.close()

    def test_auth_none_default(self):
        """Test auth is None by default"""
        client = uhttp_client.HttpClient('example.com')
        self.assertIsNone(client.auth)
        client.close()


class TestHttpClientBuildRequest(unittest.TestCase):
    """Tests for HttpClient._build_request method"""

    def test_simple_get(self):
        """Test simple GET request building"""
        client = uhttp_client.HttpClient('example.com', port=80)
        request = client._build_request('GET', '/test')

        self.assertIn(b'GET /test HTTP/1.1', request)
        self.assertIn(b'host: example.com', request)
        self.assertIn(b'user-agent: uhttp-client', request)
        client.close()

    def test_with_query(self):
        """Test request with query parameters"""
        client = uhttp_client.HttpClient('example.com')
        request = client._build_request('GET', '/api', query={'a': '1', 'b': '2'})

        self.assertIn(b'GET /api?', request)
        self.assertIn(b'a=1', request)
        self.assertIn(b'b=2', request)
        client.close()

    def test_with_json_data(self):
        """Test request with JSON data"""
        client = uhttp_client.HttpClient('example.com')
        request = client._build_request('POST', '/api', data={'key': 'value'})

        self.assertIn(b'POST /api HTTP/1.1', request)
        self.assertIn(b'content-type: application/json', request)
        self.assertIn(b'{"key": "value"}', request)
        client.close()

    def test_with_custom_headers(self):
        """Test request with custom headers"""
        client = uhttp_client.HttpClient('example.com')
        request = client._build_request(
            'GET', '/api',
            headers={'X-Custom': 'value', 'Accept': 'application/json'}
        )

        self.assertIn(b'X-Custom: value', request)
        self.assertIn(b'Accept: application/json', request)
        client.close()

    def test_base_path_prepended(self):
        """Test base_path is prepended to request path"""
        client = uhttp_client.HttpClient('http://example.com/api/v1')
        request = client._build_request('GET', '/users')

        self.assertIn(b'GET /api/v1/users HTTP/1.1', request)
        client.close()

    def test_cookies_included(self):
        """Test cookies are included in request"""
        client = uhttp_client.HttpClient('example.com')
        client._cookies = {'session': 'abc123', 'user': 'john'}
        request = client._build_request('GET', '/test')

        self.assertIn(b'cookie:', request)
        self.assertIn(b'session=abc123', request)
        self.assertIn(b'user=john', request)
        client.close()

    def test_basic_auth_header(self):
        """Test Basic auth header is generated"""
        client = uhttp_client.HttpClient('example.com', auth=('user', 'pass'))
        request = client._build_request('GET', '/test')

        self.assertIn(b'authorization: Basic ', request)
        client.close()

    def test_host_header_with_port(self):
        """Test Host header includes port for non-standard ports"""
        client = uhttp_client.HttpClient('example.com', port=8080)
        request = client._build_request('GET', '/test')

        self.assertIn(b'host: example.com:8080', request)
        client.close()


class TestHttpClientErrors(unittest.TestCase):
    """Tests for HttpClient error handling"""

    def test_request_while_busy(self):
        """Test request while another is in progress raises error"""
        client = uhttp_client.HttpClient('example.com')
        client._state = uhttp_client.STATE_SENDING

        with self.assertRaises(uhttp_client.HttpClientError):
            client.request('GET', '/test')

        client._state = uhttp_client.STATE_IDLE
        client.close()

    def test_wait_without_request(self):
        """Test wait without active request raises error"""
        client = uhttp_client.HttpClient('example.com')

        with self.assertRaises(uhttp_client.HttpClientError):
            client.wait()

        client.close()


class TestLengthBodyReader(unittest.TestCase):
    """Tests for _LengthBodyReader"""

    def test_exact_length(self):
        reader = uhttp_client._LengthBodyReader(5)
        buf = bytearray(b'hello')
        data = reader.feed(buf)
        self.assertEqual(data, b'hello')
        self.assertTrue(reader.complete)
        self.assertEqual(len(buf), 0)

    def test_split_across_feeds(self):
        reader = uhttp_client._LengthBodyReader(5)
        buf = bytearray(b'he')
        self.assertEqual(reader.feed(buf), b'he')
        self.assertFalse(reader.complete)
        buf.extend(b'llo')
        self.assertEqual(reader.feed(buf), b'llo')
        self.assertTrue(reader.complete)

    def test_does_not_overread(self):
        """Extra bytes beyond Content-Length stay in the buffer"""
        reader = uhttp_client._LengthBodyReader(5)
        buf = bytearray(b'helloEXTRA')
        self.assertEqual(reader.feed(buf), b'hello')
        self.assertTrue(reader.complete)
        self.assertEqual(bytes(buf), b'EXTRA')

    def test_zero_length(self):
        reader = uhttp_client._LengthBodyReader(0)
        self.assertTrue(reader.complete)
        self.assertEqual(reader.feed(bytearray()), b'')

    def test_wanted_tracks_remaining(self):
        reader = uhttp_client._LengthBodyReader(10)
        self.assertEqual(reader.wanted(), 10)
        reader.feed(bytearray(b'abc'))
        self.assertEqual(reader.wanted(), 7)


class TestChunkedBodyReader(unittest.TestCase):
    """Tests for _ChunkedBodyReader"""

    def test_single_chunk(self):
        reader = uhttp_client._ChunkedBodyReader()
        buf = bytearray(b'5\r\nhello\r\n0\r\n\r\n')
        data = reader.feed(buf)
        self.assertEqual(data, b'hello')
        self.assertTrue(reader.complete)
        self.assertEqual(len(buf), 0)

    def test_multiple_chunks(self):
        reader = uhttp_client._ChunkedBodyReader()
        buf = bytearray(b'5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n')
        data = reader.feed(buf)
        self.assertEqual(data, b'hello world')
        self.assertTrue(reader.complete)

    def test_split_across_feeds(self):
        # Feed incrementally on a shared buffer, splitting at awkward points
        reader = uhttp_client._ChunkedBodyReader()
        buf = bytearray()
        result = bytearray()
        for piece in (b'5\r', b'\nhel', b'lo\r\n0', b'\r\n\r\n'):
            buf.extend(piece)
            result.extend(reader.feed(buf))
        self.assertEqual(bytes(result), b'hello')
        self.assertTrue(reader.complete)

    def test_chunk_extensions_ignored(self):
        reader = uhttp_client._ChunkedBodyReader()
        buf = bytearray(b'5;name=value\r\nhello\r\n0\r\n\r\n')
        self.assertEqual(reader.feed(buf), b'hello')
        self.assertTrue(reader.complete)

    def test_trailer_headers_ignored(self):
        reader = uhttp_client._ChunkedBodyReader()
        buf = bytearray(b'5\r\nhello\r\n0\r\nX-Checksum: abc\r\n\r\n')
        self.assertEqual(reader.feed(buf), b'hello')
        self.assertTrue(reader.complete)

    def test_incomplete_not_complete(self):
        reader = uhttp_client._ChunkedBodyReader()
        buf = bytearray(b'5\r\nhel')
        self.assertEqual(reader.feed(buf), b'hel')
        self.assertFalse(reader.complete)

    def test_invalid_chunk_size_raises(self):
        reader = uhttp_client._ChunkedBodyReader()
        buf = bytearray(b'zz\r\nhello\r\n')
        with self.assertRaises(uhttp_client.HttpResponseError):
            reader.feed(buf)

    def test_hex_chunk_size(self):
        reader = uhttp_client._ChunkedBodyReader()
        payload = b'x' * 26  # 0x1a
        buf = bytearray(b'1a\r\n' + payload + b'\r\n0\r\n\r\n')
        self.assertEqual(reader.feed(buf), payload)
        self.assertTrue(reader.complete)


class TestEofBodyReader(unittest.TestCase):
    """Tests for _EofBodyReader"""

    def test_reads_all_available(self):
        reader = uhttp_client._EofBodyReader()
        buf = bytearray(b'partial data')
        self.assertEqual(reader.feed(buf), b'partial data')
        self.assertFalse(reader.complete)
        self.assertEqual(len(buf), 0)

    def test_accumulates_across_feeds(self):
        reader = uhttp_client._EofBodyReader()
        self.assertEqual(reader.feed(bytearray(b'abc')), b'abc')
        self.assertEqual(reader.feed(bytearray(b'def')), b'def')
        self.assertFalse(reader.complete)

    def test_eof_completes(self):
        reader = uhttp_client._EofBodyReader()
        reader.feed(bytearray(b'data'))
        reader.feed_eof()
        self.assertTrue(reader.complete)

    def test_not_keep_alive_capable(self):
        reader = uhttp_client._EofBodyReader()
        self.assertFalse(reader.keep_alive_capable)


if __name__ == '__main__':
    unittest.main()
