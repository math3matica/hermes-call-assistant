from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rex_voice_v1.github_improvement import ImprovementRejected, _patch, _targets, apply_github_backed_improvement


def test_improvement_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_CALL_ASSISTANT_AUTO_IMPROVE", raising=False)
    result = apply_github_backed_improvement({}, {})
    assert result == {"status": "deferred", "reason": "automatic improvement is disabled"}


def test_improvement_allowlist_rejects_non_runtime_paths() -> None:
    with pytest.raises(ImprovementRejected):
        _targets(["tests/test_boundary.py"])
    with pytest.raises(ImprovementRejected):
        _targets(["rex_voice_v1/../pyproject.toml"])


def test_improvement_patch_must_match_declared_targets() -> None:
    patch = "--- a/rex_voice_v1/launch.py\n+++ b/rex_voice_v1/launch.py\n@@ -1 +1 @@\n-x\n+y\n"
    assert _patch(patch, ["rex_voice_v1/launch.py"]) == patch
    with pytest.raises(ImprovementRejected):
        _patch(patch, ["rex_voice_v1/backend.py"])


def test_github_backed_improvement_commits_and_pushes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "master", str(repo)], check=True, capture_output=True)
    (repo / "rex_voice_v1").mkdir()
    target = repo / "rex_voice_v1" / "demo.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    git("add", ".")
    git("commit", "-m", "baseline")
    git("remote", "add", "origin", str(remote))
    git("push", "-u", "origin", "master")
    monkeypatch.setenv("HERMES_CALL_ASSISTANT_AUTO_IMPROVE", "true")
    monkeypatch.setenv("HERMES_CALL_ASSISTANT_REPOSITORY", str(repo))
    packet = {"call_record": {"conversation": [{"role": "user", "text": "Please improve this."}]}}
    proposal = {
        "id": "improve-demo",
        "title": "Improve demo",
        "request": "Please improve this.",
        "confidence": 0.99,
        "requested_by_user": True,
        "transcript_evidence": ["Please improve this."],
        "deterministic_spec": {"name": "demo", "input": "text", "output": "text"},
        "target_files": ["rex_voice_v1/demo.py"],
        "patch": "--- a/rex_voice_v1/demo.py\n+++ b/rex_voice_v1/demo.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n",
    }
    result = apply_github_backed_improvement(proposal, packet, run_tests=lambda _repo: None)
    assert result["status"] == "completed"
    assert result["commit"]
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"
    assert subprocess.run(["git", "ls-remote", "origin", "refs/heads/master"], cwd=repo, check=True, text=True, capture_output=True).stdout.strip()
