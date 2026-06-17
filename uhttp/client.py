"""uHttp Client - Micro HTTP Client
python or micropython
(c) 2026 Pavel Revak <pavelrevak@gmail.com>
"""

import errno
import socket as _socket
import select as _select
import json as _json
import ssl as _ssl
import binascii as _binascii
import hashlib as _hashlib
import time as _time

KB = 2 ** 10
MB = 2 ** 20

# On Windows a non-blocking recv that would block raises EWOULDBLOCK (10035),
# which differs from EAGAIN (11); on POSIX they are equal. Defensive getattr
# because some MicroPython ports do not define EWOULDBLOCK.
EWOULDBLOCK = getattr(errno, 'EWOULDBLOCK', errno.EAGAIN)

CONNECT_TIMEOUT = 10
TIMEOUT = 30

MAX_RESPONSE_HEADERS_LENGTH = 4 * KB
MAX_RESPONSE_LENGTH = 1 * MB
BODY_CHUNK_SIZE = 4 * KB

HEADERS_DELIMITERS = (b'\r\n\r\n', b'\n\n')
CONTENT_LENGTH = 'content-length'
CONTENT_TYPE = 'content-type'
CONTENT_TYPE_JSON = 'application/json'
CONTENT_TYPE_OCTET_STREAM = 'application/octet-stream'
CONNECTION = 'connection'
CONNECTION_CLOSE = 'close'
CONNECTION_KEEP_ALIVE = 'keep-alive'
COOKIE = 'cookie'
SET_COOKIE = 'set-cookie'
HOST = 'host'
USER_AGENT = 'user-agent'
USER_AGENT_VALUE = 'uhttp-client/1.0'
TRANSFER_ENCODING = 'transfer-encoding'
CHUNKED = 'chunked'
AUTHORIZATION = 'authorization'
WWW_AUTHENTICATE = 'www-authenticate'
EXPECT = 'expect'
EXPECT_100_CONTINUE = '100-continue'

STATE_IDLE = 0
STATE_CONNECTING = 1
STATE_SSL_HANDSHAKE = 2
STATE_SENDING = 3
STATE_RECEIVING_HEADERS = 4
STATE_RECEIVING_BODY = 5
STATE_COMPLETE = 6
STATE_WAITING_100_CONTINUE = 7
STATE_HEADERS_READY = 8

# Event types returned by wait()/process_events() in event mode.
# Names and numeric values mirror uhttp-server's HttpConnection events.
EVENT_RESPONSE = 0   # Complete response (headers + body) in one step
EVENT_HEADERS = 1    # Headers received, call accept_body*()
EVENT_DATA = 2       # Decoded body chunk available, call read_buffer()
EVENT_COMPLETE = 3   # Body fully received
EVENT_ERROR = 4      # Error occurred (timeout, disconnect, decode error)


class HttpClientError(Exception):
    """HTTP client error"""


class HttpConnectionError(HttpClientError):
    """Connection error"""


class HttpTimeoutError(HttpClientError):
    """Timeout error"""


class HttpResponseError(HttpClientError):
    """Response parsing error"""


class _BodyReader:
    """Body framing strategy for decoding an HTTP response body.

    Subclasses consume raw bytes from the recv buffer (a bytearray, modified
    in place — the consumed prefix is removed) and return decoded body bytes.
    ``self.complete`` becomes True once the end of the body has been reached.
    """

    keep_alive_capable = True

    def __init__(self):
        self.complete = False

    def wanted(self):
        """Number of raw bytes to receive next, or None if unbounded."""
        return None

    def feed(self, buffer):
        """Consume framing bytes from buffer; return decoded body bytes."""
        raise NotImplementedError


class _LengthBodyReader(_BodyReader):
    """Read exactly Content-Length bytes from the stream."""

    def __init__(self, length):
        super().__init__()
        self._remaining = length
        if length <= 0:
            self.complete = True

    def wanted(self):
        return self._remaining

    def feed(self, buffer):
        if self._remaining <= 0:
            self.complete = True
            return b''
        take = min(self._remaining, len(buffer))
        if take == 0:
            return b''
        data = bytes(buffer[:take])
        del buffer[:take]
        self._remaining -= take
        if self._remaining <= 0:
            self.complete = True
        return data


class _ChunkedBodyReader(_BodyReader):
    """Decode HTTP/1.1 chunked transfer encoding.

    Stateful — keeps partial-chunk state across recv boundaries. Chunk
    extensions and trailer headers are parsed and discarded.
    """

    _SIZE = 0     # reading the chunk-size line
    _DATA = 1     # reading chunk payload
    _CRLF = 2     # consuming the CRLF after a chunk payload
    _TRAILER = 3  # reading trailer headers after the terminal chunk

    def __init__(self):
        super().__init__()
        self._sub = self._SIZE
        self._remaining = 0

    def feed(self, buffer):
        out = bytearray()
        while True:
            if self._sub == self._SIZE:
                idx = buffer.find(b'\n')
                if idx == -1:
                    break
                line = bytes(buffer[:idx]).strip()
                del buffer[:idx + 1]
                if b';' in line:
                    line = line[:line.index(b';')].strip()
                if not line:
                    continue
                try:
                    size = int(line, 16)
                except ValueError as err:
                    raise HttpResponseError(
                        f"Invalid chunk size: {line!r}") from err
                if size == 0:
                    self._sub = self._TRAILER
                else:
                    self._remaining = size
                    self._sub = self._DATA
            elif self._sub == self._DATA:
                if not buffer:
                    break
                take = min(self._remaining, len(buffer))
                out.extend(buffer[:take])
                del buffer[:take]
                self._remaining -= take
                if self._remaining == 0:
                    self._sub = self._CRLF
            elif self._sub == self._CRLF:
                if not buffer:
                    break
                if buffer[:1] == b'\r':
                    if len(buffer) < 2:
                        break
                    del buffer[:2]
                elif buffer[:1] == b'\n':
                    del buffer[:1]
                self._sub = self._SIZE
            elif self._sub == self._TRAILER:
                idx = buffer.find(b'\n')
                if idx == -1:
                    break
                line = bytes(buffer[:idx]).strip()
                del buffer[:idx + 1]
                if not line:
                    self.complete = True
                    break
        return bytes(out)


