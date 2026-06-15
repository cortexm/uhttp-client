# uHTTP Client: micro HTTP client


## Features

- MicroPython and CPython compatible
- Fully non-blocking: TCP connect, SSL handshake, and HTTP I/O via select
- Keep-alive connections with automatic reuse
- Fluent API: `response = client.get('/path').wait()`
- URL parsing with automatic SSL detection
- Base path support for API versioning
- JSON support (auto-encode request, lazy decode response)
- Binary data support
- Chunked transfer encoding (and `Content-Length`) response decoding
- Streaming event mode (`event_mode=True`) mirroring uhttp-server: `EVENT_*` +
  `accept_body*()` — stream to memory, file, or NDJSON records
- Read-until-close responses (`stream=True`) for MJPEG / SSE
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

Everything is non-blocking by default — TCP connect, SSL handshake, and HTTP I/O all happen through `select()`. This is critical for embedded devices on slow networks (4G modems, ESP32 PPP) where each phase can take seconds.

```python
import select
import uhttp.client

client = uhttp.client.HttpClient('http://httpbin.org')

# Start request (non-blocking, including connect)
client.get('/delay/2')

# Manual select loop - handles connect, send, and receive
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

### State machine

After `client.get('/path')`, the client progresses through states automatically via `process_events()`:

| State | Description | select watches |
|---|---|---|
| `STATE_CONNECTING` | TCP connect in progress | write |
| `STATE_SSL_HANDSHAKE` | SSL handshake in progress | read or write |
| `STATE_SENDING` | Sending request data | write |
| `STATE_RECEIVING_HEADERS` | Waiting for response headers | read |
| `STATE_RECEIVING_BODY` | Receiving response body | read |
| `STATE_COMPLETE` | Response ready | — |

The `state` property exposes the current state. The `is_connected` property returns `True` only after connect and handshake are complete.

### Parallel requests

All clients share one select loop. Connect, handshake, and data transfer happen concurrently:

```python
import select
import uhttp.client

clients = [
    uhttp.client.HttpClient('http://httpbin.org'),
    uhttp.client.HttpClient('http://httpbin.org'),
    uhttp.client.HttpClient('http://httpbin.org'),
]

# Start all requests (non-blocking connects begin immediately)
for i, client in enumerate(clients):
    client.get('/delay/1', query={'n': i})

# Single select loop handles all clients
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

Server and client in the same select loop — true single-threaded concurrency:

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

### HTTPS with non-blocking handshake

SSL handshake is also non-blocking. The client tracks whether `do_handshake()` needs to read or write, and exposes the socket only in the correct direction to prevent `select()` from spinning:

```python
import select
import ssl
import uhttp.client

ctx = ssl.create_default_context()
client = uhttp.client.HttpClient(
    'api.example.com', port=443, ssl_context=ctx)

# Connect + SSL handshake + request all happen via select
client.get('/data')

while True:
    r, w, _ = select.select(
        client.read_sockets,
        client.write_sockets,
        [], 10.0
    )
    response = client.process_events(r, w)
    if response:
        print(response.json())
        break

client.close()
```

### Multiple HTTPS clients in parallel

```python
import select
import uhttp.client

urls = [
    'https://api1.example.com/data',
    'https://api2.example.com/data',
    'https://api3.example.com/data',
]

clients = [uhttp.client.HttpClient(url) for url in urls]
for c in clients:
    c.get('/')  # All start non-blocking connects + SSL handshakes

responses = [None] * len(clients)
while not all(responses):
    read_socks = []
    write_socks = []
    for c in clients:
        read_socks.extend(c.read_sockets)
        write_socks.extend(c.write_sockets)

    r, w, _ = select.select(read_socks, write_socks, [], 10.0)

    for i, c in enumerate(clients):
        if responses[i] is None:
            resp = c.process_events(r, w)
            if resp:
                responses[i] = resp

for c in clients:
    c.close()
```


## Streaming & Event Mode

For large or open-ended responses (downloads, NDJSON, MJPEG, SSE) the client
offers an **event mode** that mirrors uhttp-server's `HttpConnection` API.
With `event_mode=True`, `wait()` / `process_events()` return `EVENT_*`
constants instead of an `HttpResponse`, and you choose how the body is
delivered after the headers arrive.

