---
title: Shared Knowledge and Voice Briefings
status: active
---

Use `publish_shared_knowledge` when the user explicitly asks to save a conclusion,
decision, project understanding, or other durable fact to Shared Knowledge.
Use `prepare_for_voice` when the user asks to prepare an authorized document,
project, topic, or newly published conclusion for a Special Call Voice discussion.

Shared Knowledge is canonical Markdown under the configured configured document store. Prepared
Briefings are bounded Markdown plus metadata with source hashes and lifecycle
state. ByteRover, LCM, and raw session history may inform larger-model reasoning,
but are not direct small-model briefing stores.

The admin command is `/voice-knowledge list`, `show <topic>`, `stale`, or
`prepare <topic> <authorized-source>...`; the native CLI command has the same
operations. Never broaden source roots, include raw transcripts, or claim a
briefing is current when its metadata says stale or invalid.
