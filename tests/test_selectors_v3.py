#!/usr/bin/env python3
"""Specification tests for the selectors-based client event loop (v3).

v3 drops select.select(): the client registers its socket in a
selectors.BaseSelector and is driven either by its own wait() or by a
shared-selector loop dispatching through key.data.handle_event().
"""
import selectors
import socket
import threading
import time
import unittest

from uhttp import client as uhttp_client
from uhttp import server as uhttp_server
from uhttp.client import (
    EVENT_HEADERS, EVENT_DATA, EVENT_COMPLETE, EVENT_RESPONSE, EVENT_ERROR)


class RawServer:
    """Single connection raw TCP server sending fixed response fragments."""

    def __init__(self, fragments, delay=0.0):
        self._fragments = fragments
        self._delay = delay
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(('127.0.0.1', 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
            conn.recv(4096)
            for frag in self._fragments:
                conn.sendall(frag)
                if self._delay:
                    time.sleep(self._delay)
            try:
                conn.shutdown(socket.SHUT_WR)
                conn.settimeout(2.0)
                while conn.recv(4096):
                    pass
            except OSError:
                pass
            conn.close()
        except OSError:
            pass
        finally:
            self._sock.close()

    def stop(self):
        try:
            self._sock.close()
        except OSError:
            pass


class HangingServer:
    """Accepts the connection and never answers - no events ever arrive."""

    def __init__(self):
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(('127.0.0.1', 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._conns = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            while True:
                conn, _ = self._sock.accept()
                self._conns.append(conn)  # held open, never written to
        except OSError:
            pass

    def stop(self):
        for conn in self._conns:
            try:
                conn.close()
            except OSError:
                pass
        try:
            self._sock.close()
        except OSError:
            pass


class KeepAliveServer:
    """Serves keep-alive responses; drop_idle() acts as its idle timeout."""

    def __init__(self, keep_alive=None):
        body = b'{"key": "value"}'
        headers = [b'HTTP/1.1 200 OK',
                   b'Content-Type: application/json',
                   b'Content-Length: %d' % len(body),
                   b'Connection: keep-alive']
        if keep_alive:
            headers.append(b'Keep-Alive: ' + keep_alive.encode('ascii'))
        self._response = b'\r\n'.join(headers) + b'\r\n\r\n' + body
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(('127.0.0.1', 0))
        self._sock.listen(2)
        self.port = self._sock.getsockname()[1]
        self._lock = threading.Lock()
        self._conns = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            while True:
                conn, _ = self._sock.accept()
                with self._lock:
                    self._conns.append(conn)
                threading.Thread(
                    target=self._handle, args=(conn,), daemon=True).start()
        except OSError:
            pass

    def _handle(self, conn):
        try:
            while conn.recv(4096):
                conn.sendall(self._response)
        except OSError:
            pass

    def drop_idle(self):
        """Close held connections - simulates the server's idle timeout."""
        with self._lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        self.drop_idle()
        try:
            self._sock.close()
        except OSError:
            pass


def pump_until_closed(client, timeout=2.0):
    """Run the selector loop until the client drops its socket."""
    deadline = time.time() + timeout
    while client._socket is not None and time.time() < deadline:
        for key, mask in client.selector.select(0.05):
            key.data.handle_event(key.fileobj, mask)
    return client._socket is None


def send_request_then_idle(client, selector, path='/', timeout=2.0):
    """Start a request, pump the selector until it is out, then stop.

    Leaves the client waiting for a response that never comes, with no
    further selector activity - only maintenance() can notice the deadline.
    """
    client.get(path)
    deadline = time.time() + timeout
    while (client.state != uhttp_client.STATE_RECEIVING_HEADERS
            and time.time() < deadline):
        for key, mask in selector.select(0.05):
            key.data.handle_event(key.fileobj, mask)
    return client.state == uhttp_client.STATE_RECEIVING_HEADERS


def drive_selector(client, timeout=5.0, tick=0.1):
    """Shared-selector loop: dispatch via key.data.handle_event()."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.next():
            return client
        for key, mask in client.selector.select(tick):
            if key.data is not client:
                continue
            if client.handle_event(key.fileobj, mask) is not None:
                return client
    return None


OK_RESPONSE = (
    b'HTTP/1.1 200 OK\r\n'
    b'Content-Type: application/json\r\n'
    b'Content-Length: 16\r\n'
    b'Connection: close\r\n\r\n'
    b'{"key": "value"}')


class TestSelectorOwnership(unittest.TestCase):
    """The client owns a DefaultSelector unless one is injected."""

    def test_default_selector_is_owned_and_closed(self):
        client = uhttp_client.HttpClient('127.0.0.1', port=1)
        selector = client.selector
        self.assertIsNotNone(selector)
        client.close()
        # A closed selector drops its map (CPython marks it closed this way).
        self.assertIsNone(selector.get_map())

    def test_shared_selector_is_not_closed(self):
        selector = selectors.DefaultSelector()
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=1, selector=selector)
        self.assertIs(client.selector, selector)
        client.close()
        # Still usable: the client must not close a selector it does not own.
        self.assertEqual(selector.get_map(), {})
        selector.close()


class TestLegacySelectApiRemoved(unittest.TestCase):
    """v3 is a breaking change: the select.select() API is gone."""

    def test_select_attributes_removed(self):
        client = uhttp_client.HttpClient('127.0.0.1', port=1)
        try:
            for name in ('read_sockets', 'write_sockets', 'process_events'):
                self.assertFalse(
                    hasattr(client, name), f"{name} must be removed in v3")
        finally:
            client.close()


class TestRegistrationLifecycle(unittest.TestCase):
    """The client owns its selector registration and interest mask."""

    def _registered_fileobjs(self, client):
        return [k.fileobj for k in client.selector.get_map().values()]

    def test_request_registers_socket_with_self_as_data(self):
        server = RawServer([OK_RESPONSE])
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            client.get('/')
            self.assertIn(client._socket, self._registered_fileobjs(client))
            owners = [k.data for k in client.selector.get_map().values()]
            self.assertIn(client, owners)
            client.close()
        finally:
            server.stop()

    def test_close_unregisters(self):
        # Shared selector so the map stays inspectable after client.close().
        selector = selectors.DefaultSelector()
        server = RawServer([OK_RESPONSE])
        try:
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port, selector=selector)
            client.get('/')
            self.assertIsNotNone(client._interest)
            client.close()
            self.assertIsNone(client._interest)
            self.assertEqual(selector.get_map(), {})
        finally:
            server.stop()
            selector.close()

    def test_interest_is_read_while_receiving(self):
        server = RawServer([OK_RESPONSE], delay=0.2)
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            client.get('/')
            deadline = time.time() + 2.0
            while (client.state != uhttp_client.STATE_RECEIVING_HEADERS
                    and time.time() < deadline):
                client.selector.select(0.05)
                for key, mask in client.selector.select(0):
                    client.handle_event(key.fileobj, mask)
            self.assertEqual(client._interest, selectors.EVENT_READ)
            client.close()
        finally:
            server.stop()


class TestHandleEvent(unittest.TestCase):
    """handle_event() is the owner dispatch used by shared-selector loops."""

    def test_returns_self_and_sets_response_classic(self):
        server = RawServer([OK_RESPONSE])
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            client.get('/')
            self.assertIs(drive_selector(client), client)
            self.assertEqual(client.response.json(), {'key': 'value'})
            client.close()
        finally:
            server.stop()

    def test_returns_self_and_sets_event_in_event_mode(self):
        server = RawServer([OK_RESPONSE])
        try:
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port, event_mode=True)
            client.get('/')
            self.assertIs(drive_selector(client), client)
            self.assertEqual(client.event, EVENT_RESPONSE)
            self.assertEqual(client.response.json(), {'key': 'value'})
            client.close()
        finally:
            server.stop()


class TestNext(unittest.TestCase):
    """next() drains events already buffered from a single recv()."""

    def test_next_is_false_on_idle_client(self):
        client = uhttp_client.HttpClient('127.0.0.1', port=1)
        try:
            self.assertFalse(client.next())
        finally:
            client.close()

    def test_next_drains_buffered_ndjson_records(self):
        # All three records arrive in one segment; the selector reports
        # readable once, so next() must yield the remaining records.
        server = RawServer([
            b'HTTP/1.1 200 OK\r\n'
            b'Content-Type: application/x-ndjson\r\n'
            b'Connection: close\r\n\r\n'
            b'{"a": 1}\n{"b": 2}\n{"c": 3}\n'])
        records = []
        try:
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port, event_mode=True)
            client.get('/', stream=True)
            deadline = time.time() + 5.0
            done = False
            while not done and time.time() < deadline:
                ready = drive_selector(client, timeout=1.0)
                if ready is None:
                    break
                if client.event == EVENT_HEADERS:
                    client.accept_ndjson()
                elif client.event == EVENT_DATA:
                    records.append(client.read_record())
                elif client.event == EVENT_COMPLETE:
                    done = True
            self.assertTrue(done)
            self.assertEqual(records, [{'a': 1}, {'b': 2}, {'c': 3}])
            client.close()
        finally:
            server.stop()


class TestInterestFailure(unittest.TestCase):
    """A selector that cannot arm the interest must not leave a hung client."""

    def test_client_is_closed_when_arming_fails(self):
        server = RawServer([OK_RESPONSE])
        selector = selectors.DefaultSelector()
        try:
            def boom(*args, **kwargs):
                raise OSError("register failed")
            selector.register = boom

            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port, selector=selector)
            # Nothing could wake the client again, so it is closed and the
            # failure is reported rather than swallowed.
            with self.assertRaises(uhttp_client.HttpConnectionError):
                client.get('/')
            self.assertIsNone(client._socket)
            self.assertIsNone(client._interest)
        finally:
            selector.close()
            server.stop()


class TestSharedSelector(unittest.TestCase):
    """A shared selector means the caller drives the loop, not wait()."""

    def test_wait_refuses_a_shared_selector(self):
        # A blocking wait cannot service foreign ready keys, so it would
        # spin on them; handle_event() is the shared-loop entry point.
        selector = selectors.DefaultSelector()
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=1, event_mode=True, selector=selector)
        a, b = socket.socketpair()
        calls = []
        try:
            selector.register(
                a, selectors.EVENT_READ, lambda *args: calls.append(1))
            b.send(b'x')
            with self.assertRaises(uhttp_client.HttpClientError):
                client.wait(0.1)
            self.assertEqual(calls, [])
            self.assertIn(
                a, [k.fileobj for k in selector.get_map().values()])
        finally:
            a.close()
            b.close()
            client.close()
            selector.close()


class TestMaintenance(unittest.TestCase):
    """maintenance() enforces the deadline when no selector event ever fires.

    A shared-selector loop only calls handle_event() for ready keys, so a
    hung peer would otherwise leave the request pending forever.
    """

    def test_is_noop_when_idle(self):
        client = uhttp_client.HttpClient('127.0.0.1', port=1)
        try:
            self.assertIsNone(client.maintenance())
        finally:
            client.close()

    def test_is_noop_before_the_deadline(self):
        server = HangingServer()
        selector = selectors.DefaultSelector()
        try:
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port, event_mode=True,
                selector=selector, timeout=5)
            self.assertTrue(send_request_then_idle(client, selector))
            self.assertIsNone(client.maintenance())
            self.assertIsNotNone(client._socket)
            client.close()
        finally:
            selector.close()
            server.stop()

    def test_surfaces_timeout_as_event_error(self):
        server = HangingServer()
        selector = selectors.DefaultSelector()
        try:
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port, event_mode=True,
                selector=selector, timeout=0.2)
            self.assertTrue(send_request_then_idle(client, selector))
            time.sleep(0.25)  # past the request timeout, still no events
            self.assertIs(client.maintenance(), client)
            self.assertEqual(client.event, EVENT_ERROR)
            self.assertIsNotNone(client.error)
            self.assertIsNone(client._socket)
        finally:
            selector.close()
            server.stop()

    def test_raises_timeout_in_classic_mode(self):
        server = HangingServer()
        selector = selectors.DefaultSelector()
        try:
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port,
                selector=selector, timeout=0.2)
            self.assertTrue(send_request_then_idle(client, selector))
            time.sleep(0.25)
            with self.assertRaises(uhttp_client.HttpTimeoutError):
                client.maintenance()
            self.assertIsNone(client._socket)
        finally:
            selector.close()
            server.stop()

    def test_wait_event_reports_timeout_without_events(self):
        # Regression: wait() in event mode must not return None forever when
        # the selector never reports anything.
        server = HangingServer()
        try:
            client = uhttp_client.HttpClient(
                '127.0.0.1', port=server.port, event_mode=True, timeout=0.2)
            client.get('/')
            event = None
            deadline = time.time() + 3.0
            while event is None and time.time() < deadline:
                event = client.wait(0.05)
            self.assertEqual(event, EVENT_ERROR)
            self.assertIsNotNone(client.error)
            client.close()
        finally:
            server.stop()


class TestIdleKeepAlive(unittest.TestCase):
    """A kept-alive socket must notice the peer closing it while idle.

    Without this the stale-connection window is the whole idle period: the
    next request would always fail on a long-dead socket.
    """

    def test_idle_socket_stays_armed_for_read(self):
        server = KeepAliveServer()
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            self.assertEqual(client.get('/').wait().status, 200)
            self.assertEqual(client.state, uhttp_client.STATE_IDLE)
            self.assertIsNotNone(client._socket)  # kept alive
            self.assertEqual(client._interest, selectors.EVENT_READ)
            client.close()
        finally:
            server.stop()

    def test_peer_close_while_idle_drops_the_socket(self):
        server = KeepAliveServer()
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            client.get('/').wait()
            self.assertIsNotNone(client._socket)

            server.drop_idle()  # the server's idle timeout fires

            self.assertTrue(pump_until_closed(client))
            self.assertIsNone(client._interest)
            client.close()
        finally:
            server.stop()

    def test_idle_close_yields_no_result_to_the_caller(self):
        server = KeepAliveServer()
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            client.get('/').wait()
            server.drop_idle()

            deadline = time.time() + 2.0
            while client._socket is not None and time.time() < deadline:
                for key, mask in client.selector.select(0.05):
                    # Cleaning up a dead idle socket is not a "result".
                    self.assertIsNone(
                        key.data.handle_event(key.fileobj, mask))
            self.assertIsNone(client._socket)
            client.close()
        finally:
            server.stop()

    def test_next_request_reconnects_cleanly(self):
        server = KeepAliveServer()
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            client.get('/').wait()
            server.drop_idle()
            self.assertTrue(pump_until_closed(client))

            # Fresh connection, no stale-socket error.
            self.assertEqual(client.get('/again').wait().status, 200)
            client.close()
        finally:
            server.stop()


class TestKeepAliveHint(unittest.TestCase):
    """The server's `Keep-Alive: timeout=, max=` hint is honoured.

    Closing slightly before the server does means the next request never
    races the server's close.
    """

    def test_hint_is_parsed(self):
        server = KeepAliveServer(keep_alive='timeout=5, max=100')
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            client.get('/').wait()
            self.assertEqual(client._keep_alive_timeout, 5)
            self.assertEqual(client._keep_alive_max, 100)
            client.close()
        finally:
            server.stop()

    def test_connection_is_kept_before_the_hinted_timeout(self):
        server = KeepAliveServer(keep_alive='timeout=5')
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            client.get('/').wait()
            self.assertIsNone(client.maintenance())
            self.assertIsNotNone(client._socket)
            client.close()
        finally:
            server.stop()

    def test_connection_is_closed_after_the_hinted_timeout(self):
        server = KeepAliveServer(keep_alive='timeout=5')
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            client.get('/').wait()
            client._idle_since -= 10  # pretend it sat idle past the hint
            client.maintenance()
            self.assertIsNone(client._socket)
            client.close()
        finally:
            server.stop()

    def test_without_a_hint_the_connection_is_kept(self):
        server = KeepAliveServer()
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            client.get('/').wait()
            client._idle_since -= 3600
            client.maintenance()
            self.assertIsNotNone(client._socket)
            client.close()
        finally:
            server.stop()

    def test_max_requests_hint_closes_the_connection(self):
        server = KeepAliveServer(keep_alive='timeout=30, max=2')
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            client.get('/one').wait()
            client.maintenance()
            self.assertIsNotNone(client._socket)  # 1 of 2 used
            client.get('/two').wait()
            client.maintenance()
            self.assertIsNone(client._socket)  # budget exhausted
            client.close()
        finally:
            server.stop()

    def test_hint_is_reset_on_a_fresh_connection(self):
        server = KeepAliveServer(keep_alive='timeout=30, max=1')
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            client.get('/one').wait()
            client.maintenance()
            self.assertIsNone(client._socket)  # max=1 exhausted
            # The reconnect starts a new budget, not a carried-over one.
            client.get('/two').wait()
            self.assertEqual(client._requests_on_connection, 1)
            client.close()
        finally:
            server.stop()


class TestStaleConnectionRetry(unittest.TestCase):
    """A reused connection that died unused is replayed once on a new socket.

    Covers the race that cannot be designed away: the server closes an idle
    kept-alive connection exactly as the client sends the next request on it.
    """

    def test_reused_dead_connection_is_retried(self):
        server = KeepAliveServer()
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            self.assertEqual(client.get('/one').wait().status, 200)
            first = client._socket

            server.drop_idle()  # closed; the client never gets to notice

            self.assertEqual(client.get('/two').wait().status, 200)
            self.assertIsNot(client._socket, first)  # reconnected
            client.close()
        finally:
            server.stop()

    def test_fresh_connection_failure_is_not_retried(self):
        # Nothing listening: a brand-new connection must fail, not replay.
        client = uhttp_client.HttpClient('127.0.0.1', port=59997)
        try:
            with self.assertRaises(uhttp_client.HttpClientError):
                client.get('/').wait()
            self.assertFalse(client._retried_stale)
        finally:
            client.close()

    def test_non_idempotent_method_is_not_retried(self):
        server = KeepAliveServer()
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            client.get('/one').wait()
            server.drop_idle()
            # The server may already have processed a POST before closing.
            with self.assertRaises(uhttp_client.HttpConnectionError):
                client.post('/two', json={'a': 1}).wait()
            client.close()
        finally:
            server.stop()

    def test_retry_gives_up_when_the_server_is_gone(self):
        server = KeepAliveServer()
        client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
        try:
            client.get('/one').wait()
            server.stop()  # listener gone too: the replay cannot succeed
            with self.assertRaises(uhttp_client.HttpClientError):
                client.get('/two').wait()
        finally:
            client.close()

    def test_connection_reuse_is_tracked(self):
        server = KeepAliveServer()
        try:
            client = uhttp_client.HttpClient('127.0.0.1', port=server.port)
            client.get('/one').wait()
            self.assertFalse(client._connection_reused)  # freshly opened
            client.get('/two').wait()
            self.assertTrue(client._connection_reused)  # kept-alive reuse
            client.close()
        finally:
            server.stop()


class TestSharedLoopWithServer(unittest.TestCase):
    """One selector drives an HttpServer and an HttpClient together."""

    PORT = 9975

    def test_server_and_client_on_one_selector(self):
        selector = selectors.DefaultSelector()
        server = uhttp_server.HttpServer(port=self.PORT, selector=selector)
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=self.PORT, selector=selector)
        try:
            client.get('/ping')
            response = None
            deadline = time.time() + 5.0
            while response is None and time.time() < deadline:
                for key, mask in selector.select(0.1):
                    ready = key.data.handle_event(key.fileobj, mask)
                    if ready is None:
                        continue
                    if isinstance(ready, uhttp_server.HttpConnection):
                        ready.respond({'pong': True})
                    elif ready is client:
                        response = client.response
                server.maintenance()
            self.assertIsNotNone(response, "no response over shared selector")
            self.assertEqual(response.json(), {'pong': True})
        finally:
            client.close()
            server.close()
            selector.close()


if __name__ == '__main__':
    unittest.main()
