from __future__ import annotations

import json
from pathlib import Path

from rex_voice_v1.store import VoiceSessionStore


def test_record_assignment_appends_durable_assignment_log(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    store = VoiceSessionStore(tmp_path / "artifacts", data_root=data_root)
    store.start_session("session-1", topic="research")

    assignment = store.record_assignment("session-1", {"request": "Prepare the report", "priority": "high"})

    log_path = data_root / "Assignments" / "assignment-log.jsonl"
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert rows == [{"event": "assignment_captured", "assignment": assignment}]


def test_assignment_log_backfills_existing_assignment_files(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    assignments = data_root / "Assignments"
    assignments.mkdir(parents=True)
    existing = {"id": "assignment-old", "request": "Review the notes", "status": "queued"}
    (assignments / "assignment-old.json").write_text(json.dumps(existing) + "\n", encoding="utf-8")
    store = VoiceSessionStore(tmp_path / "artifacts", data_root=data_root)

    rows = store.assignment_log()

    assert rows == [{"event": "assignment_captured", "assignment": existing}]
    log_path = assignments / "assignment-log.jsonl"
    assert log_path.exists()
    assert json.loads(log_path.read_text(encoding="utf-8").splitlines()[0]) == rows[0]


