"""Small, stable model-facing contract for Voice Chat V1."""
from __future__ import annotations

MODEL_CAPABILITIES = (
    "retrieval",
    "topic_activate",
    "resource_read",
    "resource_mutate",
    "resource_manage",
    "draft_manage",
    "assignment_capture",
    "phone_hangup",
)

RESOURCE_OPERATIONS = ("append", "replace")
MANAGE_OPERATIONS = ("active", "create", "open", "rename")
DRAFT_OPERATIONS = ("create", "read", "revise", "discard", "promote")


def require_mapping(value: object, name: str = "arguments") -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def require_string(args: dict, key: str, *, optional: bool = False) -> str | None:
    value = args.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def validate_call(operation: str, raw_args: object) -> dict:
    if operation not in MODEL_CAPABILITIES:
        raise ValueError(f"operation is not allowlisted: {operation}")
    args = require_mapping(raw_args)
    allowed_by_operation: dict[str, set[str]] = {
        "retrieval": {"query", "mode", "limit"},
        "topic_activate": {"query"},
        "resource_read": {"target", "region"},
        "resource_mutate": {"operation", "content", "target", "region", "expected_version"},
        "resource_manage": {"operation", "target", "content", "new_name"},
        "draft_manage": {"operation", "draft_id", "content", "target", "source_handles"},
        "assignment_capture": {"request", "topic", "assignment_type", "desired_outputs", "priority", "unresolved_questions", "source_handles", "transcript_refs", "notify_on_completion", "notification_channel"},
        "phone_hangup": set(),
    }
    unknown = set(args) - allowed_by_operation[operation]
    if unknown:
        raise ValueError(f"unsupported arguments: {sorted(unknown)}")
    if operation == "phone_hangup":
        return args
    if operation == "topic_activate":
        require_string(args, "query")
    elif operation == "retrieval":
        require_string(args, "query")
        mode = require_string(args, "mode")
        if mode not in {"note", "prepared", "current_web"}:
            raise ValueError("mode must be note, prepared, or current_web")
        if "limit" in args and (not isinstance(args["limit"], int) or not 1 <= args["limit"] <= 10):
            raise ValueError("limit must be an integer between 1 and 10")
    elif operation == "resource_read":
        if "target" in args:
            require_string(args, "target", optional=True)
        if "region" in args:
            region = args["region"]
            if not isinstance(region, dict) or region.get("target_type") != "paragraph" or not isinstance(region.get("paragraph_number"), int) or region["paragraph_number"] < 1:
                raise ValueError("region must identify a positive paragraph number")
    elif operation == "resource_mutate":
        action = require_string(args, "operation")
        if action not in RESOURCE_OPERATIONS:
            raise ValueError("operation must be append or replace")
        require_string(args, "content")
        if "target" in args:
            require_string(args, "target", optional=True)
        if "expected_version" in args:
            require_string(args, "expected_version")
        if action == "replace":
            region = args.get("region")
            if not isinstance(region, dict):
                raise ValueError("replace requires a paragraph or exact_text region")
            target_type = region.get("target_type")
            if target_type == "paragraph":
                if not isinstance(region.get("paragraph_number"), int) or region["paragraph_number"] < 1:
                    raise ValueError("paragraph region must identify a positive paragraph number")
            elif target_type == "exact_text":
                require_string(region, "old_text")
            else:
                raise ValueError("replace region must be paragraph or exact_text")
    elif operation == "resource_manage":
        action = require_string(args, "operation")
        if action not in MANAGE_OPERATIONS:
            raise ValueError("operation must be active, create, open, or rename")
        if action == "create":
            require_string(args, "target")
            require_string(args, "content")
        elif action == "open":
            require_string(args, "target")
        if action == "rename":
            require_string(args, "target")
            require_string(args, "new_name")
    elif operation == "draft_manage":
        action = require_string(args, "operation")
        if action not in DRAFT_OPERATIONS:
            raise ValueError("operation must be create, read, revise, discard, or promote")
        if action == "create":
            require_string(args, "content")
            if "source_handles" in args and (not isinstance(args["source_handles"], list) or not all(isinstance(v, str) for v in args["source_handles"])):
                raise ValueError("source_handles must be a list of strings")
        elif action == "read":
            if "draft_id" not in args:
                raise ValueError("read requires an exact draft_id")
            require_string(args, "draft_id")
        elif "draft_id" in args:
            require_string(args, "draft_id")
        if action == "promote" and "draft_id" not in args:
            raise ValueError("promote requires an exact draft_id")
        if action == "revise":
            require_string(args, "content")
    elif operation == "assignment_capture":
        require_string(args, "request")
        for key in ("desired_outputs", "unresolved_questions", "source_handles", "transcript_refs"):
            if key in args and (
                not isinstance(args[key], list)
                or not all(isinstance(v, str) for v in args[key])
            ):
                raise ValueError(f"{key} must be a list of strings")
        if "transcript_refs" in args:
            if not all(value.strip() for value in args["transcript_refs"]):
                raise ValueError("transcript_refs must contain non-empty strings")
            args["transcript_refs"] = [value.strip() for value in args["transcript_refs"]]
        if "priority" in args and args["priority"] not in {"normal", "high"}:
            raise ValueError("priority must be normal or high")
        if "assignment_type" in args and args["assignment_type"] not in {"writing", "research", "preparation"}:
            raise ValueError("assignment_type must be writing, research, or preparation")
        if "notify_on_completion" in args and not isinstance(args["notify_on_completion"], bool):
            raise ValueError("notify_on_completion must be a boolean")
        if "notification_channel" in args:
            channel = require_string(args, "notification_channel")
            if channel != "sms":
                raise ValueError("notification_channel must be sms")
    return args
