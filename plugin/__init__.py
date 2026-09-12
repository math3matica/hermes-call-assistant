"""Hermes adapter for the separate, call-oriented voice chat runtime.

The adapter never overrides Hermes' built-in ``/voice``. Runtime activation is
explicit through the separate ``hermes voice-chat`` command; status remains
available as a safe in-session operation.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import argparse
from pathlib import Path
from typing import Any

from rex_voice_mode.boundary import request


def _setup_cli(parser: argparse.ArgumentParser) -> None:
    parser.description = "Start or inspect the separate voice chat runtime."
    parser.add_argument("--voice", action="store_true", help="use Hermes microphone/STT/TTS facilities")
    parser.add_argument("--manage-model", action="store_true", help="switch the specialized model through the configured supervisor")
    parser.add_argument("--topic", default="", help="initial prepared topic")
    parser.add_argument("--artifacts", help="profile-local runtime artifact root")


def _run_cli(args: argparse.Namespace) -> int:
    command = [sys.executable, "-m", "rex_voice_v1.launch"]
    for name in ("voice", "manage_model"):
        if getattr(args, name, False):
            command.append(f"--{name.replace('_', '-')}")
    for name in ("topic", "artifacts"):
        value = getattr(args, name, None)
        if value:
            command.extend((f"--{name}", value))
    return subprocess.run(command, check=False).returncode


def _socket() -> Path:
    value = os.environ.get("HERMES_VOICE_CHAT_SOCKET", os.environ.get("REX_VOICE_MODE_SOCKET"))
    if not value:
        raise RuntimeError("HERMES_VOICE_CHAT_SOCKET is required")
    return Path(value).expanduser()


def _status(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        return json.dumps(request(_socket(), "status", {}), ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error_code": "boundary_unavailable", "error": str(exc)})


def _command(raw_args: str) -> str:
    return _status({})


# Bundled Shared Knowledge / Prepared Briefing surfaces.
from rex_voice_v1.shared_knowledge import SharedKnowledgeError, SharedKnowledgeStore, configured_vault_root
import shlex

def _store() -> SharedKnowledgeStore:
    return SharedKnowledgeStore(configured_vault_root())


def _sources(store: SharedKnowledgeStore, values: list[str]) -> list[str]:
    result = []
    for value in values:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = store.root / path
        result.append(str(path))
    return result


def _publish(args: dict[str, Any], **_: Any) -> str:
    try:
        store = _store()
        result = store.publish_shared_knowledge(
            args["topic"], args["content"], category=args.get("category", "general"),
            sources=args.get("sources", []), confidence=args.get("confidence", "explicit"),
            overwrite_policy=args.get("overwrite_policy", "merge"), provenance=args.get("provenance"),
        )
        if args.get("prepare_for_voice"):
            packet = store.prepare_for_voice(
                args["topic"], [result["path"]], conversation_goal=args.get("conversation_goal"),
                expected_questions=args.get("expected_questions", []), depth=args.get("depth", "normal"),
                llm=getattr(args.get("_ctx"), "llm", None), provenance={"published_note": result["path"]},
            )
            result["prepared"] = packet
        return json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
    except (KeyError, SharedKnowledgeError, OSError, ValueError) as exc:
        return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)


def _prepare(args: dict[str, Any], **kw: Any) -> str:
    try:
        store = _store()
        source_values = args.get("sources", [])
        if not isinstance(source_values, list) or not source_values:
            raise SharedKnowledgeError("sources must contain at least one authorized path")
        result = store.prepare_for_voice(
            args["topic"], _sources(store, source_values), conversation_goal=args.get("conversation_goal"),
            expected_questions=args.get("expected_questions", []), depth=args.get("depth", "normal"),
            llm=getattr(args.get("_ctx"), "llm", None), provenance=args.get("provenance"),
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
    except (KeyError, SharedKnowledgeError, OSError, ValueError, TypeError) as exc:
        return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)


def _retrieve(args: dict[str, Any], **_: Any) -> str:
    try:
        return json.dumps(_store().retrieve_for_voice(args["query"], selected_topic=args.get("topic"), limit=args.get("limit", 3)), ensure_ascii=False, sort_keys=True)
    except (KeyError, SharedKnowledgeError, OSError, ValueError) as exc:
        return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)


def _cli(ctx: Any, raw: str) -> str:
    try:
        args = shlex.split(raw)
        store = _store()
        if not args or args[0] == "list":
            return json.dumps({"status": "ok", "packets": store.list_prepared()}, ensure_ascii=False, default=str)
        if args[0] == "show" and len(args) == 2:
            packet = store.resolve_topic(args[1])
            return json.dumps(packet or {"status": "missing", "topic": args[1]}, ensure_ascii=False, default=str)
        if args[0] == "stale":
            return json.dumps({"status": "ok", "packets": [item for item in store.list_prepared() if item.get("state") in {"stale", "invalid"}]}, ensure_ascii=False, default=str)
        if args[0] in {"prepare", "refresh"} and len(args) >= 3:
            return _prepare({"topic": args[1], "sources": args[2:], "_ctx": ctx})
        return "Usage: /voice-knowledge list | show <topic> | stale | prepare <topic> <authorized-source>..."
    except (SharedKnowledgeError, OSError, ValueError) as exc:
        return json.dumps({"status": "error", "error": str(exc)})


def _publish_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {"topic": {"type": "string"}, "content": {"type": "string"}, "category": {"type": "string", "enum": ["projects", "research", "decisions", "references", "general"]}, "sources": {"type": "array", "items": {"type": "string"}}, "confidence": {"type": "string", "enum": ["explicit", "supported", "uncertain"]}, "overwrite_policy": {"type": "string", "enum": ["create", "merge", "replace"]}, "prepare_for_voice": {"type": "boolean"}, "conversation_goal": {"type": "string"}, "expected_questions": {"type": "array", "items": {"type": "string"}}, "depth": {"type": "string", "enum": ["brief", "normal", "deep"]}}, "required": ["topic", "content"]}


def _prepare_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {"topic": {"type": "string"}, "sources": {"type": "array", "items": {"type": "string"}}, "conversation_goal": {"type": "string"}, "expected_questions": {"type": "array", "items": {"type": "string"}}, "depth": {"type": "string", "enum": ["brief", "normal", "deep"]}}, "required": ["topic", "sources"]}


def _retrieve_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {"query": {"type": "string"}, "topic": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 3}}, "required": ["query"]}


def _register_voice_knowledge(ctx: Any) -> None:
    def with_context(handler: Any):
        return lambda args, **kw: handler({**args, "_ctx": ctx}, **kw)
    ctx.register_tool(name="publish_shared_knowledge", toolset="voice_knowledge", schema=_publish_schema(), handler=with_context(_publish), emoji="📚")
    ctx.register_tool(name="prepare_for_voice", toolset="voice_knowledge", schema=_prepare_schema(), handler=with_context(_prepare), emoji="📞")
    ctx.register_tool(name="retrieve_shared_knowledge", toolset="voice_knowledge", schema=_retrieve_schema(), handler=with_context(_retrieve), emoji="🔎")
    ctx.register_command("voice-knowledge", lambda raw: _cli(ctx, raw), description="Inspect Shared Knowledge and prepared Voice briefings.", args_hint="list | show <topic> | stale | prepare <topic> <source>...")
    if hasattr(ctx, "register_cli_command"):
        def setup(parser: Any) -> None:
            parser.add_argument("operation", choices=("list", "show", "stale", "prepare", "refresh"))
            parser.add_argument("topic", nargs="?")
            parser.add_argument("sources", nargs="*")
        def handler(args: Any) -> None:
            raw = args.operation
            if args.topic:
                raw += " " + shlex.quote(args.topic)
            raw += " " + " ".join(shlex.quote(item) for item in args.sources)
            print(_cli(ctx, raw))
        ctx.register_cli_command(name="voice-knowledge", help="Manage Shared Knowledge and prepared Voice briefings.", setup_fn=setup, handler_fn=handler, description="Inspect and prepare user-authorized voice knowledge.")

def register(ctx) -> None:
    ctx.register_tool(name="voice_chat_status", toolset="voice_chat", schema={
        "description": "Read safe voice chat boundary status; never starts audio, calls, or models.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    }, handler=_status, check_fn=lambda: True, emoji="🎙️", capabilities=("voice.status",))
    ctx.register_command("voice-chat", handler=_command,
                         description="Show safe voice chat boundary status.", args_hint="status")
    ctx.register_cli_command(
        name="voice-chat",
        help="Start the separate call-oriented voice chat runtime",
        description="Explicitly launch the voice chat runtime for a telephone/call session; does not replace Hermes /voice.",
        setup_fn=_setup_cli,
        handler_fn=_run_cli,
    )
    _register_voice_knowledge(ctx)
