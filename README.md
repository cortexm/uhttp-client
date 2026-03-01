# uHTTP Client: micro HTTP client


## Features

- MicroPython and CPython compatible
- Select-based async (no async/await, no threading)
- Keep-alive connections with automatic reuse
- Fluent API: `response = client.get('/path').wait()`
- URL parsing with automatic SSL detection
- Base path support for API versioning
- JSON support (auto-encode request, lazy decode response)
- Binary data support
- Cookies persistence
- HTTP Basic and Digest authentication
- SSL/TLS support for HTTPS


## Usage

### URL-based initialization (recommended)

```python
import uhttp.client

# HTTPS with automatic SSL context
client = uhttp.client.HttpClient('https://api.example.com')
response = client.get('/users').wait()
client.close()

# With base path for API versioning
client = uhttp.client.HttpClient('https://api.example.com/v1')
response = client.get('/users').wait()  # requests /v1/users
client.close()

# HTTP
client = uhttp.client.HttpClient('http://localhost:8080')
```

### Traditional initialization

```python
import uhttp.client

client = uhttp.client.HttpClient('httpbin.org', port=80)
response = client.get('/get').wait()
client.close()

# With explicit SSL context
import ssl
ctx = ssl.create_default_context()
client = uhttp.client.HttpClient('api.example.com', port=443, ssl_context=ctx)
```

### Context manager

```python
import uhttp.client

with uhttp.client.HttpClient('https://httpbin.org') as client:
    response = client.get('/get').wait()
    print(response.status)
```

### JSON API

```python
client = uhttp.client.HttpClient('https://api.example.com/v1')

# GET with query parameters
response = client.get('/users', query={'page': 1, 'limit': 10}).wait()

# POST with JSON body
response = client.post('/users', json={'name': 'John'}).wait()

# PUT
response = client.put('/users/1', json={'name': 'Jane'}).wait()

# DELETE
response = client.delete('/users/1').wait()

client.close()
```

### Custom headers

```python
response = client.get('/protected', headers={
    'Authorization': 'Bearer token123',
    'X-Custom-Header': 'value'
}).wait()
```

### Binary data

```python
# Send binary
response = client.post('/upload', data=b'\x00\x01\x02\xff').wait()

# Receive binary
response = client.get('/image.png').wait()
image_bytes = response.data
```


## HTTPS

### Automatic (with URL)

```python
import uhttp.client

# SSL context created automatically for https:// URLs
client = uhttp.client.HttpClient('https://api.example.com')
response = client.get('/secure').wait()
client.close()
```

### Manual SSL context

```python
import ssl
import uhttp.client

ctx = ssl.create_default_context()
client = uhttp.client.HttpClient('api.example.com', port=443, ssl_context=ctx)
response = client.get('/secure').wait()
client.close()
```

### MicroPython HTTPS

```python
import ssl
import uhttp.client

ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
client = uhttp.client.HttpClient('api.example.com', port=443, ssl_context=ctx)
response = client.get('/secure').wait()
client.close()
```


## Async (non-blocking) mode

Default mode is async. Use with external select loop:

```python
import select
import uhttp.client

client = uhttp.client.HttpClient('http://httpbin.org')

# Start request (non-blocking)
client.get('/delay/2')

# Manual select loop
while True:
    r, w, _ = select.select(
        client.read_sockets,
        client.write_sockets,
        [], 10.0
    )

    response = client.process_events(r, w)
    if response:
        print(response.status)
        break

client.close()
```

### Parallel requests

```python
import select
import uhttp.client

clients = [
    uhttp.client.HttpClient('http://httpbin.org'),
    uhttp.client.HttpClient('http://httpbin.org'),
    uhttp.client.HttpClient('http://httpbin.org'),
]

# Start all requests
for i, client in enumerate(clients):
    client.get('/delay/1', query={'n': i})

# Wait for all
results = {}
while len(results) < len(clients):
    read_socks = []
    write_socks = []
    for c in clients:
        read_socks.extend(c.read_sockets)
        write_socks.extend(c.write_sockets)

    r, w, _ = select.select(read_socks, write_socks, [], 10.0)

    for i, client in enumerate(clients):
        if i not in results:
            resp = client.process_events(r, w)
            if resp:
                results[i] = resp

for client in clients:
    client.close()
```

### Combined with HttpServer

```python
import select
import uhttp.server
import uhttp.client

server = uhttp.server.HttpServer(port=8080)
backend = uhttp.client.HttpClient('http://api.example.com')

while True:
    r, w, _ = select.select(
        server.read_sockets + backend.read_sockets,
        server.write_sockets + backend.write_sockets,
        [], 1.0
    )

    # Handle incoming requests
    incoming = server.process_events(r, w)
    if incoming:
        backend.get('/data', query=incoming.query)

    # Handle backend response
    response = backend.process_events(r, w)
    if response:
        incoming.respond(data=response.data)
```


