from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable


SCHEMA = "rex-call-learning-v1"
CATEGORIES = (
    "explicit_preferences",
    "explicit_decisions",
    "behavioral_corrections",
    "feature_requests",
    "project_information",
    "memory_updates",
    "prepared_topics_affected",
    "assignment_quality_observations",
    "runtime_failures",
    "engineering_observations",
    "possible_inferences",
)
ITEM_CATEGORIES = set(CATEGORIES) - {"prepared_topics_affected"}
CONFIDENCE = {"explicit", "supported", "inference"}
MAX_ITEMS = 12
MAX_TEXT = 1200
MAX_EVIDENCE_CHARS = 10_000


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _root(root: Path) -> Path:
    return Path(root).expanduser().resolve()


def _settings_path(root: Path) -> Path:
    return _root(root) / "call-learning" / "settings.json"


def get_call_learning_status(root: Path) -> dict[str, Any]:
    path = _settings_path(root)
    if not path.exists():
        return {"schema": "rex-call-learning-settings-v1", "enabled": True}
    value = _read(path)
    if value.get("schema") != "rex-call-learning-settings-v1" or not isinstance(value.get("enabled"), bool):
        raise ValueError("call-learning settings have unsupported schema")
    return value


def set_call_learning_enabled(root: Path, enabled: bool) -> dict[str, Any]:
    if not isinstance(enabled, bool):
        raise TypeError("enabled must be a bool")
    value = {"schema": "rex-call-learning-settings-v1", "enabled": enabled, "updated_at": time.time()}
    _atomic_write(_settings_path(root), value)
    return value


def learning_id(session_id: str) -> str:
    return "learning-" + hashlib.sha256((session_id + SCHEMA).encode("utf-8")).hexdigest()[:24]


