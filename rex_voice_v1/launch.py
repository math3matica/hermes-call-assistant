from __future__ import annotations

import argparse
import errno
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .backend import CapabilityBackend
from .bridge import CapabilityBridge
from .workspace import RexVoiceWorkspace
from .hermes_backend import RexVaultAdapter
from .post_call import PostCallQueue, enqueue_closed_session
from .post_call_worker import start_background, start_detached
from .store import VoiceSessionStore
from .protocol import MODEL_CAPABILITIES


def socket_is_live(path: Path) -> bool:
    """Check AF_UNIX liveness; a leftover socket file is not evidence of life."""
    if not path.exists():
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        probe.connect(str(path))
        return True
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ECONNREFUSED, errno.ENOTSOCK}:
            return False
        return True
    finally:
        probe.close()


def cleanup_stale_socket(path: Path) -> bool:
    """Remove one endpoint only when its owner is definitively absent."""
    path = Path(path)
    if not path.exists() or socket_is_live(path):
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def cleanup_stale_sockets(directory: Path | None = None, store_root: Path | None = None, session_db: Any | None = None) -> list[Path]:
    """Remove abandoned endpoints and reconcile only their active sessions."""
    root = Path(directory or tempfile.gettempdir())
    artifacts = Path(store_root or _env("HERMES_VOICE_CHAT_ARTIFACTS", "REX_VOICE_ARTIFACTS", str(Path.home() / ".hermes/cache/hermes-voice-chat"))).expanduser()
    removed: list[Path] = []
    for pattern in ("hermes-voice-chat-*.sock", "rex-v1-rex-voice-*.sock"):
        for path in root.glob(pattern):
            if cleanup_stale_socket(path):
                removed.append(path)
                session_id = path.name.removesuffix(".sock").removeprefix("rex-v1-").removeprefix("hermes-voice-chat-")
                VoiceSessionStore(artifacts, session_db=session_db).reconcile_abandoned(session_id)
    return removed

PACKAGE_ROOT = Path(__file__).resolve().parent
def _env(name: str, legacy: str, default: str = "") -> str:
    return os.getenv(name, os.getenv(legacy, default))


PI = Path(_env("HERMES_VOICE_CHAT_PI", "REX_VOICE_PI", str(Path.home() / ".local/bin/pi")))
SUPERVISOR = Path(_env("HERMES_VOICE_CHAT_SUPERVISOR", "REX_VOICE_SUPERVISOR")) if _env("HERMES_VOICE_CHAT_SUPERVISOR", "REX_VOICE_SUPERVISOR") else None
SYSTEM_PROMPT = """You are Voice Chat, a document-aware voice conversation frontend. Be concise and natural, but use the capabilities below when the request requires real work.

DOCUMENT CAPABILITY: You can create, read, revise, rename, append to, and precisely edit documents. Never claim that you lack document capability merely because the generic write_file tool is absent. This runtime intentionally does not expose generic filesystem tools. Document work is performed through the configured document store capabilities: use resource_manage to create, open, rename, or inspect durable notes; use resource_read to read the active note; use resource_mutate only for validated edits to an existing active note.

DRAFTING RULE: Use draft_manage for composition and revision. EVERY draft_manage call must include the operation field. For creation, call exactly {operation: "create", content: "..."}; never send content alone. CREATE ONLY OPENS A DRAFT: it never saves, promotes, or implies approval. While an unpromoted draft is active, every request to read the current draft, read it back, ask what the draft says, or read the document before saving MUST call exactly {operation: "read", draft_id: "EXACT_ACTIVE_DRAFT_ID"}. Do not answer from context, and do not use resource_manage or resource_read for an unpromoted draft. After create, revise zero or more times, read/inspect as needed, and wait for explicit approval in a later current user turn. Only then may you call {operation: "promote", draft_id: "EXACT_RETURNED_ID", target: "document name"}; target must be non-empty. A revision invalidates prior approval. Do not pretend a draft was saved or promoted without a successful capability result followed by resource_manage operation=open and resource_read verification.

CONTINUITY RULE: Use the prepared working context supplied with each turn and the structured session state returned by capabilities. It contains the active resource, active drafts, last read region, assignments, retrieval handles, unresolved questions, and continuation points. Maintain continuity across turns by referring to that state, not by inventing filenames, draft IDs, or previous operations. The configured document store is the durable document store; drafts and session state are the working memory for this conversation.

CAPABILITY RULE: The available capabilities are retrieval, resource_manage, resource_read, resource_mutate, draft_manage, and assignment_capture. Use retrieval only when quick notes and the active prepared topic do not contain the requested detail. MEMORY RETRIEVAL RULE: If quick notes or the prepared topic contain the answer, answer directly; if authoritative stored notes are needed, call retrieval; questions such as "what did we decide" and "what do my notes say" use retrieval when the detail is not prepared; use resource_manage/resource_read only when the user names a saved document and asks for its exact or full text; never claim to search, check, look something up, or consult notes unless retrieval actually executes in this turn; if retrieval returns no useful result, say the stored notes did not provide the detail; do not invent missing stored information. For note mode, it searches a few bounded chunks from authoritative notes, preferring active-topic source references and widening once to the approved full-note corpus only when needed. It returns source paths, sections, and handles; do not ask for or invent source paths. Do not use current_web for ordinary memory questions. Use resource_manage {operation: "open", target: "SAVED_RESOURCE"} followed by resource_read when the user asks for an exact or full persisted note. Use draft_manage {operation: "read", draft_id: "EXACT_ACTIVE_DRAFT_ID"} for an active unpromoted draft. Use assignment_capture for explicit follow-up work. Capability results are authoritative; assistant prose alone is not proof that an operation happened. In acceptance mode, an explicit draft-read request is unsatisfied unless a successful draft_manage read result was returned.

RESOURCE RULE: resource_read never opens or switches the active resource. Before reading or explaining any named saved note that is not already active, call resource_manage operation=open with that note as target, specifically {operation: "open", target: "SAVED_RESOURCE"}, then call resource_read. Do not call resource_manage with an active draft ID; do not call resource_read for an unpromoted draft.

When the user asks to create or save a document, do not refuse. If the request is explicitly an assignment or asks the voice agent to do work after the call, assignment_capture is authoritative and must be the first capability call, even when the requested output is a document. Otherwise choose the draft_manage → approval → promote workflow, then use resource_manage/resource_read to verify the durable configured document store note. When the user explicitly asks to hang up, end the call, or stop the phone conversation, call phone_hangup and do not continue the conversation.

ASSIGNMENT ACKNOWLEDGEMENT RULE: After assignment_capture returns successfully, acknowledge that the assignment was captured or queued. Do not promise that you will notify the user when it is complete unless the authoritative result explicitly has notify_on_completion=true and a configured notification channel. If notification is disabled or unspecified, say nothing about a future notification."""