## API

### Function `parse_url`

**`uhttp.client.parse_url(url)`**

Parse URL into components. Returns `(host, port, path, ssl, auth)` tuple.

```python
import uhttp.client

uhttp.client.parse_url('https://api.example.com/v1/users')
# → ('api.example.com', 443, '/v1/users', True, None)

uhttp.client.parse_url('http://localhost:8080/api')
# → ('localhost', 8080, '/api', False, None)

uhttp.client.parse_url('https://user:pass@api.example.com')
# → ('api.example.com', 443, '', True, ('user', 'pass'))

uhttp.client.parse_url('example.com')
# → ('example.com', 80, '', False, None)
```


### Class `HttpClient`

**`uhttp.client.HttpClient(url_or_host, port=None, ssl_context=None, auth=None, connect_timeout=10, timeout=30, max_response_length=1MB)`**

Can be initialized with URL or host/port:

```python
import uhttp.client

# URL-based (recommended)
uhttp.client.HttpClient('https://api.example.com/v1')

# With auth in URL
uhttp.client.HttpClient('https://user:pass@api.example.com/v1')

# Traditional
uhttp.client.HttpClient('api.example.com', port=443, ssl_context=ctx)
```

Parameters:
- `url_or_host` - Full URL (http://... or https://...) or hostname
- `port` - Server port (auto-detected from URL: 80 for http, 443 for https)
- `ssl_context` - Optional `ssl.SSLContext` (auto-created for https:// URLs)
- `auth` - Optional (username, password) tuple for HTTP authentication
- `connect_timeout` - Connection timeout in seconds (default: 10)
- `timeout` - Response timeout in seconds (default: 30)
- `max_response_length` - Maximum response size (default: 1MB)

#### Properties

- `host` - Server hostname
- `port` - Server port
- `base_path` - Base path from URL (prepended to all request paths)
- `is_connected` - True if socket is connected
- `state` - Current state (STATE_IDLE, STATE_SENDING, etc.)
- `auth` - Authentication credentials tuple (username, password) or None
- `cookies` - Cookies dict (persistent across requests)
- `read_sockets` - Sockets to monitor for reading (for select)
- `write_sockets` - Sockets to monitor for writing (for select)

#### Methods

**`request(method, path, headers=None, data=None, query=None, json=None, auth=None, timeout=None)`**

Start HTTP request (async). Returns `self` for chaining.

- `method` - HTTP method (GET, POST, PUT, DELETE, etc.)
- `path` - Request path (base_path is prepended automatically)
- `headers` - Optional headers dict
- `data` - Request body (bytes, str, or dict/list for JSON)
- `query` - Optional query parameters dict
- `json` - Shortcut for data with JSON encoding
- `auth` - Optional (username, password) tuple, overrides client's default auth
- `timeout` - Optional timeout in seconds, overrides client's default timeout

**`get(path, **kwargs)`** - Send GET request

**`post(path, **kwargs)`** - Send POST request

**`put(path, **kwargs)`** - Send PUT request

**`delete(path, **kwargs)`** - Send DELETE request

**`head(path, **kwargs)`** - Send HEAD request

**`patch(path, **kwargs)`** - Send PATCH request

**`wait(timeout=None)`**

Wait for response (blocking). Returns `HttpResponse` when complete.

- `timeout` - Max time to spend in wait() call. If `None`, uses request timeout.
- Returns `None` if wait timeout expires (connection stays open, can call again).
- Raises `HttpTimeoutError` if request timeout expires (connection closed).

**`process_events(read_sockets, write_sockets)`**

Process select events. Returns `HttpResponse` when complete, `None` otherwise.

- First processes any ready data, then checks request timeout.
- Raises `HttpTimeoutError` if request timeout has expired and no complete response.

**`close()`**

Close connection.


### Class `HttpResponse`

#### Properties

- `status` - HTTP status code (int)
- `status_message` - HTTP status message (str)
- `headers` - Response headers dict (keys are lowercase)
- `data` - Response body as bytes
- `content_type` - Content-Type header value
- `content_length` - Content-Length header value

#### Methods

**`json()`**

Parse response body as JSON. Lazy evaluation, cached.


## Authentication

### Basic Auth

HTTP Basic authentication via URL or `auth` parameter:

```python
import uhttp.client

# Via URL
client = uhttp.client.HttpClient('https://user:password@api.example.com')
response = client.get('/protected').wait()

# Via parameter
client = uhttp.client.HttpClient('https://api.example.com', auth=('user', 'password'))
response = client.get('/protected').wait()

# Change auth at runtime
client.auth = ('new_user', 'new_password')

# Per-request auth (overrides client's default)
client = uhttp.client.HttpClient('https://api.example.com')
response = client.get('/admin', auth=('admin', 'secret')).wait()
response = client.get('/public').wait()  # no auth
```

### Digest Auth

HTTP Digest authentication is handled automatically. On 401 response with
`WWW-Authenticate: Digest` header, the client retries with digest credentials:

```python
import uhttp.client

# Same API as Basic auth - digest is automatic
client = uhttp.client.HttpClient('https://api.example.com', auth=('user', 'password'))

# First request gets 401, client automatically retries with digest auth
response = client.get('/protected').wait()
print(response.status)  # 200 (after automatic retry)
```

Supported digest features:
- MD5 and MD5-sess algorithms
- qop (quality of protection) with auth mode
- Nonce counting for multiple requests


## Cookies

Cookies are automatically:
- Stored from `Set-Cookie` response headers
- Sent with subsequent requests

```python
import uhttp.client

client = uhttp.client.HttpClient('https://example.com')

# Login - server sets session cookie
client.post('/login', json={'user': 'admin', 'pass': 'secret'}).wait()

# Subsequent requests include the cookie automatically
response = client.get('/dashboard').wait()

# Access cookies
print(client.cookies)  # {'session': 'abc123'}

client.close()
```


## Keep-Alive

Connections are reused automatically (HTTP/1.1 keep-alive).

```python
import uhttp.client

client = uhttp.client.HttpClient('https://httpbin.org')

# All requests use the same connection
for i in range(10):
    response = client.get('/get', query={'n': i}).wait()
    print(f"Request {i}: {response.status}")

client.close()
```


## Timeouts

Two types of timeouts:

### Request timeout

Total time allowed for the request. Set via `timeout` parameter on client or per-request.
When expired, raises `HttpTimeoutError` and closes connection.

```python
import uhttp.client

# Client-level timeout (default for all requests)
client = uhttp.client.HttpClient('https://example.com', timeout=30)

# Per-request timeout (overrides client default)
response = client.get('/slow', timeout=60).wait()
```

### Wait timeout

Time to spend in `wait()` call. When expired, returns `None` but keeps connection open.
Useful for polling or interleaving with other work.

```python
import uhttp.client

client = uhttp.client.HttpClient('https://example.com', timeout=60)  # request timeout
client.get('/slow')

# Try for 5 seconds, then do something else
response = client.wait(timeout=5)
if response is None:
    print("Still waiting, doing other work...")
    # Can call wait() again
    response = client.wait(timeout=10)
```


## Error handling

```python
import uhttp.client

client = uhttp.client.HttpClient('https://example.com')

try:
    response = client.get('/api').wait()
except uhttp.client.HttpConnectionError as e:
    print(f"Connection failed: {e}")
except uhttp.client.HttpTimeoutError as e:
    print(f"Timeout: {e}")
except uhttp.client.HttpResponseError as e:
    print(f"Invalid response: {e}")
except uhttp.client.HttpClientError as e:
    print(f"Client error: {e}")
finally:
    client.close()
```


## Configuration constants

```python
CONNECT_TIMEOUT = 10              # seconds
TIMEOUT = 30                      # seconds
MAX_RESPONSE_HEADERS_LENGTH = 4KB
MAX_RESPONSE_LENGTH = 1MB
```


## Examples

See [examples/](../examples/) directory:
- `client_basic.py` - Basic blocking examples
- `client_https.py` - HTTPS examples
- `client_async.py` - Async select loop examples
- `client_with_server.py` - Combined server + client examples

Run examples from project root:
```bash
PYTHONPATH=./server:./client python examples/client_basic.py
```


## CLI Tool

Simple curl-like CLI tool using uhttp.client:

```bash
# GET request
python -m tools.httpcl http://httpbin.org/get

# POST with JSON
python -m tools.httpcl http://httpbin.org/post -j '{"key": "value"}'

# Verbose mode
python -m tools.httpcl -v https://httpbin.org/get

# Save to file
python -m tools.httpcl https://httpbin.org/image/png -o image.png
```

See `python -m tools.httpcl --help` for all options.


## IPv6 Support

Client supports both IPv4 and IPv6:
- Automatically tries all addresses returned by `getaddrinfo()` (IPv4 and IPv6)
- Works with hostnames like `localhost` on all systems

```python
import uhttp.client

# Works on all systems (IPv4 or IPv6)
client = uhttp.client.HttpClient('http://localhost:8080')

# Explicit IPv4
client = uhttp.client.HttpClient('http://127.0.0.1:8080')

# Explicit IPv6
client = uhttp.client.HttpClient('http://[::1]:8080')
```


## Development

### Running tests

```bash
../.venv/bin/pip install -e .
../.venv/bin/python -m unittest discover -v tests/
```

For running tests from meta-repo, see [uhttp README](https://github.com/pavelrevak/uhttp#testing).

### CI

Tests run automatically on push/PR via GitHub Actions (Ubuntu + Windows, Python 3.10 + 3.14).
