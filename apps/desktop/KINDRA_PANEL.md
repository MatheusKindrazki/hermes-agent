# Kindra panel integration

The fork exposes one quiet Painel button beside Em paralelo in a character chat. The existing Mini panel opens at Plataforma in an in-app dialog; the character sidebar and canonical chat route do not change. Conversation and studio routes, as well as explicit open-conversation messages, open the existing executor conversation view and composer in an Em paralelo drawer. Closing the drawer preserves the panel instance underneath.

The panel is loaded from the existing HTTPS tailnet service on port 8525. It remains the source of task data, timestamps and freshness; Desktop does not create a second scheduler or claim that a loaded page means all its source data is fresh. Closing the whole panel unloads the guest; conversation drafts are retained by the existing window-local executor draft store.

## Boundary

The iframe allows scripts, same-origin access to its own service and forms, but no popups or top navigation. It gets no desktop preload capability. The host accepts messages only from that iframe window and the exact panel origin. Conversation paths must carry a UUID; query parameters never enter the executor reference. The existing Electron adapter owns allowlisted reads and explicit sends, with its app-renderer-only IPC guard. Opening a conversation never sends a message.

A 15-second handshake timeout shows a retry. Retrying replaces the frame, rejects messages from the old window and returns to its last reported route. A conversation drawer keeps using the existing polling, stale/error feedback and explicit recipient composer.

## Validation

- kindra-panel.test.tsx: origin/window validation, path filtering, conversation ownership, preserved panel, bounded timeout/retry, stale-frame rejection, no implicit sends.
- kindra-panel.spec.ts: actual Electron renderer and isolated gateway, mocked panel/executor content, guest without desktop API, inline conversation, return to panel and unchanged principal chat route. No live agent messages.
- Existing executor view/composer and persona regressions remain in the focused suite.

PR24 baseline CI debt was reproduced on main 686c615 before merging: overlay translations, generated i18n keys, pending-submission test isolation and a legacy native-title assertion. The broad CI also reports inherited lint and Linux package-name assumptions; these are not evidence that the full baseline suite is green.