def _compact_event_value(value: object, limit: int = 240) -> str:
    rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."


def _tool_result_summary(event: dict[str, Any]) -> str:
    result = event.get("result")
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        try:
            payload = json.loads(str(item.get("text", "")))
        except json.JSONDecodeError:
            return ""
        if not isinstance(payload, dict):
            return ""
        keys = ("error_code", "error", "resource_id", "draft_id", "assignment_id", "version", "status")
        fields = [f"{key}={payload[key]}" for key in keys if key in payload and not isinstance(payload[key], (dict, list))]
        return " " + " ".join(fields) if fields else ""
    return ""


_PSEUDO_TOOL_TEXT = re.compile(
    r"(?:^|\n)\s*\{\s*[\"']?operation[\"']?\s*:\s*[\"']?"
    r"(?:create|read|revise|discard|promote|open|active|rename|append|replace)\b",
    re.IGNORECASE,
)

_UNFULFILLED_RETRIEVAL_NARRATION = re.compile(
    r"\b(?:"
    r"(?:i|we)(?:\s+(?:will|am\s+going\s+to)|['’]ll)\s+"
    r"(?:search|check|look\s+(?:that|this|it)\s+up|consult|look\s+in)"
    r"|let\s+me\s+(?:search|check|consult)"
    r"|i\s+need\s+to\s+(?:search|check|look\s+(?:that|this|it)\s+up|consult)"
    r")\b[^.?!\n]{0,120}\b(?:notes?|memory|stored|previous\s+decisions?|what\s+we\s+decided|context)\b",
    re.IGNORECASE,
)


def _looks_like_pseudo_tool_text(text: str) -> bool:
    """Recognize operation-shaped assistant prose, never a native tool call."""
    return bool(_PSEUDO_TOOL_TEXT.search(text))


def _looks_like_unfulfilled_retrieval_narration(text: str) -> bool:
    """Recognize a claimed note/memory lookup, not uncertainty or an offer."""
    return bool(_UNFULFILLED_RETRIEVAL_NARRATION.search(text))


class PseudoToolTextError(RuntimeError):
    """The model emitted operation-shaped prose instead of a native tool call."""


class UnfulfilledRetrievalNarrationError(RuntimeError):
    """The model claimed a note/memory lookup without native retrieval execution."""


def _boundary_telemetry(event: dict[str, Any]) -> str | None:
    event_type = event.get("type")
    nested = event.get("assistantMessageEvent", {}) if event_type == "message_update" else {}
    if isinstance(nested, dict):
        nested_type = nested.get("type")
        if nested_type == "toolcall_end":
            call = nested.get("toolCall")
            if isinstance(call, dict) and isinstance(call.get("name"), str) and call["name"]:
                return "MODEL_RESPONSE_KIND kind=tool_call"
        if nested_type == "text_end":
            return "MODEL_RESPONSE_KIND kind=text"
    if event_type == "toolcall_end":
        call = event.get("toolCall")
        name = call.get("name") if isinstance(call, dict) else event.get("toolName")
        if isinstance(name, str) and name:
            return "MODEL_RESPONSE_KIND kind=tool_call"
    if event_type == "tool_execution_start":
        return f"TOOL_DISPATCH_REQUESTED name={event.get('toolName', 'unknown')}"
    if event_type == "tool_execution_end":
        name = event.get("toolName", "unknown")
        if event.get("isError"):
            return f"TOOL_DISPATCH_FAILED name={name}"
        return f"TOOL_DISPATCH_EXECUTED name={name}"
    return None


