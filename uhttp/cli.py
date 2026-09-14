#!/usr/bin/env python3
"""uhttp - Simple HTTP client CLI

Usage:
    uhttp [METHOD] URL [options]

Examples:
    # GET request (default)
    uhttp http://httpbin.org/get

    # POST with JSON (auto-detected from data)
    uhttp http://httpbin.org/post -j '{"key": "value"}'

    # POST with raw data
    uhttp http://httpbin.org/post -d "name=john&age=30"

    # Explicit method
    uhttp PUT http://httpbin.org/put -j '{"key": "value"}'
    uhttp DELETE http://httpbin.org/delete

    # POST with file (binary)
    uhttp http://httpbin.org/post -f image.png

    # Custom headers
    uhttp http://httpbin.org/get -H "Authorization: Bearer token"

    # Save response to file
    uhttp http://httpbin.org/image/png -o image.png

    # Verbose mode
    uhttp http://httpbin.org/get -v
"""

import sys
import time
import argparse
import json
import ssl

from uhttp import client as _client


def parse_headers(header_list):
    """Parse header strings into dict"""
    headers = {}
    if header_list:
        for h in header_list:
            if ':' in h:
                key, val = h.split(':', 1)
                headers[key.strip()] = val.strip()
    return headers


def format_size(size):
    """Format size in bytes to human readable"""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


HTTP_METHODS = ('GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS')


def main():
    parser = argparse.ArgumentParser(
        description='Simple HTTP client CLI',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        'args', nargs='+', metavar='[METHOD] URL',
        help='HTTP method (optional) and URL')
    parser.add_argument(
        '-d', '--data',
        help='Send raw data')
    parser.add_argument(
        '-j', '--json',
        help='Send JSON data (string or @file.json)')
    parser.add_argument(
        '-f', '--file',
        help='Send file content as binary data')
    parser.add_argument(
        '-H', '--header', action='append',
        help='Add header (format: "Key: Value")')
    parser.add_argument(
        '-o', '--output',
        help='Write response body to file')
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Show headers and timing info')
    parser.add_argument(
        '-k', '--insecure', action='store_true',
        help='Skip SSL certificate verification')
    parser.add_argument(
        '-t', '--timeout', type=float, default=30,
        help='Request timeout in seconds (default: 30)')

    args = parser.parse_args()

    # Parse method and URL from positional args
    explicit_method = None
    if len(args.args) == 1:
        url_arg = args.args[0]
    elif len(args.args) == 2:
        if args.args[0].upper() in HTTP_METHODS:
            explicit_method = args.args[0].upper()
            url_arg = args.args[1]
        else:
            parser.error(f"Unknown method: {args.args[0]}")
    else:
        parser.error("Expected [METHOD] URL")

    # Parse URL
    url = url_arg if '://' in url_arg else 'http://' + url_arg
    try:
        host, port, path, use_ssl, auth = _client.parse_url(url)
    except Exception as e:
        print(f"Error parsing URL: {e}", file=sys.stderr)
        sys.exit(1)

    # Parse query from path
    query = None
    if '?' in path:
        path, query_str = path.split('?', 1)
        query = {}
        for part in query_str.split('&'):
            if '=' in part:
                k, v = part.split('=', 1)
                query[k] = v
            else:
                query[part] = None

    if not path:
        path = '/'

    # Parse headers
    headers = parse_headers(args.header)

    # Determine data to send
    data = None
    if args.json:
        if args.json.startswith('@'):
            json_file = args.json[1:]
            try:
                with open(json_file, 'r') as f:
                    data = json.load(f)
            except Exception as e:
                print(f"Error reading JSON file: {e}", file=sys.stderr)
                sys.exit(1)
        else:
            try:
                data = json.loads(args.json)
            except json.JSONDecodeError as e:
                print(f"Invalid JSON: {e}", file=sys.stderr)
                sys.exit(1)
    elif args.file:
        try:
            with open(args.file, 'rb') as f:
                data = f.read()
        except Exception as e:
            print(f"Error reading file: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.data:
        data = args.data

    # Determine method
    if explicit_method:
        method = explicit_method
    elif data is not None:
        method = 'POST'
    else:
        method = 'GET'

    # SSL context
    ssl_context = None
    if use_ssl:
        ssl_context = ssl.create_default_context()
        if args.insecure:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

    # Verbose output
    if args.verbose:
        print(
            f"* Connecting to {host}:{port}{' (SSL)' if use_ssl else ''}",
            file=sys.stderr)
        query_str = '?' + '&'.join(
            f'{k}={v}' for k, v in query.items()) if query else ''
        print(f"> {method} {path}{query_str} HTTP/1.1", file=sys.stderr)
        print(f"> Host: {host}", file=sys.stderr)
        for k, v in headers.items():
            print(f"> {k}: {v}", file=sys.stderr)
        if data:
            size = len(data) if isinstance(data, (bytes, str)) else '(json)'
            print(f"> Content-Length: {size}", file=sys.stderr)
        print(">", file=sys.stderr)

    # Make request
    start_time = time.time()

    try:
        client = _client.HttpClient(
            host, port=port, ssl_context=ssl_context,
            timeout=args.timeout, auth=auth
        )

        if isinstance(data, (dict, list)):
            response = client.request(
                method, path, headers=headers, query=query, json=data
            ).wait(timeout=args.timeout)
        else:
            response = client.request(
                method, path, headers=headers, query=query, data=data
            ).wait(timeout=args.timeout)

        elapsed = time.time() - start_time

        # Verbose response info
        if args.verbose:
            print(
                f"< HTTP/1.1 {response.status} {response.status_message}",
                file=sys.stderr)
            for k, v in response.headers.items():
                print(f"< {k}: {v}", file=sys.stderr)
            print("<", file=sys.stderr)
            print(f"* Time: {elapsed:.3f}s", file=sys.stderr)
            print(f"* Size: {format_size(len(response.data))}", file=sys.stderr)
            print("", file=sys.stderr)

        # Output
        if args.output:
            with open(args.output, 'wb') as f:
                f.write(response.data)
            if args.verbose:
                print(f"* Saved to {args.output}", file=sys.stderr)
        else:
            try:
                text = response.data.decode('utf-8')
                print(text)
            except UnicodeDecodeError:
                print(f"[Binary data: {format_size(len(response.data))}]")
                print("Use -o FILE to save binary data", file=sys.stderr)

        client.close()

        if response.status >= 400:
            sys.exit(1)

    except _client.HttpConnectionError as e:
        print(f"Connection error: {e}", file=sys.stderr)
        sys.exit(1)
    except _client.HttpTimeoutError as e:
        print(f"Timeout: {e}", file=sys.stderr)
        sys.exit(1)
    except _client.HttpResponseError as e:
        print(f"Response error: {e}", file=sys.stderr)
        sys.exit(1)
    except _client.HttpClientError as e:
        print(f"Client error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        sys.exit(130)


if __name__ == '__main__':
    main()
