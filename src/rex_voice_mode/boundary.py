"""Safe external boundary for Call Assistant mode.

This package owns only a small, private control-plane service. It deliberately
has no dial, hangup, recorder, playback, STT, TTS, or model-launch code.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

_ALLOWED = {"status", "start_session", "complete_session", "abort_session"}


class BoundaryState:
    def __init__(self) -> None:
        self.state = "idle"
        self.session_id: str | None = None
        self.transcript: str | None = None
        self._lock = threading.Lock()

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method not in _ALLOWED:
            return {"ok": False, "error_code": "operation_not_allowed", "error": "operation is not allowed at this boundary"}
        with self._lock:
            if method == "status":
                return {"ok": True, **self.status()}
            if method == "start_session":
                sid = params.get("session_id")
                if not isinstance(sid, str) or not sid or len(sid) > 64 or self.state != "idle":
                    return {"ok": False, "error_code": "invalid_session", "error": "session_id must be non-empty and boundary must be idle"}
                self.state, self.session_id, self.transcript = "active", sid, None
                return {"ok": True, **self.status()}
            if method in {"complete_session", "abort_session"}:
                if params.get("session_id") != self.session_id or self.state != "active":
                    return {"ok": False, "error_code": "unknown_session", "error": "no matching active session"}
                if method == "complete_session":
                    transcript = params.get("transcript")
                    if transcript is not None and not isinstance(transcript, str):
                        return {"ok": False, "error_code": "invalid_transcript", "error": "transcript must be a string"}
                    self.transcript = transcript
                    terminal = "completed"
                else:
                    terminal = "aborted"
                sid = self.session_id
                self.state, self.session_id = terminal, None
                return {"ok": True, "state": terminal, "session_id": sid}
        raise AssertionError("unreachable")

    def status(self) -> dict[str, Any]:
        return {"schema": "hermes-call-assistant-boundary-v1", "state": self.state, "audio": "not_started", "calls": "not_started", "core_shim": "required", **({"session_id": self.session_id} if self.session_id else {})}


class BoundaryServer:
    def __init__(self, path: Path, state: BoundaryState | None = None) -> None:
        self.path = Path(path)
        self.state = state or BoundaryState()
        self._stop = threading.Event()
        self._server: socket.socket | None = None

    def serve(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.1); probe.connect(str(self.path))
                raise RuntimeError(f"boundary already live: {self.path}")
            except ConnectionRefusedError:
                self.path.unlink()
            except FileNotFoundError:
                pass
        # AF_UNIX sockets are visible immediately after bind().  Use a umask
        # that creates the final private mode before the pathname is exposed;
        # chmod below remains an explicit invariant for unusual platforms.
        old_umask = os.umask(0o177)
        try:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._server = server
            try:
                server.bind(str(self.path))
                os.chmod(self.path, 0o600)
                server.listen(8)
            except OSError:
                if self._stop.is_set():
                    return
                raise
            server.settimeout(0.2)
            while not self._stop.is_set():
                try: conn, _ = server.accept()
                except socket.timeout: continue
                except OSError:
                    if self._stop.is_set(): break
                    raise
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        finally:
            os.umask(old_umask)
            if self._server: self._server.close()
            try: self.path.unlink()
            except FileNotFoundError: pass

    def _handle(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rwb") as stream:
            for line in stream:
                try:
                    raw = json.loads(line)
                    if not isinstance(raw, dict) or not isinstance(raw.get("method"), str) or not isinstance(raw.get("params", {}), dict):
                        raise ValueError("request must contain method and object params")
                    result = self.state.dispatch(raw["method"], raw.get("params", {}))
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    result = {"ok": False, "error_code": "invalid_request", "error": str(exc)}
                stream.write((json.dumps(result, separators=(",", ":")) + "\n").encode()); stream.flush()

    def close(self) -> None:
        self._stop.set()
        if self._server: self._server.close()
        try: self.path.unlink()
        except FileNotFoundError: pass


def request(path: Path, method: str, params: dict[str, Any], timeout: float = 2.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            sock.connect(str(path))
            break
        except ConnectionRefusedError:
            sock.close()
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
    with sock:
        sock.sendall((json.dumps({"method": method, "params": params}) + "\n").encode())
        line = b""
        while not line.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk: break
            line += chunk
    if not line: raise ConnectionError("boundary closed without response")
    return json.loads(line)
