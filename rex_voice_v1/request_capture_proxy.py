#!/usr/bin/env python3
"""Transparent local OpenAI-compatible proxy for Rex Voice qualification.

It records request message/tool structure and response metadata while forwarding
bytes unchanged. It is intended for local experiments only; API credentials are
redacted from captured headers and are never written to the log.
"""
from __future__ import annotations

import argparse
import http.client
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class CaptureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "rex-voice-capture/1"

    def log_message(self, format: str, *_args: object) -> None:
        del format
        return

    @property
    def capture_server(self) -> "CaptureServer":
        return self.server  # type: ignore[return-value]

    def _forward(self, body: bytes | None = None) -> None:
        target = urlsplit(self.capture_server.upstream)
        if target.scheme not in {"http", ""} or not target.hostname:
            raise RuntimeError(f"unsupported upstream: {self.capture_server.upstream}")
        connection = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=120)
        path = self.path
        if target.path:
            path = target.path.rstrip("/") + "/" + path.lstrip("/")
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"host", "content-length", "connection"}
        }
        if body is not None:
            headers["Content-Length"] = str(len(body))
        started = time.monotonic()
        connection.request(self.command, path, body=body, headers=headers)
        response = connection.getresponse()
        response_headers = {key: value for key, value in response.getheaders() if key.lower() not in {"connection", "transfer-encoding"}}
        self.send_response(response.status, response.reason)
        for key, value in response_headers.items():
            self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        captured = bytearray()
        total = 0
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if len(captured) < self.capture_server.max_response_bytes:
                captured.extend(chunk[: self.capture_server.max_response_bytes - len(captured)])
            self.wfile.write(chunk)
            self.wfile.flush()
        connection.close()
        record: dict[str, Any] = {
            "timestamp": time.time(),
            "method": self.command,
            "path": self.path,
            "status": response.status,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
            "request_bytes": len(body or b""),
            "response_bytes": total,
            "response_sample": captured.decode("utf-8", errors="replace"),
        }
        if body:
            try:
                request_json = json.loads(body)
                if isinstance(request_json, dict):
                    request_json.pop("api_key", None)
                    request_json.pop("apiKey", None)
                record["request"] = request_json
            except json.JSONDecodeError:
                record["request_parse_error"] = True
        self.capture_server.write_record(record)

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length > self.capture_server.max_request_bytes:
            self.send_error(413, "request too large")
            return
        self._forward(self.rfile.read(length))


class CaptureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], upstream: str, log_path: Path, max_request_bytes: int, max_response_bytes: int):
        super().__init__(address, CaptureHandler)
        self.upstream = upstream
        self.log_path = log_path
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def write_record(self, record: dict[str, Any]) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1:18082")
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--log", required=True, type=Path)
    args = parser.parse_args()
    host, port_text = args.listen.rsplit(":", 1)
    server = CaptureServer((host, int(port_text)), args.upstream, args.log, 8 * 1024 * 1024, 2 * 1024 * 1024)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