### Events

| Event | Meaning |
|---|---|
| `EVENT_RESPONSE` | Complete response (headers + body) in one step — small/buffered |
| `EVENT_HEADERS` | Headers ready → call an `accept_body*()` variant |
| `EVENT_DATA` | One decoded chunk/record ready → `read_buffer()` / `read_record()` |
| `EVENT_COMPLETE` | Body fully received |
| `EVENT_ERROR` | Connection or decode error → message in `client.error` (no exception) |

Names and numeric values match uhttp-server, so the same select loop can drive
both a server and a client.

### Body delivery (choose after `EVENT_HEADERS`)

- `accept_body()` — buffer the whole body → `EVENT_COMPLETE`; read it as a full
  `HttpResponse` via `client.response` (so `.json()` / `.data` are reused)
- `accept_body_streaming()` — `EVENT_DATA` per chunk; `read_buffer()` → bytes
- `accept_body_to_file(path)` — stream the body to disk (low RAM, no `EVENT_DATA`)
- `accept_ndjson()` — `EVENT_DATA` per record; `read_record()` → decoded object

The *event* tells you the phase; the *decoder* (which `accept_*()` you call)
tells you the shape of what you read — so new formats add an `accept_*()`, not a
new event type.

### NDJSON streaming

```python
import select
from uhttp.client import (
    HttpClient, EVENT_HEADERS, EVENT_DATA, EVENT_COMPLETE, EVENT_ERROR)

client = HttpClient('http://api.example.com', event_mode=True)
client.get('/events.ndjson', stream=True)   # stream until close if unframed

while True:
    r, w, _ = select.select(client.read_sockets, client.write_sockets, [], 30)
    event = client.process_events(r, w)

    if event == EVENT_HEADERS:
        client.accept_ndjson()
    elif event == EVENT_DATA:
        record = client.read_record()        # already a decoded object
        handle(record)
    elif event == EVENT_COMPLETE:
        break
    elif event == EVENT_ERROR:
        print(client.error)
        break

client.close()
```

A line that fails to JSON-decode is reported as `EVENT_ERROR` (with the message
in `client.error`) only **after** the good records before it have been
delivered. The client never closes the connection on its own — you decide.

### Download to file (low RAM)

```python
client = HttpClient('http://example.com', event_mode=True)
client.get('/firmware.bin')

while True:
    r, w, _ = select.select(client.read_sockets, client.write_sockets, [], 10)
    event = client.process_events(r, w)
    if event == EVENT_HEADERS:
        client.accept_body_to_file('/sd/firmware.bin')
    elif event == EVENT_COMPLETE:
        print('written', client.bytes_received, 'bytes')
        break
    elif event == EVENT_ERROR:
        print(client.error)
        break

client.close()
```

### Read until close (MJPEG / SSE)

`stream=True` selects a close-delimited body reader when the response has
neither `Content-Length` nor chunked encoding — the body is read until the
server closes the connection. (Such a connection cannot be kept alive.)

```python
client = HttpClient('http://cam.local', event_mode=True)
client.get('/stream.mjpeg', stream=True)
# ... EVENT_HEADERS → accept_body_streaming() → EVENT_DATA loop ...
```

### Blocking mode still works

Small responses don't need any of this — in event mode they arrive as a single
`EVENT_RESPONSE` with the full body in `client.response`. And with the default
`event_mode=False`, `wait()` returns an `HttpResponse` exactly as before;
chunked decoding works transparently there too.


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

