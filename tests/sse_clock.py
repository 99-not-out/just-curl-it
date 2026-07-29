#!/usr/bin/env python3
"""Self-contained SSE clock for the streaming test.

Stdlib only -- the test runs this without the demos venv. Reads (and
ignores) the HTTP request, then emits 5 SSE events with sleeps between
so a non-streaming consumer (one that buffers the whole response)
collapses the timing into a single burst at the end.
"""
import json
import signal
import sys
import time


def drain_request() -> None:
    buf = b""
    while not buf.endswith(b"\r\n\r\n"):
        b = sys.stdin.buffer.read(1)
        if not b:
            return
        buf += b


def main() -> None:
    drain_request()
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    out = sys.stdout.buffer
    out.write(b"HTTP/1.1 200 OK\r\n")
    out.write(b"Content-Type: text/event-stream\r\n")
    out.write(b"Cache-Control: no-cache\r\n")
    out.write(b"X-Handled-By: jci-sse-clock\r\n")
    out.write(b"Connection: close\r\n\r\n")
    out.flush()

    for n in range(5):
        out.write(f"data: {json.dumps({'tick': n})}\n\n".encode())
        out.flush()
        time.sleep(0.5)


if __name__ == "__main__":
    main()
