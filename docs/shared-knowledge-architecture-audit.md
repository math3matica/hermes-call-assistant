# Shared Knowledge Architecture Audit

Date: 2026-09-08
Scope: Hermes text interaction and the separate `voice chat runtime` Special Call Voice runtime.

## Executive finding

The installation already has a suitable canonical Markdown Vault and a bounded Special Call Voice capability layer, but the two surfaces do not yet share a first-class publication and preparation contract. Voice can access the configured Vault through `VaultAdapter` and separately managed authorized roots, but it does not directly consume Hermes memory-provider results, LCM history, or arbitrary text-session understanding. Voice also has its own JSON session/handoff artifacts and prepared-topic store under its cache root.

The safe direction is to keep the Vault authoritative, add an explicit shared-knowledge/prepared-briefing layer inside the Vault, and leave raw session history, LCM, ByteRover, call artifacts, and Voice Workspace state out of direct small-model injection.

## Store inventory and classification

| Store | Current location/interface | Class | Voice direct use | Notes |
|---|---|---|---|---|
| Hermes text session history | Hermes `SessionDB` / session storage under Hermes home | D/F | No | Authoritative conversation history for text sessions; not a voice briefing source. |
| LCM | Configured as `context.engine: lcm`; plugin-local LCM database | F | No | Context compression/retrieval system. Summaries are recall cues, not canonical user knowledge. |
| Configured memory provider | `memory.provider: byterover`; Hermes memory provider lifecycle | F | No | Broader system memory. Must not be dumped into the small call model. |
| ByteRover | `.brv/` project context tree plus ByteRover service/provider | F/G | No | Durable broader project memory and retrieval; useful to larger-model preparation, not direct voice context. |
| configured document store / Obsidian-style notes | Configured `OBSIDIAN_VAULT_PATH`, known installation at `~/Documents/configured document store` | H/A | Yes, bounded | Markdown is the canonical user-visible document layer. configured document store plugin enforces in-vault paths, backups, active-document state, and verified resource identity. |
| Voice Workspace | `<Vault>/Voice Workspace/{drafts,working-notes,completed-notes,inbox}` | E | Yes, separately | Voice-specific working area. Current search intentionally excludes it from generic note search. It must not become canonical knowledge. |
| `voice chat runtime.note_search` | Searches authorized Markdown roots, bounded to 4 results/10,000 chars | A/H retrieval | Yes | Token/paragraph search; excludes `.rex-vault-backups` and `Voice Workspace`. Does not search raw transcripts. |
| Prepared topic packets | `~/.hermes/cache/rex-voice-v1/prepared-topics/*.json` via `VoiceSessionStore` | B/G | Yes | Existing `rex-prepared-topic-v1`; topic aliases/content/source refs/version. Current store is voice-cache-owned and JSON-only, lacks source freshness and human-readable packet files. |
| Post-call prepared context | Per-session JSON in VoiceSessionStore and post-call job artifacts | B/C/E | Yes, next-session handoff | Existing `rex-prepared-context-v1`; includes findings, decisions, questions, assignments, handles, and follow-ups. It is a bounded handoff artifact, not shared canonical knowledge. |
| Assignments/jobs | Voice cache `assignments/`, post-call `jobs/`, Hermes canonical session when available | C | Yes for continuity | Existing explicit assignment capture and post-call queue. Must remain separate from knowledge and briefings. |
| Quick notes | `~/.hermes/cache/rex-voice-v1/quick-notes.json` | B/E | Yes | Bounded hot memory with provenance/confidence validation. Not a substitute for canonical Markdown knowledge. |
| Voice session state | `~/.hermes/cache/rex-voice-v1/sessions/<id>.json` | E | Yes | Active resource, drafts, retrieval handles, transcript reference, prepared topic state, errors. Ephemeral/session continuity; not canonical. |
| Voice runtime artifacts | `~/.hermes/cache/rex-voice-v1/<session-id>/pi-events.jsonl`, `pi.log`, socket, Pi state | G/E | Runtime only | Diagnostics and live runtime state. Must remain private and out of shared knowledge unless deliberately summarized. |
| Raw voice transcript | Session record/event evidence where present | E/G | No direct injection | Preserve separately; post-call analysis may promote explicit findings only. |
| Generated work artifacts | Post-call queue work trees and evidence roots | G/C | No direct injection | Completion must be evidence-backed; link artifacts from assignments or knowledge when appropriate. |
| Project-local notes | Existing repository `docs/`, project Markdown, fixtures/evidence | H/G | Only if explicitly authorized | Not automatically a shared user knowledge root. Never widen voice access to the repository indiscriminately. |
| Built-in Hermes `/voice` state | Hermes gateway/session voice mode | D/E (built-in) | N/A | Must remain unchanged and must not depend on this feature. |

## Current source authorization boundaries

- `OBSIDIAN_VAULT_PATH` is required to start Special Call Voice.
- `VaultAdapter` delegates durable document mutations to the existing configured document store provider.
- `RexVoiceWorkspace` authorizes the Vault-local `Voice Workspace` plus explicitly granted read/search roots from `HERMES_VOICE_CHAT_PREPARED_ROOTS`.
- Additional roots are read/search-only; writes remain workspace-scoped.
- `note_search` rejects paths outside the supplied root and excludes backup files and Voice Workspace from generic search.
- The voice process does not recursively search the filesystem, Hermes home, ByteRover, LCM, or raw session databases.

## Existing gaps

1. Text Hermes has no explicit, user-facing `publish to Shared Knowledge` operation.
2. Prepared topics are currently voice-cache artifacts rather than a shared, human-readable, source-versioned prepared layer.
3. Existing prepared topics have a stable topic ID and aliases but no source hash manifest, stale/superseded/invalid lifecycle, preparation provenance, or Markdown packet.
4. Post-call prepared context is bounded and useful, but is not formally linked to canonical shared knowledge, prepared packets, or assignment records.
5. The special call runtime can retrieve Vault notes, but it has no deterministic shared retrieval order of prepared packet → canonical knowledge → authorized source.
6. There is no text-session handoff workflow that synthesizes current conclusions into shared knowledge and optionally prepares them for voice.
7. No CLI/admin surface exists for listing, inspecting, preparing, refreshing, or marking prepared briefings.

## Target boundary

```text
ByteRover / LCM / text session history / other system memory
                         |
                         v
                 larger Hermes reasoning
                         |
          +--------------+---------------+
          v                              v
   Shared Knowledge                 Prepared Briefings
   (Markdown, canonical)            (Markdown + metadata)
          |                              |
          +---------------+--------------+
                          v
                 Special Call Voice
              (bounded topical retrieval)

Voice Workspace, live state, audio, raw transcripts, and caches remain separate
unless a controlled promotion step creates a source-grounded shared artifact.
```

## Safety conclusion

Do not replace or modify built-in `/voice`. Do not expose ByteRover, LCM, raw text history, or all Vault notes directly to the small voice model. Implement the feature as a shared Vault substructure plus explicit host-side publication/preparation interfaces and bounded voice retrieval.