class _EofBodyReader(_BodyReader):
    """Read the body until the server closes the connection.

    Used when neither Content-Length nor chunked encoding is available.
    Cannot keep the connection alive — the close is the framing.
    """

    keep_alive_capable = False

    def feed(self, buffer):
        if not buffer:
            return b''
        data = bytes(buffer)
        del buffer[:]
        return data

    def feed_eof(self):
        self.complete = True


class _RecordDecoder:
    """Turns a decoded body byte stream into application-level records.

    Sits above the body reader: bytes -> _BodyReader -> decoded body ->
    _RecordDecoder -> records. Selected by the accept_*() variant the caller
    chooses; the event type stays EVENT_DATA regardless of record shape.
    """

    def feed(self, data):
        """Consume body bytes; return a list of complete records."""
        raise NotImplementedError

    def flush(self):
        """Return any final record buffered at end of stream."""
        return []


class _NdjsonDecoder(_RecordDecoder):
    """Newline-delimited JSON: one JSON value per line.

    A line that fails to decode does not discard already-parsed records: the
    good records are returned and the error is remembered (self.error) so the
    caller can deliver them first and surface the error afterwards.
    """

    def __init__(self):
        self._carry = bytearray()
        self.error = None

    def feed(self, data):
        records = []
        if self.error is not None:
            return records
        self._carry.extend(data)
        idx = self._carry.find(b'\n')
        while idx != -1:
            line = bytes(self._carry[:idx]).strip()
            del self._carry[:idx + 1]
            if line:
                try:
                    records.append(_decode_json(line))
                except HttpResponseError as err:
                    self.error = err
                    return records
            idx = self._carry.find(b'\n')
        return records

    def flush(self):
        if self.error is not None:
            return []
        line = bytes(self._carry).strip()
        self._carry = bytearray()
        if line:
            try:
                return [_decode_json(line)]
            except HttpResponseError as err:
                self.error = err
        return []


def _parse_header_line(line):
    try:
        line = line.decode('ascii')
    except ValueError as err:
        readable = line.decode('utf-8', errors='replace')
        raise HttpResponseError(f"Invalid non-ASCII characters in header: {readable}") from err
    if ':' not in line:
        raise HttpResponseError(f"Invalid header format: {line}")
    key, val = line.split(':', 1)
    return key.strip().lower(), val.strip()


def parse_url(url):
    """Parse URL to host, port, path, ssl, auth

    Returns (host, port, path, ssl, auth) tuple.
    Path includes leading slash, can be used as base_path.
    Auth is (user, password) tuple or None.
    """
    ssl = False
    if url.startswith('https://'):
        ssl = True
        url = url[8:]
    elif url.startswith('http://'):
        url = url[7:]

    # Split host_port and path first
    if '/' in url:
        host_port, path = url.split('/', 1)
        path = '/' + path
    else:
        host_port = url
        path = ''

    # Extract auth (user:pass@) only from host_port part
    auth = None
    if '@' in host_port:
        auth_part, host_port = host_port.rsplit('@', 1)
        if ':' in auth_part:
            user, password = auth_part.split(':', 1)
            auth = (user, password)
        else:
            auth = (auth_part, '')

    if ':' in host_port:
        host, port_str = host_port.rsplit(':', 1)
        port = int(port_str)
    else:
        host = host_port
        port = 443 if ssl else 80

    return host, port, path, ssl, auth


def _encode_query(query):
    if not query:
        return ''
    parts = []
    for key, val in query.items():
        if isinstance(val, list):
            for v in val:
                parts.append(f"{key}={v}")
        elif val is None:
            parts.append(key)
        else:
            parts.append(f"{key}={val}")
    return '?' + '&'.join(parts)


def _encode_request_data(data, headers):
    if data is None:
        return None
    if isinstance(data, (dict, list, tuple)):
        data = _json.dumps(data).encode('ascii')
        if CONTENT_TYPE not in headers:
            headers[CONTENT_TYPE] = CONTENT_TYPE_JSON
    elif isinstance(data, str):
        data = data.encode('utf-8')
    elif isinstance(data, (bytes, bytearray, memoryview)):
        if CONTENT_TYPE not in headers:
            headers[CONTENT_TYPE] = CONTENT_TYPE_OCTET_STREAM
    else:
        raise HttpClientError(f"Unsupported data type: {type(data).__name__}")
    return bytes(data)


def _parse_www_authenticate(header_value):
    """Parse WWW-Authenticate header into dict"""
    result = {}
    # Remove 'Digest ' or 'Basic ' prefix
    if header_value.lower().startswith('digest '):
        header_value = header_value[7:]
    elif header_value.lower().startswith('basic '):
        header_value = header_value[6:]

    # Parse key="value" or key=value pairs (handles commas in quoted values)
    i = 0
    while i < len(header_value):
        # Skip whitespace and commas
        while i < len(header_value) and header_value[i] in ' ,':
            i += 1
        if i >= len(header_value):
            break

        # Find key
        eq_pos = header_value.find('=', i)
        if eq_pos == -1:
            break
        key = header_value[i:eq_pos].strip().lower()
        i = eq_pos + 1

        # Parse value
        if i < len(header_value) and header_value[i] == '"':
            # Quoted value - find closing quote
            i += 1
            end = header_value.find('"', i)
            if end == -1:
                end = len(header_value)
            val = header_value[i:end]
            i = end + 1
        else:
            # Unquoted value - find comma or end
            end = header_value.find(',', i)
            if end == -1:
                end = len(header_value)
            val = header_value[i:end].strip()
            i = end

        result[key] = val
    return result


def _md5_hex(data):
    """Calculate MD5 hash and return hex string"""
    if isinstance(data, str):
        data = data.encode('utf-8')
    return _hashlib.md5(data).hexdigest()


