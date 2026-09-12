from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .improvement_policy import classify_improvement
from .github_improvement import apply_github_backed_improvement, default_tests
from .store import VoiceSessionStore
from .settings import user_data_root
from .call_learning import (
    get_call_learning_status,
    proposal_from_legacy,
    run_learning,
)
from .shared_knowledge import SharedKnowledgeError, promote_post_call_to_shared_knowledge


PROVENANCE = {
    "explicit_assignment",
    "inferred_followup",
    "unresolved_question",
    "research_opportunity",
    "note_consolidation",
}
WORK_STATES = {"proposed", "validated", "queued", "executing", "completed", "failed", "uncertain"}
LEARNING_KINDS = {
    "explicit_preference",
    "explicit_decision",
    "feature_request",
    "behavioral_correction",
    "project_information",
    "possible_inference",
    "runtime_failure",
}
LEARNING_CONFIDENCE_CLASSES = {"explicit", "implied", "inferred"}
LEARNING_PROVENANCE = {
    "explicit_user_statement",
    "repeated_user_preference",
    "runtime_observation",
    "model_inference",
}


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _verified_provider_evidence(queue: "PostCallQueue", proposal: dict[str, Any], evidence: Any) -> dict[str, Any]:
    """Accept completion only when the referenced provider artifact verifies."""
    if not isinstance(evidence, dict) or evidence.get("status") != "completed":
        raise ValueError(f"executor returned no completed provider evidence: {evidence}")
    refs = evidence.get("artifact_refs")
    if not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], str) or not refs[0].strip():
        raise ValueError("executor must return exactly one artifact reference")
    artifact = Path(refs[0]).expanduser()
    work_root = (queue.root / "work").resolve()
    try:
        artifact = artifact.resolve()
    except OSError as exc:
        raise ValueError(f"provider artifact path cannot be resolved: {artifact}") from exc
    if not artifact.is_relative_to(work_root) or artifact.name != "result.json" or not artifact.is_file():
        raise ValueError(f"provider artifact is outside the queue work tree: {artifact}")
    try:
        result = _read(artifact)
    except Exception as exc:
        raise ValueError(f"provider artifact is not valid JSON: {artifact}") from exc
    proposal_id = str(proposal["id"])
    if result.get("schema") != "rex-hermes-work-result-v1":
        raise ValueError("provider artifact has unsupported schema")
    if result.get("proposal_id") != proposal_id or result.get("status") != "completed":
        raise ValueError("provider artifact does not identify completed work for this proposal")
    if not isinstance(result.get("summary"), str) or not result["summary"].strip():
        raise ValueError("provider artifact requires a summary")
    content = result.get("content")
    if not ((isinstance(content, str) and content.strip()) or (isinstance(content, (dict, list)) and content)):
        raise ValueError("provider artifact requires content")
    if not isinstance(result.get("sources"), list):
        raise ValueError("provider artifact requires sources")
    execution = result.get("execution")
    if not isinstance(execution, dict) or not isinstance(execution.get("actions"), list) or not execution["actions"]:
        raise ValueError("provider artifact requires execution actions")
    if not all(isinstance(execution.get(key), str) and execution[key].strip() for key in ("provider", "session_id")):
        raise ValueError("provider artifact requires provider and session_id")
    return {
        "status": "completed",
        "provider": execution["provider"],
        "session_id": execution["session_id"],
        "artifact_refs": [str(artifact)],
        "summary": result["summary"],
        "sources": result["sources"],
        "execution": execution,
    }


def _persist_prepared_context(packet: dict[str, Any], context: dict[str, Any], job_id: str) -> None:
    path = Path(packet["evidence"]["session_record"]["path"])
    record = _read(path)
    record["post_call_prepared_context"] = context
    record["post_call_job_id"] = job_id
    _write(path, record)


def _bounded(values: Any, limit: int = 8, chars: int = 600) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value)[:chars] for value in values if isinstance(value, str) and value.strip()][-limit:]


def _learning_records(value: Any, *, packet: dict[str, Any]) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("learning_records must be a list")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value[:8]:
        if not isinstance(raw, dict):
            raise ValueError("learning record must be an object")
        kind = raw.get("kind")
        statement = raw.get("statement")
        confidence_class = raw.get("confidence_class")
        provenance = raw.get("provenance")
        source_refs = raw.get("source_refs")
        if kind not in LEARNING_KINDS:
            raise ValueError("unknown learning record kind")
        if not isinstance(statement, str) or not statement.strip():
            raise ValueError("learning record requires a statement")
        if confidence_class not in LEARNING_CONFIDENCE_CLASSES:
            raise ValueError("unknown learning confidence class")
        if provenance not in LEARNING_PROVENANCE:
            raise ValueError("unknown learning provenance")
        if not isinstance(source_refs, list) or not source_refs or not all(isinstance(item, str) and item.strip() for item in source_refs[:8]):
            raise ValueError("learning record requires source_refs")
        source_refs = [item[:300] for item in source_refs[:8]]
        identity = "|".join([packet.get("session_id", ""), kind, statement.strip(), *source_refs])
        identifier = f"learning-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"
        if identifier in seen:
            raise ValueError("learning record identities must be unique")
        seen.add(identifier)
        normalized.append({
            "id": identifier,
            "kind": kind,
            "statement": statement.strip()[:2000],
            "confidence_class": confidence_class,
            "provenance": provenance,
            "source_refs": source_refs,
        })
    return normalized


