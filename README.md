# hermes-call-assistant

Standalone package and Hermes plugin for the separate, call-oriented Call Assistant runtime.

## Scope

This package owns the specialized call assistant runtime, its Pi capability bridge, the
private JSON-lines Unix-domain control plane, and the Hermes plugin adapter. It
supports status plus bounded session lifecycle (`start_session`,
`complete_session`, `abort_session`). The endpoint is created with mode `0600`,
rejects unknown operations, and never accepts shell commands, paths, source
code, phone numbers, or provider payloads.

## Relationship to Hermes Voice Mode

Hermes has a normal built-in Voice Mode, including its `/voice` command and
interactive recorder/UI lifecycle. That upstream feature is not replaced,
overridden, or renamed by this project.

Call Assistant is a second, specialized voice runtime for telephone/call operation.
It owns the call-oriented conversational loop, specialized prompt and
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
python -m pip install -e '.[test]'
hermes-call-assistant --socket /tmp/hermes-call-assistant.sock
# Existing direct runtime entrypoint:
hermes-call-assistant-runtime --help
# Hermes plugin CLI entrypoint after plugin installation:
hermes call-assistant --help
```

Set `HERMES_CALL_ASSISTANT_SOCKET` for safe status/session control, then install the
directory `plugin/` using Hermes's normal project/user plugin mechanism. The
adapter uses public registration APIs only. `hermes call-assistant` is a deliberate
separate command and invokes the packaged voice runtime entrypoint; it
does not claim or register `/voice`.

The existing direct source invocation remains available from the source tree:

```sh
python -m rex_voice_v1.launch [--voice] [--manage-model]
```

## Requirements and configuration

- Linux or another POSIX environment with Python 3.10+.
- Hermes Agent / Nous Research Hermes for plugin loading and, for `--voice`,
  the host's existing `hermes_cli.voice` STT/TTS implementation.
- Node.js and the `pi` executable for the realtime runtime.
- A configured OpenAI-compatible or local model endpoint; model serving and
  the model supervisor are external and no weights or credentials are shipped.
- A writable profile-local artifact directory. `OBSIDIAN_VAULT_PATH` is
  required for a live session and must point to an authorized Vault; it is not
  bundled or created by this project.

The main settings are environment variables. Defaults are shown here; empty
or missing required values fail closed rather than starting a partial session.

| Variable | Required | Purpose / default |
|---|---:|---|
| `OBSIDIAN_VAULT_PATH` | live session | Authorized Vault root; no default; startup fails if absent |
| `HERMES_CALL_ASSISTANT_SOCKET` | plugin status | Private control socket; required when using the plugin adapter |
| `HERMES_CALL_ASSISTANT_ARTIFACTS` | optional | Session/post-call state; defaults to `~/.hermes/cache/hermes-call-assistant` |
| `HERMES_CALL_ASSISTANT_PI` | optional | `pi` executable; defaults to `~/.local/bin/pi` |
| `HERMES_CALL_ASSISTANT_SUPERVISOR` | model-managed run | External model supervisor; missing value blocks model management |
| `HERMES_CALL_ASSISTANT_PREPARED_ROOTS` | optional | Colon-separated authorized read/search roots; default is none |
| `HERMES_CALL_ASSISTANT_DOCUMENT_PLUGIN` | optional | Hermes document-store plugin path; defaults to `~/.hermes/plugins/document-store/__init__.py`, with legacy Rex Vault fallback |
| `HERMES_CALL_ASSISTANT_PROVIDER`, `HERMES_CALL_ASSISTANT_MODEL` | optional | Provider/model selection; defaults are implementation-local and should be set explicitly |
| `HERMES_CALL_ASSISTANT_CONTEXT_WINDOW`, `HERMES_CALL_ASSISTANT_MAX_TOKENS` | optional | Context/output bounds; defaults are `131072` and `4096` |
| `HERMES_CALL_ASSISTANT_POST_CALL_AUTOSTART` | optional | Post-call worker toggle; default is enabled, set `false` to disable; legacy `REX_POST_CALL_AUTOSTART` is also accepted |

Use synthetic values, for example:

```sh
export OBSIDIAN_VAULT_PATH="$HOME/example-hermes-vault"
export HERMES_CALL_ASSISTANT_SOCKET="/tmp/example-hermes-call-assistant.sock"
export HERMES_CALL_ASSISTANT_ARTIFACTS="$HOME/.hermes/cache/example-hermes-call-assistant"
```

## Architecture

```text
Hermes Agent / host LLM
        | bounded preparation and promotion
        v