def _build_digest_auth(username, password, method, uri, auth_params, nc=1):
    """Build Digest Authorization header value"""
    realm = auth_params.get('realm', '')
    nonce = auth_params.get('nonce', '')
    qop = auth_params.get('qop', '')
    algorithm = auth_params.get('algorithm', 'MD5').upper()
    opaque = auth_params.get('opaque', '')

    # Only MD5 supported
    if algorithm not in ('MD5', 'MD5-SESS'):
        raise HttpClientError(f"Unsupported digest algorithm: {algorithm}")

    # HA1
    ha1 = _md5_hex(f"{username}:{realm}:{password}")
    if algorithm == 'MD5-SESS':
        cnonce = _md5_hex(str(nc))[:8]
        ha1 = _md5_hex(f"{ha1}:{nonce}:{cnonce}")

    # HA2
    ha2 = _md5_hex(f"{method}:{uri}")

    # Response
    nc_str = f"{nc:08x}"
    cnonce = _md5_hex(str(nc))[:8]

    if qop:
        qop_value = qop.split(',')[0].strip()  # Use first qop option
        response = _md5_hex(
            f"{ha1}:{nonce}:{nc_str}:{cnonce}:{qop_value}:{ha2}")
    else:
        qop_value = None
        response = _md5_hex(f"{ha1}:{nonce}:{ha2}")

    # Build header
    parts = [
        f'username="{username}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
        f'response="{response}"',
    ]
    if qop_value:
        parts.extend([
            f'qop={qop_value}',
            f'nc={nc_str}',
            f'cnonce="{cnonce}"',
        ])
    if opaque:
        parts.append(f'opaque="{opaque}"')
    if algorithm != 'MD5':
        parts.append(f'algorithm={algorithm}')

    return 'Digest ' + ', '.join(parts)


def _decode_json(data):
    """Decode JSON, raising HttpResponseError on failure.

    Shared by HttpResponse.json() and the streaming NDJSON decoder so both
    use the same parsing and error path.
    """
    try:
        return _json.loads(data)
    except ValueError as err:
        raise HttpResponseError(f"JSON decode error: {err}") from err


class HttpResponse:
    """HTTP response"""

    def __init__(self, status, status_message, headers, data):
        self._status = status
        self._status_message = status_message
        self._headers = headers
        self._data = data
        self._json = None

    @property
    def content_length(self):
        val = self._headers.get(CONTENT_LENGTH)
        return int(val) if val else None

    @property
    def content_type(self):
        return self._headers.get(CONTENT_TYPE, '')

    @property
    def data(self):
        return self._data

    @property
    def headers(self):
        return self._headers

    @property
    def status(self):
        return self._status

    @property
    def status_message(self):
        return self._status_message

    def json(self):
        """Parse response body as JSON (lazy, cached)"""
        if self._json is None:
            self._json = _decode_json(self._data)
        return self._json

    def __repr__(self):
        return f"HttpResponse({self._status} {self._status_message})"