def _conversation_evidence(events: Path) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if not events.exists():
        return messages
    for line in events.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "message_end" or not isinstance(event.get("message"), dict):
            continue
        message = event["message"]
        role = message.get("role")
        if role not in {"user", "assistant"} or not isinstance(message.get("content"), list):
            continue
        text = "".join(str(item.get("text", "")) for item in message["content"] if isinstance(item, dict) and item.get("type") == "text").strip()
        if not text:
            continue
        if role == "user" and "User request: " in text:
            text = text.rsplit("User request: ", 1)[-1].strip()
        messages.append({"role": role, "text": text[:4000]})
    return messages[-80:]


def _tool_audit(events: Path) -> dict[str, Any]:
    """Build call-local execution evidence without trusting model prose."""
    requested: list[str] = []
    errors: list[dict[str, Any]] = []
    result_count = 0
    if events.exists():
        for line in events.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "tool_execution_start":
                name = event.get("toolName")
                if isinstance(name, str):
                    requested.append(name)
            elif event.get("type") == "tool_execution_end":
                result_count += 1
                if event.get("isError"):
                    errors.append({"tool": event.get("toolName"), "result": event.get("result")})
    return {"requested": requested, "result_count": result_count, "error_count": len(errors), "errors": errors[-20:]}


def _resource_audit(resources: Any) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for raw in resources if isinstance(resources, list) else []:
        if not isinstance(raw, str):
            continue
        path = Path(raw).expanduser()
        item: dict[str, Any] = {"path": raw, "exists": path.is_file()}
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
                item.update({"bytes": path.stat().st_size, "word_count_wc": len(text.split())})
            except OSError as exc:
                item["read_error"] = str(exc)
        findings.append(item)
    return findings


