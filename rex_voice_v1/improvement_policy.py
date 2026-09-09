from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

AUTO = "AUTO"
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
FORBIDDEN_SELF_MODIFICATION = "FORBIDDEN_SELF_MODIFICATION"

_FORBIDDEN = re.compile(r"\b(sudo|credential|password|secret|token|arbitrary shell|shell command|system config|privilege|root access)\b", re.I)
_APPROVAL = re.compile(r"\b(send|email|message|external|permission|access|delete|overwrite|mutate|modify user data|broader authority)\b", re.I)


def _user_texts(packet: dict[str, Any]) -> list[str]:
    return [m.get("text", "") for m in packet.get("call_record", {}).get("conversation", [])
            if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("text"), str)]


def classify_improvement(proposal: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    request = str(proposal.get("request", ""))
    if _FORBIDDEN.search(request):
        policy = FORBIDDEN_SELF_MODIFICATION
    elif _APPROVAL.search(request):
        policy = APPROVAL_REQUIRED
    else:
        evidence = proposal.get("transcript_evidence", [])
        spec = proposal.get("deterministic_spec")
        evidence_ok = isinstance(evidence, list) and bool(evidence) and all(
            isinstance(item, str) and item in _user_texts(packet) for item in evidence
        )
        if proposal.get("requested_by_user") is True and float(proposal.get("confidence", 0)) >= 0.9 and evidence_ok and isinstance(spec, dict) and spec:
            policy = AUTO
        else:
            policy = APPROVAL_REQUIRED
    status = "rejected" if policy == FORBIDDEN_SELF_MODIFICATION else ("implemented" if proposal.get("status") == "implemented" else "approval_required")
    return {"policy_class": policy, "approval": status, "reason": "policy classification", "reviewed_at": time.time()}


class AutoImprovementPipeline:
    """Hermes-owned gate for bounded improvements; reviewers cannot call the implementer."""

    def __init__(self, registry_path: Path, *, implementer: Callable[[dict[str, Any], Path], dict[str, Any]] | None = None, tests: Callable[[dict[str, Any]], None] | None = None, bridge_register: Callable[[str, Callable[[dict[str, Any]], Any]], None] | None = None):
        self.registry_path = Path(registry_path)
        self.audit_path = self.registry_path.with_name("improvement-audit.jsonl")
        self.work_root = self.registry_path.with_name("improvement-work")
        self.implementer = implementer
        self.tests = tests or self._default_tests
        self.bridge_register = bridge_register
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return {}
        value = json.loads(self.registry_path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _default_tests(spec: dict[str, Any]) -> None:
        if spec.get("name") == "word_count":
            assert AutoImprovementPipeline._word_count({"text": "one two"}) == 2
        else:
            raise ValueError("no Hermes-owned focused test for capability")

    @staticmethod
    def _word_count(arguments: dict[str, Any]) -> int:
        text = arguments.get("text")
        if not isinstance(text, str) or len(text) > 100_000:
            raise ValueError("text must be a bounded string")
        return len(text.split())

    def apply(self, proposal: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
        decision = classify_improvement(proposal, packet)
        if decision["policy_class"] != AUTO:
            result = {**proposal, **decision, "status": "rejected" if decision["policy_class"] == FORBIDDEN_SELF_MODIFICATION else "deferred"}
            self._audit(result)
            return result
        spec = proposal["deterministic_spec"]
        name = spec.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name):
            raise ValueError("invalid deterministic capability name")
        if self.implementer is None:
            result = {**proposal, **decision, "status": "deferred", "reason": "no gated implementer configured"}
            self._audit(result)
            return result
        workdir = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=self.work_root if self.work_root.exists() else None))
        try:
            implementation = self.implementer(spec, workdir)
            if not isinstance(implementation, dict):
                raise ValueError("implementer must return an implementation manifest")
            self.tests(spec)
            registry = self._load()
            previous = registry.get(name)
            registry[name] = {"status": "active", "spec": spec, "implementation": implementation, "registered_at": time.time()}
            self.registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
            if self.bridge_register is not None:
                self.bridge_register(name, lambda arguments, capability=name: self.invoke(capability, arguments))
            result = {**proposal, **decision, "status": "implemented", "capability": name,
                      "rollback": {"available": True, "previous": previous}, "implementation": implementation}
        except Exception as exc:
            result = {**proposal, **decision, "status": "deferred", "reason": str(exc)}
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        self._audit(result)
        return result

    def invoke(self, name: str, arguments: dict[str, Any]) -> Any:
        entry = self._load().get(name)
        if not entry or entry.get("status") != "active":
            raise KeyError(f"capability is not registered: {name}")
        if name == "word_count":
            return self._word_count(arguments)
        raise KeyError(f"no Hermes-owned implementation: {name}")

    def _audit(self, result: dict[str, Any]) -> None:
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"timestamp": time.time(), "proposal_id": result.get("id"), "status": result.get("status"), "policy_class": result.get("policy_class"), "result": result}, sort_keys=True) + "\n")
