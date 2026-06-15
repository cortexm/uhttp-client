"""Event-mode streaming examples (event_mode=True).

In event mode wait()/process_events() return EVENT_* constants instead of an
HttpResponse, mirroring uhttp-server's HttpConnection event API. After
EVENT_HEADERS you choose how the body is delivered with an accept_body*()
variant.
"""

import select
from uhttp.client import (
    HttpClient,
    EVENT_RESPONSE, EVENT_HEADERS, EVENT_DATA, EVENT_COMPLETE, EVENT_ERROR)


def example_small_response():
    """Small response arrives as a single EVENT_RESPONSE"""
    print("=== Small response (EVENT_RESPONSE) ===")

    client = HttpClient('httpbin.org', port=80, event_mode=True)
    client.get('/get', query={'mode': 'event'})

    while True:
        r, w, _ = select.select(
            client.read_sockets, client.write_sockets, [], 10.0)
        event = client.process_events(r, w)

        if event == EVENT_RESPONSE:
            # Completed body: read it as a full HttpResponse (reuses .json())
            print(f"status={client.status} type={client.content_type}")
            print("args:", client.response.json().get('args'))
            break
        elif event == EVENT_ERROR:
            print("error:", client.error)
            break

    client.close()


def example_download_to_file():
    """Large download streamed straight to disk (low RAM)"""
    print("\n=== Download to file (accept_body_to_file) ===")

    client = HttpClient('httpbin.org', port=80, event_mode=True)
    client.get('/bytes/4096')

    while True:
        r, w, _ = select.select(
            client.read_sockets, client.write_sockets, [], 10.0)
        event = client.process_events(r, w)

        if event == EVENT_HEADERS:
            print("downloading", client.content_length, "bytes")
            client.accept_body_to_file('/tmp/uhttp_download.bin')
        elif event == EVENT_COMPLETE:
            print("done,", client.bytes_received, "bytes written")
            break
        elif event == EVENT_ERROR:
            print("error:", client.error)
            break

    client.close()


def example_stream_chunks():
    """Process the body chunk-by-chunk (accept_body_streaming)"""
    print("\n=== Stream chunks (accept_body_streaming) ===")

    total = 0
    client = HttpClient('httpbin.org', port=80, event_mode=True)
    client.get('/stream-bytes/8192')

    while True:
        r, w, _ = select.select(
            client.read_sockets, client.write_sockets, [], 10.0)
        event = client.process_events(r, w)

        if event == EVENT_HEADERS:
            client.accept_body_streaming()
        elif event == EVENT_DATA:
            chunk = client.read_buffer()
            total += len(chunk)
        elif event == EVENT_COMPLETE:
            print("streamed", total, "bytes")
            break
        elif event == EVENT_ERROR:
            print("error:", client.error)
            break

    client.close()


def example_ndjson():
    """Newline-delimited JSON: one decoded record per EVENT_DATA"""
    print("\n=== NDJSON stream (accept_ndjson) ===")

    # stream=True reads until close when there is no Content-Length/chunked.
    client = HttpClient('httpbin.org', port=80, event_mode=True)
    client.get('/stream/5', stream=True)

    count = 0
    while True:
        r, w, _ = select.select(
            client.read_sockets, client.write_sockets, [], 30.0)
        event = client.process_events(r, w)

        if event == EVENT_HEADERS:
            client.accept_ndjson()
        elif event == EVENT_DATA:
            record = client.read_record()   # already a decoded object
            count += 1
            print(f"record {count}: id={record.get('id')}")
        elif event == EVENT_COMPLETE:
            print("received", count, "records")
            break
        elif event == EVENT_ERROR:
            print("error:", client.error)
            break

    client.close()


if __name__ == '__main__':
    example_small_response()
    example_download_to_file()
    example_stream_chunks()
    example_ndjson()