def validate_interpretation(value: Any, *, packet: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("interpretation must be an object")
    result = {
        "topics": _bounded(value.get("topics"), 8),
        "ideas": _bounded(value.get("ideas"), 8),
        "decisions": _bounded(value.get("decisions"), 8),
        "corrections": _bounded(value.get("corrections"), 8),
        "unresolved_questions": _bounded(value.get("unresolved_questions"), 8),
        "candidate_followups": _bounded(value.get("candidate_followups"), 8),
        "work_proposals": [],
        "suppressed_work_proposals": [],
        "improvement_proposals": [],
        "learning_records": [],
    }
    if value.get("work_proposals", []) is None:
        proposals: list[Any] = []
    else:
        proposals = value.get("work_proposals", [])
    if not isinstance(proposals, list):
        raise ValueError("work_proposals must be a list")
    seen: set[str] = set()
    for raw in proposals[:8]:
        if not isinstance(raw, dict):
            raise ValueError("work proposal must be an object")
        identifier = raw.get("id")
        request = raw.get("request")
        provenance = raw.get("provenance")
        if not all(isinstance(item, str) and item.strip() for item in (identifier, request, provenance)):
            raise ValueError("work proposal requires id, request, and provenance")
        if identifier in seen:
            raise ValueError("work proposal ids must be unique")
        if provenance not in PROVENANCE:
            raise ValueError("unknown work proposal provenance")
        confidence = raw.get("confidence", 0.0)
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("work proposal confidence must be between 0 and 1")
        seen.add(identifier)
        proposal = {
            "id": identifier[:128],
            "title": str(raw.get("title", request))[:240],
            "request": request[:2000],
            "provenance": provenance,
            "confidence": float(confidence),
            "source_refs": _bounded(raw.get("source_refs"), 8, 300),
            "assignment_id": raw.get("assignment_id") if isinstance(raw.get("assignment_id"), str) else None,
            "notify_on_completion": bool(raw.get("notify_on_completion", False)),
        }
        if packet is not None and not _has_work_warrant(packet):
            result["suppressed_work_proposals"].append({
                **proposal,
                "reason": "no work warrant in closed call",
            })
        else:
            result["work_proposals"].append(proposal)
    improvements = value.get("improvement_proposals", [])
    if improvements is None:
        improvements = []
    if not isinstance(improvements, list):
        raise ValueError("improvement_proposals must be a list")
    seen_improvements: set[str] = set()
    for raw in improvements[:8]:
        if not isinstance(raw, dict):
            raise ValueError("improvement proposal must be an object")
        identifier, title, request = raw.get("id"), raw.get("title"), raw.get("request")
        if not all(isinstance(item, str) and item.strip() for item in (identifier, title, request)):
            raise ValueError("improvement proposal requires id, title, and request")
        assert isinstance(identifier, str) and isinstance(title, str) and isinstance(request, str)
        if identifier in seen_improvements:
            raise ValueError("improvement proposal ids must be unique")
        confidence = raw.get("confidence", 0.0)
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("improvement proposal confidence must be between 0 and 1")
        seen_improvements.add(identifier)
        normalized = {
            "id": identifier[:128], "title": title[:240], "request": request[:2000],
            "confidence": float(confidence), "execution": str(raw.get("execution", "background_or_next_release"))[:80],
            "requested_by_user": bool(raw.get("requested_by_user", False)),
            "transcript_evidence": _bounded(raw.get("transcript_evidence"), 8, 600),
            "deterministic_spec": raw.get("deterministic_spec") if isinstance(raw.get("deterministic_spec"), dict) else {},
            "source_refs": _bounded(raw.get("source_refs"), 8, 300),
            "target_files": _bounded(raw.get("target_files"), 8, 240),
            "patch": raw.get("patch") if isinstance(raw.get("patch"), str) else "",
        }
        decision = classify_improvement(normalized, packet or {})
        normalized.update({"policy_class": decision["policy_class"], "status": decision["approval"],
                           "approval": "not_required" if decision["policy_class"] == "AUTO" else "required"})
        result["improvement_proposals"].append(normalized)
    result["learning_records"] = _learning_records(value.get("learning_records"), packet=packet or {})
    return result


def _has_work_warrant(packet: dict[str, Any]) -> bool:
    """Require call-local evidence before turning interpretation into work."""
    record = packet.get("call_record", {})
    return any(record.get(key) for key in ("assignments", "explicit_follow_ups", "unresolved_questions"))


def _explicit_assignment_proposals(packet: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn every captured assignment into executable work deterministically."""
    proposals: list[dict[str, Any]] = []
    for assignment in packet.get("call_record", {}).get("assignments", []):
        if not isinstance(assignment, dict) or assignment.get("status") not in {"queued", "captured", "completed"}:
            continue
        assignment_id = assignment.get("id")
        request = assignment.get("request")
        if not isinstance(assignment_id, str) or not isinstance(request, str) or not request.strip():
            continue
        proposals.append({
            "id": f"assignment-work-{assignment_id}",
            "title": str(assignment.get("topic") or request)[:240],
            "request": request[:2000],
            "assignment_type": assignment.get("assignment_type", "writing"),
            "provenance": "explicit_assignment",
            "confidence": 1.0,
            "source_refs": [str(value) for value in assignment.get("source_handles", []) if isinstance(value, str)],
            "assignment_id": assignment_id,
            "notify_on_completion": bool(assignment.get("notify_on_completion", False)),
        })
    return proposals


def _persist_assignment_result(packet: dict[str, Any], proposal: dict[str, Any], *, status: str, notification: dict[str, Any] | None = None, error: str | None = None) -> None:
    assignment_id = proposal.get("assignment_id")
    if not isinstance(assignment_id, str):
        return
    path = Path(packet["evidence"]["session_record"]["path"])
    record = _read(path)
    for assignment in record.get("assignments", []):
        if assignment.get("id") != assignment_id:
            continue
        assignment["status"] = status
        if notification is not None:
            assignment["notification"] = notification
        if error:
            assignment["error"] = error
        for packet_assignment in packet.get("call_record", {}).get("assignments", []):
            if packet_assignment.get("id") == assignment_id:
                packet_assignment.clear()
                packet_assignment.update(assignment)
        _write(path, record)
        assignment_path = user_data_root() / "Assignments" / f"{assignment_id}.json"
        _write(assignment_path, assignment)
        return


def send_assignment_completion_sms(proposal: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    """Send a concise completion notice through the existing phone bridge."""
    number = os.getenv("HERMES_PHONE_OWNER", os.getenv("REX_SMS_OWNER", "")).strip()
    if not number:
        raise RuntimeError("HERMES_PHONE_OWNER is not configured")
    summary = str(evidence.get("summary", "Assignment completed")).strip()
    message = f"Assignment finished: {proposal.get('title', 'task')}. {summary}"[:1200]
    adb = os.getenv("HERMES_PHONE_ADB") or shutil.which("adb") or "adb"
    serial = os.getenv("HERMES_PHONE_ADB_SERIAL")
    bridge = f"SEND {number} {message}"
    wire = f"printf '%s\\n' {json.dumps(bridge, ensure_ascii=False)} | nc -w 10 127.0.0.1 9999"
    command = [adb] + (["-s", serial] if serial else []) + ["shell", wire]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    response = result.stdout.strip()
    if result.returncode != 0 or not response.startswith("OK sent to "):
        raise RuntimeError(result.stderr.strip() or response or f"SMS bridge exited {result.returncode}")
    return {"status": "sent", "channel": "sms", "recipient": number, "bridge_response": response}


def build_post_call_packet(store: Any, session_id: str, *, artifact_root: Path | None = None) -> dict[str, Any]:
    record = store.load_session(session_id)
    if record.get("status") not in {"completed", "aborted"}:
        raise ValueError("post-call packet requires a closed session")
    root = Path(artifact_root or store.root).expanduser() / session_id
    events = root / "pi-events.jsonl"
    event_count = 0
    if events.exists():
        event_count = sum(1 for line in events.read_text(encoding="utf-8").splitlines() if line.strip())
    working_state = record.get("working_state", {})
    return {
        "schema": "rex-post-call-packet-v1",
        "session_id": session_id,
        "closed_at": record.get("ended_at"),
        "evidence": {
            "session_record": {"path": str(store._session_path(session_id)), "status": record.get("status")},
            "pi_events": {"path": str(events), "event_count": event_count},
            "resources": record.get("resources_touched", []),
            "tool_audit": _tool_audit(events),
            "resource_audit": _resource_audit(record.get("resources_touched", [])),
        },
        "call_record": {
            "status": record.get("status"),
            "started_at": record.get("started_at"),
            "ended_at": record.get("ended_at"),
            "topic": record.get("topic", ""),
            "active_resource": record.get("active_resource"),
            "resources_touched": record.get("resources_touched", []),
            "assignments": record.get("assignments", []),
            "drafts": record.get("drafts", []),
            "unresolved_questions": record.get("unresolved_questions", []),
            "explicit_follow_ups": record.get("explicit_follow_ups", []),
            "working_state": working_state,
            "backend_errors": record.get("backend_errors", []),
            "conversation": _conversation_evidence(events),
        },
        "interpretation": {},
        "prepared_context": {},
    }


class PostCallQueue:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser()
        self.jobs = self.root / "jobs"
        self.activity = self.root / "activity"
        self.jobs.mkdir(parents=True, exist_ok=True)
        self.activity.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self.jobs / f"post-call-{session_id}.json"

    def enqueue(self, session_id: str, packet: dict[str, Any]) -> dict[str, Any]:
        path = self._path(session_id)
        if path.exists():
            existing = _read(path)
            # A failed pre-audit job is safely enrichable on retry; completed
            # jobs remain immutable evidence.
            if existing.get("status") != "completed" and packet.get("evidence", {}).get("tool_audit"):
                existing["packet"] = packet
                self.save(existing)
            return existing
        job = {
            "schema": "rex-post-call-job-v1",
            "job_id": f"post-call-{session_id}",
            "session_id": session_id,
            "status": "queued",
            "created_at": time.time(),
            "updated_at": time.time(),
            "attempts": 0,
            "execution_gate": {
                "status": "awaiting_normal_model",
                "reason": "embedded voice handoff requires verified QWEN_READY",
            },
            "packet": packet,
            "interpretation": {},
            "learning_records": [],
            "work": [],
            "prepared_context": {},
        }
        _write(path, job)
        return job

    def load(self, session_id: str) -> dict[str, Any]:
        return _read(self._path(session_id))

    def save(self, job: dict[str, Any]) -> None:
        job["updated_at"] = time.time()
        _write(self._path(job["session_id"]), job)

    def pending(self) -> list[dict[str, Any]]:
        pending: list[dict[str, Any]] = []
        for path in sorted(self.jobs.glob("post-call-*.json")):
            job = _read(path)
            if job.get("status") not in {"completed"}:
                pending.append(job)
        return pending

    def write_activity(self, session_id: str, job: dict[str, Any]) -> None:
        lines = [
            "POST-CALL",
            f"Session: {session_id}",
            f"Status: {job.get('status', 'unknown')}",
            f"Attempts: {job.get('attempts', 0)}",
            "",
            "WORK",
        ]
        for item in job.get("work", []):
            mark = "✓" if item.get("status") == "completed" else "!"
            lines.append(f"{mark} {item.get('title') or item.get('request')} [{item.get('status')}] ({item.get('provenance')})")
        if not job.get("work"):
            lines.append("(none)")
        lines += ["", "NEXT CALL", "prepared context updated" if job.get("prepared_context") else "prepared context unavailable"]
        proposals = job.get("interpretation", {}).get("improvement_proposals", [])
        if proposals:
            lines += ["", "IMPROVEMENT PROPOSALS"]
            lines.extend(f"? {item.get('title')} [{item.get('policy_class', 'APPROVAL_REQUIRED')}; {item.get('status', 'approval_required')}]" for item in proposals)
        (self.activity / f"{session_id}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


class PostCallProcessor:
    def __init__(self, queue: PostCallQueue, *, interpreter: Callable[[dict[str, Any]], dict[str, Any]], executor: Callable[[dict[str, Any]], dict[str, Any]] | None = None, notifier: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None, learn_after_work: bool = False):
        self.queue = queue
        self.interpreter = interpreter
        self.executor = executor or (lambda _proposal: {"status": "uncertain", "reason": "no executor configured"})
        self.notifier = notifier
        self.learn_after_work = learn_after_work

    def process(self, session_id: str) -> dict[str, Any]:
        job = self.queue.load(session_id)
        if job.get("status") == "completed":
            return job
        job["attempts"] = int(job.get("attempts", 0)) + 1
        job["status"] = "executing"
        self.queue.save(job)
        explicit = _explicit_assignment_proposals(job["packet"])
        session_path = Path(job["packet"]["evidence"]["session_record"]["path"])
        state_root = session_path.parent.parent
        learning_enabled = get_call_learning_status(state_root)["enabled"]
        raw_interpretation: dict[str, Any] = {}
        try:
            if self.learn_after_work and explicit:
                interpretation = {"topics": [], "ideas": [], "decisions": [], "corrections": [], "unresolved_questions": [], "candidate_followups": [], "work_proposals": [], "suppressed_work_proposals": [], "improvement_proposals": [], "learning_records": []}
            elif learning_enabled:
                raw_value = self.interpreter(job["packet"])
                if not isinstance(raw_value, dict):
                    raise ValueError("interpreter must return an object")
                raw_interpretation = raw_value
                interpretation = validate_interpretation(raw_value, packet=job["packet"])
            else:
                interpretation = {"topics": [], "ideas": [], "decisions": [], "corrections": [], "unresolved_questions": [], "candidate_followups": [], "work_proposals": [], "suppressed_work_proposals": [], "improvement_proposals": [], "learning_records": []}
        except Exception as exc:
            if not explicit:
                job.update({"status": "failed", "error": f"interpretation: {exc}", "work": []})
                self.queue.save(job)
                self.queue.write_activity(session_id, job)
                return job
            interpretation = {"topics": [], "ideas": [], "decisions": [], "corrections": [], "unresolved_questions": [], "candidate_followups": [], "work_proposals": [], "suppressed_work_proposals": [], "improvement_proposals": [], "learning_records": []}
            job["interpretation_error"] = f"{exc}"
        explicit_ids = {item.get("assignment_id") for item in explicit}
        model_work = interpretation["work_proposals"]
        if explicit and any(item.get("provenance") == "explicit_assignment" for item in model_work):
            if len(explicit) == 1 and len(model_work) == 1 and not model_work[0].get("assignment_id"):
                model_work[0].update({"assignment_id": explicit[0]["assignment_id"], "notify_on_completion": explicit[0]["notify_on_completion"]})
            interpretation["work_proposals"] = model_work
        else:
            interpretation["work_proposals"] = [item for item in model_work if item.get("assignment_id") not in explicit_ids]
            interpretation["work_proposals"] = explicit + interpretation["work_proposals"]
        job["interpretation"] = interpretation
        job["learning_records"] = interpretation["learning_records"]
        auto_results: list[dict[str, Any]] = []
        for improvement in interpretation.get("improvement_proposals", []):
            try:
                auto_results.append(apply_github_backed_improvement(improvement, job["packet"], run_tests=default_tests))
            except Exception as exc:
                auto_results.append({"status": "deferred", "reason": str(exc)})
        job["auto_improvements"] = auto_results
        if auto_results:
            job["interpretation"]["improvement_proposals"] = [
                {**proposal, "execution_result": result}
                for proposal, result in zip(interpretation.get("improvement_proposals", []), auto_results)
            ]
            interpretation = job["interpretation"]
        quick_notes_error = None
        work: list[dict[str, Any]] = []
        previous = {item.get("id"): item for item in job.get("work", []) if isinstance(item, dict)}
        for proposal in interpretation["work_proposals"]:
            prior = previous.get(proposal["id"], {})
            if prior.get("status") == "completed":
                notification = prior.get("notification") or {}
                if proposal.get("notify_on_completion") and notification.get("status") != "sent" and self.notifier is not None:
                    try:
                        prior["notification"] = self.notifier(proposal, prior.get("evidence", {}))
                    except Exception as notify_exc:
                        prior["notification"] = {"status": "failed", "error": str(notify_exc)}
                    _persist_assignment_result(job["packet"], proposal, status="completed", notification=prior["notification"])
                work.append(prior)
                continue
            if prior.get("status") in {"uncertain", "executing"}:
                preserved = dict(prior)
                if preserved.get("status") == "executing":
                    preserved.update({"status": "uncertain", "uncertainty": "worker restarted while external work was executing"})
                work.append(preserved)
                continue
            item = dict(proposal, status="validated")
            item["status"] = "executing"
            job["work"] = work + [item]
            self.queue.save(job)
            evidence: Any = None
            try:
                evidence = self.executor(proposal)
            except Exception as exc:
                item.update({"status": "failed", "error": str(exc)})
                _persist_assignment_result(job["packet"], proposal, status="failed", error=str(exc))
            else:
                try:
                    verified = _verified_provider_evidence(self.queue, proposal, evidence)
                    item.update({"status": "completed", "evidence": verified})
                    if proposal.get("notify_on_completion"):
                        if self.notifier is None:
                            item["notification"] = {"status": "uncertain", "error": "no completion notifier configured"}
                        else:
                            try:
                                item["notification"] = self.notifier(proposal, verified)
                            except Exception as notify_exc:
                                item["notification"] = {"status": "failed", "error": str(notify_exc)}
                    _persist_assignment_result(
                        job["packet"],
                        proposal,
                        status="completed",
                        notification=item.get("notification"),
                    )
                except Exception as exc:
                    item.update({"status": "uncertain", "uncertainty": str(exc), "executor_result": evidence})
                    _persist_assignment_result(job["packet"], proposal, status="uncertain", error=str(exc))
            work.append(item)
            job["work"] = work
            self.queue.save(job)
        job["work"] = work
        if learning_enabled and self.learn_after_work and explicit:
            learning_packet = dict(job["packet"])
            learning_packet["assignment_results"] = [
                {key: item.get(key) for key in ("id", "status", "result", "verification", "artifact", "error") if key in item}
                for item in work
            ]
            try:
                raw_value = self.interpreter(learning_packet)
                if not isinstance(raw_value, dict):
                    raise ValueError("learner must return an object")
                raw_interpretation = raw_value
            except Exception as exc:
                job["learning_error"] = str(exc)
        if learning_enabled:
            proposal = {category: raw_interpretation.get(category, []) for category in ("explicit_preferences", "explicit_decisions", "behavioral_corrections", "feature_requests", "project_information", "memory_updates", "prepared_topics_affected", "assignment_quality_observations", "runtime_failures", "engineering_observations", "possible_inferences")}
            if not any(proposal[category] for category in proposal):
                proposal = proposal_from_legacy(interpretation["learning_records"], session_id)
            try:
                job["learning"] = run_learning(
                    state_root,
                    job["packet"],
                    proposal,
                    model=os.getenv("REX_POST_CALL_ANALYSIS_MODEL", "gpt-5.6-luna"),
                    provider=os.getenv("REX_POST_CALL_ANALYSIS_PROVIDER", "openai-codex"),
                    job=job,
                )
                job["quick_notes"] = VoiceSessionStore(state_root).quick_notes()
            except Exception as exc:
                job["learning"] = {"status": "failed", "error": str(exc)}
        else:
            job["learning"] = {"status": "disabled", "learning_enabled": False}
        completed = [item for item in work if item.get("status") == "completed"]
        job["prepared_context"] = self._prepared_context(job, interpretation, completed)
        _persist_prepared_context(job["packet"], job["prepared_context"], job["job_id"])
        vault_root = os.getenv("OBSIDIAN_VAULT_PATH", "").strip()
        if vault_root:
            try:
                job["shared_knowledge"] = promote_post_call_to_shared_knowledge(vault_root, job)
            except (OSError, SharedKnowledgeError, ValueError) as exc:
                job["shared_knowledge"] = {"status": "failed", "error": str(exc)}
        else:
            job["shared_knowledge"] = {"status": "skipped", "reason": "OBSIDIAN_VAULT_PATH_not_configured"}
        if any(item.get("status") == "failed" for item in work):
            job["status"] = "failed"
        elif quick_notes_error:
            job["status"] = "uncertain"
        elif job.get("learning", {}).get("status") == "failed":
            job["status"] = "failed"
        elif any(item.get("status") == "uncertain" or item.get("notification", {}).get("status") in {"failed", "uncertain"} for item in work):
            job["status"] = "uncertain"
        else:
            job["status"] = "completed"
        self.queue.save(job)
        self.queue.write_activity(session_id, job)
        return job

    @staticmethod
    def _prepared_context(job: dict[str, Any], interpretation: dict[str, Any], completed: list[dict[str, Any]]) -> dict[str, Any]:
        record = job["packet"].get("call_record", {})
        context = {
            "schema": "rex-prepared-context-v1",
            "session_id": job["session_id"],
            "current_activity": record.get("topic", ""),
            "active_resource": record.get("active_resource"),
            "explicit_assignments": [
                {"request": item.get("request", ""), "provenance": "user_statement", "status": item.get("status", "captured")}
                for item in record.get("assignments", [])[-8:]
            ],
            "user_was_exploring": interpretation["topics"],
            "open_questions": interpretation["unresolved_questions"],
            "work_completed_since_last_call": [
                {"title": item.get("title"), "summary": item["evidence"].get("summary", ""), "artifact_refs": item["evidence"].get("artifact_refs", []), "provenance": item.get("provenance")}
                for item in completed
            ],
            "pending_work": [
                {"title": item.get("title"), "status": item.get("status"), "provenance": item.get("provenance"),
                 "uncertainty": item.get("uncertainty"), "error": item.get("error")}
                for item in job.get("work", []) if item.get("status") != "completed"
            ],
            "important_findings": [item["evidence"].get("summary", "") for item in completed if item["evidence"].get("summary")],
            "decisions": interpretation["decisions"],
            "disagreements_uncertainties": interpretation["corrections"],
            "relevant_handles": record.get("resources_touched", [])[-8:],
            "likely_next_discussion": interpretation["candidate_followups"],
            "improvement_proposals": interpretation.get("improvement_proposals", []),
            "learning_records": interpretation.get("learning_records", []),
            "source": {"user_call": job["session_id"], "post_call_job": job["job_id"]},
        }
        encoded = json.dumps(context, ensure_ascii=False)
        if len(encoded) > 3800:
            context["important_findings"] = [str(item)[:500] for item in context["important_findings"][:4]]
            context["likely_next_discussion"] = [str(item)[:300] for item in context["likely_next_discussion"][:4]]
        return context


def enqueue_closed_session(store: Any, session_id: str, *, artifact_root: Path | None = None) -> dict[str, Any]:
    packet = build_post_call_packet(store, session_id, artifact_root=artifact_root)
    queue = PostCallQueue(Path(artifact_root or store.root) / "post-call")
    return queue.enqueue(session_id, packet)


def command_json(command: str, payload: dict[str, Any], *, timeout: float = 900.0) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        shell=True,
        capture_output=True,
        timeout=timeout,
        check=True,
        start_new_session=True,
    )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise ValueError("configured post-call command must return a JSON object")
    return value


def configured_processor(queue: PostCallQueue) -> PostCallProcessor:
    interpreter_command = os.getenv("REX_POST_CALL_INTERPRETER_COMMAND", "").strip()
    executor_command = os.getenv("REX_POST_CALL_EXECUTOR_COMMAND", "").strip()
    if not interpreter_command or not executor_command:
        return default_model_processor(queue)
    return PostCallProcessor(
        queue,
        interpreter=lambda packet: command_json(interpreter_command, packet),
        executor=lambda proposal: command_json(executor_command, proposal),
        notifier=send_assignment_completion_sms,
        learn_after_work=True,
    )


def _model_json(prompt: str) -> dict[str, Any]:
    hermes = shutil.which("hermes")
    if not hermes:
        raise RuntimeError("Hermes CLI is not available for post-call processing")
    command = [
        hermes,
        "chat",
        "--ignore-rules",
        "--toolsets",
        "",
        "--max-turns",
        "2",
        "--reasoning",
        "none",
        "-Q",
    ]
    command.extend(["-m", os.getenv("REX_POST_CALL_ANALYSIS_MODEL", "gpt-5.6-luna")])
    command.extend(["--provider", os.getenv("REX_POST_CALL_ANALYSIS_PROVIDER", "openai-codex")])
    command.extend(["--source", "rex_post_call", "-q", prompt])
    timeout = float(os.getenv("REX_POST_CALL_LEARNING_TIMEOUT", "120"))
    for attempt in range(2):
        attempt_prompt = prompt
        if attempt:
            attempt_prompt += (
                "\n\nThe previous response was not valid JSON. Return exactly one JSON object now. "
                "Do not explain, use markdown, call tools, or include any text before or after the object."
            )
        attempt_command = list(command)
        attempt_command[-1] = attempt_prompt
        try:
            completed = subprocess.run(
                attempt_command,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=True,
                start_new_session=True,
            )
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            if attempt == 0:
                return _direct_model_json(attempt_prompt)
            raise
        try:
            value = _parse_model_json(completed.stdout)
        except ValueError:
            if attempt == 1:
                raise
            continue
        if not isinstance(value, dict):
            raise ValueError("post-call model must return a JSON object")
        return value
    raise ValueError("post-call model did not return a JSON object")


def _direct_model_json(prompt: str) -> dict[str, Any]:
    """Bounded completion fallback for providers whose CLI wrapper stalls."""
    endpoint = os.getenv(
        "REX_POST_CALL_ENDPOINT",
        "http://127.0.0.1:8080/v1/chat/completions",
    )
    body = {
        "model": os.getenv("REX_POST_CALL_MODEL", ""),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": int(os.getenv("REX_POST_CALL_MAX_TOKENS", "768")),
        "chat_template_kwargs": {"enable_thinking": False},
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=float(os.getenv("REX_POST_CALL_DIRECT_TIMEOUT", "45"))) as response:
        payload = json.loads(response.read().decode("utf-8"))
    content = payload["choices"][0]["message"]["content"]
    return _parse_model_json(content)


def _parse_model_json(output: str) -> dict[str, Any]:
    try:
        value = json.loads(output.strip())
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\{]", output):
        try:
            value, _ = decoder.raw_decode(output[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("post-call model output did not contain a JSON object")


class HermesWorkExecutor:
    """Run validated work in Hermes and require a provider-written result artifact."""

    def __init__(self, queue: PostCallQueue, *, runner: Callable[..., Any] = subprocess.run, timeout: float = 1800.0) -> None:
        self.queue = queue
        self.runner = runner
        self.timeout = timeout

    def __call__(self, proposal: dict[str, Any]) -> dict[str, Any]:
        hermes = shutil.which("hermes")
        if not hermes:
            raise RuntimeError("Hermes CLI is not available for post-call work")
        proposal_id = str(proposal["id"])
        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", proposal_id)[:96]
        workdir = self.queue.root / "work" / safe_id
        workdir.mkdir(parents=True, exist_ok=True)
        artifact = workdir / "result.json"
        prompt = (
            "Execute this validated Rex post-call work proposal using appropriate Hermes tools. "
            "Do not edit the repository, Vault notes, or the original call record. "
            f"You MUST write exactly one JSON artifact to {artifact} before finishing. "
            "It must have schema rex-hermes-work-result-v1, proposal_id, status=completed, summary, "
            "content, sources, and execution with a non-empty actions list plus provider and session_id. "
            "The artifact is the only completion evidence; do not claim completion merely in prose.\n\n"
            "PROPOSAL:\n" + json.dumps(proposal, ensure_ascii=False)
        )
        command = [
            hermes, "chat", "-Q",
            "--toolsets", os.getenv("REX_POST_CALL_WORK_TOOLSETS", "web,terminal,file"),
            "--max-turns", os.getenv("REX_POST_CALL_WORK_MAX_TURNS", "16"), "--yolo",
        ]
        command.extend(["-m", os.getenv("REX_POST_CALL_WORK_MODEL", os.getenv("REX_POST_CALL_MODEL", "/models/Qwen3.8-27B-UD-Q4_K_XL.gguf"))])
        command.extend(["--provider", os.getenv("REX_POST_CALL_WORK_PROVIDER", os.getenv("REX_POST_CALL_PROVIDER", "Qwen 27B"))])
        command.extend(["--source", "rex_post_call_work", "--no-restore-cwd", "-q", prompt])
        completed = self.runner(
            command,
            cwd=str(workdir),
            text=True,
            capture_output=True,
            timeout=self.timeout,
            check=True,
            start_new_session=True,
        )
        (workdir / "hermes.stdout.txt").write_text(str(getattr(completed, "stdout", "")), encoding="utf-8")
        if not artifact.exists():
            raise ValueError(f"Hermes work completed without result artifact: {artifact}")
        try:
            result = _read(artifact)
        except Exception as exc:
            raise ValueError(f"Hermes result artifact is not valid JSON: {artifact}") from exc
        if result.get("schema") != "rex-hermes-work-result-v1":
            raise ValueError("Hermes result artifact has unsupported schema")
        if result.get("proposal_id") != proposal_id:
            raise ValueError("Hermes result artifact proposal_id does not match")
        if result.get("status") != "completed":
            raise ValueError("Hermes result artifact is not completed")
        if not isinstance(result.get("summary"), str) or not result["summary"].strip():
            raise ValueError("Hermes result artifact requires a summary")
        content = result.get("content")
        if not ((isinstance(content, str) and content.strip()) or (isinstance(content, (dict, list)) and content)):
            raise ValueError("Hermes result artifact requires content")
        execution = result.get("execution")
        if not isinstance(execution, dict) or not isinstance(execution.get("actions"), list) or not execution["actions"]:
            raise ValueError("Hermes result artifact requires execution actions")
        if not all(isinstance(execution.get(key), str) and execution[key].strip() for key in ("provider", "session_id")):
            raise ValueError("Hermes result artifact requires provider and session_id")
        return {
            "status": "completed",
            "provider": execution["provider"],
            "session_id": execution["session_id"],
            "artifact_refs": [str(artifact)],
            "summary": result["summary"],
            "sources": result.get("sources", []),
            "execution": execution,
        }


def default_model_processor(queue: PostCallQueue) -> PostCallProcessor:
    def interpret(packet: dict[str, Any]) -> dict[str, Any]:
        prompt = (
            "/no_think\n"
            "Extract durable learning from this closed voice chat call. This is one bounded extraction task, not "
            "post-call planning or work execution. Do not execute assignments, browse, edit files, or research. "
            "Return exactly one JSON object and then stop. "
            "Use exactly these top-level keys, each with an array value: explicit_preferences, explicit_decisions, "
            "behavioral_corrections, feature_requests, project_information, memory_updates, prepared_topics_affected, "
            "assignment_quality_observations, runtime_failures, engineering_observations, possible_inferences, "
            "improvement_proposals. "
            "Empty categories must be []. Return at most one item in any category and at most eight items total. "
            "Keep every statement and evidence_summary under 240 characters. Every item must contain only the fields required by the learning validator: "
            "statement, confidence_class, source_session_id, source_turn_ids, evidence_summary, and topic_ids. "
            "Use explicit confidence only for direct user statements, supported for strong evidence, and inference "
            "only for hypotheses. Feature requests are requests, not capability facts. Runtime and engineering "
            "observations are not user memory. Assignment-quality observations must use the deterministic requested "
            "and actual values when supplied; do not invent counts. Do not include chain-of-thought, markdown, logs, "
            "legacy work-planning fields, or any text outside the JSON object. For improvement_proposals, only "
            "propose a bounded Python runtime repair when the call contains concrete evidence and confidence is "
            "at least 0.9. Include id, title, request, confidence, requested_by_user, transcript_evidence, "
            "deterministic_spec, target_files, and a small unified diff patch. Target files must be existing "
            "rex_voice_v1/*.py files; never propose tests, secrets, system files, phone operations, or arbitrary "
            "shell. Use [] when no evidence-backed repair is justified.\n\n"
            + "Evidence packet:\n"
            + json.dumps(packet, ensure_ascii=False)
        )
        return _model_json(prompt)

    work_executor = HermesWorkExecutor(queue)
    execute_inferred = os.getenv("REX_POST_CALL_EXECUTE_APPROVED", "false").lower() in {"1", "true", "yes", "on"}

    def execute(proposal: dict[str, Any]) -> dict[str, Any]:
        if proposal.get("provenance") == "explicit_assignment" or execute_inferred:
            return work_executor(proposal)
        return {"status": "uncertain", "reason": "inferred post-call execution disabled; explicit assignment required"}

    return PostCallProcessor(queue, interpreter=interpret, executor=execute, notifier=send_assignment_completion_sms, learn_after_work=True)
