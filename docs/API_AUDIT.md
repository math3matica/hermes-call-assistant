# Hermes public API audit

Date: 2026-09-08

## Observed contract

The installed Hermes source tree exposes
`PluginContext.register_tool`, `register_command`, `register_cli_command`,
`register_hook`, and `register_middleware`. Plugin manifests use
`plugin.yaml`, `register(ctx)`, and an explicit `kind`. `register_tool` expects
the complete schema body (`description` plus `parameters`), not only the inner
parameter object.

The `plugin/` adapter registers a safe status tool, a status slash command, and
an explicit `call-assistant` native CLI command. The actual specialized runtime is
packaged as `rex_voice_v1` and contains the Pi extension, Unix JSONL capability
bridge, session store, and post-call handoff. Registering a tool or hook still
cannot replace Hermes's built-in `/voice` owner through the public API, and the
adapter does not attempt to do so.

## Implementation decision

`hermes-call-assistant` is a clean standalone source tree. Its service owns a
private `0600` Unix socket and a bounded state machine. The Hermes adapter
registers `call_assistant_status`, `/call-assistant`, and explicit
`hermes call-assistant` activation. The activation launches only the separate Call Assistant
runtime; it does not override `/voice` or silently perform telephony/audio
actions. Unknown control-plane operations fail with stable
`operation_not_allowed` errors.

## Explicit limitation

No core shim is required for the separate terminal or Phone Bridge runtime:
those paths use the explicit packaged runtime boundary.
Hermes's built-in `/voice` remains independent. Any future attempt to embed
the special runtime inside Hermes's generic voice lifecycle must use a future
documented public API or a small upstream core change; this plugin must not
silently convert one system into the other.

## Runtime ownership audit

The claim is deliberately split:

* **Actual call assistant functionality in this package:** the session runtime owns the
  separate Pi RPC conversational loop, prompt/turn handling, capability bridge,
  session artifacts, bounded cancellation, and post-call queue/recovery
  handoff. `rex_voice_v1/index.ts` owns the specialized Pi extension and
  provider-payload/tool routing rules.
* **Reused, not owned:** with `--voice`, the launcher imports Hermes's public
  `hermes_cli.voice` functions for continuous capture/STT and TTS playback.
  The package has no independent STT/TTS implementation and does not modify
  the built-in voice loop.
* **External/private boundary:** model serving/supervisor switching is an
  operator-configured dependency. Phone authorization, dial/answer/hangup,
  call-state polling, and call audio worker behavior are outside this checkout
  in Phone Bridge (or another integration shim). No physical-call claim is
  supported by this repository's tests.

Thus `rex_voice_v1` is more than adapter/packaging, but it is not a complete
telephone voice product in isolation. Its installed Hermes adapter is only a
public registration/activation adapter around that runtime.

## No-impact invariant

Registration is intentionally side-effect-free. The adapter exposes exactly
`call_assistant_status`, `/call-assistant`, and `call-assistant`; it never exposes
`/voice` and never starts a process at registration. If the plugin is absent,
Hermes loads no call assistant registrations. If it is installed but inactive, no
registration or runtime code runs. The regression suite asserts this inert
registration contract, and the clean-install simulation below repeats it from
an installed package with `PYTHONPATH` unset.

## Clean-install simulation

This source export has no `.git` directory, so the reproducible local check is
a clean-tree copy rather than a literal `git clone`. Copy tracked source
content to a fresh directory, install it into a fresh `uv` environment, copy
`plugin/` as a project plugin, and run the registration probe from a neutral
working directory with `PYTHONPATH` unset. The probe must resolve
`rex_voice_mode` from the installed environment, record only the three Call Assistant
surfaces above, and show zero subprocess calls. This exercises packaging and
inactive-plugin safety; it intentionally does not start Pi, model services,
Phone Bridge, STT/TTS, or audio.
