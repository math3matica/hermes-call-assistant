from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
import threading
from pathlib import Path

from .post_call import PostCallQueue, configured_processor


def process_one(queue: PostCallQueue, session_id: str) -> dict:
    job = queue.load(session_id)
    gate = job.get("execution_gate", {})
    if gate.get("status") != "released":
        return {
            "status": "blocked",
            "session_id": session_id,
            "reason": "qwen_not_ready",
            "execution_gate": gate,
        }
    lock = queue.root / f"{session_id}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = lock.open("a+")
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already_running", "session_id": session_id}
        acquired = True
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        return configured_processor(queue).process(session_id)
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def process_pending(root: Path, session_id: str | None = None) -> list[dict]:
    queue = PostCallQueue(root)
    sessions = [session_id] if session_id else [job["session_id"] for job in queue.pending()]
    return [process_one(queue, sid) for sid in sessions]


def start_background(root: Path, session_id: str) -> threading.Thread | None:
    if os.getenv("HERMES_VOICE_CHAT_POST_CALL_AUTOSTART", os.getenv("REX_POST_CALL_AUTOSTART", "true")).lower() in {"0", "false", "no", "off"}:
        return None
    thread = threading.Thread(target=process_one, args=(PostCallQueue(root), session_id), name=f"rex-post-call-{session_id}", daemon=True)
    thread.start()
    return thread


def start_detached(
    root: Path,
    session_id: str,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    """Run one post-call job outside a short-lived standalone voice process."""
    queue_root = Path(root)
    log_path = queue_root / "activity" / f"worker-{session_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab")
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "rex_voice_v1.post_call_worker", "--root", str(queue_root), "--session", session_id],
            cwd=str(queue_root.parent.parent),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
    finally:
        log.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover and process Rex post-call jobs")
    parser.add_argument("--root", required=True, type=Path, help="post-call queue root")
    parser.add_argument("--session")
    args = parser.parse_args()
    process_pending(args.root, args.session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
