from __future__ import annotations

import json
from pathlib import Path

import pytest

from rex_voice_v1.shared_knowledge import SharedKnowledgeError, SharedKnowledgeStore, promote_post_call_to_shared_knowledge


def _store(tmp_path: Path) -> SharedKnowledgeStore:
    root = tmp_path / "vault"
    root.mkdir()
    return SharedKnowledgeStore(root)


def _llm(prompt: str):
    assert "REFERENCE DATA" in prompt
    return {"title": "Phone Bridge Briefing", "summary": "Bridge is separate from built-in voice.", "decisions": ["Keep the runtimes independent."], "risks": ["Do not inject stale material."], "talking_points": ["Discuss the authorized handoff."], "things_not_to_assume": ["Voice does not search all memory."], "current_state": "Prepared for discussion."}


def test_publish_merge_preserves_existing_and_records_provenance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.publish_shared_knowledge("Phone Bridge", "Initial decision.", category="projects", sources=["conversation:text-1"], provenance={"conversation": "text-1"})
    second = store.publish_shared_knowledge("Phone Bridge", "Later finding.", category="projects", sources=["file:bridge.md"], overwrite_policy="merge")
    text = Path(first["path"]).read_text()
    assert second["status"] == "updated"
    assert "Initial decision." in text and "Later finding." in text
    assert "Source references" in text


def test_publish_create_does_not_overwrite(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.publish_shared_knowledge("Topic", "one")
    with pytest.raises(SharedKnowledgeError):
        store.publish_shared_knowledge("Topic", "two", overwrite_policy="create")


def test_prepare_packet_has_markdown_metadata_and_stable_topic(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = store.root / "project.md"
    source.write_text("# Project\nKeep voice independent.", encoding="utf-8")
    packet = store.prepare_for_voice("Phone Bridge", [source], llm=_llm, preparation_model="fixture-model", conversation_goal="next call")
    assert packet["topic_id"] == "phone-bridge"
    assert Path(packet["briefing_path"]).is_file()
    metadata = json.loads(Path(packet["metadata_path"]).read_text())
    assert metadata["schema"] == "rex-prepared-briefing-v1"
    assert metadata["preparation_model"] == "fixture-model"
    assert metadata["source_version"]


def test_stale_detection_and_no_stale_resolution(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = store.root / "project.md"
    source.write_text("v1", encoding="utf-8")
    store.prepare_for_voice("Phone Bridge", [source], llm=_llm)
    source.write_text("v2", encoding="utf-8")
    states = {item["topic_id"]: item["state"] for item in store.list_prepared()}
    assert states["phone-bridge"] == "stale"
    assert store.resolve_topic("phone-bridge") is None


def test_retrieval_is_bounded_topical_and_excludes_unrelated_packet(tmp_path: Path) -> None:
    store = _store(tmp_path)
    phone = store.root / "phone.md"; phone.write_text("phone bridge", encoding="utf-8")
    book = store.root / "book.md"; book.write_text("capacitance", encoding="utf-8")
    store.prepare_for_voice("Phone Bridge", [phone], llm=_llm)
    store.prepare_for_voice("Capacitance", [book], llm=_llm)
    result = store.retrieve_for_voice("phone bridge")
    assert result["status"] == "ok"
    assert {item["topic_id"] for item in result["results"] if item["kind"] == "prepared"} == {"phone-bridge"}
    assert result["context_chars"] <= 12000
    assert "reference data" in result["reference_rule"].lower()


def test_authorized_root_enforcement(tmp_path: Path) -> None:
    store = _store(tmp_path)
    outside = tmp_path / "outside.md"; outside.write_text("private", encoding="utf-8")
    with pytest.raises(SharedKnowledgeError):
        store.prepare_for_voice("bad", [outside], llm=_llm)


def test_missing_host_model_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = store.root / "source.md"; source.write_text("data", encoding="utf-8")
    result = store.prepare_for_voice("Topic", [source])
    assert result["status"] == "blocked"
    assert result["reason"] == "host_llm_unavailable"


def test_post_call_promotion_requires_explicit_decisions_or_findings(tmp_path: Path) -> None:
    skipped = promote_post_call_to_shared_knowledge(tmp_path, {"packet": {"call_record": {"topic": "Phone Bridge"}}, "interpretation": {}})
    assert skipped["status"] == "skipped"
    result = promote_post_call_to_shared_knowledge(tmp_path, {
        "session_id": "call-1", "job_id": "job-1",
        "packet": {"call_record": {"topic": "Phone Bridge"}},
        "interpretation": {"decisions": ["Keep built-in voice independent."]},
        "prepared_context": {"important_findings": []},
    })
    assert result["status"] == "created"
    assert "Keep built-in voice independent." in Path(result["path"]).read_text()
