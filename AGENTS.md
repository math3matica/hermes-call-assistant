# Agent instructions: Hermes Call Assistant

## Canonical source and runtime boundaries

- Canonical Call Assistant source: `/home/math3matica/hermes-call-assistant`.
- Installed Hermes plugin: `~/.hermes/plugins/hermes-call-assistant`, normally a symlink to `<canonical>/plugin`.
- Phone control and host call worker live in `/home/math3matica/hermes-phone-bridge`.
- Hermes core/runtime lives under `~/.hermes/hermes-agent`.
- The old integrated checkout `/home/math3matica/hermes` is not the canonical Call Assistant source. Do not use it as `REX_VOICE_ROOT` unless explicitly testing legacy compatibility.

Read `docs/OPERATIONS.md` and `docs/OWNERSHIP.md` before changing call behavior.

## Non-negotiable rules

1. Inspect both repositories and the installed symlink before editing.
2. Keep source, installed payload, running process, cache, and evidence as separate boundaries.
3. Never edit `~/.hermes/plugins`, cache directories, `__pycache__`, logs, or generated call artifacts as canonical source.
4. A corrected source file is not active in a long-running Hermes or phone-relay process until a fresh process loads it.
5. Do not replace Hermes `/voice`; this plugin owns only the explicit Call Assistant surface.
6. Do not place a physical call, redial, hang up, or change phone/audio hardware state without explicit authorization.
7. Do not report a call as working from call connection, plugin registration, worker startup, or TTS file creation alone.
8. Preserve failed evidence. Do not broadly delete caches or historical artifacts to make a test appear clean.

## Required verification

For source changes, run the focused tests in each affected repository with its supported isolated runner, then run a fresh-process import/registration probe. For live call diagnosis, identify the first missing lifecycle marker:

`CALL_AUDIO_ACTIVE -> CAPTURED -> TRANSCRIPT -> RESPONSE -> TTS_READY -> playback`

The first missing marker defines the fault domain. Stop at missing audio prerequisites; do not compensate with model or plugin changes.

## Environment ownership

The phone worker must receive explicit roots, including:

- `HERMES_ROOT` for Hermes imports;
- `REX_VOICE_ROOT=/home/math3matica/hermes-call-assistant` for the canonical runtime;
- `HERMES_HOME` for profile state and evidence;
- `HERMES_CALL_WORKER_SESSION_ID` for correlation.

Never rely on the caller's current working directory or ambient `PYTHONPATH` for production behavior.
