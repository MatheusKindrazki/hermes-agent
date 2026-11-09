# Kindra fork update to Hermes v0.23.0

Upstream release: `v0.23.0` (`2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`).
Fork base: `c0cf21ca3389c5eebb2ec203fb791b262de78492` (embedded Kindra panel).

The integration preserves the dark, character-based roster, canonical continuous
Bot Chats, immediate recipient selection, embedded Kindra panel, and explicit
agent steering in the parallel activity drawer. The upstream group-room poller
replaces the old manual refresh surface without adding a sidebar status block.

Runtime reconciliation retains durable admission, delivery receipts, idempotency,
and the optional durable delivery queue while adopting upstream message-chain
limits, notify-only messages, fan-out coordination, and detached-route recovery.
The existing extracted runtime helpers remain the owners of their execution phases.

Validation performed on macOS arm64:

- Desktop TypeScript checks and production build.
- 57 focused UI/Electron unit tests covering panel, executor, avatars, gateway
  switching, group rooms, late tool completions, and recipient switching.
- 317 focused Python tests covering admission, delivery, configuration isolation,
  background notifications, and oversized compression (77 platform/config skips).
- Electron end-to-end panel test with an isolated gateway and mocked executor;
  no live agent messages are sent.

Signing, installation, native acceptance, and repository-wide checks are recorded
separately in the PR and Work Control receipts. A successful build alone is not an
installation receipt.

Work-Control-ID: 01a1c10e-67d0-7ca2-a1bc-e3115f84c4f5