def _format_pi_event(event: dict[str, Any]) -> str | None:
    """Format authoritative execution telemetry for the human operator."""
    event_type = event.get("type")
    if event_type == "agent_start":
        return "[Pi] agent started"
    if event_type == "agent_end":
        return f"[Pi] agent ended (will_retry={event.get('willRetry')})"
    if event_type == "agent_settled":
        return "[Pi] turn settled"
    if event_type == "tool_execution_start":
        tool = str(event.get("toolName", "unknown"))
        return f"[Hermes] capability requested: {tool} {_compact_event_value(event.get('args', {}))}"
    if event_type == "tool_execution_end":
        tool = str(event.get("toolName", "unknown"))
        outcome = "FAILED" if event.get("isError") else "OK"
        return f"[Hermes] capability result: {tool} {outcome}{_tool_result_summary(event)}"
    if event_type == "message_update":
        nested = event.get("assistantMessageEvent", {})
        if isinstance(nested, dict) and nested.get("type") == "text_end":
            text = str(nested.get("content", "")).strip()
            if text:
                if len(text) > 500:
                    text = text[:497] + "..."
                return f"[Pi] assistant: {text} (not evidence of execution)"
    if event_type == "extension_ui_request" and event.get("method") == "notify":
        return f"[Pi status] {event.get('message', '')}"
    return None


def _pi_agent_error(event: dict[str, Any]) -> str | None:
    """Return a provider error carried by a terminal Pi agent_end event."""
    messages = event.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        error = message.get("errorMessage")
        if isinstance(error, str) and error.strip():
            return error.strip()
        if message.get("stopReason") == "error":
            return "provider returned stopReason=error"
    return None


