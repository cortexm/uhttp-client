"""MicroPython integration tests for uhttp-client

Run on PC, executes tests on ESP32 via mpytool.

Configuration (in order of priority):
    1. Environment variables:
        MPY_TEST_PORT      - Serial port (e.g., /dev/ttyUSB0)
        MPY_WIFI_SSID      - WiFi SSID
        MPY_WIFI_PASSWORD  - WiFi password

    2. Config files:
        ~/.config/uhttp/wifi.json       - local config
        ~/actions-runner/.config/uhttp/wifi.json  - CI runner

        Format: {"ssid": "MyWiFi", "password": "secret"}

    3. Port auto-detection from mpytool config:
        ~/.config/mpytool/ESP32
        ~/actions-runner/.config/mpytool/ESP32

Run tests:
    # With env vars
    MPY_TEST_PORT=/dev/ttyUSB0 MPY_WIFI_SSID=MyWiFi python -m unittest tests.test_mpy_integration -v

    # With config file
    python -m unittest tests.test_mpy_integration -v
"""

import json
import os
import unittest
from pathlib import Path


def _load_config():
    """Load configuration from env vars and config files"""
    config = {
        'port': os.environ.get('MPY_TEST_PORT'),
        'ssid': os.environ.get('MPY_WIFI_SSID'),
        'password': os.environ.get('MPY_WIFI_PASSWORD'),
    }

    # Config file paths
    home = Path.home()
    wifi_paths = [
        home / '.config' / 'uhttp' / 'wifi.json',
        home / 'actions-runner' / '.config' / 'uhttp' / 'wifi.json',
    ]
    port_paths = [
        home / '.config' / 'mpytool' / 'ESP32',
        home / 'actions-runner' / '.config' / 'mpytool' / 'ESP32',
    ]

    # Load WiFi from config file
    if not config['ssid']:
        for path in wifi_paths:
            if path.exists():
                try:
                    data = json.loads(path.read_text())
                    config['ssid'] = data.get('ssid')
                    config['password'] = data.get('password', '')
                    break
                except (json.JSONDecodeError, IOError):
                    pass

    # Load port from mpytool config
    if not config['port']:
        for path in port_paths:
            if path.exists():
                try:
                    config['port'] = path.read_text().strip()
                    break
                except IOError:
                    pass

    # Default password to empty string
    if config['password'] is None:
        config['password'] = ''

    return config


# Load configuration
_config = _load_config()
PORT = _config['port']
WIFI_SSID = _config['ssid']
WIFI_PASSWORD = _config['password']


def requires_device(cls):
    """Skip tests if device not configured"""
    if not PORT:
        return unittest.skip("MPY_TEST_PORT not set")(cls)
    if not WIFI_SSID:
        return unittest.skip("MPY_WIFI_SSID not set")(cls)
    return cls


# Minimum free RAM for HTTPS tests (SSL needs ~35KB + fragmentation buffer)
MIN_RAM_FOR_HTTPS = 200000


class MpyTestCase(unittest.TestCase):
    """Base class for MicroPython tests"""

    mpy = None
    conn = None
    mount_handler = None
    wifi_connected = False

    @classmethod
    def setUpClass(cls):
        import mpytool
        from pathlib import Path

        cls.conn = mpytool.ConnSerial(port=PORT, baudrate=115200)
        cls.mpy = mpytool.Mpy(cls.conn)
        cls.mpy.stop()

        # Mount uhttp client module
        client_dir = Path(__file__).parent.parent / 'uhttp'
        cls.mount_handler = cls.mpy.mount(str(client_dir), mount_point='/lib/uhttp')

        # Connect WiFi once
        if not cls.wifi_connected:
            cls._connect_wifi()
            cls.wifi_connected = True

    @classmethod
    def tearDownClass(cls):
        if cls.mpy:
            cls.mpy.stop()
        if cls.conn:
            cls.conn.close()

    @classmethod
    def _connect_wifi(cls):
        """Connect ESP32 to WiFi"""
        code = f"""
import network
import time

wlan = network.WLAN(network.STA_IF)
wlan.active(True)

if not wlan.isconnected():
    wlan.connect({repr(WIFI_SSID)}, {repr(WIFI_PASSWORD)})
    for _ in range(30):
        if wlan.isconnected():
            break
        time.sleep(0.5)

if wlan.isconnected():
    print('WIFI_OK:', wlan.ifconfig()[0])
else:
    print('WIFI_FAIL')
"""
        result = cls.mpy.comm.exec(code, timeout=30)
        if b'WIFI_FAIL' in result:
            raise RuntimeError("WiFi connection failed")

    def run_on_device(self, code, timeout=30):
        """Run code on device and return output"""
        full_code = "import sys; sys.path.insert(0, '/lib')\n" + code
        return self.mpy.comm.exec(full_code, timeout=timeout).decode('utf-8')

    @classmethod
    def get_free_memory(cls):
        """Get free memory on device"""
        result = cls.mpy.comm.exec_raw_paste(
            "import gc; gc.collect(); print(gc.mem_free())", timeout=5)
        return int(result.strip())

    def assertDeviceResult(self, code, expected, timeout=30):
        """Run code and check result contains expected string"""
        result = self.run_on_device(code, timeout)
        self.assertIn(expected, result, f"Expected '{expected}' in output:\n{result}")


