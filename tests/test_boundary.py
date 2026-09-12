import argparse
import importlib.util
import threading
import time
from pathlib import Path

from rex_voice_mode.boundary import BoundaryServer, request

_plugin_spec = importlib.util.spec_from_file_location("rex_voice_plugin", Path(__file__).parents[1] / "plugin" / "__init__.py")
_plugin = importlib.util.module_from_spec(_plugin_spec)
assert _plugin_spec.loader is not None
_plugin_spec.loader.exec_module(_plugin)
register = _plugin.register


def running_server(tmp_path: Path):
    path = tmp_path / "voice.sock"
    server = BoundaryServer(path)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists()
    return server, thread, path


def test_status_is_safe_and_reports_core_shim(tmp_path):
    server, thread, path = running_server(tmp_path)
    try:
        result = request(path, "status", {})
        assert result == {"ok": True, "schema": "hermes-call-assistant-boundary-v1", "state": "idle", "audio": "not_started", "calls": "not_started", "core_shim": "required"}
    finally:
        server.close(); thread.join(timeout=2)


def test_session_lifecycle_is_durable_in_server_only(tmp_path):
    server, thread, path = running_server(tmp_path)
    try:
        assert request(path, "start_session", {"session_id": "s1"})["state"] == "active"
        assert request(path, "status", {})["session_id"] == "s1"
        assert request(path, "complete_session", {"session_id": "s1", "transcript": "hello"})["state"] == "completed"
        assert request(path, "status", {})["state"] == "completed"
    finally:
        server.close(); thread.join(timeout=2)


def test_unsafe_or_unknown_operations_fail_closed(tmp_path):
    server, thread, path = running_server(tmp_path)
    try:
        result = request(path, "dial", {"number": "+15551212"})
        assert result["ok"] is False
        assert result["error_code"] == "operation_not_allowed"
    finally:
        server.close(); thread.join(timeout=2)


def test_endpoint_is_private(tmp_path):
    server, thread, path = running_server(tmp_path)
    try:
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        server.close(); thread.join(timeout=2)


def test_hermes_adapter_uses_public_non_overriding_surfaces():
    class Context:
        def __init__(self): self.tools, self.commands, self.cli_commands = [], [], []
        def register_tool(self, **kwargs): self.tools.append(kwargs)
        def register_command(self, *args, **kwargs): self.commands.append((args, kwargs))
        def register_cli_command(self, **kwargs): self.cli_commands.append(kwargs)
    ctx = Context()
    register(ctx)
    assert [tool["name"] for tool in ctx.tools] == ["call_assistant_status", "publish_shared_knowledge", "prepare_for_voice", "retrieve_shared_knowledge"]
    assert ctx.tools[0]["schema"]["parameters"]["additionalProperties"] is False
    assert ctx.commands[0][0][0] == "call-assistant"
    assert [command["name"] for command in ctx.cli_commands] == ["call-assistant", "call-knowledge"]


def test_plugin_registration_is_inert_and_never_claims_builtin_voice(monkeypatch):
    """Installing/loading the adapter must not start a call or touch Hermes /voice."""
    calls = []
    monkeypatch.setattr(_plugin.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    class Context:
        def __init__(self): self.tools, self.commands, self.cli_commands = [], [], []
        def register_tool(self, **kwargs): self.tools.append(kwargs)
        def register_command(self, *args, **kwargs): self.commands.append((args, kwargs))
        def register_cli_command(self, **kwargs): self.cli_commands.append(kwargs)

    ctx = Context()
    register(ctx)
    names = [tool["name"] for tool in ctx.tools]
    names += [args[0] for args, _kwargs in ctx.commands]
    names += [command["name"] for command in ctx.cli_commands]
    assert names == ["call_assistant_status", "publish_shared_knowledge", "prepare_for_voice", "retrieve_shared_knowledge", "call-assistant", "call-knowledge", "call-assistant", "call-knowledge"]
    assert "/voice" not in names
    assert calls == []


def test_call_voice_cli_dispatches_existing_runtime_without_voice_namespace(monkeypatch):
    ctx_command = None
    call_voice_command = None

    class Context:
        def register_tool(self, **_kwargs): pass
        def register_command(self, *_args, **_kwargs): pass
        def register_cli_command(self, **kwargs):
            nonlocal ctx_command, call_voice_command
            ctx_command = kwargs
            if kwargs["name"] == "call-assistant":
                call_voice_command = kwargs

    register(Context())
    parser = argparse.ArgumentParser()
    call_voice_command["setup_fn"](parser)
    args = parser.parse_args(["--manage-model", "--topic", "calls"])
    captured = []
    monkeypatch.setattr(_plugin.subprocess, "run", lambda argv, check: captured.append((argv, check)) or type("Result", (), {"returncode": 0})())
    assert call_voice_command["handler_fn"](args) == 0
    assert captured == [([_plugin.sys.executable, "-m", "rex_voice_v1.launch", "--manage-model", "--topic", "calls"], False)]
