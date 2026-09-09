from __future__ import annotations

import json
import hashlib
import re
import time
import uuid
from pathlib import Path
from typing import Any


class VoiceSessionStore:
    """Durable, Hermes-owned V1 handoff artifacts.

    The store is intentionally boring JSON: atomic replace, no hidden database,
    and easy recovery/inspection after a Pi crash. It is not a second memory
    system; it is the bounded handoff ledger for one voice session.
    """

    def __init__(self, root: Path, *, session_db: Any | None = None):
        self.root = Path(root).expanduser()
        self.session_db = session_db
        self.sessions = self.root / "sessions"
        self.assignments = self.root / "assignments"
        self.drafts = self.root / "drafts"
        self.quick_notes_path = self.root / "quick-notes.json"
        self.prepared_topics = self.root / "prepared-topics"
        self.sessions.mkdir(parents=True, exist_ok=True)
        self.assignments.mkdir(parents=True, exist_ok=True)
        self.drafts.mkdir(parents=True, exist_ok=True)
        self.prepared_topics.mkdir(parents=True, exist_ok=True)

    def ensure_canonical_session(self, session_id: str) -> None:
        if self.session_db is None:
            return
        if self.session_db.get_session(session_id) is None:
            self.session_db.create_session(session_id, source="rex_voice_v1", model="rex-gemma")

    def _session_path(self, session_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id):
            raise ValueError("invalid session_id")
        return self.sessions / f"{session_id}.json"

    def start_session(self, session_id: str, *, topic: str = "", active_resource: dict | None = None) -> dict[str, Any]:
        self.ensure_canonical_session(session_id)
        record = {
            "voice_session_id": session_id,
            "status": "active",
            "started_at": time.time(),
            "ended_at": None,
            "topic": topic,
            "active_resource": active_resource,
            "resources_touched": [],
            "assignments": [],
            "drafts": [],
            "current_draft_id": None,
            "draft_workflow_events": [],
            "retrieval_handles": [],
            "unresolved_questions": [],
            "explicit_follow_ups": [],
            "backend_errors": [],
            "transcript": None,
            "last_call_summary": "",
            "post_call_prepared_context": None,
            "last_read": None,
            "authorized_resource_roots": [],
            "active_prepared_topic": None,
            "prepared_topic_events": [],
        }
        self._write(self._session_path(session_id), record)
        return record

    def load_session(self, session_id: str) -> dict[str, Any]:
        path = self._session_path(session_id)
        if not path.exists():
            raise KeyError(f"unknown voice session: {session_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def update_session(self, session_id: str, **updates: Any) -> dict[str, Any]:
        record = self.load_session(session_id)
        record.update(updates)
        self._write(self._session_path(session_id), record)
        return record

    def quick_notes(self) -> dict[str, Any]:
        if not self.quick_notes_path.exists():
            return {"schema": "rex-quick-notes-v1", "entries": []}
        value = json.loads(self.quick_notes_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema") != "rex-quick-notes-v1" or not isinstance(value.get("entries"), list):
            raise ValueError("quick notes artifact has unsupported schema")
        return value

    def set_quick_notes(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(entries, list) or len(entries) > 64:
            raise ValueError("quick notes must contain at most 64 entries")
        keys: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("quick note must be an object")
            key = entry.get("key")
            if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{1,96}", key):
                raise ValueError("quick note key must be a lowercase stable identifier")
            if key in keys:
                raise ValueError("quick note keys must be unique")
            keys.add(key)
            if entry.get("confidence_class") not in {"explicit", "implied"}:
                raise ValueError("quick notes cannot contain inferred confidence")
            if entry.get("provenance") not in {"explicit_user_statement", "repeated_user_preference", "runtime_observation"}:
                raise ValueError("unsupported quick note provenance")
            source_refs = entry.get("source_refs")
            if not isinstance(source_refs, list) or not source_refs or not all(isinstance(ref, str) and ref.strip() for ref in source_refs):
                raise ValueError("quick notes require source_refs")
            if "value" not in entry:
                raise ValueError("quick note requires value")
        result = {"schema": "rex-quick-notes-v1", "entries": entries}
        if len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) > 20_000:
            raise ValueError("quick notes exceed the 20 KB bound")
        self._write(self.quick_notes_path, result)
        return result

    def promote_learning_records(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        current = self.quick_notes()["entries"]
        by_key = {entry["key"]: entry for entry in current}
        for record in records:
            if record.get("confidence_class") == "inferred" or record.get("provenance") == "model_inference":
                continue
            identifier = record.get("id")
            statement = record.get("statement")
            if not isinstance(identifier, str) or not isinstance(statement, str) or not statement.strip():
                continue
            by_key[f"learning.{identifier}"] = {
                "key": f"learning.{identifier}",
                "value": statement.strip(),
                "confidence_class": record["confidence_class"],
                "provenance": record["provenance"],
                "source_refs": list(record["source_refs"]),
            }
        return self.set_quick_notes(list(by_key.values()))

    @staticmethod
    def _topic_text(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()

    def _prepared_topic_path(self, topic_id: str) -> Path:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", topic_id):
            raise ValueError("invalid prepared topic id")
        return self.prepared_topics / f"{topic_id}.json"

    def set_prepared_topic(
        self,
        topic_id: str,
        *,
        aliases: list[str],
        content: str,
        source_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        path = self._prepared_topic_path(topic_id)
        if not isinstance(aliases, list) or not aliases:
            raise ValueError("prepared topic requires aliases")
        normalized_aliases = sorted({self._topic_text(alias) for alias in aliases if isinstance(alias, str) and self._topic_text(alias)}, key=lambda value: (-len(value), value))
        if not normalized_aliases:
            raise ValueError("prepared topic requires non-empty aliases")
        if not isinstance(content, str) or not content.strip() or len(content) > 12_000:
            raise ValueError("prepared topic content must be non-empty and at most 12 KB")
        refs = list(source_refs or [])
        if not all(isinstance(ref, str) and ref.strip() for ref in refs):
            raise ValueError("prepared topic source_refs must contain strings")
        digest = hashlib.sha256((topic_id + "\n" + "\n".join(normalized_aliases) + "\n" + content).encode("utf-8")).hexdigest()[:16]
        packet = {
            "schema": "rex-prepared-topic-v1",
            "topic_id": topic_id,
            "aliases": normalized_aliases,
            "content": content.strip(),
            "source_refs": refs[:16],
            "version": digest,
        }
        self._write(path, packet)
        return packet

    def activate_prepared_topic(self, session_id: str, query: str) -> dict[str, Any]:
        self.load_session(session_id)
        packet = self._matching_prepared_topic(query)
        if packet is None:
            return {"ok": False, "reason": "no_match", "query": query}
        return self._activate_topic_packet(session_id, packet)

    def activate_prepared_topic_live_turn(self, session_id: str, query: str) -> dict[str, Any]:
        """Conservatively activate one prepared packet before a live model turn."""
        record = self.load_session(session_id)
        text = query if isinstance(query, str) else ""
        normalized = self._topic_text(text)
        active = record.get("active_prepared_topic")
        intent = self._live_topic_intent(text)
        if self._live_topic_negation(text):
            message = "PREPARED_TOPIC_NO_MATCH"
            self._record_prepared_topic_event(session_id, message)
            return {"ok": False, "reason": "no_match", "telemetry": message}
        if not intent:
            if isinstance(active, dict) and active.get("topic_id") and active.get("version"):
                message = f"PREPARED_TOPIC_REUSED topic_id={active['topic_id']} version={active['version']}"
                self._record_prepared_topic_event(session_id, message)
                return {"ok": True, "reused": True, "topic_id": active["topic_id"], "version": active["version"], "telemetry": message}
            message = "PREPARED_TOPIC_NO_MATCH"
            self._record_prepared_topic_event(session_id, message)
            return {"ok": False, "reason": "no_match", "telemetry": message}
        packet = self._matching_prepared_topic(text)
        if packet is None:
            message = "PREPARED_TOPIC_NO_MATCH"
            self._record_prepared_topic_event(session_id, message)
            return {"ok": False, "reason": "no_match", "telemetry": message}
        match_message = f"PREPARED_TOPIC_MATCH query={normalized[:120]} topic_id={packet['topic_id']} version={packet['version']}"
        self._record_prepared_topic_event(session_id, match_message, query=normalized)
        if isinstance(active, dict) and active.get("topic_id") == packet["topic_id"] and active.get("version") == packet["version"]:
            message = f"PREPARED_TOPIC_REUSED topic_id={packet['topic_id']} version={packet['version']}"
            self._record_prepared_topic_event(session_id, message)
            return {"ok": True, "reused": True, "topic_id": packet["topic_id"], "version": packet["version"], "telemetry": message}
        result = self._activate_topic_packet(session_id, packet)
        message = f"PREPARED_TOPIC_ACTIVATED topic_id={packet['topic_id']} version={packet['version']} source=automatic_live_turn"
        self._record_prepared_topic_event(session_id, message, query=normalized)
        result.update({"source": "automatic_live_turn", "telemetry": message})
        return result

    @staticmethod
    def _live_topic_negation(query: str) -> bool:
        normalized = query.casefold()
        return bool(re.search(r"\b(?:don['’]?t|do not|never)\s+(?:want to\s+)?(?:talk about|discuss|work on|go over)", normalized))

    @staticmethod
    def _live_topic_intent(query: str) -> bool:
        normalized = query.casefold()
        return bool(re.search(
            r"\b(?:i want to\s+(?:talk about|discuss|work on|go over)|"
            r"let['’]?s\s+(?:talk about|discuss|work on|go over)|"
            r"tell me about|can we\s+(?:talk about|discuss|work on|go over)|"
            r"what do we know about)\b",
            normalized,
        ))

    def _matching_prepared_topic(self, query: str) -> dict[str, Any] | None:
        normalized_query = self._topic_text(query)
        if not normalized_query:
            return None
        candidates: list[tuple[int, str, dict[str, Any]]] = []
        for path in self.prepared_topics.glob("*.json"):
            packet = json.loads(path.read_text(encoding="utf-8"))
            for alias in packet.get("aliases", []):
                if isinstance(alias, str) and re.search(rf"(?:^| ){re.escape(alias)}(?:$| )", normalized_query):
                    candidates.append((len(alias), packet["topic_id"], packet))
        return sorted(candidates, key=lambda item: (-item[0], item[1]))[0][2] if candidates else None

    def _activate_topic_packet(self, session_id: str, packet: dict[str, Any]) -> dict[str, Any]:
        active = {
            "topic_id": packet["topic_id"],
            "version": packet["version"],
            "content": packet["content"],
            "source_refs": packet.get("source_refs", []),
        }
        self.update_session(session_id, active_prepared_topic=active)
        return {"ok": True, "topic_id": packet["topic_id"], "version": packet["version"], "packet": packet}

    def _record_prepared_topic_event(self, session_id: str, message: str, *, query: str = "") -> None:
        record = self.load_session(session_id)
        event = {"at": time.time(), "message": message}
        if query:
            event["query"] = query[:120]
        record.setdefault("prepared_topic_events", []).append(event)
        record["prepared_topic_events"] = record["prepared_topic_events"][-100:]
        self._write(self._session_path(session_id), record)

    def record_assignment(self, session_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        record = self.load_session(session_id)
        assignment = {
            "id": f"assignment-{uuid.uuid4().hex[:12]}",
            "voice_session_id": session_id,
            "created_at": time.time(),
            "status": "captured",
            "topic": fields.get("topic") or record.get("topic", ""),
            "request": fields["request"],
            "assignment_type": fields.get("assignment_type", "writing"),
            "active_resource": record.get("active_resource"),
            "transcript_refs": fields.get("transcript_refs", []),
            "source_handles": fields.get("source_handles", []),
            "desired_outputs": fields.get("desired_outputs", []),
            "priority": fields.get("priority", "normal"),
            "unresolved_questions": fields.get("unresolved_questions", []),
            "notify_on_completion": bool(fields.get("notify_on_completion", False)),
            "notification_channel": fields.get("notification_channel", "sms"),
            "notification": {"status": "pending"} if fields.get("notify_on_completion", False) else None,
        }
        self._write(self.assignments / f"{assignment['id']}.json", assignment)
        record["assignments"].append(assignment)
        self._write(self._session_path(session_id), record)
        if self.session_db is not None:
            self.session_db.patch_session_model_config(
                session_id,
                {"_rex_voice_assignments": record["assignments"]},
            )
        return assignment

    def create_draft(self, session_id: str, content: str, source_handles: list[str] | None = None) -> dict[str, Any]:
        self.load_session(session_id)
        draft = {
            "id": f"draft-{uuid.uuid4().hex[:12]}",
            "voice_session_id": session_id,
            "created_at": time.time(),
            "updated_at": time.time(),
            "status": "active",
            "workflow_status": "DRAFT_OPEN",
            "revision": 0,
            "revision_version": 0,
            "approved": False,
            "approved_at_turn": None,
            "approved_version": None,
            "content": content,
            "source_handles": list(source_handles or []),
            "promoted_resource": None,
        }
        self._write(self.drafts / f"{draft['id']}.json", draft)
        record = self.load_session(session_id)
        record.setdefault("drafts", []).append(self._draft_summary(draft))
        self._write(self._session_path(session_id), record)
        return draft

    def load_draft(self, draft_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"draft-[A-Za-z0-9]{12}", draft_id):
            raise ValueError("invalid draft_id")
        path = self.drafts / f"{draft_id}.json"
        if not path.exists():
            raise KeyError(f"unknown draft: {draft_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def active_draft(self, session_id: str) -> dict[str, Any]:
        matches = []
        for path in self.drafts.glob("draft-*.json"):
            draft = json.loads(path.read_text(encoding="utf-8"))
            if draft.get("voice_session_id") == session_id and draft.get("status") == "active":
                matches.append(draft)
        if not matches:
            raise KeyError("no active draft")
        if len(matches) > 1:
            raise ValueError("multiple active drafts; draft_id is required")
        return matches[0]

    def latest_draft(self, session_id: str) -> dict[str, Any]:
        matches = []
        for path in self.drafts.glob("draft-*.json"):
            draft = json.loads(path.read_text(encoding="utf-8"))
            if draft.get("voice_session_id") == session_id:
                matches.append(draft)
        if not matches:
            raise KeyError("no draft")
        return max(matches, key=lambda item: item.get("updated_at", item.get("created_at", 0)))

    def update_draft(self, draft: dict[str, Any]) -> dict[str, Any]:
        draft["updated_at"] = time.time()
        self._write(self.drafts / f"{draft['id']}.json", draft)
        record = self.load_session(draft["voice_session_id"])
        summaries = record.setdefault("drafts", [])
        summary = self._draft_summary(draft)
        record["drafts"] = [summary if item.get("id") == draft["id"] else item for item in summaries]
        self._write(self._session_path(draft["voice_session_id"]), record)
        return draft

    @staticmethod
    def _draft_summary(draft: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": draft["id"],
            "status": draft["status"],
            "workflow_status": draft.get("workflow_status", "DRAFT_OPEN"),
            "revision": draft["revision"],
            "revision_version": draft.get("revision_version", draft["revision"]),
            "approved": bool(draft.get("approved", False)),
            "approved_at_turn": draft.get("approved_at_turn"),
            "approved_version": draft.get("approved_version"),
            "source_handles": list(draft.get("source_handles", [])),
        }

    def complete(self, session_id: str, *, transcript: str | None, final_topic: str = "", errors: list[str] | None = None) -> dict[str, Any]:
        record = self.load_session(session_id)
        for assignment in record.get("assignments", []):
            if assignment.get("status") == "captured":
                assignment["status"] = "queued"
                self._write(self.assignments / f"{assignment['id']}.json", assignment)
        record = self.update_session(
            session_id,
            status="completed",
            ended_at=time.time(),
            transcript=transcript,
            topic=final_topic or self.load_session(session_id).get("topic", ""),
            assignments=record.get("assignments", []),
            backend_errors=errors or self.load_session(session_id).get("backend_errors", []),
        )
        if self.session_db is not None:
            self.session_db.patch_session_model_config(
                session_id,
                {
                    "_rex_voice_assignments": record.get("assignments", []),
                    "_rex_voice_handoff": {
                        "status": "queued",
                        "voice_session_id": session_id,
                        "assignment_ids": [item["id"] for item in record.get("assignments", [])],
                        "transcript": transcript,
                    },
                },
            )
        return record

    def abort(self, session_id: str, *, error: str) -> dict[str, Any]:
        record = self.load_session(session_id)
        errors = list(record.get("backend_errors", []))
        errors.append(error)
        record = self.update_session(session_id, status="aborted", ended_at=time.time(), backend_errors=errors)
        if self.session_db is not None:
            self.session_db.patch_session_model_config(
                session_id,
                {"_rex_voice_handoff": {"status": "aborted", "voice_session_id": session_id, "error": error}},
            )
        return record

    def reconcile_abandoned(self, session_id: str, *, error: str = "owner_missing") -> dict[str, Any] | None:
        """Abort an active session whose owning runtime is definitively gone."""
        try:
            record = self.load_session(session_id)
        except KeyError:
            return None
        if record.get("status") != "active":
            return record
        return self.abort(session_id, error=error)

    def prepared_context(self, session_id: str) -> dict[str, Any]:
        record = self.load_session(session_id)
        context = {
            "quick_notes": self.quick_notes()["entries"],
            "topic": record.get("topic", ""),
            "active_resource": record.get("active_resource"),
            "active_topic": record.get("active_prepared_topic"),
            "last_read": record.get("last_read"),
            "last_call_summary": record.get("last_call_summary", ""),
            "outstanding_assignments": [
                {"id": a["id"], "request": a["request"], "status": a["status"]}
                for a in record.get("assignments", [])
                if a.get("status") != "completed"
            ],
            "active_drafts": [
                draft for draft in record.get("drafts", []) if draft.get("status") == "active"
            ],
            "unresolved_questions": record.get("unresolved_questions", [])[-8:],
            "relevant_handles": record.get("retrieval_handles", [])[-8:],
            "continuation_points": record.get("explicit_follow_ups", [])[-8:],
            "authorized_resource_roots": record.get("authorized_resource_roots", []),
        }
        prepared = record.get("post_call_prepared_context")
        if isinstance(prepared, dict):
            context["post_call"] = prepared
        else:
            latest = self._latest_prepared_context(exclude_session_id=session_id)
            if latest is not None:
                context["post_call"] = latest
        pending = self._pending_post_call_jobs()
        if pending:
            context["pending_post_call_jobs"] = pending
        return context

    def _latest_prepared_context(self, *, exclude_session_id: str) -> dict[str, Any] | None:
        candidates: list[tuple[float, dict[str, Any]]] = []
        for path in self.sessions.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if record.get("voice_session_id") == exclude_session_id:
                continue
            prepared = record.get("post_call_prepared_context")
            if isinstance(prepared, dict):
                candidates.append((float(record.get("ended_at") or 0), prepared))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def _pending_post_call_jobs(self) -> list[dict[str, Any]]:
        jobs = self.root / "post-call" / "jobs"
        pending: list[dict[str, Any]] = []
        for path in sorted(jobs.glob("post-call-*.json")):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if job.get("status") == "completed":
                continue
            pending.append({
                "session_id": job.get("session_id"),
                "status": job.get("status"),
                "work": [
                    {"title": item.get("title"), "status": item.get("status"), "provenance": item.get("provenance")}
                    for item in job.get("work", []) if isinstance(item, dict)
                ],
            })
        return pending[-8:]

    @staticmethod
    def _write(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
