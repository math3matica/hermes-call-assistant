# Hermes Call Assistant operations

This is the recovery runbook for agents and operators. It is deliberately conservative: a connected call is not the same thing as a working conversational call.

## Source and deployment map

1. Edit `/home/math3matica/hermes-call-assistant` for Call Assistant runtime/plugin changes.
2. Edit `/home/math3matica/hermes-phone-bridge` for dialing, call state, audio-worker, and relay changes.
3. Verify `~/.hermes/plugins/hermes-call-assistant` resolves to the canonical `hermes-call-assistant/plugin` payload.
4. Restart or replace long-running Hermes/relay processes after source changes. Do not infer activation from a successful import in the checkout.

## Non-mutating investigation sequence

Run these before any authorized physical call:

```bash
cd /home/math3matica/hermes-call-assistant
git status --short --branch
readlink -f ~/.hermes/plugins/hermes-call-assistant

cd /home/math3matica/hermes-phone-bridge
git status --short --branch
pgrep -af 'hermes|hermes_phone_relay|call_audio_loop'
```

Then inspect fresh supervisor status, relay logs, and the newest call artifact. Keep the evidence timestamp and process identity with the diagnosis.

## Lifecycle fault isolation

The expected order is:

```text
CALL_AUDIO_ACTIVE
  -> CAPTURED
  -> TRANSCRIPT
  -> RESPONSE
  -> TTS_READY
  -> physical playback
```

Interpret the first missing marker:

| Last observed marker | Investigate |
|---|---|
| no `CALL_AUDIO_ACTIVE` | phone call state/worker startup |
| active but no `CAPTURED` | capture device, HFP/analog route, worker crash |
| captured but no `TRANSCRIPT` | STT import/model/VAD/input quality |
| transcript but no `RESPONSE` | Call Assistant session, model endpoint, model handoff |
| response but no `TTS_READY` | TTS provider or synthesis invocation |
| TTS ready but no audible speech | playback target, mixer, splitter, HFP sink, physical route |

Important distinctions:

- `CAPTURED` with `TRANSCRIBING_EMPTY` is an STT/input failure. It is not evidence of a TTS failure.
- A generated WAV proves synthesis output existed. It does not prove playback reached the phone.
- Bluetooth pairing or an `audio-gateway` card does not prove HFP media endpoints. Require both `bluez_input...` and `bluez_output...` nodes for Bluetooth call audio.
- A call intent or `OFFHOOK` state proves telephony progress, not agent speech.

## Import/runtime checks

The worker must be launched with explicit environment roots:

```text
HERMES_ROOT=/home/math3matica/.hermes/hermes-agent
REX_VOICE_ROOT=/home/math3matica/hermes-call-assistant
HERMES_HOME=~/.hermes
HERMES_CALL_WORKER_SESSION_ID=<call-session-id>
```

If logs mention `No module named 'tools'` or an old checkout path, classify that as a runtime-environment/source-boundary failure before changing model or audio code. If logs mention `rex_voice_v1.shared_knowledge` missing, inspect the resolved runtime origin and installed payload for a stale/incomplete copy.

## Verification gates

- Unit tests are necessary but do not prove a physical call.
- A fresh-process plugin probe must verify discovery, registration, and inertness.
- A fresh worker probe must verify environment propagation and import origin without dialing.
- Physical acceptance requires fresh call-state, audio-route, lifecycle-marker, and human-observed speech evidence.
- Preserve failed probe roots and logs when they contain meaningful evidence.