**`uhttp.client.HttpClient(url_or_host, port=None, ssl_context=None, auth=None, connect_timeout=10, timeout=30, max_response_length=1MB, event_mode=False)`**

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
- `max_response_length` - Maximum buffered body size (default: 1MB)
- `event_mode` - If `True`, `wait()`/`process_events()` return `EVENT_*`
  constants instead of `HttpResponse` (see [Streaming & Event Mode](#streaming--event-mode))

#### Properties

- `host` - Server hostname
- `port` - Server port
- `base_path` - Base path from URL (prepended to all request paths)
- `is_connected` - True if TCP (and SSL) connection is fully established
- `state` - Current state (STATE_IDLE, STATE_CONNECTING, STATE_SSL_HANDSHAKE, STATE_SENDING, etc.)
- `auth` - Authentication credentials tuple (username, password) or None
- `cookies` - Cookies dict (persistent across requests)
- `read_sockets` - Sockets to monitor for reading (for select)
- `write_sockets` - Sockets to monitor for writing (for select)

Event-mode properties (available once headers are received):

- `event` - Last `EVENT_*` constant returned
- `error` - Error message when the last event was `EVENT_ERROR`
- `status` - Response status code (int)
- `status_message` - Response status message (str)
- `headers` - Response headers dict (keys lowercase)
- `content_type` - Response Content-Type
- `content_length` - Response Content-Length, or `None` if unknown
- `bytes_received` - Decoded body bytes received so far
- `response` - Completed `HttpResponse` (after `EVENT_RESPONSE` or buffered `EVENT_COMPLETE`)

#### Methods

**`request(method, path, headers=None, data=None, query=None, json=None, auth=None, timeout=None, expect_continue=False, stream=False)`**

Start HTTP request (async). Returns `self` for chaining.

- `method` - HTTP method (GET, POST, PUT, DELETE, etc.)
- `path` - Request path (base_path is prepended automatically)
- `headers` - Optional headers dict
- `data` - Request body (bytes, str, or dict/list for JSON)
- `query` - Optional query parameters dict
- `json` - Shortcut for data with JSON encoding
- `auth` - Optional (username, password) tuple, overrides client's default auth
- `timeout` - Optional timeout in seconds, overrides client's default timeout
- `expect_continue` - Send `Expect: 100-continue` header and wait for server confirmation before sending body (default: False)
- `stream` - Read a response without `Content-Length`/chunked framing until the server closes the connection (MJPEG, SSE); default: False

**`get(path, **kwargs)`** - Send GET request

**`post(path, **kwargs)`** - Send POST request

**`put(path, **kwargs)`** - Send PUT request

**`delete(path, **kwargs)`** - Send DELETE request

**`head(path, **kwargs)`** - Send HEAD request

**`patch(path, **kwargs)`** - Send PATCH request

**`wait(timeout=None)`**

Wait for response (blocking).

- Classic mode: returns `HttpResponse` when complete; raises `HttpTimeoutError`
  if the request timeout expires; returns `None` if the wait timeout expires
  (connection stays open, can call again).
- Event mode: returns the next `EVENT_*` constant, or `None` when the wait
  timeout expires with nothing new.
- `timeout` - Max time to spend in wait() call. If `None`, uses request timeout.

**`process_events(read_sockets, write_sockets)`**

Process select events from an external select loop.

- Classic mode: returns `HttpResponse` when complete (`None` otherwise); raises
  on errors.
- Event mode: returns an `EVENT_*` constant (`None` when nothing new yet);
  connection/decode errors surface as `EVENT_ERROR` with the message in
  `client.error`.

#### Event-mode body methods

Call one of these after `EVENT_HEADERS` to choose how the body is delivered
(see [Streaming & Event Mode](#streaming--event-mode)):

**`accept_body()`** - Buffer the whole body, then emit `EVENT_COMPLETE`; read
via the `response` property.

**`accept_body_streaming()`** - Emit `EVENT_DATA` per decoded chunk; read bytes
via `read_buffer()`.

**`accept_body_to_file(path)`** - Stream the decoded body to a file, then emit
`EVENT_COMPLETE`.

**`accept_ndjson()`** - Decode newline-delimited JSON; emit `EVENT_DATA` per
record; read decoded objects via `read_record()`.

**`read_buffer()`** - Return decoded body bytes buffered so far, or `None`.

**`read_record()`** - Return the next decoded NDJSON record, or `None`.

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


## Expect: 100-continue

For large uploads, use `expect_continue=True` to wait for server confirmation before sending the body. This saves bandwidth when the server rejects the request (e.g., 413 Too Large, 401 Unauthorized):

```python
import uhttp.client

client = uhttp.client.HttpClient('https://api.example.com')

# Large file upload with expect_continue
large_data = b'x' * 10_000_000  # 10 MB
response = client.post('/upload', data=large_data, expect_continue=True).wait()

if response.status == 413:
    print("Server rejected - body was NOT sent (bandwidth saved)")
else:
    print(f"Upload complete: {response.status}")

client.close()
```

How it works:
1. Client sends headers with `Expect: 100-continue`
2. Waits for server response
3. If server sends `100 Continue` → sends body → waits for final response
4. If server sends other status (413, 401, etc.) → returns that response (body not sent)


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

Three types of timeouts:

### Connect timeout

Time allowed for TCP connect + SSL handshake. Set via `connect_timeout` parameter (default: 10s).
When expired during connect/handshake phase, raises `HttpTimeoutError`.

```python
import uhttp.client

# Short connect timeout for fast-fail on unreachable hosts
client = uhttp.client.HttpClient('https://example.com', connect_timeout=3)

# Long connect timeout for slow 4G/satellite links
client = uhttp.client.HttpClient('https://example.com', connect_timeout=30)
```

### Request timeout

Total time allowed for the entire request (including connect). Set via `timeout` parameter on client or per-request.
When expired, raises `HttpTimeoutError` and closes connection.

```python
import uhttp.client

# Client-level timeout (default for all requests)
client = uhttp.client.HttpClient('https://example.com', timeout=30)

# Per-request timeout (overrides client default)
response = client.get('/slow', timeout=60).wait()
```

Both `connect_timeout` and `timeout` are checked during connect/handshake phases — whichever expires first triggers `HttpTimeoutError`.

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
- `client_stream.py` - Event-mode streaming (download-to-file, chunks, NDJSON)
- `client_with_server.py` - Combined server + client examples

Run examples from project root:
```bash
PYTHONPATH=./server:./client python examples/client_basic.py
```


## CLI Tool

After installing the package, `uhttp` command is available:

```bash
pip install uhttp-client
```

### Basic usage

```bash
# GET request (default)
uhttp https://httpbin.org/get

# POST with JSON data
uhttp https://httpbin.org/post -j '{"key": "value"}'

# POST with form data (method auto-detected from data)
uhttp https://httpbin.org/post -d "name=john&age=30"

# Explicit HTTP method
uhttp PUT https://httpbin.org/put -j '{"update": true}'
uhttp DELETE https://httpbin.org/delete
uhttp PATCH https://httpbin.org/patch -d "field=value"
```

### Options

```bash
# Custom headers
uhttp https://httpbin.org/get -H "Authorization: Bearer token"

# Save response to file
uhttp https://httpbin.org/image/png -o image.png

# Send file content
uhttp https://httpbin.org/post -f document.pdf

# JSON from file
uhttp https://httpbin.org/post -j @data.json

# Verbose mode (show headers and timing)
uhttp https://httpbin.org/get -v

# Skip SSL verification
uhttp https://self-signed.example.com -k

# Custom timeout
uhttp https://slow-api.example.com -t 60
```

### Method detection

- No data → `GET`
- With `-d`, `-j`, or `-f` → `POST`
- Explicit method before URL → uses that method

```bash
uhttp example.com/api           # GET
uhttp example.com/api -d "x=1"  # POST (auto)
uhttp GET example.com/api -d "" # GET (explicit, ignores data rule)
```

### Run without installation

```bash
python -m uhttp.cli https://httpbin.org/get
```

See `uhttp --help` for all options.


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

### MicroPython integration tests

Tests run on real ESP32 hardware via [mpytool](https://github.com/pavelrevak/mpytool).

**Configuration:**

1. WiFi credentials in `~/.config/uhttp/wifi.json`:
   ```json
   {"ssid": "MyWiFi", "password": "secret"}
   ```

2. Serial port via environment variable or mpytool config:
   ```bash
   # Environment variable
   export MPY_TEST_PORT=/dev/ttyUSB0

   # Or mpytool config
   echo "/dev/ttyUSB0" > ~/.config/mpytool/ESP32
   ```

**Run tests:**

```bash
MPY_TEST_PORT=/dev/ttyUSB0 ../.venv/bin/python -m unittest tests.test_mpy_integration -v
```

**Note:** MicroPython requires explicit `ssl_context` for HTTPS connections.

### CI

Tests run automatically on push/PR via GitHub Actions:
- Unit tests: Ubuntu + Windows, Python 3.10 + 3.14
- MicroPython tests: Self-hosted runner with ESP32
