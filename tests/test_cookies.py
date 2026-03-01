#!/usr/bin/env python3
"""
HTTP client cookies tests
"""
import unittest
import threading
import time
from uhttp import server as uhttp_server
from uhttp import client as uhttp_client


class TestClientCookies(unittest.TestCase):
    """Test cookie handling"""

    server = None
    server_thread = None
    PORT = 9906

    @classmethod
    def setUpClass(cls):
        cls.server = uhttp_server.HttpServer(port=cls.PORT)

        def run_server():
            try:
                while cls.server:
                    client = cls.server.wait(timeout=0.1)
                    if client:
                        if client.path == '/set-cookie':
                            client.respond(
                                {'status': 'ok'},
                                cookies={'session': 'abc123', 'user': 'john'}
                            )
                        elif client.path == '/get-cookies':
                            client.respond({'cookies': client.cookies})
                        elif client.path == '/set-single':
                            client.respond(
                                {'status': 'ok'},
                                cookies={'single': 'value'}
                            )
                        else:
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

    def test_receive_cookies(self):
        """Test cookies are received and stored"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        client.get('/set-cookie').wait()

        self.assertIn('session', client.cookies)
        self.assertEqual(client.cookies['session'], 'abc123')
        self.assertIn('user', client.cookies)
        self.assertEqual(client.cookies['user'], 'john')
        client.close()

    def test_send_cookies(self):
        """Test cookies are sent with requests"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)
        client._cookies = {'my_cookie': 'my_value'}
        response = client.get('/get-cookies').wait()

        cookies = response.json()['cookies']
        self.assertEqual(cookies.get('my_cookie'), 'my_value')
        client.close()

    def test_cookies_persist(self):
        """Test cookies persist across requests"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        # First request sets cookies
        client.get('/set-cookie').wait()
        self.assertIn('session', client.cookies)

        # Second request sends them back
        response = client.get('/get-cookies').wait()
        cookies = response.json()['cookies']
        self.assertEqual(cookies.get('session'), 'abc123')
        client.close()

    def test_cookies_accumulate(self):
        """Test new cookies are added to existing ones"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        # Set first cookie
        client.get('/set-single').wait()
        self.assertIn('single', client.cookies)

        # Set more cookies
        client.get('/set-cookie').wait()
        self.assertIn('single', client.cookies)
        self.assertIn('session', client.cookies)
        self.assertIn('user', client.cookies)

        client.close()

    def test_cookies_property(self):
        """Test cookies property access"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        # Initially empty
        self.assertEqual(client.cookies, {})

        # After receiving cookies
        client.get('/set-cookie').wait()
        self.assertIsInstance(client.cookies, dict)
        self.assertEqual(len(client.cookies), 2)

        client.close()

    def test_manual_cookie_setting(self):
        """Test manually setting cookies"""
        client = uhttp_client.HttpClient('127.0.0.1', port=self.PORT)

        # Set cookie manually
        client._cookies['manual'] = 'set_manually'

        response = client.get('/get-cookies').wait()
        cookies = response.json()['cookies']
        self.assertEqual(cookies.get('manual'), 'set_manually')

        client.close()


if __name__ == '__main__':
    unittest.main()
