"""User-visible Call Assistant data layout and fail-closed access settings."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

SCHEMA = "hermes-call-assistant-settings-v1"


def user_data_root() -> Path:
    configured = os.environ.get("HERMES_CALL_ASSISTANT_DATA_ROOT", "").strip()
    return (Path(configured).expanduser() if configured else Path.home() / "Documents" / "Hermes Call Assistant").resolve()


def settings_path(root: Path | None = None) -> Path:
    return (root or user_data_root()) / "settings.json"


def _default(root: Path) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "data_root": str(root),
        "authorized_note_roots": [{"id": "user-notes", "path": str(root / "Notes"), "modes": ["read", "search"]}],
        "prepared_talking_points_roots": [{"id": "prepared-talking-points", "path": str(root / "Prepared Talking Points"), "modes": ["read", "search"]}],
    }


def load_settings(*, create: bool = True) -> dict[str, Any]:
    root = user_data_root()
    path = settings_path(root)
    if not path.exists():
        value = _default(root)
    else:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Call Assistant settings are unreadable: {path}") from exc
        if not isinstance(value, dict) or value.get("schema") != SCHEMA:
            raise ValueError(f"Call Assistant settings have an unsupported schema: {path}")
    if create:
        root.mkdir(parents=True, exist_ok=True)
        (root / "Assignments").mkdir(exist_ok=True)
        (root / "Prepared Talking Points").mkdir(exist_ok=True)
        (root / "Notes").mkdir(exist_ok=True)
    _validate(value, root)
    if create:
        (root / "README.md").write_text(_readme(root), encoding="utf-8") if not (root / "README.md").exists() else None
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return value


def _write_settings(value: dict[str, Any], root: Path) -> dict[str, Any]:
    _validate(value, root)
    path = settings_path(root)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return value


def grant_root(path: str, *, kind: str = "notes", root_id: str | None = None) -> dict[str, Any]:
    """Persist an explicit read/search grant for an existing directory."""
    if kind not in {"notes", "prepared"}:
        raise ValueError("kind must be notes or prepared")
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"authorized root is not a directory: {resolved}")
    settings = load_settings(create=True)
    field = "authorized_note_roots" if kind == "notes" else "prepared_talking_points_roots"
    identifier = root_id or re.sub(r"[^a-z0-9_-]+", "-", resolved.name.casefold()).strip("-") or "authorized-root"
    if not re.match(r"^[a-z][a-z0-9_-]*$", identifier):
        raise ValueError("root_id must start with a letter and contain only a-z, 0-9, _ or -")
    entry = {"id": identifier, "path": str(resolved), "modes": ["read", "search"]}
    entries = [item for item in settings[field] if item.get("id") != identifier and item.get("path") != str(resolved)]
    entries.append(entry)
    settings[field] = entries
    _write_settings(settings, user_data_root())
    return entry


def revoke_root(root_id: str, *, kind: str = "notes") -> dict[str, Any]:
    """Remove a user-granted root, but never remove the built-in data roots."""
    if kind not in {"notes", "prepared"}:
        raise ValueError("kind must be notes or prepared")
    settings = load_settings(create=True)
    field = "authorized_note_roots" if kind == "notes" else "prepared_talking_points_roots"
    entries = settings[field]
    retained = [item for item in entries if item.get("id") != root_id]
    if len(retained) == len(entries):
        raise ValueError(f"authorized root does not exist: {root_id}")
    settings[field] = retained
    _write_settings(settings, user_data_root())
    return {"id": root_id, "kind": kind, "status": "revoked"}


def _validate(value: dict[str, Any], root: Path) -> None:
    if Path(str(value.get("data_root", ""))).expanduser().resolve() != root:
        raise ValueError("settings data_root does not match HERMES_CALL_ASSISTANT_DATA_ROOT")
    for field in ("authorized_note_roots", "prepared_talking_points_roots"):
        entries = value.get(field)
        if not isinstance(entries, list):
            raise ValueError(f"settings field {field} must be a list")
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("path"), str):
                raise ValueError(f"settings field {field} contains an invalid root")
            path = Path(item["path"]).expanduser().resolve()
            if not path.is_dir():
                raise ValueError(f"configured voice root does not exist: {path}")
            if item.get("modes", ["read", "search"]) != ["read", "search"]:
                raise ValueError("voice roots may only grant read and search")


def inspect_settings() -> dict[str, Any]:
    value = load_settings(create=True)
    return {**value, "settings_path": str(settings_path()), "directories": {
        "assignments": str(user_data_root() / "Assignments"),
        "prepared_talking_points": str(user_data_root() / "Prepared Talking Points"),
        "notes": str(user_data_root() / "Notes"),
    }}


def _readme(root: Path) -> str:
    return f"""# Hermes Call Assistant data\n\nThis is the user-visible data folder for the phone/call assistant.\n\n- `Assignments/` — captured phone assignments, one JSON file per assignment.\n- `Prepared Talking Points/` — files intentionally made available to the small voice model.\n- `Notes/` — default user note directory; only configured roots are readable by voice.\n- `settings.json` — inspectable access policy and data-root configuration.\n\nPrivate call transcripts, session state, audio, and post-call worker artifacts remain under the Hermes profile cache.\n\nData root: `{root}`\n"""
