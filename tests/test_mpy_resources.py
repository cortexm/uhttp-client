#!/usr/bin/env python3
"""Portability and resource-use regressions that bite on MicroPython.

MicroPython's socket.recv(n) preallocates n bytes and some ports omit
errno names, so both the would-block handling and every recv/send size
have to stay bounded and defensive.
"""
import errno
import re
import unittest

from uhttp import client as uhttp_client


class FakeSocket:
    """Records the sizes asked of recv()/send() without any real I/O."""

    def __init__(self, send_accepts=None):
        self.recv_sizes = []
        self.send_sizes = []
        self._send_accepts = send_accepts

    def recv(self, size):
        self.recv_sizes.append(size)
        raise OSError(errno.EAGAIN, 'again')

    def send(self, data):
        self.send_sizes.append(len(data))
        if self._send_accepts is None:
            raise OSError(errno.EAGAIN, 'again')
        return self._send_accepts


class TestWouldBlock(unittest.TestCase):
    """Every would-block site must go through the portable predicate."""

    def test_no_site_reads_errno_ewouldblock_directly(self):
        with open(uhttp_client.__file__) as handle:
            source = handle.read()
        # The module-level fallback exists because some MicroPython ports
        # do not define EWOULDBLOCK; reading errno.EWOULDBLOCK anywhere
        # below it raises AttributeError on exactly those ports.
        below_fallback = source.split('EWOULDBLOCK = getattr', 1)[1]
        offenders = [
            line.strip() for line in below_fallback.splitlines()
            if 'errno.EWOULDBLOCK' in line]
        self.assertEqual(offenders, [], "must use the EWOULDBLOCK fallback")

    def test_predicate_accepts_the_no_progress_family(self):
        for name in ('EAGAIN', 'EWOULDBLOCK', 'EINPROGRESS', 'EALREADY'):
            value = getattr(errno, name, None)
            if value is None:
                continue
            self.assertTrue(
                uhttp_client._would_block(OSError(value, name)),
                f"{name} must count as would-block")

    def test_predicate_rejects_real_errors(self):
        for name in ('ECONNREFUSED', 'ECONNRESET', 'EPIPE'):
            value = getattr(errno, name, None)
            if value is None:
                continue
            self.assertFalse(
                uhttp_client._would_block(OSError(value, name)),
                f"{name} must not count as would-block")


class TestSliceDeletion(unittest.TestCase):
    """bytearray slice deletion is CPython-only.

    MicroPython implements slice assignment but not deletion, so
    `del buf[:n]` raises TypeError there while `buf[:] = buf[n:]` shrinks
    in place and keeps the object on both. No behavioural test can catch
    this - the construct is perfectly legal here - and the failure mode
    hides itself on-device: in the send path the bytes are already on the
    wire when the exception fires, so the peer sees a valid request and
    only the next operation reveals that the loop died.
    """

    def test_no_bytearray_slice_deletion(self):
        with open(uhttp_client.__file__) as handle:
            source = handle.read()
        offenders = [
            line.strip() for line in source.splitlines()
            if re.search(r'\bdel\s+[\w.]+\[[^\]]*:', line)]
        self.assertEqual(
            offenders, [], "slice deletion is not supported on MicroPython")


class TestBoundedRecv(unittest.TestCase):
    """recv() sizes must stay bounded regardless of Content-Length."""

    def test_body_recv_is_capped_to_the_chunk_size(self):
        client = uhttp_client.HttpClient('127.0.0.1', port=1)
        try:
            sock = FakeSocket()
            client._socket = sock
            client._state = uhttp_client.STATE_RECEIVING_BODY
            client._body_reader = uhttp_client._LengthBodyReader(5 * 1024 * 1024)
            client._recv_into_body()
            self.assertTrue(sock.recv_sizes)
            self.assertLessEqual(
                sock.recv_sizes[0], uhttp_client.BODY_CHUNK_SIZE,
                "a 5 MB Content-Length must not ask for a 5 MB buffer")
        finally:
            client._socket = None
            client.close()

    def test_header_recv_is_capped(self):
        client = uhttp_client.HttpClient('127.0.0.1', port=1)
        try:
            sock = FakeSocket()
            client._socket = sock
            client._state = uhttp_client.STATE_RECEIVING_HEADERS
            client._process_recv_headers()
            self.assertTrue(sock.recv_sizes)
            self.assertLessEqual(
                sock.recv_sizes[0], uhttp_client.BODY_CHUNK_SIZE)
        finally:
            client._socket = None
            client.close()


