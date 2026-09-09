# Changelog

## Unreleased

- Established the standalone canonical source tree.
- Documented Hermes built-in Voice Mode as independent from the specialized call runtime.
- Added explicit `hermes call-voice` and `rex-call-voice` activation paths without changing `/voice`.
- Packaged the existing `rex_voice_v1` runtime, Pi extension, capability bridge, and post-call modules.
- Preserved opt-in model/audio/call behavior; no live audio or telephony state was changed.

## 0.1.0

- Initial separate call-oriented runtime and safe control-plane adapter.