class PiRpc:
    def __init__(self, process: subprocess.Popen[str], log_path: Path, events_path: Path | None = None, observer: Callable[[str], None] | None = None):
        self.process = process
        self.log_path = log_path
        self.events_path = events_path
        self.observer = observer
        self.counter = 0
        if self.observer is not None:
            self.observer(f"MODEL_TOOLS_EXPOSED names=[{','.join(MODEL_CAPABILITIES)}]")

    def _command(self, command: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        """Send a non-prompt RPC command and return its response."""
        assert self.process.stdin and self.process.stdout
        self.process.stdin.write(json.dumps(command) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([self.process.stdout], [], [], min(0.5, remaining))
            if not ready:
                continue
            line = self.process.stdout.readline()
            if not line:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("id") == command["id"]:
                if not event.get("success", False):
                    raise RuntimeError(str(event.get("error", "Pi RPC command failed")))
                return event
        raise TimeoutError(f"timed out waiting for Pi RPC command {command['type']}")

    def fork_before_rejected_response(self, detector: Callable[[str], bool]) -> str:
        """Fork at the rejected user turn, excluding its invalid assistant child."""
        response = self._command({"id": "rex-fork-tree", "type": "get_tree"})
        entries: list[dict[str, Any]] = []

        def collect(nodes: list[dict[str, Any]]) -> None:
            for node in nodes:
                entry = node.get("entry")
                if isinstance(entry, dict):
                    entries.append(entry)
                children = node.get("children", [])
                if isinstance(children, list):
                    collect(children)

        collect(response.get("data", {}).get("tree", []))
        rejected_user_id: str | None = None
        for entry in entries:
            message = entry.get("message")
            content = message.get("content", []) if isinstance(message, dict) else []
            text = "".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
            if isinstance(message, dict) and message.get("role") == "assistant" and detector(text):
                parent_id = entry.get("parentId")
                if isinstance(parent_id, str) and parent_id:
                    rejected_user_id = parent_id
        if rejected_user_id is None:
            raise RuntimeError("cannot clean retry context: rejected assistant turn not found")
        self._command({"id": "rex-fork-clean", "type": "fork", "entryId": rejected_user_id})
        return rejected_user_id

    def fork_before_last_pseudo_tool(self) -> str:
        """Backward-compatible wrapper for pseudo-tool corrective retries."""
        return self.fork_before_rejected_response(_looks_like_pseudo_tool_text)

    def prompt(self, message: str, timeout: float = 180.0, cancel_event: threading.Event | None = None) -> str:
        self.counter += 1
        native_tool_call_seen = False
        retrieval_executed = False
        request_id = f"prompt-{self.counter}"
        assert self.process.stdin and self.process.stdout
        self.process.stdin.write(json.dumps({"id": request_id, "type": "prompt", "message": message}) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        parts: list[str] = []
        display_parts: list[str] = []
        agent_started = False
        terminal_fallback: str | None = None
        fallback_deadline: float | None = None

        def settled_text() -> str:
            return_code = self.process.poll()
            if return_code not in (None, 0):
                raise RuntimeError(f"Pi exited after final response with status {return_code}")
            text = "".join(parts).strip()
            if _looks_like_pseudo_tool_text(text):
                if self.observer is not None:
                    self.observer("TOOL_DISPATCH_REJECTED reason=pseudo_tool_text")
                raise PseudoToolTextError("model emitted pseudo-tool text; native structured tool call required")
            if _looks_like_unfulfilled_retrieval_narration(text) and not retrieval_executed:
                if self.observer is not None:
                    self.observer("TOOL_DISPATCH_REJECTED reason=unfulfilled_retrieval_narration")
                raise UnfulfilledRetrievalNarrationError(
                    "model claimed a stored-note lookup; native retrieval was not executed"
                )
            return text

        def flush_display() -> None:
            if not display_parts or self.observer is None:
                display_parts.clear()
                return
            text = "".join(display_parts).strip()
            display_parts.clear()
            if text:
                self.observer(f"[Pi] assistant: {text} (not evidence of execution)")

        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                flush_display()
                raise InterruptedError("Pi turn cancelled")
            now = time.monotonic()
            if terminal_fallback is not None and fallback_deadline is not None and now >= fallback_deadline:
                flush_display()
                return_code = self.process.poll()
                if return_code not in (None, 0):
                    raise RuntimeError(f"Pi exited after final response with status {return_code}")
                return settled_text()
            remaining = max(0.1, deadline - now)
            if fallback_deadline is not None:
                remaining = min(remaining, max(0.0, fallback_deadline - now))
            ready, _, _ = select.select([self.process.stdout], [], [], min(1.0, remaining))
            if not ready:
                if terminal_fallback is not None:
                    flush_display()
                    return settled_text()
                if self.process.poll() is not None:
                    raise RuntimeError("Pi exited during prompt")
                continue
            line = self.process.stdout.readline()
            if not line:
                if terminal_fallback is not None:
                    flush_display()
                    return settled_text()
                raise RuntimeError("Pi closed RPC output")
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if self.events_path is not None:
                with self.events_path.open("a", encoding="utf-8") as events_file:
                    events_file.write(json.dumps(event, ensure_ascii=False) + "\n")
            if event.get("type") == "tool_execution_end" and event.get("toolName") == "retrieval" and not event.get("isError"):
                retrieval_executed = True
            if self.observer is not None:
                telemetry = _boundary_telemetry(event)
                if telemetry == "MODEL_RESPONSE_KIND kind=tool_call":
                    native_tool_call_seen = True
                if telemetry is not None and not (
                    telemetry == "MODEL_RESPONSE_KIND kind=text" and native_tool_call_seen
                ):
                    self.observer(telemetry)
                if event.get("type") == "tool_execution_start":
                    flush_display()
                formatted = _format_pi_event(event)
                if formatted is not None:
                    self.observer(formatted)
            if event.get("type") == "agent_start":
                agent_started = True
            elif event.get("type") == "message_update":
                nested = event.get("assistantMessageEvent", {})
                if nested.get("type") == "text_delta":
                    delta = str(nested.get("delta", ""))
                    parts.append(delta)
                    display_parts.append(delta)
                elif nested.get("type") == "text_end":
                    display_parts.clear()
                elif (
                    agent_started
                    and nested.get("type") == "thinking_end"
                    and parts
                    and terminal_fallback is None
                ):
                    # Some llama.cpp/Pi combinations finish the HTTP stream
                    # with [DONE] after text deltas but omit Pi's text_end,
                    # message_end, and agent settlement events. The completed
                    # thinking_end is the last event in the captured failure
                    # shape; use a bounded grace window rather than waiting
                    # for the full turn timeout.
                    terminal_fallback = "".join(parts).strip()
                    fallback_deadline = time.monotonic() + 0.5
            elif event.get("type") == "agent_settled" and agent_started:
                flush_display()
                if not "".join(parts).strip():
                    raise RuntimeError("Pi settled without assistant text")
                return settled_text()
            elif event.get("type") == "agent_end" and agent_started and event.get("willRetry") is False:
                provider_error = _pi_agent_error(event)
                if provider_error is not None:
                    raise RuntimeError(f"Pi provider error: {provider_error}")
                # Pi 0.84.1 has emitted agent_end without the documented
                # agent_settled event after some valid multi-turn runs.
                # Keep a bounded fallback window so a following settlement is
                # consumed before the next prompt begins.
                terminal_fallback = "".join(parts).strip()
                fallback_deadline = time.monotonic() + 0.5
            elif event.get("type") == "message_end" and agent_started:
                # Some Pi runs omit both agent_end and agent_settled after a
                # completed final text message.  A tool-call message_end is
                # not terminal: its tool execution follows immediately.
                message = event.get("message", {})
                content = message.get("content", []) if isinstance(message, dict) else []
                if (
                    isinstance(message, dict)
                    and message.get("role") == "assistant"
                    and isinstance(content, list)
                    and content
                    and all(isinstance(item, dict) for item in content)
                    and any(item.get("type") == "text" and str(item.get("text", "")).strip() for item in content)
                    and not any(item.get("type") == "toolCall" for item in content)
                ):
                    return settled_text()

        flush_display()
        raise TimeoutError("timed out waiting for Pi turn")


class RexVoiceSession:
    """Embeddable Voice Chat runtime for Hermes' existing voice loop."""

    def __init__(self, artifact_root: Path | None = None, topic: str = "", acceptance_gate: Any | None = None) -> None:
        self.session_id = f"hermes-voice-chat-{uuid.uuid4().hex[:12]}"
        root = artifact_root or Path(_env("HERMES_VOICE_CHAT_ARTIFACTS", "REX_VOICE_ARTIFACTS", str(Path.home() / ".hermes/cache/hermes-voice-chat")))
        self.session_root = root.expanduser() / self.session_id
        self.session_root.mkdir(parents=True, exist_ok=True)
        self.socket_path = socket_path_for(self.session_root, self.session_id)
        self.pi_dir = self.session_root / "pi"
        self.pi_dir.mkdir()
        self.topic = topic
        self.acceptance_gate = acceptance_gate
        self.profile = _model_profile()
        self.process: subprocess.Popen[str] | None = None
        self.rpc: PiRpc | None = None
        self.bridge: CapabilityBridge | None = None
        self.bridge_thread: threading.Thread | None = None
        self.session_db: Any = None
        self.backend: CapabilityBackend | None = None
        self._intentional_stop = False
        self._cancel_event = threading.Event()
        self._observer: Callable[[str], None] | None = None

    def start(self, observer: Callable[[str], None] | None = None) -> None:
        if not os.environ.get("OBSIDIAN_VAULT_PATH"):
            raise RuntimeError("OBSIDIAN_VAULT_PATH is required for Voice Chat")
        _models_config(self.pi_dir / "models.json", self.profile)
        os.environ["PI_CODING_AGENT_DIR"] = str(self.pi_dir)
        os.environ["REX_VOICE_BRIDGE_SOCKET"] = str(self.socket_path)
        os.environ["REX_VOICE_SESSION_ID"] = self.session_id
        vault_root = Path(os.environ["OBSIDIAN_VAULT_PATH"])
        workspace = RexVoiceWorkspace(vault_root)
        prepared_roots = os.getenv("HERMES_VOICE_CHAT_PREPARED_ROOTS", os.getenv("REX_VOICE_PREPARED_ROOTS", ""))
        if prepared_roots:
            for item in json.loads(prepared_roots):
                workspace.grant_root(str(item["id"]), Path(str(item["path"])), modes=("read", "search"))
        # The adapter still receives the Vault root so its provider identity and
        # backup semantics remain canonical; all voice targets are workspace-scoped.
        vault = RexVaultAdapter(vault_root)
        try:
            from hermes_state import SessionDB
            self.session_db = SessionDB()
        except Exception as exc:
            print(f"WARNING: canonical Hermes session store unavailable: {exc}", file=sys.stderr)
        store = VoiceSessionStore(self.session_root.parent, session_db=self.session_db)
        self.backend = CapabilityBackend(vault, store, workspace=workspace, acceptance_gate=self.acceptance_gate, telemetry=observer)
        cleanup_stale_sockets(store_root=self.session_root.parent, session_db=self.session_db)
        self.backend.start_session(self.session_id, topic=self.topic)
        self.bridge = CapabilityBridge(self.socket_path, self.backend)
        self.bridge_thread = threading.Thread(target=self.bridge.serve, daemon=True)
        self.bridge_thread.start()
        for _ in range(100):
            if self.socket_path.exists() or not self.bridge_thread.is_alive():
                break
            time.sleep(0.01)
        if not self.socket_path.exists():
            raise RuntimeError("capability bridge failed to start")
        command = build_command(self.session_id, self.socket_path, self.pi_dir, self.backend.store.prepared_context(self.session_id), self.profile)
        log_path = self.session_root / "pi.log"
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log_path.open("w"), text=True, bufsize=1, env=os.environ.copy(), start_new_session=True)
        self._observer = observer
        self.rpc = PiRpc(self.process, log_path, self.session_root / "pi-events.jsonl", observer=observer)
        print(f"[Voice Chat] Pi runtime active session={self.session_id}", flush=True)
        print(f"[Voice Chat] artifacts={self.session_root}", flush=True)

    def prompt(self, message: str) -> str:
        if self.rpc is None or self.backend is None:
            raise RuntimeError("Voice Chat runtime is not started")
        if self.process is not None and self.process.poll() not in (None, 0):
            raise RuntimeError(f"Pi exited before prompt with status {self.process.returncode}")
        self.backend.set_acceptance_turn(transcript=message, model_facing_request=message)
        self.backend.set_current_turn(self.session_id, message, turn_number=self.rpc.counter + 1)
        topic_result = self.backend.store.activate_prepared_topic_live_turn(self.session_id, message)
        if self._observer is not None and topic_result.get("telemetry"):
            self._observer(str(topic_result["telemetry"]))
        prepared_topic_control = None
        if topic_result.get("ok") is True:
            topic_id = topic_result.get("topic_id")
            version = topic_result.get("version")
            if isinstance(topic_id, str) and isinstance(version, str):
                prepared_topic_control = {"topic_id": topic_id, "version": version}
        turn_message = build_turn_message(
            message,
            self.backend.store.prepared_context(self.session_id),
            prepared_topic_control=prepared_topic_control,
        )
        try:
            reply = self.rpc.prompt(turn_message, cancel_event=self._cancel_event)
        except PseudoToolTextError:
            if self._observer is not None:
                self._observer("TOOL_DISPATCH_RETRY attempt=1 reason=pseudo_tool_text")
            try:
                forked_entry = self.rpc.fork_before_last_pseudo_tool()
                if self._observer is not None:
                    self._observer(f"TOOL_DISPATCH_RETRY_CONTEXT clean_fork entry={forked_entry}")
                retry_message = (
                    f"{turn_message}\n\n"
                    "Correction: your previous response used pseudo-tool text and was rejected. "
                    "Do not print JSON, pseudo-tool text, or describe a tool. Execute the requested "
                    "action with the native structured capability tool. Return no operation object in assistant text."
                )
                reply = self.rpc.prompt(retry_message, cancel_event=self._cancel_event)
            except Exception:
                if self._observer is not None:
                    self._observer("TOOL_DISPATCH_RETRY_RESULT failed")
                raise
            else:
                if self._observer is not None:
                    self._observer("TOOL_DISPATCH_RETRY_RESULT success")
        except UnfulfilledRetrievalNarrationError:
            if self._observer is not None:
                self._observer("TOOL_DISPATCH_RETRY attempt=1 reason=unfulfilled_retrieval_narration")
            try:
                forked_entry = self.rpc.fork_before_rejected_response(_looks_like_unfulfilled_retrieval_narration)
                if self._observer is not None:
                    self._observer(f"TOOL_DISPATCH_RETRY_CONTEXT clean_fork entry={forked_entry}")
                retry_message = (
                    f"{turn_message}\n\n"
                    "Correction: your previous response claimed you were searching stored notes, but no retrieval tool was executed. "
                    "Either execute the native retrieval capability now, or answer without claiming a search and state that the detail is unavailable from current context."
                )
                reply = self.rpc.prompt(retry_message, cancel_event=self._cancel_event)
            except Exception:
                if self._observer is not None:
                    self._observer("TOOL_DISPATCH_RETRY_RESULT failed")
                raise
            else:
                if self._observer is not None:
                    self._observer("TOOL_DISPATCH_RETRY_RESULT success")
        if self.process is not None and self.process.poll() not in (None, 0):
            raise RuntimeError(f"Pi exited after prompt with status {self.process.returncode}")
        return reply

    def cancel_prompt(self) -> None:
        """Request cancellation of the currently waiting Pi turn."""
        self._cancel_event.set()

    def _observe_post_call(self, root: Path, session_id: str, worker: subprocess.Popen[bytes]) -> None:
        """Report bounded post-call state transitions while Hermes remains foregrounded."""
        job_path = Path(root) / "post-call" / "jobs" / f"post-call-{session_id}.json"
        previous: tuple[object, object, object] | None = None
        while True:
            try:
                job = json.loads(job_path.read_text(encoding="utf-8"))
                work = job.get("work", [])
                signature = (job.get("status"), job.get("attempts", 0), tuple((item.get("id"), item.get("status")) for item in work if isinstance(item, dict)))
                if signature != previous:
                    print(f"[Rex Post-Call] status={signature[0]} attempts={signature[1]} work={list(signature[2])}", flush=True)
                    previous = signature
                if signature[0] in {"completed", "failed", "uncertain"}:
                    return
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                pass
            if worker.poll() is not None:
                return
            time.sleep(0.25)

    def launch_post_call_worker(
        self,
        job: dict[str, Any] | None = None,
        *,
        model: str | None = None,
        provider: str | None = None,
    ) -> Any | None:
        """Start the already-persisted job exactly once.

        The embedded phone path calls this only after the normal model
        supervisor has reported QWEN_READY.  Queue persistence deliberately
        remains in ``stop`` so a failed model handoff leaves recoverable work.
        """
        if getattr(self, "_post_call_worker_started", False):
            return None
        job = job or getattr(self, "_post_call_job", None)
        if not job or not self.backend:
            return None
        worker_env = os.environ.copy()
        worker_env["REX_POST_CALL_MODEL"] = model or os.getenv(
            "REX_POST_CALL_MODEL", "/models/Qwen3.8-27B-UD-Q4_K_XL.gguf"
        )
        worker_env["REX_POST_CALL_PROVIDER"] = provider or os.getenv(
            "REX_POST_CALL_PROVIDER", "Qwen 27B"
        )
        print(
            f"POST_CALL_WORKER_START_ALLOWED job_id={job['job_id']} "
            f"model={worker_env['REX_POST_CALL_MODEL']} provider={worker_env['REX_POST_CALL_PROVIDER']}",
            flush=True,
        )
        worker = start_detached(
            self.backend.store.root / "post-call",
            self.session_id,
            env=worker_env,
        )
        released = dict(job)
        released["execution_gate"] = {
            "status": "released",
            "model": worker_env["REX_POST_CALL_MODEL"],
            "provider": worker_env["REX_POST_CALL_PROVIDER"],
        }
        PostCallQueue(self.backend.store.root / "post-call").save(released)
        self._post_call_worker_started = True
        print(f"POST_CALL_WORKER_STARTED job_id={job['job_id']} pid={worker.pid}", flush=True)
        threading.Thread(
            target=self._observe_post_call,
            args=(self.backend.store.root, self.session_id, worker),
            name=f"rex-post-call-observer-{self.session_id}",
            daemon=True,
        ).start()
        return worker

    def stop(self, aborted: bool = False, *, launch_post_call: bool = True) -> dict[str, Any] | None:
        self._intentional_stop = True
        self._cancel_event.set()
        # The voice runtime must be down before the session is handed to the
        # post-call lifecycle.  In particular, do not finalize/enqueue while
        # Pi can still issue a Gemma turn.
        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=5)
            except Exception:
                self.process.kill()
        if self.backend is not None:
            try:
                if aborted:
                    self.backend.store.abort(self.session_id, error="voice session stopped")
                else:
                    self.backend.complete_session(self.session_id, transcript=None, final_topic=self.topic)
            except Exception as exc:
                print(f"[Voice Chat] session finalization failed: {exc}", file=sys.stderr)
            try:
                job = enqueue_closed_session(self.backend.store, self.session_id, artifact_root=self.backend.store.root)
                self._post_call_job = job
                print(
                    f"POST_CALL_JOB_ENQUEUED session_id={self.session_id} job_id={job['job_id']}",
                    flush=True,
                )
                if launch_post_call:
                    self.launch_post_call_worker(job)
            except Exception as exc:
                # Post-call work is recoverable and must never make voice stop fail.
                print(f"[Rex Post-Call] enqueue failed: {exc}", file=sys.stderr)
        if self.bridge is not None:
            self.bridge.close()
        if self.bridge_thread is not None:
            self.bridge_thread.join(timeout=2)
        if self.session_db is not None:
            self.session_db.close()
        return getattr(self, "_post_call_job", None)


def _model_profile() -> dict[str, Any]:
    return {
        "provider": os.getenv("HERMES_VOICE_CHAT_PROVIDER", os.getenv("REX_VOICE_PROVIDER", "local")),
        "base_url": os.getenv("REX_VOICE_MODEL_BASE_URL", "http://127.0.0.1:8082/v1"),
        "model": os.getenv("REX_VOICE_MODEL_ID", "/models/gemma-4-E2B-it-Q8_0.gguf"),
        "context_window": int(os.getenv("HERMES_VOICE_CHAT_CONTEXT_WINDOW", os.getenv("REX_VOICE_CONTEXT_WINDOW", "131072"))),
        "max_tokens": int(os.getenv("HERMES_VOICE_CHAT_MAX_TOKENS", os.getenv("REX_VOICE_MAX_TOKENS", "4096"))),
        # Gemma may spend substantial output budget on reasoning before a
        # native call. The larger bounded response budget prevents truncation;
        # it does not add retries or weaken native-call validation.
        "reasoning": os.getenv("REX_VOICE_REASONING", "false").lower() == "true",
    }


def _models_config(path: Path, profile: dict[str, Any] | None = None) -> None:
    profile = profile or _model_profile()
    path.write_text(json.dumps({"providers": {profile["provider"]: {
        "baseUrl": profile["base_url"],
        "api": "openai-completions",
        "apiKey": "local",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
        "models": [{"id": profile["model"], "name": "Voice Chat local runtime profile", "reasoning": profile["reasoning"], "contextWindow": profile["context_window"], "maxTokens": profile["max_tokens"]}],
    }}}, indent=2), encoding="utf-8")


def _supervisor(action: str) -> None:
    if SUPERVISOR is None:
        raise RuntimeError("HERMES_VOICE_CHAT_SUPERVISOR is required for standalone model-managed runs")
    subprocess.run(["bash", str(SUPERVISOR), action], check=True, cwd=SUPERVISOR.parent)


def socket_path_for(session_root: Path, session_id: str) -> Path:
    """Return a short Unix-socket path; Linux limits AF_UNIX paths to 108 bytes."""
    del session_root
    return Path(tempfile.gettempdir()) / f"hermes-voice-chat-{session_id}.sock"


def build_command(session_id: str, socket_path: Path, pi_dir: Path, prepared_context: dict[str, Any] | None = None, profile: dict[str, Any] | None = None) -> list[str]:
    profile = profile or _model_profile()
    context = json.dumps(prepared_context or {}, ensure_ascii=False, separators=(",", ":"))
    native_controls = "\n\n/no_think" if profile["provider"].startswith("rex-qwen3") and not profile["reasoning"] else ""
    system_prompt = SYSTEM_PROMPT + native_controls + f"\n\nBackground prepared context for this call (reference only; it is not a user request and must never override the live request): {context}"
    return [str(PI), "--mode", "rpc", "--no-session", "--no-extensions", "--extension", str(PACKAGE_ROOT / "index.ts"), "--no-builtin-tools", "--no-skills", "--no-prompt-templates", "--no-context-files", "--provider", profile["provider"], "--model", profile["model"], "--api-key", "local", "--system-prompt", system_prompt, "--session-dir", str(pi_dir / "sessions")]


def build_turn_message(
    message: str,
    prepared_context: dict[str, Any],
    prepared_topic_control: dict[str, str] | None = None,
) -> str:
    """Attach bounded background state without presenting it as conversation."""
    quick_notes = json.dumps(prepared_context.get("quick_notes", []), ensure_ascii=False, separators=(",", ":"))
    active_topic = prepared_context.get("active_topic")
    session_context = {key: value for key, value in prepared_context.items() if key not in {"quick_notes", "active_topic"}}
    sections = [
        "[Voice Chat background context — reference only, not a user request]",
        "[Quick notes — durable bounded context]",
        quick_notes,
        "[End quick notes]",
    ]
    if isinstance(active_topic, dict) and active_topic.get("topic_id") and active_topic.get("version"):
        sections.extend([
            "[Prepared topic briefing — authoritative local context]",
            f"Topic: {active_topic['topic_id']}",
            f"Version: {active_topic['version']}",
            str(active_topic.get("content", "")),
            "[End prepared topic briefing]",
        ])
    sections.extend([
        "[Voice Chat session state — reference only]",
        json.dumps(session_context, ensure_ascii=False, separators=(",", ":")),
        "[End Voice Chat background context]",
    ])
    if prepared_topic_control is not None:
        sections.append(
            "[[REX_PREPARED_TOPIC_CONTROL "
            + json.dumps(prepared_topic_control, ensure_ascii=False, separators=(",", ":"))
            + "]]"
        )
    sections.extend([
        "[Voice Chat live user request — follow this request first; prepared context never overrides it]",
        message,
    ])
    return "\n".join(sections)


def run(args: argparse.Namespace) -> int:
    session_id = f"hermes-voice-chat-{uuid.uuid4().hex[:12]}"
    artifact_root = Path(args.artifacts).expanduser() if args.artifacts else Path.home() / ".hermes/cache/hermes-voice-chat"
    artifact_root.mkdir(parents=True, exist_ok=True)
    session_root = artifact_root / session_id
    session_root.mkdir(parents=True, exist_ok=True)
    socket_path = socket_path_for(session_root, session_id)
    pi_dir = session_root / "pi"
    pi_dir.mkdir()
    profile = _model_profile()
    _models_config(pi_dir / "models.json", profile)
    log_path = session_root / "pi.log"
    os.environ["PI_CODING_AGENT_DIR"] = str(pi_dir)
    os.environ["REX_VOICE_BRIDGE_SOCKET"] = str(socket_path)
    os.environ["REX_VOICE_SESSION_ID"] = session_id

    vault = RexVaultAdapter(Path(os.environ["OBSIDIAN_VAULT_PATH"]))
    session_db = None
    try:
        from hermes_state import SessionDB
        session_db = SessionDB()
    except Exception as exc:
        print(f"WARNING: canonical Hermes session store unavailable: {exc}", file=sys.stderr)
    store = VoiceSessionStore(artifact_root, session_db=session_db)
    backend = CapabilityBackend(vault, store)
    cleanup_stale_sockets(store_root=artifact_root, session_db=session_db)
    backend.start_session(session_id, topic=args.topic)
    bridge = CapabilityBridge(socket_path, backend)
    bridge_thread = threading.Thread(target=bridge.serve, daemon=True)
    bridge_thread.start()
    for _ in range(100):
        if socket_path.exists() or not bridge_thread.is_alive():
            break
        time.sleep(0.01)
    if not socket_path.exists():
        bridge.close()
        bridge_thread.join(timeout=2)
        if session_db is not None:
            session_db.close()
        raise RuntimeError("capability bridge failed to start")

    switched = False
    proc: subprocess.Popen[str] | None = None
    voice_stop = threading.Event()
    result_code = 1
    closed = False
    try:
        if args.manage_model:
            _supervisor("switch-to-gemma")
            switched = True
        command = build_command(session_id, socket_path, pi_dir, store.prepared_context(session_id), profile)
        print("Voice Chat ready")
        print(f"session: {session_id}")
        print(f"artifacts: {session_root}")
        print("type text, or use --voice for microphone mode; Ctrl-C ends the session")
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log_path.open("w"), text=True, bufsize=1, env=os.environ.copy(), start_new_session=True)
        rpc = PiRpc(proc, log_path, session_root / "pi-events.jsonl", observer=lambda line: print(line, flush=True))

        def handle(text: str) -> None:
            if text.strip().lower() in {"/exit", "/quit", "exit", "quit", "stop voice", "/voice off"}:
                voice_stop.set()
                return
            reply = rpc.prompt(build_turn_message(text, store.prepared_context(session_id)))
            print(f"Voice Chat: {reply}")
            if args.voice:
                from hermes_cli.voice import speak_text
                speak_text(reply)

        if args.voice:
            from hermes_cli.voice import start_continuous, stop_continuous
            print("VOICE START — speak after the listening indicator.")
            start_continuous(handle, on_status=lambda status: print(f"[{status}]", flush=True), auto_restart=True, on_silent_limit=voice_stop.set, on_stop_phrase=lambda _text: voice_stop.set())
            while proc.poll() is None and not voice_stop.is_set():
                time.sleep(0.5)
        else:
            for line in sys.stdin:
                try:
                    handle(line.rstrip("\n"))
                except EOFError:
                    break
        transcript = None
        result = backend.complete_session(session_id, transcript=transcript, final_topic=args.topic)
        closed = True
        print(f"handoff: {result['status']} ({session_id})")
        result_code = 0
    except (KeyboardInterrupt, EOFError):
        backend.complete_session(session_id, transcript=None, final_topic=args.topic)
        closed = True
        result_code = 0
    except Exception as exc:
        backend.store.abort(session_id, error=str(exc))
        closed = True
        print(f"Voice Chat aborted: {exc}", file=sys.stderr)
        result_code = 1
    finally:
        try:
            if args.voice:
                from hermes_cli.voice import stop_continuous
                stop_continuous()
        except Exception:
            pass
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        bridge.close()
        bridge_thread.join(timeout=2)
        if switched:
            try:
                _supervisor("switch-to-qwen")
            except Exception as exc:
                result_code = 1
                print(f"WARNING: Qwen restoration failed: {exc}", file=sys.stderr)
        if closed:
            try:
                enqueue_closed_session(backend.store, session_id, artifact_root=artifact_root)
                start_detached(backend.store.root / "post-call", session_id)
            except Exception as exc:
                result_code = 1
                print(f"WARNING: post-call worker launch failed: {exc}", file=sys.stderr)
        if session_db is not None:
            session_db.close()
    return result_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Opt-in Voice Chat")
    parser.add_argument("--voice", action="store_true", help="use existing Hermes microphone/STT/TTS stack")
    parser.add_argument("--manage-model", action="store_true", help="switch Gemma/Qwen through the existing supervisor")
    parser.add_argument("--topic", default="")
    parser.add_argument("--artifacts")
    parser.add_argument("--print-command", action="store_true")
    args = parser.parse_args()
    if args.print_command:
        print(" ".join(build_command("SESSION", Path("/tmp/rex.sock"), Path("/tmp/rex-pi"), profile=_model_profile())))
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
