from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

from .backend import CapabilityBackend, CapabilityError


class CapabilityBridge:
    """Strict JSONL Unix-socket bridge; one request gets one response."""

    METHODS = {"start_session", "capability", "complete_session", "abort_session", "prepared_context", "auto_capability"}

    def __init__(self, path: Path, backend: CapabilityBackend):
        self.path = Path(path)
        self.backend = backend
        self._stop = threading.Event()
        self._server: socket.socket | None = None
        self._registered: dict[str, Any] = {}

    def register_capability(self, name: str, handler: Any) -> None:
        """Register only a pipeline-approved handler; never accepts source code."""
        if not isinstance(name, str) or not name.isidentifier() or name.startswith("_") or not callable(handler):
            raise ValueError("invalid bridge capability registration")
        self._registered[name] = handler

    def serve(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Never unlink a live endpoint.  A crashed bridge can leave only the
        # filesystem node behind, so classify it by connection first.
        from .launch import cleanup_stale_socket, socket_is_live
        if socket_is_live(self.path):
            raise RuntimeError(f"capability bridge already live: {self.path}")
        cleanup_stale_socket(self.path)
        os.umask(0o077)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server = server
        server.bind(str(self.path))
        bound_inode = self.path.stat().st_ino
        os.chmod(self.path, 0o600)
        server.listen(8)
        server.settimeout(0.5)
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        finally:
            server.close()
            try:
                if self.path.stat().st_ino == bound_inode:
                    self.path.unlink()
            except FileNotFoundError:
                pass

    def close(self) -> None:
        self._stop.set()
        if self._server:
            self._server.close()
        # Closing the listener wakes serve(), but its cleanup runs on another
        # thread. Remove the filesystem endpoint immediately so callers that
        # join with a bounded timeout cannot mistake a stale socket for a
        # live bridge.
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            file = conn.makefile("rwb")
            for line in file:
                try:
                    request = json.loads(line)
                    response = self._dispatch(request)
                except CapabilityError as exc:
                    response = {"ok": False, "error": str(exc), "error_code": exc.code, **exc.details}
                except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
                    response = {"ok": False, "error": str(exc)}
                file.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
                file.flush()

    def _dispatch(self, request: object) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        method = request.get("method")
        if method not in self.METHODS:
            raise ValueError("method is not allowlisted")
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        if method == "start_session":
            result = self.backend.start_session(params["session_id"], topic=params.get("topic", ""))
            return {"ok": True, "session_id": result.session_id}
        sid = params["session_id"]
        if method == "capability":
            return self.backend.call(sid, params["operation"], params.get("arguments", {}))
        if method == "auto_capability":
            name = params.get("name")
            if not isinstance(name, str):
                raise CapabilityError("capability name must be a string")
            handler = self._registered.get(name)
            if handler is None:
                raise CapabilityError("capability is not registered")
            return {"ok": True, "name": name, "result": handler(params.get("arguments", {}))}
        if method == "complete_session":
            return self.backend.complete_session(sid, transcript=params.get("transcript"), final_topic=params.get("final_topic", ""))
        if method == "abort_session":
            return self.backend.store.abort(sid, error=params.get("error", "aborted"))
        return {"ok": True, "context": self.backend.store.prepared_context(sid)}


class BridgeClient:
    def __init__(self, path: Path, timeout: float = 10.0):
        self.path = str(path)
        self.timeout = timeout

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.timeout)
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    sock.connect(self.path)
                    break
                except ConnectionRefusedError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            sock.sendall(json.dumps({"method": method, "params": params}).encode() + b"\n")
            line = b""
            while not line.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                line += chunk
        if not line:
            raise ConnectionError("capability bridge closed without a response")
        response = json.loads(line)
        if not response.get("ok", False) and "error" in response:
            details = {
                key: value
                for key, value in response.items()
                if key not in {"ok", "error", "error_code"}
            }
            raise CapabilityError(
                response["error"],
                code=response.get("error_code", "capability_error"),
                details=details,
            )
        return response
