from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any

from .note_search import MAX_RESULTS, NoteSearchError, search_note_chunks


class WorkspaceAccessError(ValueError):
    pass


class RexVoiceWorkspace:
    """Explicit resource boundary for Voice Chat; never exposes arbitrary paths."""

    SUBFOLDERS = {"drafts", "working-notes", "completed-notes", "inbox"}

    def __init__(self, vault_root: Path):
        self.vault_root = Path(vault_root).expanduser().resolve()
        self.root = self.vault_root / "Voice Workspace"
        self.root.mkdir(parents=True, exist_ok=True)
        for folder in self.SUBFOLDERS:
            (self.root / folder).mkdir(exist_ok=True)
        self._roots: dict[str, Path] = {"voice-workspace": self.root}
        self._manifest = self.root / ".authorized-roots.json"
        if self._manifest.exists():
            try:
                for item in json.loads(self._manifest.read_text(encoding="utf-8")):
                    path = Path(str(item["path"])).expanduser().resolve()
                    if path.is_dir() and str(item.get("id", "")) != "voice-workspace":
                        self._roots[str(item["id"])] = path
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                pass

    @property
    def default_root(self) -> Path:
        return self.root / "completed-notes"

    def grant_root(self, root_id: str, path: Path, *, modes: tuple[str, ...] = ("read", "search")) -> dict[str, Any]:
        if not root_id or not root_id.replace("-", "").replace("_", "").isalnum():
            raise WorkspaceAccessError("invalid root id")
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise WorkspaceAccessError(f"authorized root is not a directory: {resolved}")
        if any(mode not in {"read", "search"} for mode in modes):
            raise WorkspaceAccessError("roots may only grant read and search")
        self._roots[root_id] = resolved
        self._manifest.write_text(json.dumps(self.authorized_roots(), indent=2, sort_keys=True), encoding="utf-8")
        return {"id": root_id, "path": str(resolved), "modes": list(modes)}

    def authorized_roots(self) -> list[dict[str, Any]]:
        return [{"id": key, "path": str(path), "modes": ["read", "search"]} for key, path in self._roots.items()]

    def resolve(self, target: str, *, root_id: str = "voice-workspace", write: bool = False) -> Path:
        if not isinstance(target, str) or not target.strip() or "\x00" in target:
            raise WorkspaceAccessError("target must be a non-empty safe path")
        base = self._roots.get(root_id)
        if base is None:
            raise WorkspaceAccessError(f"resource root is not authorized: {root_id}")
        candidate = Path(target).expanduser() if Path(target).is_absolute() else base / target
        candidate = candidate.resolve()
        try:
            candidate.relative_to(base.resolve())
        except ValueError as exc:
            raise WorkspaceAccessError("resource path is outside its authorized root") from exc
        if write and root_id != "voice-workspace":
            raise WorkspaceAccessError("additional roots are read/search only")
        return candidate

    def document_target(self, name: str) -> str:
        if Path(name).is_absolute():
            return str(self.resolve(name, write=True))
        candidate = self.resolve(name, write=True, root_id="voice-workspace") if str(name).startswith(tuple(f"{folder}/" for folder in self.SUBFOLDERS)) else (self.default_root / name).resolve()
        candidate.relative_to(self.root)
        return str(candidate.relative_to(self.vault_root))

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        query = query.casefold()
        found: list[dict[str, Any]] = []
        for root_id, root in self._roots.items():
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in {".md", ".txt"}:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if query in text.casefold() or query in path.name.casefold():
                    found.append({"path": str(path), "canonical_id": str(path), "display_name": str(path.relative_to(root)), "content": text[:1200], "root_id": root_id, "provider": "rex-voice-workspace"})
                    if len(found) >= limit:
                        return found
        return found

    def search_chunks(self, query: str, *, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for root_id, root in self._roots.items():
            try:
                results = search_note_chunks(root, query, limit=limit)
            except NoteSearchError:
                continue
            for result in results:
                item = dict(result)
                item["root_id"] = root_id
                item["source_id"] = f"{root_id}:{result['source_id']}"
                found.append(item)
        found.sort(key=lambda item: (-float(item.get("score", 0)), str(item.get("source_id", ""))))
        return found[: min(max(int(limit), 1), MAX_RESULTS)]