class HttpClient:
    """HTTP client with keep-alive support

    Can be initialized with URL or host/port:
        HttpClient('https://api.example.com/v1')
        HttpClient('api.example.com', port=443, ssl_context=ctx)
    """

    def __init__(
            self, url_or_host, port=None, ssl_context=None, auth=None,
            connect_timeout=CONNECT_TIMEOUT, timeout=TIMEOUT,
            max_response_length=MAX_RESPONSE_LENGTH, event_mode=False):
        # Parse URL if provided
        if '://' in url_or_host or url_or_host.startswith('http'):
            host, parsed_port, base_path, use_ssl, url_auth = parse_url(
                url_or_host)
            if port is None:
                port = parsed_port
            if auth is None:
                auth = url_auth
            if use_ssl and ssl_context is None:
                if hasattr(_ssl, 'create_default_context'):
                    ssl_context = _ssl.create_default_context()
                else:
                    raise HttpClientError(
                        "HTTPS requires explicit ssl_context on MicroPython")
        else:
            host = url_or_host
            base_path = ''
            if port is None:
                port = 443 if ssl_context else 80

        self._host = host
        self._port = port
        self._base_path = base_path.rstrip('/')
        self._ssl_context = ssl_context
        self._auth = auth
        self._digest_params = None
        self._digest_nc = 0
        self._connect_timeout = connect_timeout
        self._timeout = timeout
        self._max_response_length = max_response_length
        self._event_mode = event_mode

        self._socket = None
        self._state = STATE_IDLE
        self._ssl_want_read = True
        self._buffer = bytearray()
        self._send_buffer = bytearray()

        self._request_method = None
        self._request_path = None
        self._request_headers = None
        self._request_data = None
        self._request_query = None
        self._request_auth = None
        self._request_timeout = None
        self._request_start_time = None
        self._request_stream = False

        self._response_status = None
        self._response_status_message = None
        self._response_headers = None
        self._response = None
        self._body = bytearray()
        self._body_reader = None

        # Event-mode state
        self._event = None
        self._error = None
        # None / 'buffer' / 'stream' / 'file' / 'record'
        self._accept_mode = None
        self._body_file_handle = None
        self._bytes_received = 0
        self._record_decoder = None
        self._records = []

        self._cookies = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    @property
    def cookies(self):
        return self._cookies

    @property
    def auth(self):
        return self._auth

    @auth.setter
    def auth(self, value):
        self._auth = value

    @property
    def host(self):
        return self._host

    @property
    def is_connected(self):
        return (self._socket is not None
                and self._state not in (STATE_CONNECTING, STATE_SSL_HANDSHAKE))

    @property
    def port(self):
        return self._port

    @property
    def base_path(self):
        return self._base_path

    @property
    def event(self):
        """Current event type (event mode only)"""
        return self._event

    @property
    def error(self):
        """Error message when the last event was EVENT_ERROR"""
        return self._error

    @property
    def response(self):
        """Completed HttpResponse (EVENT_RESPONSE / buffered EVENT_COMPLETE)"""
        return self._response

    @property
    def status(self):
        """Response status code (available from EVENT_HEADERS on)"""
        return self._response_status

    @property
    def status_message(self):
        """Response status message (available from EVENT_HEADERS on)"""
        return self._response_status_message

    @property
    def headers(self):
        """Response headers dict (available from EVENT_HEADERS on)"""
        return self._response_headers

    @property
    def content_type(self):
        """Response Content-Type (available from EVENT_HEADERS on)"""
        if self._response_headers is None:
            return ''
        return self._response_headers.get(CONTENT_TYPE, '')

    @property
    def content_length(self):
        """Response Content-Length, or None if not present/known"""
        if self._response_headers is None:
            return None
        val = self._response_headers.get(CONTENT_LENGTH)
        return int(val) if val else None

    @property
    def bytes_received(self):
        """Number of decoded body bytes received so far"""
        return self._bytes_received

    @property
    def read_sockets(self):
        if self._socket and self._state == STATE_SSL_HANDSHAKE:
            return [self._socket] if self._ssl_want_read else []
        if self._socket and self._state in (
                STATE_WAITING_100_CONTINUE,
                STATE_RECEIVING_HEADERS, STATE_RECEIVING_BODY):
            return [self._socket]
        return []

    @property
    def state(self):
        return self._state

    @property
    def write_sockets(self):
        if self._socket and self._state == STATE_CONNECTING:
            return [self._socket]
        if self._socket and self._state == STATE_SSL_HANDSHAKE:
            return [self._socket] if not self._ssl_want_read else []
        if self._socket and self._state == STATE_SENDING and self._send_buffer:
            return [self._socket]
        return []

    def _build_request(
            self, method, path, headers=None, data=None, query=None,
            expect_continue=False):
        if headers is None:
            headers = {}

        encoded_data = _encode_request_data(data, headers)

        # Prepend base_path
        if self._base_path and not path.startswith(self._base_path):
            path = self._base_path + (path if path.startswith('/') else '/' + path)
        elif not path.startswith('/'):
            path = '/' + path

        full_path = path + _encode_query(query)

        if HOST not in headers:
            if self._port == 80 or (self._ssl_context and self._port == 443):
                headers[HOST] = self._host
            else:
                headers[HOST] = f"{self._host}:{self._port}"

        if USER_AGENT not in headers:
            headers[USER_AGENT] = USER_AGENT_VALUE

        if encoded_data:
            headers[CONTENT_LENGTH] = len(encoded_data)

        # Add Expect: 100-continue header if requested and there's data to send
        if expect_continue and encoded_data:
            headers[EXPECT] = EXPECT_100_CONTINUE

        if self._cookies:
            cookie_str = '; '.join(
                f"{k}={v}" for k, v in self._cookies.items())
            headers[COOKIE] = cookie_str

        # Use request-specific auth if set, otherwise client's default
        auth = self._request_auth if self._request_auth is not None else self._auth
        if auth and AUTHORIZATION not in headers:
            if self._digest_params:
                # Digest auth
                self._digest_nc += 1
                headers[AUTHORIZATION] = _build_digest_auth(
                    auth[0], auth[1],
                    method, full_path, self._digest_params, self._digest_nc)
            else:
                # Basic auth
                credentials = f"{auth[0]}:{auth[1]}".encode('utf-8')
                b64 = _binascii.b2a_base64(credentials).decode('ascii').strip()
                headers[AUTHORIZATION] = f"Basic {b64}"

        lines = [f"{method} {full_path} HTTP/1.1"]
        for key, val in headers.items():
            lines.append(f"{key}: {val}")
        lines.append('')
        lines.append('')

        request_headers = '\r\n'.join(lines).encode('ascii')

        # If expect_continue, return headers and body separately
        if expect_continue and encoded_data:
            return (request_headers, encoded_data)

        # Otherwise return combined request
        request = request_headers
        if encoded_data:
            request += encoded_data

        return request

    def _close(self):
        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        self._state = STATE_IDLE
        self._buffer = bytearray()
        self._send_buffer = bytearray()
        self._body = bytearray()
        self._body_reader = None

    def _connect(self):
        if self._socket is not None:
            return

        try:
            addr_info = _socket.getaddrinfo(
                self._host, self._port, 0, _socket.SOCK_STREAM)
            if not addr_info:
                raise HttpConnectionError(
                    f"Cannot resolve host: {self._host}")
            family, socktype, proto, _, addr = addr_info[0]
            sock = _socket.socket(family, socktype, proto)
            sock.setblocking(False)
            try:
                sock.connect(addr)
                # Connect completed immediately (e.g. loopback)
                self._socket = sock
                self._connect_complete()
            except OSError as err:
                if err.errno in (
                        errno.EINPROGRESS, errno.EAGAIN,
                        errno.EALREADY, errno.EWOULDBLOCK):
                    self._socket = sock
                    self._state = STATE_CONNECTING
                else:
                    sock.close()
                    raise HttpConnectionError(
                        f"Connect failed: {err}") from err
        except HttpConnectionError:
            raise
        except OSError as err:
            raise HttpConnectionError(f"Connect failed: {err}") from err

    def _connect_complete(self):
        """TCP connection established, start SSL or proceed to sending"""
        if self._ssl_context:
            self._wrap_ssl()
        else:
            self._build_and_start_sending()

    def _wrap_ssl(self):
        """Wrap socket with SSL and start non-blocking handshake"""
        try:
            self._socket = self._ssl_context.wrap_socket(
                self._socket, server_hostname=self._host,
                do_handshake_on_connect=False)
        except OSError as err:
            self._close()
            raise HttpConnectionError(
                f"SSL wrap failed: {err}") from err
        self._state = STATE_SSL_HANDSHAKE
        self._process_ssl_handshake()

    def _check_connect_timeout(self):
        """Check if connect/handshake phase has timed out"""
        if self._request_start_time is not None:
            elapsed = _time.time() - self._request_start_time
            if self._connect_timeout and elapsed > self._connect_timeout:
                self._close()
                raise HttpTimeoutError("Connect timed out")
            timeout = (self._request_timeout
                       if self._request_timeout is not None
                       else self._timeout)
            if timeout and elapsed > timeout:
                self._close()
                raise HttpTimeoutError("Request timed out")

    def _process_connecting(self):
        """Handle TCP connect completion (socket became writable)"""
        try:
            err = self._socket.getsockopt(
                _socket.SOL_SOCKET, _socket.SO_ERROR)
        except (AttributeError, OSError):
            # MicroPython socket may not have getsockopt
            err = 0
        if err != 0:
            self._close()
            raise HttpConnectionError(f"Connect failed: error {err}")
        self._connect_complete()

    def _process_ssl_handshake(self):
        """Continue non-blocking SSL handshake"""
        try:
            self._socket.do_handshake()
        except _ssl.SSLWantReadError:
            self._ssl_want_read = True
            return
        except _ssl.SSLWantWriteError:
            self._ssl_want_read = False
            return
        except AttributeError:
            # MicroPython: no do_handshake(), handshake happens
            # implicitly during first send/recv
            pass
        except OSError as err:
            if err.errno in (errno.EAGAIN, EWOULDBLOCK, errno.ENOENT):
                self._ssl_want_read = True  # MicroPython
                return
            self._close()
            raise HttpConnectionError(
                f"SSL handshake failed: {err}") from err
        # Handshake complete
        self._build_and_start_sending()

    def _build_and_start_sending(self):
        """Build HTTP request and start sending"""
        headers_copy = dict(
            self._request_headers) if self._request_headers else {}
        request_data = self._build_request(
            self._request_method, self._request_path,
            headers_copy, self._request_data, self._request_query,
            expect_continue=self._request_expect_continue)

        if isinstance(request_data, tuple):
            headers, body = request_data
            self._send_buffer.extend(headers)
            self._pending_body = body
        else:
            self._send_buffer.extend(request_data)
            self._pending_body = None

        self._state = STATE_SENDING
        self._try_send()

    def _finalize_response(self):
        # Handle 401 Digest challenge
        auth = self._request_auth if self._request_auth is not None else self._auth
        if (self._response_status == 401 and
                auth and
                not self._digest_params):
            www_auth = self._response_headers.get(WWW_AUTHENTICATE, '')
            if www_auth.lower().startswith('digest '):
                # Parse digest params and retry
                self._digest_params = _parse_www_authenticate(www_auth)
                self._digest_nc = 0
                # Close connection if server requested
                if not self._should_keep_alive():
                    self._close()
                # Reset for retry, but keep request params
                self._reset_request(clear_request=False)
                self._start_request()
                return None  # Signal to continue waiting

        response = HttpResponse(
            self._response_status,
            self._response_status_message,
            self._response_headers,
            bytes(self._body)
        )

        if not self._should_keep_alive():
            self._close()
        else:
            self._reset_request()
            self._state = STATE_IDLE

        return response

    def _parse_set_cookie(self, val):
        """Parse single Set-Cookie header value"""
        # Simple parsing - just key=value before first ;
        if '=' in val:
            cookie_part = val.split(';')[0]
            name, value = cookie_part.split('=', 1)
            self._cookies[name.strip()] = value.strip()

    def _parse_headers(self, header_lines):
        self._response_headers = {}

        while header_lines:
            line = header_lines.pop(0)
            if not line:
                break
            if self._response_status is None:
                self._parse_status_line(line)
            else:
                key, val = _parse_header_line(line)
                # Handle Set-Cookie specially - parse each one immediately
                # (dict would overwrite multiple Set-Cookie headers)
                if key == SET_COOKIE:
                    self._parse_set_cookie(val)
                else:
                    self._response_headers[key] = val

        self._body_reader = self._make_body_reader()

    def _make_body_reader(self):
        """Select a body framing strategy based on response headers."""
        te = self._response_headers.get(TRANSFER_ENCODING, '').lower()
        if CHUNKED in te:
            return _ChunkedBodyReader()
        cl = self._response_headers.get(CONTENT_LENGTH)
        if cl is not None:
            return _LengthBodyReader(int(cl))
        if self._request_stream:
            # Streaming request without framing: read until the server closes
            # the connection (MJPEG, SSE, close-delimited bodies).
            return _EofBodyReader()
        # Neither Content-Length nor chunked: preserve historical behavior
        # and treat the body as empty.
        return _LengthBodyReader(0)

    def _check_response_length(self):
        """Fast-fail when the advertised Content-Length is too large."""
        cl = self._response_headers.get(CONTENT_LENGTH)
        if cl is not None and int(cl) > self._max_response_length:
            raise HttpResponseError(f"Response too large: {cl}")

    def _feed_body(self):
        """Feed buffered raw bytes through the body reader into self._body."""
        decoded = self._body_reader.feed(self._buffer)
        if decoded:
            self._body.extend(decoded)
            self._bytes_received += len(decoded)
        if len(self._body) > self._max_response_length:
            raise HttpResponseError(f"Response too large: {len(self._body)}")
        if self._body_reader.complete:
            self._state = STATE_COMPLETE
        else:
            self._state = STATE_RECEIVING_BODY

    def _on_headers_complete(self):
        """Decide how the body is delivered once headers are parsed.

        Classic mode proceeds straight into the body. Event mode pauses at
        EVENT_HEADERS for an accept_body*() call, unless the whole body
        already arrived together with the headers (then EVENT_RESPONSE).
        """
        self._check_response_length()
        self._feed_body()  # decode any body bytes already in the buffer
        if not self._event_mode:
            return
        if self._state == STATE_COMPLETE:
            self._event = EVENT_RESPONSE
        else:
            self._state = STATE_HEADERS_READY
            self._event = EVENT_HEADERS

    def _parse_status_line(self, line):
        try:
            line = line.decode('ascii')
        except ValueError as err:
            raise HttpResponseError(f"Invalid status line: {line}") from err

        parts = line.split(' ', 2)
        if len(parts) < 2:
            raise HttpResponseError(f"Invalid status line: {line}")

        protocol = parts[0]
        if not protocol.startswith('HTTP/'):
            raise HttpResponseError(f"Invalid protocol: {protocol}")

        try:
            self._response_status = int(parts[1])
        except ValueError as err:
            raise HttpResponseError(
                f"Invalid status code: {parts[1]}") from err

        self._response_status_message = parts[2] if len(parts) > 2 else ''

    def _recv_into_body(self):
        """Receive and decode body bytes into self._body."""
        recv_size = self._body_reader.wanted()
        if recv_size is None:
            recv_size = BODY_CHUNK_SIZE
        if recv_size > 0 and not self._body_reader.complete:
            self._recv_to_buffer(recv_size)
        self._feed_body()

    def _process_100_continue(self):
        """Process response while waiting for 100 Continue"""
        self._recv_to_buffer(MAX_RESPONSE_HEADERS_LENGTH - len(self._buffer))

        for delimiter in HEADERS_DELIMITERS:
            if delimiter in self._buffer:
                end_index = self._buffer.index(delimiter) + len(delimiter)
                header_lines = self._buffer[:end_index].splitlines()
                self._buffer = self._buffer[end_index:]
                self._parse_headers(header_lines)

                if self._response_status == 100:
                    # Got 100 Continue - send body, reset response state
                    self._response_status = None
                    self._response_status_message = None
                    self._response_headers = None
                    self._body_reader = None
                    self._send_buffer.extend(self._pending_body)
                    self._pending_body = None
                    self._state = STATE_SENDING
                    self._try_send()
                    return

                # Not 100 Continue - this is final response, don't send body
                self._pending_body = None
                self._on_headers_complete()
                return

        if len(self._buffer) >= MAX_RESPONSE_HEADERS_LENGTH:
            raise HttpResponseError("Response headers too large")

    def _process_recv_headers(self):
        self._recv_to_buffer(MAX_RESPONSE_HEADERS_LENGTH - len(self._buffer))

        for delimiter in HEADERS_DELIMITERS:
            if delimiter in self._buffer:
                end_index = self._buffer.index(delimiter) + len(delimiter)
                header_lines = self._buffer[:end_index].splitlines()
                self._buffer = self._buffer[end_index:]
                self._parse_headers(header_lines)
                self._on_headers_complete()
                return

        if len(self._buffer) >= MAX_RESPONSE_HEADERS_LENGTH:
            raise HttpResponseError("Response headers too large")

    def _has_ssl_pending(self):
        """Check if SSL socket has buffered data that select() can't see"""
        return (self._socket is not None and
                hasattr(self._socket, 'pending') and
                self._socket.pending() > 0)

    def _recv_to_buffer(self, recv_size):
        """Receive up to recv_size bytes, appending them to self._buffer.

        Returns True if data was received (or EOF was handled), False on
        EAGAIN / SSL want-read. Raises HttpConnectionError if the peer closes
        the connection while a framed body is still expected.
        """
        if recv_size <= 0:
            return False
        try:
            data = self._socket.recv(recv_size)
        except (_ssl.SSLWantReadError, _ssl.SSLWantWriteError):
            return False
        except OSError as err:
            if err.errno in (errno.EAGAIN, EWOULDBLOCK, errno.ENOENT):
                # EAGAIN/EWOULDBLOCK: no data yet (non-blocking; the two
                # differ on Windows). ENOENT: MicroPython SSL would-block.
                return False
            # A close-delimited body (EOF reader) ends when the connection
            # ends. The termination may surface as an empty read OR an error
            # (reset/abort - notably on Windows, with a platform-specific
            # errno). Without framing there is no way to tell a clean end from
            # a truncated one, so treat any non-would-block error as
            # end-of-body (browsers/curl do the same for HTTP/1.0 close).
            if (self._body_reader is not None
                    and not self._body_reader.keep_alive_capable):
                self._body_reader.feed_eof()
                return True
            raise HttpConnectionError(f"Recv failed: {err}") from err
        if data is None:
            # MicroPython SSL returns None when the recv would block.
            return False
        if not data:
            # Peer closed the connection. For close-delimited bodies this is
            # the end of the body, not an error.
            if (self._body_reader is not None
                    and not self._body_reader.keep_alive_capable):
                self._body_reader.feed_eof()
                return True
            raise HttpConnectionError("Connection closed by server")
        self._buffer.extend(data)
        return True

    def _reset_request(self, clear_request=True):
        if clear_request:
            self._request_method = None
            self._request_path = None
            self._request_headers = None
            self._request_data = None
            self._request_query = None
            self._request_auth = None
            self._request_timeout = None
            self._request_start_time = None
            self._request_expect_continue = False
            self._request_stream = False
        self._response_status = None
        self._response_status_message = None
        self._response_headers = None
        self._response = None
        self._body = bytearray()
        self._body_reader = None
        self._buffer = bytearray()
        self._send_buffer = bytearray()
        self._pending_body = None
        self._event = None
        self._error = None
        self._accept_mode = None
        self._bytes_received = 0
        self._record_decoder = None
        self._records = []
        self._close_body_file()

    def _should_keep_alive(self):
        if not self._response_headers:
            return False
        # Close-delimited bodies (EOF reader) cannot keep the connection alive.
        if (self._body_reader is not None
                and not self._body_reader.keep_alive_capable):
            return False
        conn = self._response_headers.get(CONNECTION, '').lower()
        if conn == CONNECTION_CLOSE:
            return False
        return True  # HTTP/1.1 defaults to keep-alive

    def _try_send(self):
        while self._send_buffer and self._state == STATE_SENDING:
            try:
                sent = self._socket.send(self._send_buffer)
                if sent is None:  # MicroPython SSL returns None on full buffer
                    break
                if sent > 0:
                    self._send_buffer = self._send_buffer[sent:]
            except (_ssl.SSLWantReadError, _ssl.SSLWantWriteError):
                break
            except OSError as err:
                if err.errno in (errno.EAGAIN, EWOULDBLOCK):
                    break  # send buffer full (EWOULDBLOCK differs on Windows)
                raise HttpConnectionError(f"Send failed: {err}") from err

        if not self._send_buffer:
            if self._pending_body is not None:
                # Waiting for 100 Continue before sending body
                self._state = STATE_WAITING_100_CONTINUE
            else:
                self._state = STATE_RECEIVING_HEADERS

    def close(self):
        """Close connection"""
        self._close()

    def delete(self, path, **kwargs):
        """Send DELETE request"""
        return self.request('DELETE', path, **kwargs)

    def get(self, path, **kwargs):
        """Send GET request"""
        return self.request('GET', path, **kwargs)

    def head(self, path, **kwargs):
        """Send HEAD request"""
        return self.request('HEAD', path, **kwargs)

    def patch(self, path, **kwargs):
        """Send PATCH request"""
        return self.request('PATCH', path, **kwargs)

    def post(self, path, **kwargs):
        """Send POST request"""
        return self.request('POST', path, **kwargs)

    def process_events(self, read_sockets, write_sockets):
        """Process select events from an external select loop.

        Classic mode: returns HttpResponse when the response is complete
        (or None), and raises on errors.

        Event mode: returns an EVENT_* constant (or None when there is
        nothing new yet); connection errors surface as EVENT_ERROR with the
        message in client.error.
        """
        if self._event_mode:
            return self._process_events_eventmode(read_sockets, write_sockets)
        return self._process_events_classic(read_sockets, write_sockets)

    def _process_events_classic(self, read_sockets, write_sockets):
        if self._state == STATE_IDLE:
            return None

        try:
            # Handle non-blocking connect completion
            if self._state == STATE_CONNECTING:
                if self._socket in write_sockets:
                    self._process_connecting()
                if self._state == STATE_CONNECTING:
                    self._check_connect_timeout()
                    return None

            # Handle non-blocking SSL handshake
            if self._state == STATE_SSL_HANDSHAKE:
                if (self._socket in read_sockets
                        or self._socket in write_sockets):
                    self._process_ssl_handshake()
                if self._state == STATE_SSL_HANDSHAKE:
                    self._check_connect_timeout()
                    return None

            # Send request data
            if self._socket in write_sockets and self._state == STATE_SENDING:
                self._try_send()

            # SSL may buffer decrypted data internally that select() can't see
            socket_readable = (self._socket in read_sockets or
                               self._has_ssl_pending())
            if socket_readable:
                if self._state == STATE_WAITING_100_CONTINUE:
                    self._process_100_continue()
                elif self._state == STATE_RECEIVING_HEADERS:
                    self._process_recv_headers()
                elif self._state == STATE_RECEIVING_BODY:
                    self._recv_into_body()

            if self._state == STATE_COMPLETE:
                response = self._finalize_response()
                if response is not None:
                    return response
                # None means digest retry, continue processing

        except (HttpConnectionError, HttpTimeoutError, HttpResponseError):
            self._close()
            raise

        # Check request timeout for sending/receiving phases
        if self._request_start_time is not None:
            timeout = self._request_timeout if self._request_timeout is not None else self._timeout
            if timeout and _time.time() - self._request_start_time > timeout:
                self._close()
                raise HttpTimeoutError("Request timed out")

        return None

    def _process_events_eventmode(self, read_sockets, write_sockets):
        self._event = None
        # IDLE: nothing in progress. HEADERS_READY: waiting for accept_body().
        if self._state in (STATE_IDLE, STATE_HEADERS_READY):
            return None

        try:
            # Non-blocking connect completion
            if self._state == STATE_CONNECTING:
                if self._socket in write_sockets:
                    self._process_connecting()
                if self._state == STATE_CONNECTING:
                    self._check_connect_timeout()
                    return None

            # Non-blocking SSL handshake
            if self._state == STATE_SSL_HANDSHAKE:
                if (self._socket in read_sockets
                        or self._socket in write_sockets):
                    self._process_ssl_handshake()
                if self._state == STATE_SSL_HANDSHAKE:
                    self._check_connect_timeout()
                    return None

            # Send request data
            if self._socket in write_sockets and self._state == STATE_SENDING:
                self._try_send()

            readable = (self._socket in read_sockets
                        or self._has_ssl_pending()
                        or self._has_pending_body())
            if readable:
                if self._state == STATE_WAITING_100_CONTINUE:
                    self._process_100_continue()
                elif self._state == STATE_RECEIVING_HEADERS:
                    self._process_recv_headers()
                elif self._state == STATE_RECEIVING_BODY:
                    self._process_body_streaming()

            if self._state == STATE_COMPLETE:
                if self._event == EVENT_DATA:
                    pass  # deliver buffered data/records before EVENT_COMPLETE
                elif self._accept_mode == 'stream' and self._body:
                    self._event = EVENT_DATA
                elif self._accept_mode == 'record' and self._records:
                    self._event = EVENT_DATA
                else:
                    self._complete_event()

        except (HttpConnectionError, HttpTimeoutError,
                HttpResponseError) as err:
            self._error = str(err)
            self._close()
            self._event = EVENT_ERROR
            return self._event

        # Overall request timeout applies only to pre-body phases; a live
        # stream may run indefinitely and is bounded by the caller's select
        # timeout instead.
        if (self._event is None
                and self._request_start_time is not None
                and self._state in (
                    STATE_SENDING, STATE_RECEIVING_HEADERS,
                    STATE_WAITING_100_CONTINUE)):
            timeout = (self._request_timeout
                       if self._request_timeout is not None
                       else self._timeout)
            if timeout and _time.time() - self._request_start_time > timeout:
                self._error = "Request timed out"
                self._close()
                self._event = EVENT_ERROR

        return self._event

    def _process_body_streaming(self):
        """Event-mode body step: recv, decode, emit EVENT_DATA / write file."""
        self._recv_into_body()
        if self._accept_mode == 'file':
            if self._body:
                self._flush_body_file()
        elif self._accept_mode == 'stream':
            if self._body:
                self._event = EVENT_DATA
        elif self._accept_mode == 'record':
            if self._body:
                data = bytes(self._body)
                self._body = bytearray()
                self._records.extend(self._record_decoder.feed(data))
            if self._state == STATE_COMPLETE:
                # Flush the trailing line (no newline) once the stream ends.
                self._records.extend(self._record_decoder.flush())
            if self._records:
                self._event = EVENT_DATA
            elif self._record_decoder.error is not None:
                # All good records consumed; surface the decode error now.
                raise HttpResponseError(str(self._record_decoder.error))
        # 'buffer' mode accumulates silently until STATE_COMPLETE

    def _complete_event(self):
        """Finalize a completed response in event mode."""
        if self._accept_mode == 'file':
            self._close_body_file()
        if self._accept_mode in (None, 'buffer'):
            self._response = HttpResponse(
                self._response_status,
                self._response_status_message,
                self._response_headers,
                bytes(self._body))
        # EVENT_RESPONSE (body arrived with headers) takes precedence.
        if self._event != EVENT_RESPONSE:
            self._event = EVENT_COMPLETE
        self._finish_keepalive()

    def _finish_keepalive(self):
        """Keep-alive vs close after a completed response (event mode).

        Response metadata (status/headers/response) is preserved for the
        caller; it is cleared on the next request().
        """
        if self._should_keep_alive():
            self._buffer = bytearray()
            self._body = bytearray()
            self._body_reader = None
            self._state = STATE_IDLE
        else:
            self._close()  # drops the socket, keeps response metadata

    def _has_pending_body(self):
        """True when buffered body data can progress without a socket read."""
        if self._state == STATE_COMPLETE:
            return True
        if self._state != STATE_RECEIVING_BODY:
            return False
        if self._buffer:
            return True
        if self._body_reader is not None and self._body_reader.complete:
            return True
        if self._accept_mode in ('stream', 'file') and self._body:
            return True
        if self._accept_mode == 'record' and (
                self._body or self._records
                or (self._record_decoder is not None
                    and self._record_decoder.error is not None)):
            return True
        return False

    def accept_body(self):
        """Buffer the whole body, then emit EVENT_COMPLETE.

        Read the result via the .response property. Valid only after
        EVENT_HEADERS.
        """
        self._accept_common()
        self._accept_mode = 'buffer'

    def accept_body_streaming(self):
        """Emit EVENT_DATA for each decoded chunk; read via read_buffer().

        Valid only after EVENT_HEADERS.
        """
        self._accept_common()
        self._accept_mode = 'stream'

    def accept_ndjson(self):
        """Decode newline-delimited JSON, one record per EVENT_DATA.

        Read each record via read_record(). A line that fails to decode
        surfaces as EVENT_ERROR (the caller decides whether to close).
        Valid only after EVENT_HEADERS.
        """
        self._accept_common()
        self._accept_mode = 'record'
        self._record_decoder = _NdjsonDecoder()

    def accept_body_to_file(self, path):
        """Write the decoded body to a file, then emit EVENT_COMPLETE.

        Valid only after EVENT_HEADERS.
        """
        self._accept_common()
        self._accept_mode = 'file'
        try:
            self._body_file_handle = open(path, 'wb')
        except OSError as err:
            self._close()
            raise HttpClientError(
                f"Cannot open file {path}: {err}") from err

    def _accept_common(self):
        if self._state != STATE_HEADERS_READY:
            raise HttpClientError(
                "accept_body() can only be called after EVENT_HEADERS")
        self._state = STATE_RECEIVING_BODY

    def read_buffer(self):
        """Return decoded body bytes buffered so far, or None if empty."""
        if not self._body:
            return None
        data = bytes(self._body)
        self._body = bytearray()
        return data

    def read_record(self):
        """Return the next decoded record (accept_ndjson), or None if none.

        One record is delivered per EVENT_DATA, so the typical handler is
        simply ``handle(client.read_record())``.
        """
        if not self._records:
            return None
        return self._records.pop(0)

    def _flush_body_file(self):
        try:
            self._body_file_handle.write(self._body)
        except OSError as err:
            self._close_body_file()
            raise HttpConnectionError(f"Failed to write file: {err}") from err
        self._body = bytearray()

    def _close_body_file(self):
        if self._body_file_handle is not None:
            try:
                self._body_file_handle.close()
            except OSError:
                pass
            self._body_file_handle = None

    def put(self, path, **kwargs):
        """Send PUT request"""
        return self.request('PUT', path, **kwargs)

    def request(
            self, method, path,
            headers=None, data=None, query=None, json=None, auth=None,
            timeout=None, expect_continue=False, stream=False):
        """Start HTTP request (async), returns self for chaining

        auth parameter overrides client's default auth for this request.
        timeout parameter overrides client's default timeout for this request.
        expect_continue sends Expect: 100-continue header and waits for
        server confirmation before sending body (saves bandwidth on rejection).
        stream=True reads a response without Content-Length/chunked framing
        until the server closes the connection (MJPEG, SSE, etc.).
        """
        if json is not None:
            data = json

        if self._state != STATE_IDLE:
            raise HttpClientError("Request already in progress")

        # Validate data type early (before non-blocking connect)
        if (data is not None
                and not isinstance(
                    data, (dict, list, tuple, str, bytes,
                           bytearray, memoryview))):
            raise HttpClientError(
                f"Unsupported data type: {type(data).__name__}")

        self._reset_request()
        self._request_method = method
        self._request_path = path
        self._request_headers = dict(headers) if headers else {}
        self._request_data = data
        self._request_query = query
        self._request_auth = auth  # None means use client's default
        self._request_timeout = timeout  # None means use client's default
        self._request_start_time = _time.time()
        self._request_expect_continue = expect_continue
        self._request_stream = stream

        self._start_request()

        return self

    def _start_request(self):
        """Internal: start sending current request"""
        if self._socket is None:
            self._connect()
            # If non-blocking connect in progress, request will be
            # built when connection completes
            if self._state in (STATE_CONNECTING, STATE_SSL_HANDSHAKE):
                return
        self._build_and_start_sending()

    def wait(self, timeout=None):
        """Wait for response (blocking).

        Classic mode: returns HttpResponse when complete, raises on timeout.
        Event mode: returns the next EVENT_* constant, or None on timeout.

        timeout is the max time to spend in this wait() call.
        If None, uses request timeout or client default.
        """
        if self._event_mode:
            return self._wait_event(timeout)

        if self._state == STATE_IDLE:
            raise HttpClientError("No request in progress")

        if timeout is None:
            timeout = self._request_timeout if self._request_timeout is not None else self._timeout

        start_time = _time.time()

        while True:
            # Calculate remaining time for this wait() call
            if timeout:
                elapsed = _time.time() - start_time
                remaining = timeout - elapsed
                if remaining <= 0:
                    self._close()
                    raise HttpTimeoutError("Request timed out")
            else:
                remaining = None

            # SSL may have buffered data that select() can't see
            select_timeout = 0 if self._has_ssl_pending() else remaining
            r, w, x = _select.select(
                self.read_sockets,
                self.write_sockets,
                self.write_sockets, select_timeout
            )
            # Windows signals connect errors via except set
            if x:
                w = list(set(w) | set(x))

            # Always call process_events to check request timeout
            response = self.process_events(r, w)

            if response is not None:
                return response

            # select() timed out (not SSL pending poll)
            if not r and not w and select_timeout != 0:
                self._close()
                raise HttpTimeoutError("Request timed out")

            # Digest retry failure - state changed to IDLE
            if self._state == STATE_IDLE:
                raise HttpResponseError("Request failed")

    def _wait_event(self, timeout):
        """Wait for the next event (event mode).

        Returns an EVENT_* constant, or None when the select() timeout
        expires with nothing new (the caller loops and calls wait() again).
        """
        # IDLE: no request. HEADERS_READY: caller must call accept_body*().
        if self._state in (STATE_IDLE, STATE_HEADERS_READY):
            return None

        if timeout is None:
            timeout = (self._request_timeout
                       if self._request_timeout is not None
                       else self._timeout)

        # SSL or already-buffered body data is invisible to select() - poll.
        if self._has_ssl_pending() or self._has_pending_body():
            select_timeout = 0
        else:
            select_timeout = timeout

        try:
            r, w, x = _select.select(
                self.read_sockets,
                self.write_sockets,
                self.write_sockets, select_timeout)
        except (OSError, ValueError) as err:
            self._error = f"select failed: {err}"
            self._close()
            self._event = EVENT_ERROR
            return EVENT_ERROR

        # Windows signals connect errors via the except set
        if x:
            w = list(set(w) | set(x))

        return self.process_events(r, w)
