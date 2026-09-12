from __future__ import annotations

import json
from pathlib import Path

from rex_voice_v1.settings import grant_root, load_settings, revoke_root


def test_grant_and_revoke_root_is_persistent_and_typed(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "data"
    extra = tmp_path / "research-notes"
    extra.mkdir()
    monkeypatch.setenv("HERMES_CALL_ASSISTANT_DATA_ROOT", str(data_root))

    entry = grant_root(str(extra), kind="prepared")
    assert entry == {"id": "research-notes", "path": str(extra.resolve()), "modes": ["read", "search"]}
    saved = json.loads((data_root / "settings.json").read_text(encoding="utf-8"))
    assert entry in saved["prepared_talking_points_roots"]
    assert entry not in saved["authorized_note_roots"]

    result = revoke_root("research-notes", kind="prepared")
    assert result["status"] == "revoked"
    assert not any(item["id"] == "research-notes" for item in load_settings()["prepared_talking_points_roots"])


def test_grant_rejects_missing_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_CALL_ASSISTANT_DATA_ROOT", str(tmp_path / "data"))
    try:
        grant_root(str(tmp_path / "missing"))
    except ValueError as exc:
        assert "not a directory" in str(exc)
    else:
        raise AssertionError("missing access root was accepted")
