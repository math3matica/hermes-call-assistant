# Call path ownership

Keep these boundaries explicit. Similar names and compatible Python modules do not make repositories interchangeable.

| Capability | Canonical owner | Evidence boundary |
|---|---|---|
| Hermes plugin manifest and `register(ctx)` | `hermes-call-assistant/plugin` | fresh Hermes registration probe |
| Call Assistant session, Pi RPC, prompt loop, capabilities, post-call queue | `hermes-call-assistant/rex_voice_v1` | runtime/session artifacts and focused tests |
| Phone authorization, dial, answer, hangup, Telecom/call state | `hermes-phone-bridge` | bridge response plus independent phone state |
| Host capture/playback worker | `hermes-phone-bridge/host/call_audio_loop.py` | fresh lifecycle and audio-route logs |
| Hermes STT/TTS facilities | Hermes runtime/provider | `CAPTURED`, `TRANSCRIPT`, `TTS_READY`; not physical speech |
| Model preparation and restoration | configured model supervisor | verified supervisor status and health |
| User-visible assignments/notes/knowledge | configured profile data/vault roots | validated resource/artifact identity |
| Logs, transcripts, WAVs, post-call jobs | profile/cache evidence roots | timestamped raw artifacts; not source |

## Path classes

```text
Canonical source:
  /home/math3matica/hermes-call-assistant
  /home/math3matica/hermes-phone-bridge

Installed payload:
  ~/.hermes/plugins/hermes-call-assistant
  ~/.hermes/plugins/hermes-phone

Hermes runtime:
  ~/.hermes/hermes-agent

Profile/runtime state and evidence:
  ~/.hermes/cache
  ~/.hermes/phone-call-audio.log

Legacy or unrelated checkout:
  /home/math3matica/hermes
  /home/math3matica/hermes-phone-legacy
```

## Cross-repository contract

The phone bridge may construct and supervise a `RexVoiceSession`, but it does not own the Call Assistant implementation. It must pass the canonical `REX_VOICE_ROOT` explicitly and must not rely on its working directory or ambient `PYTHONPATH`.

The Call Assistant plugin is inert at registration time: it must not replace Hermes `/voice`, dial, hang up, or spawn the phone worker. The phone bridge owns those external side effects.

## Acceptance language

Use precise claims:

- plugin registered: integration evidence;
- call reached `OFFHOOK`: telephony evidence;
- worker captured/transcribed: receive/STT evidence;
- response and `TTS_READY`: model/TTS evidence;
- human heard speech: physical playback evidence.

Never collapse these into “the call assistant works.”
