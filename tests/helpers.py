#!/usr/bin/env python3
"""Shared fixtures for the client tests.

Keeping the raw TCP servers in one place matters: the graceful-close
sequence below (FIN then linger) is what stops Windows from RST-ing away a
trailing response fragment, and a per-file copy would silently lose it.
"""
import os
import socket
import ssl
import threading
import time

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
CERT_FILE = os.path.join(TESTS_DIR, 'test_cert.pem')
KEY_FILE = os.path.join(TESTS_DIR, 'test_key.pem')
SSL_AVAILABLE = os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)


def server_ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT_FILE, KEY_FILE)
    return ctx


def client_ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def graceful_close(conn):
    """Close so every sent byte is delivered first.

    A plain close() can send a RST that discards in-flight data on Windows;
    shutdown() sends a FIN after the data and the linger loop waits for the
    peer to close, so the OS flushes everything first.
    """
    try:
        conn.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    try:
        conn.settimeout(2.0)
        while conn.recv(4096):
            pass
    except OSError:
        pass
    try:
        conn.close()
    except OSError:
        pass


class _ListenerThread:
    """Binds an ephemeral port and serves accepted connections in a thread."""

    def __init__(self, backlog=2):
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(('127.0.0.1', 0))
        self._sock.listen(backlog)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        raise NotImplementedError

    def stop(self):
        try:
            self._sock.close()
        except OSError:
            pass


class RawServer(_ListenerThread):
    """Answers with fixed response fragments, optionally spaced by delay."""

    def __init__(self, fragments, delay=0.0, requests=1, close=True):
        self._fragments = fragments
        self._delay = delay
        self._requests = requests
        self._close = close
        self.received = b''
        super().__init__(backlog=1)

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
            for _ in range(self._requests):
                self.received += conn.recv(4096)
                for fragment in self._fragments:
                    conn.sendall(fragment)
                    if self._delay:
                        time.sleep(self._delay)
            if self._close:
                graceful_close(conn)
            else:
                time.sleep(0.5)
                conn.close()
        except OSError:
            pass
        finally:
            self.stop()


class KeepAliveServer(_ListenerThread):
    """Keep-alive responder; drop_idle() acts as its idle timeout."""

    def __init__(self, keep_alive=None, body=b'{"key": "value"}'):
        # keep_alive: hint string for every response, or a list with one
        # entry per response (None = no header on that response).
        self._body = body
        self._hints = (keep_alive if isinstance(keep_alive, list)
                       else [keep_alive])
        self._lock = threading.Lock()
        self._conns = []
        super().__init__()

    def _response(self, index):
        headers = [b'HTTP/1.1 200 OK',
                   b'Content-Type: application/json',
                   b'Content-Length: %d' % len(self._body),
                   b'Connection: keep-alive']
        hint = self._hints[min(index, len(self._hints) - 1)]
        if hint:
            headers.append(b'Keep-Alive: ' + hint.encode('ascii'))
        return b'\r\n'.join(headers) + b'\r\n\r\n' + self._body

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
        index = 0
        try:
            while conn.recv(4096):
                conn.sendall(self._response(index))
                index += 1
        except OSError:
            pass

    def drop_idle(self):
        """Close held connections - the server's idle timeout firing."""
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
        super().stop()


class HangingServer(_ListenerThread):
    """Accepts connections and never answers."""

    def __init__(self):
        self._conns = []
        super().__init__()

    def _serve(self):
        try:
            while True:
                conn, _ = self._sock.accept()
                self._conns.append(conn)
        except OSError:
            pass

    def stop(self):
        for conn in self._conns:
            try:
                conn.close()
            except OSError:
                pass
        super().stop()
