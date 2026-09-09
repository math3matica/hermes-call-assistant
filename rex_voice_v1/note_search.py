from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable


MAX_RESULTS = 4
MAX_TOTAL_CHARS = 10_000
MAX_CHUNK_CHARS = 2_400
_EXCLUDED_PARTS = {".rex-vault-backups", "Voice Workspace"}
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_-]*", re.IGNORECASE)
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


class NoteSearchError(ValueError):
    pass


def _safe_relative(root: Path, value: str) -> Path:
    candidate = Path(value).expanduser() if Path(value).is_absolute() else root / value
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise NoteSearchError("source reference is outside the authorized note root") from exc
    return candidate


def _eligible(path: Path, root: Path) -> bool:
    return path.is_file() and path.suffix.lower() == ".md" and not any(part in _EXCLUDED_PARTS for part in path.relative_to(root).parts)


def _paragraph_chunks(text: str) -> Iterable[tuple[str | None, str]]:
    section: str | None = None
    paragraphs: list[str] = []
    for line in text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            if paragraphs:
                yield section, "\n".join(paragraphs).strip()
                paragraphs = []
            section = heading.group(2).strip()
            continue
        if not line.strip() and paragraphs:
            yield section, "\n".join(paragraphs).strip()
            paragraphs = []
        elif line.strip():
            paragraphs.append(line.rstrip())
    if paragraphs:
        yield section, "\n".join(paragraphs).strip()


def _bounded_parts(section: str | None, content: str) -> Iterable[tuple[str | None, str]]:
    if len(content) <= MAX_CHUNK_CHARS:
        yield section, content
        return
    for offset in range(0, len(content), MAX_CHUNK_CHARS):
        piece = content[offset:offset + MAX_CHUNK_CHARS].strip()
        if piece:
            yield section, piece


def search_note_chunks(root: Path, query: str, *, source_refs: list[str] | None = None, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise NoteSearchError(f"authorized note root does not exist: {root}")
    if not isinstance(query, str) or not query.strip():
        raise NoteSearchError("query is required")
    bounded_limit = min(max(int(limit), 1), MAX_RESULTS)
    terms = sorted(set(token.casefold() for token in _TOKEN.findall(query)))
    if not terms:
        return []

    if source_refs is not None:
        paths: list[Path] = []
        for reference in source_refs:
            if not isinstance(reference, str) or not reference.strip():
                raise NoteSearchError("source references must be non-empty strings")
            path = _safe_relative(root, reference.strip())
            if not _eligible(path, root):
                if path.exists() and path.is_file():
                    raise NoteSearchError("source reference must identify an authorized Markdown note")
                continue
            paths.append(path)
    else:
        paths = [path for path in sorted(root.rglob("*.md")) if _eligible(path, root)]

    candidates: list[dict[str, Any]] = []
    for path in sorted(set(paths)):
        try:
            text = path.read_text(encoding="utf-8")
            stat = path.stat()
        except (OSError, UnicodeDecodeError):
            continue
        relative = str(path.relative_to(root))
        version = f"{stat.st_mtime_ns}:{stat.st_size}"
        chunk_number = 0
        for section, paragraph in _paragraph_chunks(text):
            for chunk_section, raw_content in _bounded_parts(section, paragraph):
                content = raw_content.strip()
                haystack = f"{relative} {chunk_section or ''} {content}".casefold()
                matched = sum(term in haystack for term in terms)
                if not matched:
                    chunk_number += 1
                    continue
                score = matched / len(terms)
                if query.casefold() in haystack:
                    score += 0.25
                candidates.append({
                    "source_id": relative,
                    "source_path": relative,
                    "section": chunk_section,
                    "chunk_id": f"{relative}#chunk-{chunk_number + 1}",
                    "content": content,
                    "score": round(score, 6),
                    "version": version,
                })
                chunk_number += 1

    candidates.sort(key=lambda item: (-item["score"], item["source_path"], item["chunk_id"]))
    selected: list[dict[str, Any]] = []
    total = 0
    for item in candidates:
        if len(selected) >= bounded_limit:
            break
        remaining = MAX_TOTAL_CHARS - total
        if remaining <= 0:
            break
        if len(item["content"]) > remaining:
            item = dict(item)
            item["content"] = item["content"][:remaining].rstrip()
        if not item["content"]:
            continue
        selected.append(item)
        total += len(item["content"])
    return selected
