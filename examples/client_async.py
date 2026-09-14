"""Non-blocking (async) HTTP client examples using selectors"""

import selectors

from uhttp.client import HttpClient


def example_single_async():
    """Single async request with manual select loop"""
    print("=== Single Async Request ===")

    client = HttpClient('httpbin.org', port=80)

    # Start request without blocking (async is default)
    client.get('/get', query={'mode': 'async'})
    print("Request started, waiting for response...")

    # Manual selector loop - handles connect, send, and receive
    done = False
    while not done:
        for key, mask in client.selector.select(10.0):
            if key.data.handle_event(key.fileobj, mask) is not None:
                response = client.response
                print(f"Response: status={response.status}")
                print(f"Data: {response.json()['args']}")
                done = True
        client.maintenance()   # deadline has no event to ride on

    client.close()


def example_parallel_requests():
    """Multiple clients working in parallel"""
    print("\n=== Parallel Requests ===")

    # One shared selector drives every client from a single loop
    selector = selectors.DefaultSelector()
    clients = [
        HttpClient('httpbin.org', port=80, selector=selector)
        for _ in range(3)
    ]

    # Start all requests (async is default)
    for i, client in enumerate(clients):
        client.get('/delay/1', query={'client': i})
        print(f"Client {i} request started")

    # Wait for all responses
    responses = {}
    while len(responses) < len(clients):
        for key, mask in selector.select(10.0):
            ready = key.data.handle_event(key.fileobj, mask)
            if ready is None:
                continue
            index = clients.index(ready)
            responses[index] = ready.response
            print(f"Client {index} done: status={ready.response.status}")
        for client in clients:
            client.maintenance()

    # Cleanup
    for client in clients:
        client.close()
    selector.close()

    print(f"All {len(responses)} requests completed in parallel")


def example_mixed_operations():
    """Async requests with different methods"""
    print("\n=== Mixed Async Operations ===")

    client = HttpClient('httpbin.org', port=80)

    operations = [
        ('GET', '/get', None),
        ('POST', '/post', {'action': 'create'}),
        ('PUT', '/put', {'action': 'update'}),
        ('DELETE', '/delete', None),
    ]

    for method, path, json_data in operations:
        client.request(method, path, json=json_data)  # async is default

        done = False
        while not done:
            for key, mask in client.selector.select(5.0):
                if key.data.handle_event(key.fileobj, mask) is not None:
                    print(f"{method} {path}: status={client.response.status}")
                    done = True
            client.maintenance()

    client.close()


def example_with_timeout_handling():
    """Handling timeouts in async mode"""
    print("\n=== Timeout Handling ===")

    client = HttpClient('httpbin.org', port=80)
    client.get('/delay/2')  # 2 second delay, async is default

    timeout_seconds = 5
    elapsed = 0

    while elapsed < timeout_seconds:
        events = client.selector.select(1.0)  # 1 second intervals

        if not events:
            elapsed += 1
            print(f"Waiting... {elapsed}s")
            continue

        for key, mask in events:
            if key.data.handle_event(key.fileobj, mask) is not None:
                print(f"Response received: status={client.response.status}")
                break
        else:
            continue
        break
    else:
        print("Request timed out!")

    client.close()


if __name__ == '__main__':
    example_single_async()
    example_parallel_requests()
    example_mixed_operations()
    example_with_timeout_handling()