class StepSocket:
    """Accepts a scripted number of bytes per send(), then blocks."""

    def __init__(self, steps):
        self._steps = list(steps)
        self.sent = bytearray()

    def send(self, data):
        if not self._steps:
            raise OSError(errno.EAGAIN, 'again')
        chunk = bytes(data[:self._steps.pop(0)])
        self.sent.extend(chunk)
        return len(chunk)


class TestSendBufferConsumption(unittest.TestCase):
    """A partial send must not reallocate the remaining buffer."""

    def _sending_client(self, payload, steps):
        client = uhttp_client.HttpClient('127.0.0.1', port=1)
        client._socket = StepSocket(steps)
        client._state = uhttp_client.STATE_SENDING
        client._pending_body = None
        client._send_buffer = bytearray(payload)
        client._send_offset = 0
        return client

    def test_partial_send_leaves_the_remainder_in_place(self):
        # The consumed prefix is tracked by an offset, so a short send does
        # not touch the remaining bytes at all.
        client = self._sending_client(b'0123456789', [3])
        try:
            client._try_send()
            self.assertEqual(client._send_offset, 3)
            self.assertEqual(len(client._send_buffer), 10)
        finally:
            client._socket = None
            client.close()

    def test_send_resumes_at_the_offset(self):
        # Every byte must go out exactly once and in order across the
        # partial sends - an off-by-one in the memoryview slice would
        # repeat or drop bytes.
        client = self._sending_client(b'0123456789', [3, 4, 3])
        try:
            client._try_send()
            self.assertEqual(bytes(client._socket.sent), b'0123456789')
            self.assertEqual(client._send_offset, 0)
            self.assertEqual(bytes(client._send_buffer), b'')
        finally:
            client._socket = None
            client.close()

    def test_buffer_compacts_once_the_prefix_outgrows_the_remainder(self):
        # Compacting on every partial send is the O(n^2) this replaces;
        # compacting only past the halfway point keeps it amortised O(n).
        client = self._sending_client(b'0123456789', [4])
        try:
            client._try_send()
            self.assertEqual(len(client._send_buffer), 10)  # 4 < 6 left
            client._socket = StepSocket([2])
            client._try_send()
            self.assertEqual(bytes(client._send_buffer), b'6789')
            self.assertEqual(client._send_offset, 0)
        finally:
            client._socket = None
            client.close()

    def test_partial_send_keeps_the_same_buffer_object(self):
        client = uhttp_client.HttpClient('127.0.0.1', port=1)
        try:
            client._socket = FakeSocket(send_accepts=3)
            client._state = uhttp_client.STATE_SENDING
            client._pending_body = None
            client._send_buffer = bytearray(b'abcdefghij')
            buffer = client._send_buffer
            client._try_send()
            self.assertIs(
                client._send_buffer, buffer,
                "slicing the remainder reallocates on every partial send")
            self.assertEqual(bytes(client._send_buffer), b'')
        finally:
            client._socket = None
            client.close()


class TestRecordQueueBackPressure(unittest.TestCase):
    """Queued records must be delivered before pulling more off the wire."""

    def test_no_recv_while_records_are_pending(self):
        client = uhttp_client.HttpClient(
            '127.0.0.1', port=1, event_mode=True)
        try:
            sock = FakeSocket()
            client._socket = sock
            client._state = uhttp_client.STATE_RECEIVING_BODY
            client._body_reader = uhttp_client._EofBodyReader()
            client._accept_mode = 'record'
            client._record_decoder = uhttp_client._NdjsonDecoder()
            client._records = [{'a': 1}, {'b': 2}]
            client._process_body_streaming()
            self.assertEqual(
                sock.recv_sizes, [],
                "draining the queue must not keep reading more records")
        finally:
            client._socket = None
            client.close()


if __name__ == '__main__':
    unittest.main()
