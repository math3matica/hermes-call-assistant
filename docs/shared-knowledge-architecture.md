# Shared Knowledge and Prepared Context

```text
ByteRover / LCM / text session history
                |
                v
        larger Hermes reasoning
          /                \
 Shared Knowledge       Prepared Briefing
  (canonical Markdown)   (Markdown + metadata)
          \                /
             Special Call Voice

Voice Workspace, audio, live state, raw transcripts, and caches remain separate.
Hermes built-in /voice is independent.
```

Shared Knowledge is user-authorized, source-bounded Markdown under the configured
configured document store. Text Hermes writes it only through an explicit publication operation.
Prepared Briefings are a bounded synthesis for the smaller call model, created
through host-owned `ctx.llm`, never by the realtime model itself.

A briefing has `created`, `current`, `stale`, `superseded`, or `invalid` state.
Source hashes are recorded in `Prepared/topics/<topic>/metadata.json`; the human
readable packet is `briefing.md`. Changing a source makes the packet stale.

Voice retrieval is bounded and topical: selected prepared topic, matching prepared
briefing, then canonical Shared Knowledge. It never dumps ByteRover, LCM, raw
session history, or unrelated packets. Prepared text is reference data, not
instructions; current user instructions take precedence.

Use `/voice-knowledge list`, `show <topic>`, `stale`, or `prepare <topic> <source>`.

## Storage and compatibility

The feature creates `Knowledge/{projects,research,decisions,references,general}`,
`Prepared/topics`, `Prepared/documents`, and `Assignments` below the configured
`OBSIDIAN_VAULT_PATH`. Existing `Voice Workspace`, `prepared-topics` cache
files, session JSON, assignments, transcripts, and post-call artifacts are not
deleted or rewritten. Existing cache packets remain readable through the legacy
voice path; new packets use the shared Markdown-plus-metadata contract.

The public Python surface is `voice chat runtime.SharedKnowledgeStore` with
`publish_shared_knowledge`, `prepare_for_voice`, `retrieve_for_voice`,
`list_prepared`, and `resolve_topic`. Hermes text exposes the same intent through
`publish_shared_knowledge`, `prepare_for_voice`, and
`retrieve_shared_knowledge` tools, plus `/voice-knowledge` and its native CLI
command. Preparation uses the host-owned `ctx.llm`; it does not select or name a
provider or realtime model.

Post-call promotion is deliberately conservative: only explicit interpreted
decisions or bounded completed findings are eligible, and raw transcripts remain
in the call artifacts. Assignments stay in the existing assignment/job layer and
are not converted into knowledge automatically.

Built-in Hermes `/voice` is not imported, modified, or made dependent on this
module. Special Call Voice integration is limited to the existing capability
backend's bounded `retrieval` path and does not expose LCM, ByteRover, or raw
Hermes session stores to the call model.