@requires_device
class TestHTTP(MpyTestCase):
    """Test HTTP requests"""

    def test_http_get(self):
        """Test basic HTTP GET"""
        code = """
from uhttp.client import HttpClient
client = HttpClient('http://httpbin.org')
try:
    response = client.get('/get').wait()
    print('STATUS:', response.status)
    data = response.json()
    print('HAS_HEADERS:', 'headers' in data)
finally:
    client.close()
"""
        result = self.run_on_device(code)
        self.assertIn('STATUS: 200', result)
        self.assertIn('HAS_HEADERS: True', result)

    def test_http_post_json(self):
        """Test HTTP POST with JSON"""
        code = """
from uhttp.client import HttpClient
client = HttpClient('http://httpbin.org')
try:
    payload = {'name': 'micropython', 'value': 42}
    response = client.post('/post', json=payload).wait()
    print('STATUS:', response.status)
    data = response.json()
    print('JSON_MATCH:', data['json'] == payload)
finally:
    client.close()
"""
        result = self.run_on_device(code)
        self.assertIn('STATUS: 200', result)
        self.assertIn('JSON_MATCH: True', result)

    def test_http_query_params(self):
        """Test HTTP GET with query parameters"""
        code = """
from uhttp.client import HttpClient
client = HttpClient('http://httpbin.org')
try:
    response = client.get('/get', query={'foo': 'bar', 'num': '123'}).wait()
    print('STATUS:', response.status)
    data = response.json()
    print('FOO:', data['args'].get('foo'))
    print('NUM:', data['args'].get('num'))
finally:
    client.close()
"""
        result = self.run_on_device(code)
        self.assertIn('STATUS: 200', result)
        self.assertIn('FOO: bar', result)
        self.assertIn('NUM: 123', result)

    def test_http_custom_headers(self):
        """Test HTTP with custom headers"""
        code = """
from uhttp.client import HttpClient
client = HttpClient('http://httpbin.org')
try:
    response = client.get('/headers', headers={'X-Custom': 'esp32-test'}).wait()
    print('STATUS:', response.status)
    data = response.json()
    print('HEADER:', data['headers'].get('X-Custom'))
finally:
    client.close()
"""
        result = self.run_on_device(code)
        self.assertIn('STATUS: 200', result)
        self.assertIn('HEADER: esp32-test', result)


@requires_device
class TestHTTPS(MpyTestCase):
    """Test HTTPS requests (requires sufficient RAM for SSL)"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        free_mem = cls.get_free_memory()
        if free_mem < MIN_RAM_FOR_HTTPS:
            raise unittest.SkipTest(
                f"Not enough RAM for HTTPS: {free_mem} < {MIN_RAM_FOR_HTTPS}")

    SSL_SETUP = """
import ssl
ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
"""

    def test_https_get(self):
        """Test basic HTTPS GET"""
        code = self.SSL_SETUP + """
from uhttp.client import HttpClient
client = HttpClient('https://httpbin.org', ssl_context=ssl_ctx)
try:
    response = client.get('/get').wait()
    print('STATUS:', response.status)
    data = response.json()
    print('HAS_HEADERS:', 'headers' in data)