Shared Knowledge (durable, authorized Markdown)
        | topic-bounded briefing
        v
Prepared Briefing (bounded, source-hashed)
        | retrieval during a call
        v
Voice Workspace (ephemeral live state) -> Call Assistant runtime
        ^                                      |
        +------ post-call artifacts/promotion-+

ByteRover / LCM / Hermes session history remain broader host memory;
they are not blindly injected into the small realtime model.
```

Host-owned LLM preparation creates bounded briefings. The realtime runtime
uses those briefings and authorized Shared Knowledge, while the Voice
Workspace holds ephemeral drafts, assignments, and call state. Post-call
promotion is explicit and source-bounded; it is not automatic transcript
dumping.

For Phone Bridge calls, the bridge's call audio worker creates one
`rex_voice_v1.launch.RexVoiceSession` per call and owns capture/playback,
authorization, hangup, and terminal cleanup. Call Assistant owns the specialized
conversation, capability bridge, session artifacts, and post-call handoff.
The model supervisor remains an external configured dependency; no model or
credentials are shipped.

### What is and is not implemented here

`rex_voice_v1` is a real standalone voice conversational runtime: it owns the
Pi RPC turn loop, voice capability bridge, session artifacts, and post-call
queue/recovery handoff. It is not merely packaging. However, `--voice` reuses
Hermes's existing `hermes_cli.voice` capture/STT/TTS functions; this project
does not provide a parallel audio or speech stack. Phone dialing,
authorization, call-state polling, hangup, and call audio remain external
Phone Bridge/worker responsibilities. The configured model supervisor is also
external. Therefore a successful install or dry runtime probe is not evidence
of a physical call or end-to-end audio conversation.

The adapter is inert until explicitly invoked: it registers only the safe voice
status tool, `/call-assistant`, and `hermes call-assistant`. It never registers or
mutates Hermes `/voice`, starts a process during registration, or performs
telephony implicitly. This invariant is tested for both installed/inactive
behavior and the no-plugin case (when the adapter is simply not loaded).

## Shared Knowledge and Prepared Briefings

This repository bundles the text-facing Shared Knowledge component with the
separate Call Assistant runtime. It does not add a fourth project.

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
- `/call-knowledge` and the native `hermes call-knowledge` command

The reusable workflow is documented in `skills/prepare-for-voice/SKILL.md`.
Post-call promotion is conservative: only explicit decisions and bounded
findings can be promoted, and promotion remains disabled when no authorized
Vault is configured.

## Verification

```sh
python -m pytest -q
python -m compileall -q src plugin
```

These are automated package/plugin checks only. They do not verify a provider,
STT/TTS, microphone routing, Bluetooth handset audio, a Phone Bridge call, or
post-call conversational use. See `MANUAL_ACCEPTANCE.md` for those gates.

## Status and limitations

Implemented and automated-test verified: standalone runtime boundary, private
control protocol, plugin registration/inertness, bounded knowledge retrieval,
session artifacts, and post-call state handling. Manual or physical validation
is still required for realtime model startup, speech recognition/synthesis,
microphone and Bluetooth routing, handset conversation, Phone Bridge
integration, and Shared Knowledge use in a real call. This is an experimental
third-party ecosystem project, not an official Nous Research product or
endorsement.

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
