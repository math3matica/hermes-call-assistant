from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, cast

from .protocol import validate_call
from .note_search import MAX_RESULTS, NoteSearchError, search_note_chunks
from .shared_knowledge import SharedKnowledgeStore
from .store import VoiceSessionStore
from .workspace import RexVoiceWorkspace

try:
    hermes_root = str(Path.home() / ".hermes/hermes-agent")
    if hermes_root not in sys.path:
        sys.path.insert(0, hermes_root)
    from agent.context_compiler import HandleRef, ResourceRef, WorkingState, WorkingStateStore
except ImportError:  # Focused unit tests do not need the Hermes source tree.
    HandleRef = ResourceRef = WorkingState = WorkingStateStore = None  # type: ignore[assignment]


class CapabilityError(ValueError):
    def __init__(self, message: str, *, code: str = "capability_error", details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class InMemoryVault:
    """Vault-shaped test double with the same safety invariants as the adapter."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.active: dict[str, str] = {}

    def _path(self, name: str) -> Path:
        path = (self.root / name).resolve()
        path.relative_to(self.root.resolve())
        if path.suffix != ".md":
            path = path.with_suffix(".md")
        return path

    def create(self, session_id: str, name: str, content: str = "") -> dict[str, Any]:
        path = self._path(name)
        if path.exists():
            raise CapabilityError(f"resource already exists: {name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.active[session_id] = str(path.relative_to(self.root))
        return self.resource(path)

    def open(self, session_id: str, name: str) -> dict[str, Any]:
        path = self._path(name)
        if not path.exists():
            raise CapabilityError(f"resource does not exist: {name}")
        self.active[session_id] = str(path.relative_to(self.root))
        return self.resource(path)

    def read(self, name: str) -> str:
        return self._path(name).read_text(encoding="utf-8")

    def append(self, name: str, content: str, session_id: str = "") -> dict[str, Any]:
        path = self._path(name)
        old = path.read_text(encoding="utf-8")
        separator = "" if not old or old.endswith("\n") else "\n"
        new = old + separator + content
        if not new.endswith("\n"):
            new += "\n"
        path.write_text(new, encoding="utf-8")
        return self.resource(path)

    def replace_paragraph(self, name: str, number: int, content: str, session_id: str = "") -> dict[str, Any]:
        path = self._path(name)
        old = path.read_text(encoding="utf-8")
        paragraphs = old.splitlines(keepends=True)
        if number < 1 or number > len(paragraphs):
            raise CapabilityError("paragraph target is stale or out of range")
        paragraphs[number - 1] = content.rstrip("\n") + "\n"
        path.write_text("".join(paragraphs), encoding="utf-8")
        return self.resource(path)

    def replace_text(self, name: str, old_text: str, content: str, session_id: str = "") -> dict[str, Any]:
        path = self._path(name)
        old = path.read_text(encoding="utf-8")
        if old.count(old_text) != 1:
            raise CapabilityError("exact text target must occur exactly once", code="stale_text_target")
        path.write_text(old.replace(old_text, content), encoding="utf-8")
        return self.resource(path)

    def rename(self, session_id: str, old_name: str, new_name: str) -> dict[str, Any]:
        source = self._path(old_name)
        dest = self._path(new_name)
        if not source.exists():
            raise CapabilityError(f"resource does not exist: {old_name}")
        if dest.exists():
            raise CapabilityError(f"resource already exists: {new_name}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        source.rename(dest)
        if self.active.get(session_id) == str(source.relative_to(self.root)):
            self.active[session_id] = str(dest.relative_to(self.root))
        return self.resource(dest)

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        result = []
        for path in sorted(self.root.rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            if query.casefold() in text.casefold() or query.casefold() in path.stem.casefold():
                result.append({"path": str(path.relative_to(self.root)), "content": text[:1200]})
                if len(result) >= limit:
                    break
        return result

    def search_chunks(self, query: str, *, source_refs: list[str] | None = None, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
        return search_note_chunks(self.root, query, source_refs=source_refs, limit=limit)

    def resource(self, path: Path) -> dict[str, Any]:
        stat = path.stat()
        return {
            "canonical_id": str(path.resolve()),
            "provider": "rex-vault",
            "source": "rex-vault",
            "display_name": str(path.relative_to(self.root)),
            "version": f"{stat.st_mtime_ns}:{stat.st_size}",
            "version_verified": True,
        }


class CapabilityBackend:
    def __init__(self, vault: Any, store: VoiceSessionStore, *, web_retriever: Callable[[str, int], list[dict[str, Any]]] | None = None, workspace: RexVoiceWorkspace | None = None, acceptance_gate: Any | None = None, telemetry: Callable[[str], None] | None = None, shared_knowledge: SharedKnowledgeStore | None = None):
        self.vault = vault
        self.store = store
        self.web_retriever = web_retriever or (lambda query, limit: [])
        self.workspace = workspace
        # This is deliberately opt-in. Ordinary Rex Voice sessions have no
        # gate and therefore retain their normal capability behavior.
        self.acceptance_gate = acceptance_gate
        self.telemetry = telemetry
        vault_root = getattr(vault, "root", None)
        if shared_knowledge is not None:
            self.shared_knowledge = shared_knowledge
        elif vault_root is not None:
            try:
                self.shared_knowledge = SharedKnowledgeStore(vault_root)
            except (OSError, ValueError):
                self.shared_knowledge = None
        else:
            self.shared_knowledge = None
        self._acceptance_evidence: dict[str, Any] = {}
        self.working_states: dict[str, Any] = {}
        self.working_state_stores: dict[str, Any] = {}

    def set_acceptance_turn(self, *, transcript: str, model_facing_request: str, evidence_ref: str | None = None) -> None:
        self._acceptance_evidence = {
            "transcript": transcript,
            "model_facing_request": model_facing_request,
            "evidence_ref": evidence_ref if evidence_ref is not None else self._acceptance_evidence.get("evidence_ref"),
        }

    def _record_draft_event(self, session_id: str, event: dict[str, Any]) -> None:
        record = self.store.load_session(session_id)
        events = list(record.get("draft_workflow_events", []))
        events.append({"at": time.time(), **event})
        self.store.update_session(session_id, draft_workflow_events=events[-100:])

    @staticmethod
    def _is_explicit_approval(transcript: str) -> tuple[bool, str]:
        text = " ".join(transcript.split())
        lowered = text.casefold()
        if re.search(r"\b(?:if|whether)\b.*\bapprove(?:d)?\b", lowered):
            return False, "hypothetical"
        if re.search(r"\b(?:don't|do not|doesn't|didn't|never)\b.*\bapprove(?:d)?\b", lowered):
            return False, "negated"
        if re.search(r"\b(?:says|said|told|example|quoted|quote)\b.*\bapprove(?:d)?\b", lowered):
            return False, "reported_or_quoted"
        if re.search(r"\b(?:yesterday|previously|already|later)\b", lowered) and re.search(r"\bapprove(?:d)?\b", lowered):
            return False, "historical_or_future"
        if re.match(r"\s*(?:did|do|does|why|when|can|should|would)\b", lowered):
            return False, "question"
        if re.search(r"\b(?:will|i'll|i will|going to)\s+approve\b", lowered):
            return False, "future"
        if re.search(r"\bapprove(?:d)?\b", lowered) or re.search(
            r"\b(?:i am|i'm)\s+(?:confirming|giving|granting)\s+(?:my\s+)?approval\b|\bi\s+give\s+(?:my\s+)?approval\b",
            lowered,
        ):
            return True, "explicit_approval"
        if re.search(r"\b(?:looks good|that's correct|that is correct|yes,? that's final|that's good|that is good|okay|ok|yes)\b", lowered) and re.search(r"\b(?:save|final|approve)\b", lowered):
            return True, "explicit_final_confirmation"
        return False, "no_explicit_approval"

    def set_current_turn(self, session_id: str, transcript: str, *, turn_number: int) -> None:
        """Record the real user turn; only explicit approval can approve a draft."""
        if not isinstance(turn_number, int) or turn_number < 1:
            raise ValueError("turn_number must be a positive integer")
        self.store.update_session(session_id, current_user_turn={"number": turn_number, "transcript": transcript})
        approved, reason = self._is_explicit_approval(transcript)
        record = self.store.load_session(session_id)
        draft_id = record.get("current_draft_id")
        self._record_draft_event(session_id, {
            "event": "DRAFT_APPROVAL_EVALUATED",
            "draft_id": draft_id,
            "approved": approved,
            "reason": reason,
            "turn": turn_number,
        })
        if not approved:
            return
        try:
            draft = self.store.load_draft(draft_id) if draft_id else self.store.active_draft(session_id)
        except (KeyError, ValueError):
            self._record_draft_event(session_id, {
                "event": "DRAFT_APPROVAL_EVALUATED",
                "draft_id": draft_id,
                "approved": False,
                "reason": "no_unambiguous_current_draft",
                "turn": turn_number,
            })
            return
        if draft.get("voice_session_id") != session_id or draft.get("status") != "active":
            self._record_draft_event(session_id, {
                "event": "DRAFT_APPROVAL_EVALUATED",
                "draft_id": draft.get("id"),
                "approved": False,
                "reason": "current_draft_not_active",
                "turn": turn_number,
            })
            return
        draft["approved"] = True
        draft["approved_at_turn"] = turn_number
        draft["approved_version"] = draft.get("revision_version", draft.get("revision", 0))
        self.store.update_draft(draft)
        self._record_draft_event(session_id, {
            "event": "DRAFT_APPROVAL_SET",
            "draft_id": draft["id"],
            "revision": draft["approved_version"],
            "turn": turn_number,
        })

    @staticmethod
    def _is_mutation(operation: str, args: dict[str, Any]) -> bool:
        if operation in {"resource_mutate"}:
            return True
        if operation == "resource_manage":
            return args.get("operation") in {"create", "rename"}
        if operation == "draft_manage":
            return args.get("operation") in {"create", "revise", "promote", "discard"}
        return False

    def _enforce_acceptance_gate(self, session_id: str, operation: str, args: dict[str, Any]) -> None:
        gate = self.acceptance_gate
        if gate is None or not self._is_mutation(operation, args):
            return
        status = getattr(getattr(gate, "status", None), "value", getattr(gate, "status", "unknown"))
        if status == "passed":
            return
        evidence = {
            **self._acceptance_evidence,
            "attempted_capability": operation,
            "arguments": args,
            "block_reason": "physical acceptance target has not been observed",
            "gate_status": status,
        }
        record = self.store.load_session(session_id)
        blocked = list(record.get("acceptance_blocked_calls", []))
        blocked.append(evidence)
        self.store.update_session(session_id, acceptance_blocked_calls=blocked)
        raise CapabilityError(
            "document mutation blocked by physical acceptance gate",
            code="physical_acceptance_gate_blocked",
            details={"gate_status": status, "blocked": True, **evidence},
        )

    @staticmethod
    def _hangup_phone() -> dict[str, Any]:
        adb = os.environ.get("HERMES_PHONE_ADB") or shutil.which("adb") or "adb"
        serial = os.environ.get("HERMES_PHONE_ADB_SERIAL")
        command = [adb] + (["-s", serial] if serial else []) + [
            "shell", "printf '%s\\n' HANGUP | nc -w 15 127.0.0.1 9999",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
        if result.returncode != 0:
            raise CapabilityError(result.stderr.strip() or "phone hangup failed", code="phone_hangup_failed")
        response = result.stdout.strip()
        if not response.startswith("OK hangup"):
            raise CapabilityError(response or "phone bridge rejected hangup", code="phone_hangup_rejected")
        return {"ok": True, "bridge_response": response, "authoritative": True}

    def start_session(self, session_id: str, *, topic: str = "") -> Any:
        record = self.store.start_session(session_id, topic=topic)
        if self.workspace is not None:
            record = self.store.update_session(session_id, authorized_resource_roots=self.workspace.authorized_roots())
        if WorkingState is not None:
            if WorkingStateStore is not None and self.store.session_db is not None:
                state_store = WorkingStateStore(self.store.session_db, session_id)
                self.working_state_stores[session_id] = state_store
                self.working_states[session_id] = state_store.load()
            else:
                self.working_states[session_id] = WorkingState(session_id)
            self._save_working_state(session_id, self.working_states[session_id])
        elif self.store.session_db is not None:
            # Keep the canonical handoff key present even in focused/test
            # environments where Hermes' optional context compiler is absent.
            self.store.session_db.patch_session_model_config(
                session_id,
                {"_context_compiler_working_state": {"session_id": session_id, "revision": 0}},
            )
        return type("Session", (), {"session_id": session_id, **record})()

    def _target(self, target: str) -> str:
        return self.workspace.document_target(target) if self.workspace is not None else target

    def _save_working_state(self, session_id: str, state: Any) -> None:
        state_store = self.working_state_stores.get(session_id)
        if state_store is not None:
            state_store.save(state)
        self.store.update_session(session_id, working_state=state.to_dict())

    def _record_resource(self, session_id: str, resource: dict[str, Any]) -> None:
        record = self.store.load_session(session_id)
        self.store.update_session(session_id, active_resource=resource, resources_touched=list({*record.get("resources_touched", []), resource["canonical_id"]}))
        state = self.working_states.get(session_id)
        if state is not None and ResourceRef is not None:
            state.record_resource(ResourceRef(
                canonical_id=resource["canonical_id"], provider=resource.get("provider", ""),
                source=resource.get("source", "validated_operation"), display_name=resource.get("display_name", ""),
                version=resource.get("version", ""), version_kind="provider", version_verified=bool(resource.get("version_verified")),
            ), provenance="validated_operation")
            self._save_working_state(session_id, state)

    def _active_name(self, session_id: str, args: dict) -> str:
        target = args.get("target")
        active = self.store.load_session(session_id).get("active_resource")
        if target:
            accepted = {str(active.get("display_name", ""))} if active else set()
            if active:
                canonical_id = str(active.get("canonical_id", ""))
                display_name = str(active.get("display_name", ""))
                accepted.update({canonical_id, Path(canonical_id).name, Path(display_name).stem})
            if active and target not in accepted:
                raise CapabilityError("explicit target does not match active resource")
            return str(active.get("display_name", target)) if active else target
        if not active:
            raise CapabilityError("no active resource")
        return str(active["display_name"])

    def _current_resource(self, session_id: str, name: str) -> dict[str, Any]:
        record = self.store.load_session(session_id).get("active_resource") or {}
        path_builder = getattr(self.vault, "_path", None)
        resource_builder = getattr(self.vault, "resource", None)
        if callable(path_builder) and callable(resource_builder):
            current = resource_builder(path_builder(name))
            if isinstance(current, dict):
                return current
        return record

    @staticmethod
    def _retrieval_envelopes(results: list[dict[str, Any]], query: str, handle_id: str) -> list[dict[str, Any]]:
        terms = [term.casefold() for term in query.split() if term.strip()]
        envelopes: list[dict[str, Any]] = []
        for index, item in enumerate(results):
            source_id = item.get("source_id") or item.get("canonical_id") or item.get("id") or item.get("path") or item.get("source") or f"result-{index + 1}"
            title = item.get("title") or item.get("display_name") or Path(str(item.get("path", source_id))).stem
            excerpt = item.get("excerpt") or item.get("content") or item.get("text") or ""
            excerpt = str(excerpt)[:1200]
            haystack = f"{title} {excerpt}".casefold()
            relevance = item.get("relevance")
            if not isinstance(relevance, (int, float)):
                relevance = sum(1 for term in terms if term in haystack) / max(1, len(terms))
            envelopes.append({
                "id": str(source_id),
                "title": str(title),
                "excerpt": excerpt,
                "relevance": float(relevance),
                "source_handle": f"{handle_id}:{index + 1}",
                "source": item.get("provider") or item.get("source") or "vault",
                "root_id": item.get("root_id"),
                "source_id": str(item.get("source_id", source_id)),
                "source_path": str(item.get("source_path", item.get("path", source_id))),
                "section": item.get("section"),
                "chunk_id": str(item.get("chunk_id", source_id)),
                "content": excerpt,
                "score": float(item.get("score", relevance)),
                "version": item.get("version"),
            })
        return envelopes

    def _emit_retrieval(self, message: str) -> None:
        if self.telemetry is not None:
            self.telemetry(message)

    def _search_authoritative_notes(self, session_id: str, query: str, limit: int) -> tuple[list[dict[str, Any]], str, str | None, str]:
        record = self.store.load_session(session_id)
        active = record.get("active_prepared_topic")
        topic_id = active.get("topic_id") if isinstance(active, dict) else None
        refs = active.get("source_refs") if isinstance(active, dict) else None
        refs = refs if isinstance(refs, list) and refs else None
        query_id = uuid.uuid4().hex[:8]
        started = time.perf_counter()
        initial_scope = "active_topic" if refs else "global_notes"
        self._emit_retrieval(f"MEMORY_RETRIEVAL_START query_id={query_id} scope={initial_scope}")
        search_chunks = getattr(self.vault, "search_chunks", None)
        if not callable(search_chunks):
            raise CapabilityError("Vault provider does not support bounded note retrieval")
        search_chunks = cast(Callable[..., list[dict[str, Any]]], search_chunks)
        try:
            results = search_chunks(query, source_refs=refs, limit=min(limit, MAX_RESULTS))
            scope = initial_scope
            if not results and refs:
                self._emit_retrieval(f"MEMORY_RETRIEVAL_START query_id={query_id} scope=global_notes")
                results = search_chunks(query, source_refs=None, limit=min(limit, MAX_RESULTS))
                scope = "global_notes"
            if not results and self.workspace is not None:
                workspace_search = getattr(self.workspace, "search_chunks", None)
                if callable(workspace_search):
                    workspace_search = cast(Callable[..., list[dict[str, Any]]], workspace_search)
                    results = workspace_search(query, limit=min(limit, MAX_RESULTS))
                    scope = "voice_workspace"
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            if not results:
                self._emit_retrieval(f"MEMORY_RETRIEVAL_NO_RESULT query_id={query_id} elapsed_ms={elapsed_ms}")
            else:
                returned_chars = sum(len(str(item.get("content", ""))) for item in results)
                self._emit_retrieval(f"MEMORY_RETRIEVAL_COMPLETE query_id={query_id} scope={scope} result_count={len(results)} elapsed_ms={elapsed_ms} returned_chars={returned_chars}")
            return results, scope, topic_id, query_id
        except NoteSearchError as exc:
            raise CapabilityError(str(exc)) from exc

    def call(self, session_id: str, operation: str, raw_args: object) -> dict[str, Any]:
        try:
            if operation == "resource_manage" and isinstance(raw_args, dict):
                target = raw_args.get("target")
                if isinstance(target, str) and target.strip():
                    try:
                        active_draft = self.store.active_draft(session_id)
                    except (KeyError, ValueError):
                        active_draft = None
                    if active_draft is not None and target.strip() == active_draft["id"]:
                        raise CapabilityError(
                            "active unpromoted drafts must be read with draft_manage",
                            code="draft_read_required",
                            details={
                                "draft_id": active_draft["id"],
                                "capability": "draft_manage",
                                "operation": "read",
                            },
                        )
            args = validate_call(operation, raw_args)
            if operation == "phone_hangup":
                return self._hangup_phone()
            self._enforce_acceptance_gate(session_id, operation, args)
            if operation == "resource_manage":
                action = args["operation"]
                if action == "active":
                    return {"ok": True, "resource": self.store.load_session(session_id).get("active_resource")}
                if action == "create":
                    try:
                        active_draft = self.store.active_draft(session_id)
                    except KeyError:
                        active_draft = None
                    if active_draft is not None and (
                        "content" not in args or active_draft["content"] == args["content"]
                    ):
                        raise CapabilityError(
                            "matching active draft must be promoted with draft_manage",
                            code="draft_promotion_required",
                            details={"draft_id": active_draft["id"], "target": args["target"]},
                        )
                    target = self._target(args["target"])
                    try:
                        existing_content = self.vault.read(target)
                    except (CapabilityError, FileNotFoundError, KeyError, OSError):
                        existing_content = None
                    if existing_content is not None and existing_content == args.get("content", ""):
                        existing_resource = self._current_resource(session_id, target)
                        return {"ok": True, "resource": existing_resource, "idempotent": True}
                    existing = self.store.load_session(session_id).get("active_resource")
                    if existing and Path(str(existing.get("path", ""))).stem == Path(target).stem:
                        current_content = self.vault.read(existing["path"])
                        if current_content == args.get("content", ""):
                            return {"ok": True, "resource": existing, "idempotent": True}
                    resource = self.vault.create(session_id, target, args.get("content", ""))
                elif action == "open":
                    target = self._target(args["target"])
                    search_target = target
                    vault_root = getattr(self.vault, "root", getattr(self.vault, "vault_root", None))
                    if vault_root is not None and Path(target).is_absolute():
                        try:
                            search_target = str(Path(target).resolve().relative_to(Path(vault_root).resolve()))
                        except ValueError:
                            pass
                    matches = self.vault.search(search_target, 10)
                    candidate_names = sorted({
                        str(item.get("path") or item.get("display_name") or item.get("title"))
                        for item in matches
                        if item.get("path") or item.get("display_name") or item.get("title")
                    })
                    exact = [
                        name for name in candidate_names
                        if name in {target, search_target}
                        or Path(name).stem == Path(search_target).stem
                    ]
                    if len(candidate_names) > 1 and not exact:
                        raise CapabilityError(
                            "ambiguous resource; choose one candidate",
                            code="ambiguous_resource",
                            details={"candidates": candidate_names},
                        )
                    if len(candidate_names) == 1:
                        open_target = candidate_names[0]
                    elif len(exact) == 1:
                        open_target = exact[0]
                    else:
                        open_target = search_target
                    resource = self.vault.open(session_id, open_target)
                else:
                    old = self._active_name(session_id, args)
                    resource = self.vault.rename(session_id, old, self._target(args["new_name"]))
                self._record_resource(session_id, resource)
                return {"ok": True, "resource": resource}
            if operation == "resource_read":
                name = self._active_name(session_id, args)
                record = self.store.load_session(session_id)
                region = args.get("region")
                reread = False
                if region is None and "target" not in args and record.get("last_read"):
                    region = record["last_read"].get("region")
                    reread = True
                full_content = self.vault.read(name)
                content = full_content
                if region is not None:
                    paragraphs = full_content.splitlines()
                    number = region["paragraph_number"]
                    if number > len(paragraphs):
                        raise CapabilityError("paragraph target is stale or out of range", code="stale_region")
                    content = paragraphs[number - 1]
                resource = record["active_resource"]
                self.store.update_session(session_id, last_read={"resource": resource, "content": content, "region": region})
                return {"ok": True, "resource": resource, "content": content, "region": region, "reread": reread, "read_verified": True}
            if operation == "resource_mutate":
                name = self._active_name(session_id, args)
                current = self._current_resource(session_id, name)
                expected_version = args.get("expected_version")
                if expected_version is not None and expected_version != current.get("version"):
                    raise CapabilityError(
                        "stale resource version; reread before mutating",
                        code="stale_resource",
                        details={"expected_version": expected_version, "actual_version": current.get("version")},
                    )
                idempotent = False
                if args["operation"] == "append":
                    current_content = self.vault.read(name)
                    if current_content.rstrip("\n").endswith(args["content"]):
                        resource = current
                        idempotent = True
                    else:
                        resource = self.vault.append(name, args["content"], session_id)
                        idempotent = False
                else:
                    region = args["region"]
                    if region["target_type"] == "paragraph":
                        resource = self.vault.replace_paragraph(name, region["paragraph_number"], args["content"], session_id)
                    else:
                        current_content = self.vault.read(name)
                        old_text = region["old_text"]
                        occurrences = current_content.count(old_text)
                        if occurrences != 1:
                            raise CapabilityError(
                                "exact text target must occur exactly once",
                                code="ambiguous_text_target" if occurrences > 1 else "stale_text_target",
                                details={"occurrences": occurrences},
                            )
                        resource = self.vault.replace_text(name, old_text, args["content"], session_id)
                self._record_resource(session_id, resource)
                persisted_content = self.vault.read(name)
                return {
                    "ok": True,
                    "resource": resource,
                    "operation": args["operation"],
                    "changed_region": args.get("region") or {"target_type": "append"},
                    "revision": resource["version"],
                    "persisted_content": persisted_content,
                    "content": persisted_content,
                    "write_verified": True,
                    "read_verified": True,
                    "idempotent": idempotent if args["operation"] == "append" else False,
                }
            if operation == "draft_manage":
                action = args["operation"]
                if action == "create":
                    draft = self.store.create_draft(session_id, args["content"], args.get("source_handles"))
                    self.store.update_session(session_id, current_draft_id=draft["id"])
                    return {"ok": True, "draft": draft}
                if action == "promote" and not args.get("draft_id"):
                    raise CapabilityError("promote requires an exact draft_id", code="invalid_draft_id")
                if args.get("draft_id"):
                    draft = self.store.load_draft(args["draft_id"])
                elif action == "read":
                    draft = self.store.latest_draft(session_id)
                else:
                    draft = self.store.active_draft(session_id)
                if draft["voice_session_id"] != session_id:
                    raise CapabilityError("draft belongs to another session")
                self.store.update_session(session_id, current_draft_id=draft["id"])
                if action == "promote" and draft["status"] == "discarded":
                    raise CapabilityError("draft has been discarded", code="draft_discarded")
                if action == "read":
                    return {"ok": True, "draft": draft, "content": draft["content"]}
                if action == "promote":
                    self._record_draft_event(session_id, {
                        "event": "DRAFT_PROMOTION_REQUEST",
                        "draft_id": draft["id"],
                        "revision": draft.get("revision_version", draft.get("revision", 0)),
                    })
                    approved_version = draft.get("approved_version")
                    revision_version = draft.get("revision_version", draft.get("revision", 0))
                    if not draft.get("approved") or approved_version != revision_version:
                        self._record_draft_event(session_id, {
                            "event": "DRAFT_PROMOTION_REJECTED",
                            "draft_id": draft["id"],
                            "revision": revision_version,
                            "reason": "draft_not_approved" if not draft.get("approved") else "approval_revision_mismatch",
                        })
                        raise CapabilityError(
                            "draft has not been explicitly approved for its current revision",
                            code="draft_not_approved",
                            details={"draft_id": draft["id"], "approved": bool(draft.get("approved")), "revision_version": revision_version, "approved_version": approved_version},
                        )
                if action == "promote" and draft["status"] == "promoted":
                    promoted_resource = draft.get("promoted_resource") or {}
                    target = args.get("target")
                    accepted_targets = {
                        str(promoted_resource.get("display_name", "")),
                        str(promoted_resource.get("canonical_id", "")),
                        Path(str(promoted_resource.get("canonical_id", ""))).name,
                        Path(str(promoted_resource.get("display_name", ""))).stem,
                    }
                    if not target or target in accepted_targets:
                        self._record_draft_event(session_id, {
                            "event": "DRAFT_PROMOTION_ALLOWED",
                            "draft_id": draft["id"],
                            "revision": draft.get("revision_version", draft.get("revision", 0)),
                            "idempotent": True,
                        })
                        return {"ok": True, "draft": draft, "resource": promoted_resource, "content": draft["content"], "idempotent": True}
                    raise CapabilityError("promoted draft already has a different target", code="draft_already_promoted")
                if action == "discard":
                    if draft["status"] == "promoted":
                        raise CapabilityError("promoted draft cannot be discarded")
                    draft["status"] = "discarded"
                    return {"ok": True, "draft": self.store.update_draft(draft)}
                if draft["status"] != "active":
                    raise CapabilityError(f"{draft['status']} draft cannot be {action}d")
                if action == "revise":
                    old_revision = draft.get("revision_version", draft.get("revision", 0))
                    draft["content"] = args["content"]
                    draft["revision"] += 1
                    draft["revision_version"] = draft["revision"]
                    draft["approved"] = False
                    draft["approved_at_turn"] = None
                    draft["approved_version"] = None
                    draft["workflow_status"] = "DRAFT_OPEN"
                    self._record_draft_event(session_id, {
                        "event": "DRAFT_APPROVAL_INVALIDATED",
                        "draft_id": draft["id"],
                        "old_revision": old_revision,
                        "new_revision": draft["revision_version"],
                        "reason": "content_changed",
                    })
                    if "source_handles" in args:
                        draft["source_handles"] = list(args["source_handles"] or [])
                    return {"ok": True, "draft": self.store.update_draft(draft)}
                if not args.get("target"):
                    existing = self.store.load_session(session_id).get("active_resource")
                    if existing:
                        try:
                            if self.vault.read(existing["path"]) == draft["content"]:
                                draft["status"] = "promoted"
                                draft["promoted_resource"] = existing
                                self.store.update_draft(draft)
                                self._record_draft_event(session_id, {
                                    "event": "DRAFT_PROMOTION_ALLOWED",
                                    "draft_id": draft["id"],
                                    "revision": draft.get("revision_version", draft.get("revision", 0)),
                                    "idempotent": True,
                                })
                                return {"ok": True, "draft": draft, "resource": existing, "content": draft["content"], "idempotent": True}
                        except (CapabilityError, FileNotFoundError, KeyError, OSError):
                            pass
                    raise CapabilityError("target must be a non-empty string")
                target = self._target(args["target"])
                try:
                    existing_content = self.vault.read(target)
                except (CapabilityError, FileNotFoundError, KeyError, OSError):
                    existing_content = None
                if existing_content is not None:
                    if existing_content != draft["content"]:
                        raise CapabilityError(f"resource already exists: {target}")
                    resource = self._current_resource(session_id, target)
                    reread = existing_content
                    idempotent = True
                else:
                    resource = self.vault.create(session_id, target, draft["content"])
                    reread = self.vault.read(target)
                    idempotent = False
                verified_resource = self.vault.open(session_id, target)
                verified_content = self.vault.read(target)
                self._record_resource(session_id, verified_resource)
                draft["status"] = "promoted"
                draft["workflow_status"] = "SAVED"
                draft["promoted_resource"] = verified_resource
                self.store.update_draft(draft)
                self._record_draft_event(session_id, {
                    "event": "DRAFT_PROMOTION_ALLOWED",
                    "draft_id": draft["id"],
                    "revision": draft.get("revision_version", draft.get("revision", 0)),
                    "idempotent": idempotent,
                })
                return {"ok": True, "draft": draft, "resource": verified_resource, "content": verified_content, "verification": {"resource": verified_resource, "content": verified_content, "read_verified": True}, "idempotent": idempotent}
            if operation == "retrieval":
                mode = args["mode"]
                if mode == "note":
                    shared = self.shared_knowledge.retrieve_for_voice(
                        args["query"],
                        selected_topic=(self.store.load_session(session_id).get("active_prepared_topic") or {}).get("topic_id"),
                        limit=args.get("limit", MAX_RESULTS),
                    ) if self.shared_knowledge is not None else {"status": "missing", "results": []}
                    if shared["results"]:
                        results = shared["results"]
                        scope = "shared_knowledge"
                        topic_id = next((item.get("topic_id") for item in results if item.get("kind") == "prepared"), None)
                    else:
                        results, scope, topic_id, _query_id = self._search_authoritative_notes(session_id, args["query"], args.get("limit", MAX_RESULTS))
                elif mode == "current_web":
                    results = self.web_retriever(args["query"], args.get("limit", 5))
                    scope = "current_web"
                    topic_id = None
                else:
                    results = [self.store.prepared_context(session_id)]
                    scope = "prepared"
                    topic_id = None
                handle = {"id": f"retrieval-{int(time.time() * 1000)}", "mode": mode, "query": args["query"]}
                results = self._retrieval_envelopes(results, args["query"], handle["id"])
                record = self.store.load_session(session_id)
                self.store.update_session(session_id, retrieval_handles=record.get("retrieval_handles", []) + [handle])
                state = self.working_states.get(session_id)
                if state is not None and HandleRef is not None:
                    state.retrieval_handles.append(HandleRef(kind="retrieval", identifier=handle["id"], source=mode))
                    self._save_working_state(session_id, state)
                response = {"ok": True, "handle": handle, "results": results, "scope": scope}
                if topic_id is not None:
                    response["topic_id"] = topic_id
                return response
            if operation == "assignment_capture":
                assignment = self.store.record_assignment(session_id, args)
                return {"ok": True, "assignment_id": assignment["id"], "assignment": assignment}
            if operation == "topic_activate":
                return self.store.activate_prepared_topic(session_id, args["query"])
        except CapabilityError:
            raise
        except (KeyError, OSError, ValueError, TypeError) as exc:
            raise CapabilityError(str(exc)) from exc
        raise CapabilityError(f"unsupported operation: {operation}")

    def complete_session(self, session_id: str, *, transcript: str | None, final_topic: str = "") -> dict[str, Any]:
        return self.store.complete(session_id, transcript=transcript, final_topic=final_topic)