def _item(value: Any, session_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("learning proposal item must be an object")
    statement = value.get("statement")
    confidence = value.get("confidence_class")
    source_session = value.get("source_session_id")
    turns = value.get("source_turn_ids")
    evidence = value.get("evidence_summary")
    topics = value.get("topic_ids")
    if not isinstance(statement, str) or not statement.strip() or len(statement) > MAX_TEXT:
        raise ValueError("learning item requires a bounded statement")
    if confidence not in CONFIDENCE:
        raise ValueError("learning item has unsupported confidence_class")
    if source_session != session_id:
        raise ValueError("learning item source_session_id must match the closed session")
    if not isinstance(turns, list) or not turns or len(turns) > 16 or not all(isinstance(x, str) and x.strip() for x in turns):
        raise ValueError("learning item requires bounded source_turn_ids")
    if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 800:
        raise ValueError("learning item requires a bounded evidence_summary")
    if not isinstance(topics, list) or len(topics) > 8 or not all(isinstance(x, str) and x.strip() for x in topics):
        raise ValueError("learning item requires topic_ids")
    normalized = {
        "statement": statement.strip(),
        "confidence_class": confidence,
        "source_session_id": session_id,
        "source_turn_ids": turns[:16],
        "evidence_summary": evidence.strip(),
        "topic_ids": topics[:8],
    }
    for field in ("assignment_id", "constraint", "constraint_type", "requested", "actual", "satisfied"):
        if field in value:
            normalized[field] = value[field]
    normalized["id"] = "observation-" + hashlib.sha256(json.dumps(normalized, sort_keys=True).encode("utf-8")).hexdigest()[:20]
    return normalized


def validate_learning_proposal(value: Any, session_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("learning proposal must be an object")
    result: dict[str, Any] = {category: [] for category in CATEGORIES}
    for category in ITEM_CATEGORIES:
        raw = value.get(category, [])
        if not isinstance(raw, list) or len(raw) > MAX_ITEMS:
            raise ValueError(f"{category} must be a bounded list")
        result[category] = [_item(item, session_id) for item in raw]
        for item in result[category]:
            item["id"] = "observation-" + hashlib.sha256(
                (category + "\n" + " ".join(item["statement"].lower().split())).encode("utf-8")
            ).hexdigest()[:20]
        if len({item["id"] for item in result[category]}) != len(result[category]):
            raise ValueError(f"{category} contains duplicate observations")
    topics = value.get("prepared_topics_affected", [])
    if not isinstance(topics, list) or len(topics) > 8 or not all(isinstance(x, str) and x.strip() for x in topics):
        raise ValueError("prepared_topics_affected must be topic IDs")
    result["prepared_topics_affected"] = list(dict.fromkeys(topics))
    return result


def build_learning_evidence(packet: dict[str, Any], job: dict[str, Any] | None = None) -> dict[str, Any]:
    record = packet.get("call_record", {})
    evidence = packet.get("evidence", {})
    conversation = record.get("conversation", []) if isinstance(record, dict) else []
    turns = []
    for index, item in enumerate(conversation[-40:], 1):
        if isinstance(item, dict) and item.get("role") in {"user", "assistant"} and isinstance(item.get("text"), str):
            turns.append({"turn_id": f"turn-{index}", "role": item["role"], "text": item["text"][:800]})
    audit = evidence.get("tool_audit", {}) if isinstance(evidence, dict) else {}
    return {
        "schema": "rex-call-learning-evidence-v1",
        "session_id": packet.get("session_id"),
        "closed_at": packet.get("closed_at"),
        "turns": turns,
        "tool_audit": {
            "requested": list(audit.get("requested", []))[:32],
            "result_count": int(audit.get("result_count", 0)),
            "error_count": int(audit.get("error_count", 0)),
            "errors": list(audit.get("errors", []))[-8:],
        },
        "assignments": list(record.get("assignments", []))[-12:] if isinstance(record, dict) else [],
        "assignment_results": [
            {key: item.get(key) for key in ("id", "assignment_id", "status", "result", "artifact", "verification", "evidence", "error", "uncertainty")}
            for item in (job or {}).get("work", []) if isinstance(item, dict)
        ][-12:],
        "prepared_topic_events": list(record.get("prepared_topic_events", []))[-16:] if isinstance(record, dict) else [],
        "runtime_errors": list(record.get("backend_errors", []))[-8:] if isinstance(record, dict) else [],
        "active_topic": record.get("active_prepared_topic") if isinstance(record, dict) else None,
    }


def _item_path(root: Path, category: str, item_id: str) -> Path:
    directory = {"feature_requests": "feature-requests", "behavioral_corrections": "behavioral-corrections", "engineering_observations": "engineering-issues"}.get(category, "observations")
    return root / "call-learning" / directory / f"{item_id}.json"


def _merge_item(path: Path, item: dict[str, Any], session_id: str) -> None:
    if path.exists():
        existing = _read(path)
        sessions = list(dict.fromkeys(existing.get("source_sessions", []) + [session_id]))
        refs = list(dict.fromkeys(existing.get("source_turn_ids", []) + item["source_turn_ids"]))
        existing.update({"source_sessions": sessions[-32:], "source_turn_ids": refs[-32:], "evidence_summary": item["evidence_summary"]})
        _atomic_write(path, existing)
    else:
        _atomic_write(path, {"schema": "rex-call-learning-observation-v1", **item, "source_sessions": [session_id]})


def _apply_quick_notes(root: Path, proposal: dict[str, Any]) -> list[str]:
    path = root / "quick-notes.json"
    current = _read(path) if path.exists() else {"schema": "rex-quick-notes-v1", "entries": []}
    entries = list(current.get("entries", []))
    changed: list[str] = []
    for category in ("explicit_preferences", "explicit_decisions", "behavioral_corrections"):
        for item in proposal[category]:
            if item["confidence_class"] != "explicit":
                continue
            topic = item["topic_ids"][0] if item["topic_ids"] else "general"
            identity = topic.lower().replace("-", "_") if topic != "general" else "general." + hashlib.sha256(" ".join(item["statement"].lower().split()).encode("utf-8")).hexdigest()[:12]
            key = "learning." + identity
            replacement = {
                "key": key,
                "value": item["statement"],
                "confidence_class": "explicit",
                "provenance": "explicit_user_statement",
                "source_refs": [item["id"], item["source_session_id"], *item["source_turn_ids"]],
            }
            prior = next((entry for entry in entries if entry.get("key") == key), None)
            if prior and prior.get("value") != replacement["value"]:
                replacement["supersedes"] = prior.get("source_refs", [None])[0]
            entries = [entry for entry in entries if entry.get("key") != key]
            entries.append(replacement)
            changed.append(key)
    if len(entries) > 64:
        entries = entries[-64:]
    encoded = {"schema": "rex-quick-notes-v1", "entries": entries}
    if len(json.dumps(encoded, ensure_ascii=False, separators=(",", ":"))) > 20_000:
        raise ValueError("call learning would exceed quick-note bound")
    _atomic_write(path, encoded)
    return list(dict.fromkeys(changed))


def _rebuild_topics(root: Path, proposal: dict[str, Any]) -> list[str]:
    rebuilt: list[str] = []
    topics_root = root / "prepared-topics"
    for topic_id in proposal["prepared_topics_affected"]:
        path = topics_root / f"{topic_id}.json"
        if not path.exists():
            continue
        topic = _read(path)
        additions = [item["statement"] for category in ("explicit_decisions", "behavioral_corrections") for item in proposal[category] if topic_id in item["topic_ids"] and item["confidence_class"] == "explicit"]
        if not additions:
            continue
        content = topic.get("content", "")
        section = "\n\nValidated call decisions:\n" + "\n".join(f"- {text}" for text in additions)
        if all(text in content for text in additions):
            continue
        updated = content + section
        if len(updated) > 12_000:
            raise ValueError("prepared topic rebuild exceeds size limit")
        topic["content"] = updated
        topic["source_refs"] = list(dict.fromkeys(topic.get("source_refs", []) + [f"call-learning:{item['id']}" for category in ("explicit_decisions", "behavioral_corrections") for item in proposal[category] if topic_id in item["topic_ids"]]))[:16]
        topic["version"] = hashlib.sha256((topic_id + "\n" + "\n".join(topic["aliases"]) + "\n" + updated).encode()).hexdigest()[:16]
        _atomic_write(path, topic)
        rebuilt.append(topic_id)
    return rebuilt


def proposal_from_legacy(records: list[dict[str, Any]], session_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {category: [] for category in CATEGORIES}
    category_map = {
        "explicit_preference": "explicit_preferences",
        "explicit_decision": "explicit_decisions",
        "behavioral_correction": "behavioral_corrections",
        "feature_request": "feature_requests",
        "project_information": "project_information",
        "possible_inference": "possible_inferences",
        "runtime_failure": "runtime_failures",
    }
    for record in records:
        category = category_map.get(record.get("kind"))
        if category is None:
            continue
        confidence = record.get("confidence_class", "inferred")
        confidence = {"explicit": "explicit", "implied": "supported", "inferred": "inference"}.get(confidence, "inference")
        result[category].append({
            "statement": str(record.get("statement", "")),
            "confidence_class": confidence,
            "source_session_id": session_id,
            "source_turn_ids": [str(ref) for ref in record.get("source_refs", [])][:16] or ["post-call"],
            "evidence_summary": str(record.get("statement", ""))[:800],
            "topic_ids": [],
        })
    return result


def _assignment_quality_observations(packet: dict[str, Any], job: dict[str, Any] | None, session_id: str) -> list[dict[str, Any]]:
    assignments = packet.get("call_record", {}).get("assignments", [])
    results = {str(item.get("assignment_id") or item.get("id")): item for item in (job or {}).get("work", []) if isinstance(item, dict)}
    observations: list[dict[str, Any]] = []
    for assignment in assignments[-12:]:
        if not isinstance(assignment, dict):
            continue
        request = str(assignment.get("request", ""))
        match = re.search(r"\b(\d+)\s*-?\s*word", request, re.IGNORECASE)
        if not match:
            continue
        assignment_id = str(assignment.get("id", ""))
        result = results.get(assignment_id, {})
        evidence: dict[str, Any] = {}
        if isinstance(result.get("evidence"), dict):
            evidence = result["evidence"]
        result_value = result.get("result")
        artifact = result.get("artifact")
        if not isinstance(artifact, str):
            refs = result.get("artifact_refs")
            if not isinstance(refs, list):
                refs = evidence.get("artifact_refs")
            artifact = refs[0] if isinstance(refs, list) and refs and isinstance(refs[0], str) else None
        if not isinstance(artifact, str) or not Path(artifact).exists():
            continue
        text = Path(artifact).read_text(encoding="utf-8")
        if Path(artifact).name == "result.json":
            try:
                payload = json.loads(text)
                content = payload.get("content", "")
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            except (OSError, ValueError):
                pass
        actual = len(re.findall(r"[\w’'-]+", text))
        requested = int(match.group(1))
        observations.append({
            "statement": f"Assignment requested about {requested} words; verified artifact contains {actual} words.",
            "confidence_class": "supported",
            "source_session_id": session_id,
            "source_turn_ids": [assignment_id or "assignment"],
            "evidence_summary": f"request={request[:400]}; artifact={artifact}; actual_words={actual}",
            "topic_ids": [],
            "assignment_id": assignment_id,
            "constraint": "word_count",
            "constraint_type": "target",
            "requested": requested,
            "actual": actual,
            "satisfied": abs(actual - requested) <= max(10, round(requested * 0.1)),
        })
    return observations


def run_learning(root: Path, packet: dict[str, Any], proposal: Any, *, model: str, provider: str, telemetry: Callable[[str], None] | None = None, job: dict[str, Any] | None = None) -> dict[str, Any]:
    root = _root(root)
    session_id = str(packet["session_id"])
    identifier = learning_id(session_id)
    run_path = root / "call-learning" / "runs" / f"{session_id}.json"
    if run_path.exists():
        existing = _read(run_path)
        if existing.get("status") == "completed":
            return existing
    if not get_call_learning_status(root)["enabled"]:
        result = {"schema": SCHEMA, "learning_id": identifier, "session_id": session_id, "status": "disabled", "learning_enabled": False}
        _atomic_write(run_path, result)
        return result
    started = time.time()
    run = {"schema": SCHEMA, "learning_id": identifier, "session_id": session_id, "status": "running", "learning_enabled": True, "started_at": started, "model": model, "provider": provider, "input_evidence": build_learning_evidence(packet, job), "proposal": {}, "applied_updates": {}, "errors": []}
    _atomic_write(run_path, run)
    if telemetry:
        telemetry(f"CALL_LEARNING_START learning_id={identifier}")
    mutation_paths: list[Path] = []
    backups: dict[Path, bytes] = {}
    try:
        normalized = validate_learning_proposal(proposal, session_id)
        for observation in _assignment_quality_observations(packet, job, session_id):
            derived = _item(observation, session_id)
            derived.update({key: observation[key] for key in ("assignment_id", "constraint", "constraint_type", "requested", "actual", "satisfied")})
            if not any(item.get("assignment_id") == derived.get("assignment_id") for item in normalized["assignment_quality_observations"]):
                normalized["assignment_quality_observations"].append(derived)
        normalized["assignment_quality_observations"] = normalized["assignment_quality_observations"][:MAX_ITEMS]
        run["proposal"] = normalized
        _atomic_write(run_path, run)
        mutation_paths = [_item_path(root, category, item["id"]) for category in ITEM_CATEGORIES for item in normalized[category]]
        mutation_paths.append(root / "quick-notes.json")
        mutation_paths.extend(root / "prepared-topics" / f"{topic_id}.json" for topic_id in normalized["prepared_topics_affected"])
        backups = {path: path.read_bytes() for path in mutation_paths if path.exists()}
        observations = 0
        for category in ITEM_CATEGORIES:
            for item in normalized[category]:
                _merge_item(_item_path(root, category, item["id"]), item, session_id)
                observations += 1
        quick = _apply_quick_notes(root, normalized)
        topics = _rebuild_topics(root, normalized)
        run.update({"status": "completed", "completed_at": time.time(), "applied_updates": {"observations": observations, "quick_notes_changed": quick, "prepared_topics_rebuilt": topics}, "elapsed_ms": round((time.time() - started) * 1000, 2)})
        _atomic_write(run_path, run)
        if telemetry:
            telemetry(f"CALL_LEARNING_COMPLETE elapsed_ms={run['elapsed_ms']}")
        return run
    except Exception as exc:
        for path in mutation_paths:
            if path in backups:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(backups[path])
            elif path.exists():
                path.unlink()
        run.update({"status": "failed", "completed_at": time.time(), "errors": [str(exc)], "elapsed_ms": round((time.time() - started) * 1000, 2)})
        _atomic_write(run_path, run)
        if telemetry:
            telemetry(f"CALL_LEARNING_FAILED stage=validate_or_apply reason={str(exc)[:200]}")
        raise
