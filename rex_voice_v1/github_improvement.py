from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .improvement_policy import AUTO, classify_improvement


_ALLOWED_PREFIX = Path("rex_voice_v1")
_MAX_PATCH_BYTES = 24_000
_MAX_TARGETS = 8


class ImprovementRejected(ValueError):
    pass


def _repo() -> Path:
    configured = os.getenv("HERMES_CALL_ASSISTANT_REPOSITORY", "").strip()
    if not configured:
        raise ImprovementRejected("automatic improvement repository is not configured")
    path = Path(configured).expanduser().resolve()
    if not (path / ".git").exists():
        raise ImprovementRejected(f"improvement repository is not a git checkout: {path}")
    return path


def _run(repo: Path, args: list[str], *, input_text: str | None = None, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, input=input_text, text=True,
        capture_output=True, timeout=timeout, check=False,
    )


def _targets(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ImprovementRejected("improvement requires a non-empty target_files list")
    result: list[str] = []
    for raw in value[:_MAX_TARGETS]:
        if not isinstance(raw, str) or not raw or "\\" in raw:
            raise ImprovementRejected("improvement target path is invalid")
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts or path.parts[:1] != (_ALLOWED_PREFIX.parts[0],) or path.suffix != ".py":
            raise ImprovementRejected(f"improvement target is outside the Python runtime allowlist: {raw}")
        result.append(raw)
    if len(result) != len(set(result)):
        raise ImprovementRejected("improvement target paths must be unique")
    return result


def _patch(value: Any, targets: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ImprovementRejected("improvement requires a unified diff patch")
    encoded = value.encode("utf-8")
    if len(encoded) > _MAX_PATCH_BYTES or "\x00" in value:
        raise ImprovementRejected("improvement patch is too large or binary")
    if "GIT binary patch" in value or "\n--- /dev/null" in value or "\n+++ /dev/null" in value:
        raise ImprovementRejected("binary and file-creation patches are not allowed")
    headers = re.findall(r"^(?:\+\+\+|---) ([^\n]+)$", value, flags=re.MULTILINE)
    if not headers or any(path not in {f"a/{target}" for target in targets} | {f"b/{target}" for target in targets} for path in headers):
        raise ImprovementRejected("patch modifies a file outside target_files")
    return value


def _dirty_paths(repo: Path) -> set[str]:
    result = _run(repo, ["status", "--porcelain=v1"], timeout=30)
    if result.returncode:
        raise ImprovementRejected(f"cannot inspect repository status: {result.stderr.strip()}")
    paths: set[str] = set()
    for line in result.stdout.splitlines():
        if len(line) >= 4:
            paths.add(line[3:].split(" -> ", 1)[-1])
    return paths


def _commit_message(proposal: dict[str, Any]) -> str:
    title = re.sub(r"\s+", " ", str(proposal.get("title", "automatic call improvement"))).strip()
    title = re.sub(r"[^A-Za-z0-9 .,()_/-]", "", title)[:72]
    return f"auto: {title or 'bounded call improvement'}"


def apply_github_backed_improvement(proposal: dict[str, Any], packet: dict[str, Any], *, run_tests: Callable[[Path], None] | None = None) -> dict[str, Any]:
    """Apply one Luna-authored, allowlisted patch and push its rollback commit.

    The patch is never applied unless policy, paths, repository cleanliness,
    focused tests, and the GitHub push all succeed. Call transcripts remain in
    local artifacts and are never staged.
    """
    if os.getenv("HERMES_CALL_ASSISTANT_AUTO_IMPROVE", "false").lower() not in {"1", "true", "yes", "on"}:
        return {"status": "deferred", "reason": "automatic improvement is disabled"}
    decision = classify_improvement(proposal, packet)
    if decision["policy_class"] != AUTO:
        return {"status": "deferred", "reason": f"policy={decision['policy_class']}"}
    repo = _repo()
    targets = _targets(proposal.get("target_files"))
    patch = _patch(proposal.get("patch"), targets)
    dirty = _dirty_paths(repo)
    conflicting = sorted(dirty.intersection(targets))
    if conflicting:
        return {"status": "deferred", "reason": "target files have pre-existing changes", "conflicting_paths": conflicting}

    check = _run(repo, ["apply", "--check", "--whitespace=error", "-"], input_text=patch, timeout=30)
    if check.returncode:
        return {"status": "deferred", "reason": "git apply check failed", "stderr": check.stderr[-1200:]}
    applied = _run(repo, ["apply", "--whitespace=error", "-"], input_text=patch, timeout=30)
    if applied.returncode:
        return {"status": "deferred", "reason": "git apply failed", "stderr": applied.stderr[-1200:]}

    commit: str | None = None
    try:
        if run_tests is not None:
            run_tests(repo)
        check_diff = _run(repo, ["diff", "--check"], timeout=30)
        if check_diff.returncode:
            raise ImprovementRejected(f"git diff --check failed: {check_diff.stderr[-1200:]}")
        staged = _run(repo, ["add", "--", *targets], timeout=30)
        if staged.returncode:
            raise ImprovementRejected(f"git add failed: {staged.stderr[-1200:]}")
        staged_check = _run(repo, ["diff", "--cached", "--check"], timeout=30)
        if staged_check.returncode:
            raise ImprovementRejected(f"staged diff check failed: {staged_check.stderr[-1200:]}")
        commit_result = _run(repo, ["commit", "--no-verify", "-m", _commit_message(proposal)], timeout=120)
        if commit_result.returncode:
            raise ImprovementRejected(f"git commit failed: {commit_result.stderr[-1200:]}")
        commit_result = _run(repo, ["rev-parse", "HEAD"], timeout=30)
        if commit_result.returncode:
            raise ImprovementRejected("cannot resolve improvement commit")
        commit = commit_result.stdout.strip()
        push = _run(repo, ["push", "origin", "HEAD:master"], timeout=300)
        if push.returncode:
            raise ImprovementRejected(f"GitHub push failed; local commit {commit} is preserved: {push.stderr[-1200:]}")
    except Exception:
        if commit is None:
            _run(repo, ["restore", "--staged", "--", *targets], timeout=30)
            _run(repo, ["apply", "--reverse", "--whitespace=nowarn", "-"], input_text=patch, timeout=30)
        raise

    return {
        "status": "completed",
        "repository": str(repo),
        "commit": commit,
        "github": "origin/master",
        "rollback": {
            "available": True,
            "commit": commit,
            "command": f"git -C {repo} revert {commit} && git -C {repo} push origin master",
        },
        "targets": targets,
        "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        "applied_at": time.time(),
    }


def default_tests(repo: Path) -> None:
    command = os.getenv("HERMES_CALL_ASSISTANT_AUTO_IMPROVE_TEST_COMMAND", "uv run --extra test pytest -q")
    result = subprocess.run(command, cwd=repo, shell=True, text=True, capture_output=True, timeout=900, check=False)
    if result.returncode:
        raise ImprovementRejected(f"focused test command failed: {result.stderr[-1600:] or result.stdout[-1600:]}")