finally:
    client.close()
"""
        result = self.run_on_device(code, timeout=60)
        self.assertIn('STATUS: 200', result)
        self.assertIn('HAS_HEADERS: True', result)

    def test_https_post_json(self):
        """Test HTTPS POST with JSON"""
        code = self.SSL_SETUP + """
from uhttp.client import HttpClient
client = HttpClient('https://httpbin.org', ssl_context=ssl_ctx)
try:
    payload = {'secure': True, 'platform': 'esp32'}
    response = client.post('/post', json=payload).wait()
    print('STATUS:', response.status)
    data = response.json()
    print('JSON_MATCH:', data['json'] == payload)
finally:
    client.close()
"""
        result = self.run_on_device(code, timeout=60)
        self.assertIn('STATUS: 200', result)
        self.assertIn('JSON_MATCH: True', result)

    def test_https_keep_alive(self):
        """Test HTTPS with multiple requests on same connection"""
        code = self.SSL_SETUP + """
from uhttp.client import HttpClient
client = HttpClient('https://httpbin.org', ssl_context=ssl_ctx)
try:
    r1 = client.get('/get', query={'req': '1'}).wait()
    r2 = client.get('/get', query={'req': '2'}).wait()
    r3 = client.get('/get', query={'req': '3'}).wait()
    print('R1:', r1.status)
    print('R2:', r2.status)
    print('R3:', r3.status)
finally:
    client.close()
"""
        result = self.run_on_device(code, timeout=90)
        self.assertIn('R1: 200', result)
        self.assertIn('R2: 200', result)
        self.assertIn('R3: 200', result)

    def test_https_binary(self):
        """Test HTTPS binary response"""
        code = self.SSL_SETUP + """
from uhttp.client import HttpClient
client = HttpClient('https://httpbin.org', ssl_context=ssl_ctx)
try:
    response = client.get('/bytes/100').wait()
    print('STATUS:', response.status)
    print('LENGTH:', len(response.data))
finally:
    client.close()
"""
        result = self.run_on_device(code, timeout=60)
        self.assertIn('STATUS: 200', result)
        self.assertIn('LENGTH: 100', result)


@requires_device
class TestHTTPMethods(MpyTestCase):
    """Test HTTP methods and status codes (uses HTTPS)"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        free_mem = cls.get_free_memory()
        if free_mem < MIN_RAM_FOR_HTTPS:
            raise unittest.SkipTest(
                f"Not enough RAM for HTTPS: {free_mem} < {MIN_RAM_FOR_HTTPS}")

    SSL_SETUP = """
import ssl
ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
"""

    def test_status_404(self):
        """Test 404 Not Found"""
        code = self.SSL_SETUP + """
from uhttp.client import HttpClient
client = HttpClient('https://httpbin.org', ssl_context=ssl_ctx)
try:
    response = client.get('/status/404').wait()
    print('STATUS:', response.status)
finally:
    client.close()
"""
        result = self.run_on_device(code, timeout=60)
        self.assertIn('STATUS: 404', result)

    def test_put_method(self):
        """Test PUT request"""
        code = self.SSL_SETUP + """
from uhttp.client import HttpClient
client = HttpClient('https://httpbin.org', ssl_context=ssl_ctx)
try:
    response = client.put('/put', json={'key': 'value'}).wait()
    print('STATUS:', response.status)
    data = response.json()
    print('JSON:', data['json'])
finally:
    client.close()
"""
        result = self.run_on_device(code, timeout=60)
        self.assertIn('STATUS: 200', result)
        self.assertIn("'key': 'value'", result)

    def test_delete_method(self):
        """Test DELETE request"""
        code = self.SSL_SETUP + """
from uhttp.client import HttpClient
client = HttpClient('https://httpbin.org', ssl_context=ssl_ctx)
try:
    response = client.delete('/delete').wait()
    print('STATUS:', response.status)
finally:
    client.close()
"""
        result = self.run_on_device(code, timeout=60)
        self.assertIn('STATUS: 200', result)


if __name__ == '__main__':
    unittest.main()
