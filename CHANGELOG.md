# Changelog

## Unreleased

- Established the standalone canonical source tree.
- Documented Hermes built-in Voice Mode as independent from the specialized call runtime.
- Added explicit `hermes voice-chat` and `hermes-voice-chat` activation paths without changing `/voice`.
- Packaged the existing `voice chat runtime` runtime, Pi extension, capability bridge, and post-call modules.
- Preserved opt-in model/audio/call behavior; no live audio or telephony state was changed.

## 0.1.0

- Initial separate call-oriented runtime and safe control-plane adapter.
