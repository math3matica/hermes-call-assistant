from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

from .backend import CapabilityError
from .note_search import MAX_RESULTS, search_note_chunks


class RexVaultAdapter:
    """Adapt a configured Hermes document-store plugin without exposing raw tools to Pi."""

    def __init__(self, vault_root: Path, plugin_path: Path | None = None):
        self.vault_root = Path(vault_root).expanduser().resolve()
        if not self.vault_root.is_dir():
            raise CapabilityError(f"vault does not exist: {self.vault_root}")
        if plugin_path is None:
            configured = os.environ.get("HERMES_VOICE_CHAT_DOCUMENT_PLUGIN", "").strip()
            plugin_path = Path(configured).expanduser() if configured else Path.home() / ".hermes/plugins/document-store/__init__.py"
            if not plugin_path.exists():
                legacy_path = Path.home() / ".hermes/plugins/rex-vault/__init__.py"
                if legacy_path.exists():
                    plugin_path = legacy_path
        os.environ["OBSIDIAN_VAULT_PATH"] = str(self.vault_root)
        # The plugin is a Hermes provider, so import it in the Hermes source
        # environment rather than copying its storage/backup implementation.
        hermes_root = str(Path.home() / ".hermes/hermes-agent")
        if hermes_root not in sys.path:
            sys.path.insert(0, hermes_root)
        spec = importlib.util.spec_from_file_location("hermes_voice_chat_document_provider", plugin_path)
        if not spec or not spec.loader:
            raise CapabilityError(f"cannot load document-store provider: {plugin_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.provider = module

    def _call(self, name: str, args: dict[str, Any], session_id: str = "") -> dict[str, Any]:
        try:
            raw = getattr(self.provider, name)(args, session_id=session_id)
            result = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:
            raise CapabilityError(str(exc)) from exc
        if not isinstance(result, dict) or not result.get("ok"):
            raise CapabilityError(str((result or {}).get("error", f"provider operation failed: {name}")))
        return result

    def create(self, session_id: str, name: str, content: str = "") -> dict[str, Any]:
        return self._resource(self._call("note_create", {"path": self._note_path(name), "content": content}, session_id))

    def open(self, session_id: str, name: str) -> dict[str, Any]:
        return self._resource(self._call("note_open", {"path": self._note_path(name)}, session_id))

    def read(self, name: str) -> str:
        result = self._call("note_read", {"path": self._note_path(name)})
        return str(result.get("content", ""))

    def append(self, name: str, content: str, session_id: str = "") -> dict[str, Any]:
        return self._resource(self._call("note_append", {"path": self._note_path(name), "content": content}, session_id))

    def replace_paragraph(self, name: str, number: int, content: str, session_id: str = "") -> dict[str, Any]:
        return self._resource(self._call("note_replace_target", {
            "path": self._note_path(name), "target_type": "paragraph", "paragraph_number": number, "new_content": content,
        }, session_id))

    def replace_text(self, name: str, old_text: str, content: str, session_id: str = "") -> dict[str, Any]:
        return self._resource(self._call("note_update", {
            "path": self._note_path(name), "old_text": old_text, "new_text": content,
        }, session_id))

    def rename(self, session_id: str, old_name: str, new_name: str) -> dict[str, Any]:
        return self._resource(self._call("note_rename", {"path": self._note_path(old_name), "new_path": self._note_path(new_name)}, session_id))

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        result = self._call("vault_search", {"query": query})
        return list(result.get("matches", []))[:limit]

    def search_chunks(self, query: str, *, source_refs: list[str] | None = None, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
        """Search bounded authoritative Markdown chunks without using the web or Vault summaries."""
        return search_note_chunks(self.vault_root, query, source_refs=source_refs, limit=limit)

    @staticmethod
    def _note_path(name: str) -> str:
        name = str(name).strip()
        return name if Path(name).suffix == ".md" else f"{name}.md"

    def _resource(self, result: dict[str, Any]) -> dict[str, Any]:
        envelope = result.get("context_compiler")
        if not isinstance(envelope, dict) or envelope.get("status") != "success":
            raise CapabilityError("document store returned no validated resource identity")
        resource = envelope.get("resource")
        if not isinstance(resource, dict) or not resource.get("canonical_id"):
            raise CapabilityError("document store returned an invalid resource identity")
        canonical = Path(str(resource["canonical_id"])).resolve()
        try:
            display_name = str(canonical.relative_to(self.vault_root))
        except ValueError:
            display_name = canonical.name
        return {
            "canonical_id": resource["canonical_id"],
            "provider": resource.get("provider", "rex_vault"),
            "source": resource.get("source", "validated_tool_operation"),
            "display_name": display_name,
            "version": resource.get("version", ""),
            "version_verified": bool(resource.get("version_verified", False)),
        }
