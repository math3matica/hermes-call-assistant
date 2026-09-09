# hermes-voice-mode

Standalone package and Hermes plugin for the separate, call-oriented Rex Voice runtime.

## Scope

This package owns the specialized Rex runtime, its Pi capability bridge, the
private JSON-lines Unix-domain control plane, and the Hermes plugin adapter. It
supports status plus bounded session lifecycle (`start_session`,
`complete_session`, `abort_session`). The endpoint is created with mode `0600`,
rejects unknown operations, and never accepts shell commands, paths, source
code, phone numbers, or provider payloads.

## Relationship to Hermes Voice Mode

Hermes has a normal built-in Voice Mode, including its `/voice` command and
interactive recorder/UI lifecycle. That upstream feature is not replaced,
overridden, or renamed by this project.

Rex Voice is a second, specialized voice runtime for telephone/call operation.
It owns the call-oriented conversational loop, Rex-specific prompt and
capability behavior, the configured specialized model/runtime, and its
session/post-call handoff. It may reuse Hermes's public voice facilities for
microphone/STT/TTS when launched with `--voice`, but that does not make it the
implementation of Hermes `/voice`. Both systems can coexist independently.

It intentionally does **not** implement or claim a replacement for Hermes
`/voice`, and it does not silently dial, hang up, or start a call from plugin
registration. Phone Bridge owns telephone transport and call state; this
runtime is activated explicitly and receives an authorized call/audio boundary
from that bridge.

## Run

```sh
python -m pip install -e .
rex-voice-mode --socket /tmp/rex-voice-mode.sock
# Existing direct runtime entrypoint:
rex-call-voice --help
# Hermes plugin CLI entrypoint after plugin installation:
hermes call-voice --help
```

Set `REX_VOICE_MODE_SOCKET` for safe status/session control, then install the
directory `plugin/` using Hermes's normal project/user plugin mechanism. The
adapter uses public registration APIs only. `hermes call-voice` is a deliberate
separate command and invokes the packaged `rex_voice_v1.launch` entrypoint; it
does not claim or register `/voice`.

The existing direct source invocation remains available from the source tree:

```sh
python -m rex_voice_v1.launch [--voice] [--manage-model]
```

For Phone Bridge calls, the bridge's call audio worker creates one
`rex_voice_v1.launch.RexVoiceSession` per call and owns capture/playback,
authorization, hangup, and terminal cleanup. Rex Voice owns the specialized
conversation, capability bridge, session artifacts, and post-call handoff.
The model supervisor remains an external configured dependency; no model or
credentials are shipped.

### What is and is not implemented here

`rex_voice_v1` is a real standalone Rex conversational runtime: it owns the
Pi RPC turn loop, Rex capability bridge, session artifacts, and post-call
queue/recovery handoff. It is not merely packaging. However, `--voice` reuses
Hermes's existing `hermes_cli.voice` capture/STT/TTS functions; this project
does not provide a parallel audio or speech stack. Phone dialing,
authorization, call-state polling, hangup, and call audio remain external
Phone Bridge/worker responsibilities. The configured model supervisor is also
external. Therefore a successful install or dry runtime probe is not evidence
of a physical call or end-to-end audio conversation.

The adapter is inert until explicitly invoked: it registers only the safe Rex
status tool, `/rex-voice-mode`, and `hermes call-voice`. It never registers or
mutates Hermes `/voice`, starts a process during registration, or performs
telephony implicitly. This invariant is tested for both installed/inactive
behavior and the no-plugin case (when the adapter is simply not loaded).

## Shared Knowledge and Prepared Briefings

This repository bundles the text-facing Shared Knowledge component with the
separate Special Call Voice runtime. It does not add a fourth project.

- **Shared Knowledge** is durable, canonical, user-authorized knowledge stored
  as Markdown under `OBSIDIAN_VAULT_PATH/Knowledge`. It is published explicitly;
  raw transcripts, LCM artifacts, ByteRover data, assignments, and arbitrary
  filesystem content are not dumped into it.
- **Prepared Briefing** is bounded, large-model-prepared context for Special
  Call Voice. Packets record authorized source paths, SHA-256 source metadata,
  provenance, and current/stale/invalid state. Voice retrieval is topical and
  bounded, and stale packets are not activated.
- **Voice Workspace** is ephemeral live voice working state: drafts, notes,
  assignments, and per-call artifacts remain separate from canonical knowledge.
- **ByteRover/LCM/session history** is broader system memory used by the larger
  Hermes reasoning layer. It is not directly dumped into the smaller voice
  model.

The bundled Hermes surfaces are registered by `plugin/`:

- `publish_shared_knowledge`
- `prepare_for_voice`
- `retrieve_shared_knowledge`
- `/voice-knowledge` and the native `hermes voice-knowledge` command

The reusable workflow is documented in `skills/prepare-for-voice/SKILL.md`.
Post-call promotion is conservative: only explicit decisions and bounded
findings can be promoted, and promotion remains disabled when no authorized
Vault is configured.

## Verification

```sh
python -m pytest -q
python -m compileall -q src plugin
```

## License

MIT. See `LICENSE`.

For an installed-path simulation, use a fresh `uv` environment and run the
registration probe from outside the checkout with `PYTHONPATH` unset; see
`docs/API_AUDIT.md`. This is intentionally a no-call, no-audio check.

## Integration boundary

Hermes's public plugin API can register tools, slash commands, CLI commands,
and hooks, but it does not expose the internal `/voice` lifecycle owner. That
is not a blocker for this separate runtime: terminal activation and Phone
Bridge integration use the explicit `rex_voice_v1.launch` boundary rather than
trying to connect the runtime to Hermes's built-in `/voice` implementation.

Any future embedded-Hermes optimization must use a documented public voice or
session API. Until then, this plugin remains deliberately separate and fails
closed when its configured runtime, model, or audio dependencies are absent.
