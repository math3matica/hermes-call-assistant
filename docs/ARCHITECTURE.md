# Voice architecture and dependency classification

## Dependency graph

```text
Hermes built-in `/voice` (independent, Hermes-owned)

Phone Bridge call lifecycle/audio worker
  -> explicit VoiceChatSession boundary
       -> rex_voice_v1 specialized conversation/runtime
            -> Pi model server + configured model supervisor
            -> Hermes public STT/TTS facilities when --voice is selected
            -> voice capability bridge and post-call/store/learning handoff

Hermes plugin adapter (public register(ctx))
  -> safe status tool + /voice-chat
  -> explicit `hermes voice-chat` terminal activation
  -> never registers or replaces `/voice`
```

## Classification

| Integration | Owner | Class | Boundary |
|---|---|---:|---|
| Plugin manifest and `register(ctx)` | Hermes | D | Public directory-plugin API |
| `ctx.register_tool`, `ctx.register_command` | Hermes | A | Public plugin API |
| Status/diagnostic reporting | Voice Chat | A | Plugin tools |
| voice chat runtime state, bridge, protocol, post-call store | Voice Chat | A/B | Plugin package and profile-local artifacts |
| Model supervisor process and model serving | External | B | Configured subprocess/service; no model artifact shipped |
| STT implementation | Hermes/provider or external service | B/D | Existing transcription dispatch or configured service |
| TTS implementation | Hermes/provider or external service | B/D | Existing TTS dispatch or configured service |
| `/voice on` and `/voice off` command ownership | Hermes | D | Existing built-in command; independent |
| `hermes voice-chat` activation | voice chat plugin | A/B | Explicit CLI subprocess boundary |
| Specialized model selection/supervisor | plugin + external service | B | Configured supervisor; optional `--manage-model` |
| Recorder/UI/audio device handling | Hermes or Phone worker | B/D | Reused Hermes facilities or bridge-owned call audio |
| Cellular call/audio bridge | Phone Bridge project | B | Phone bridge protocol; never implicit in CI |
| Learning handoff | Voice Chat | A/B | Plugin-owned durable proposal and host job boundary |
| Credentials and profile state | Hermes/user | D | Host config and `$HERMES_HOME`; never repository data |

## Independence and lifecycle

The built-in Hermes Voice Mode remains usable with this plugin absent, installed
but inactive, or active through `hermes voice-chat`. The special runtime does
does not mutate Hermes's `/voice` command or its generic voice implementation.

## Audit conclusion and ownership limits

`rex_voice_v1` is an actual separate conversation/runtime package, not only a
manifest wrapper: `VoiceChatSession` creates the Pi RPC process, capability
bridge, session ledger, prompt/turn loop, and bounded post-call handoff. Its
`--voice` path calls Hermes's existing `hermes_cli.voice.start_continuous`,
`speak_text`, and `stop_continuous`; it does not implement a second STT/TTS
provider or take ownership of Hermes's generic recorder lifecycle. Text mode
uses the same Voice Chat turn loop without audio.

The model supervisor is external and is invoked only with explicit
`--manage-model`; model restoration is also delegated to that configured
supervisor. Phone dialing, answering, hangup, authorization, call-state
polling, and call audio are **not implemented in this repository**. They belong
to the external Phone Bridge/worker integration that is expected to construct
one `VoiceChatSession` after an authorized active call. Consequently, this
project proves the voice chat runtime/control-plane boundary and adapter packaging,
not a physical phone call or end-to-end telephony/audio result. Post-call
recovery is package-owned at the queue/lock/artifact layer, while model
readiness and phone terminal state remain external prerequisites.

The adapter is inert at registration time: it registers only
`voice_chat_status`, `/voice-chat`, and the separate `voice-chat` CLI
surface. It does not register `/voice`, replace a voice handler, spawn a
worker, dial, or alter Hermes state. This is covered by a regression test and
must remain true when the plugin is absent or installed but not enabled.

For a telephone session, Phone Bridge owns authorization, dial/answer/hangup,
call state, and audio transport. Once the call is active, it creates the
specialized Voice Chat session. Voice Chat owns model conversation, allowlisted
document capabilities, session artifacts, and post-call enqueue. The worker
stops before bridge teardown; model restoration and durable handoff are
independently verified. A missing or failed voice chat runtime is reported as a
voice-chat failure, while ordinary Hermes Voice Mode remains unaffected.
