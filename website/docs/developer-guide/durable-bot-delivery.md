# Durable local bot delivery

Work-Control-ID: 01a08721-a666-7414-9fe7-5ada1d1ebe34

User requirement: an occupied profile must not discard later requests after a
120-second wait. Preserve existing conversations, Work identities, authority
checks, and completion callbacks. Do not repeat ambiguous external effects.

Acceptance:

1. With `bot_mode.durable_delivery_queue: true`, a local message is persisted
   before returning `queued`. This does not mean received or completed.
2. Queue entries are FIFO per destination profile, wait outside the recipient
   conversation, and survive worker restart before dispatch.
3. Different destinations may progress independently. A target's human turn
   or another bot delivery is never interrupted to make room.
4. The existing delivery implementation validates authority at dispatch and
   verifies destination receipts. Expired authority is a visible failure,
   not permission to re-admit or replay the request.
5. An orphaned running delivery becomes `unknown`, never automatically retried.
   The original request and diagnostic result remain available for recovery.
6. Existing background completion callbacks receive the reply. Durable status
   receipts also appear in the source profile's updates inbox.
7. Disabled configuration and peer/relay delivery retain their current behavior.

The POSIX worker is started with `python -m tools.bot_delivery_queue serve`.
`status` lists content-free receipts. `wait ID` is the internal background
completion listener. Source profiles keep their own configuration; credentials
are not copied into queue records. The queue stores private payloads under the
Hermes root, outside the temporary DM cache. No historical failed messages are
automatically imported or replayed when enabling this feature.
