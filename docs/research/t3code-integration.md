# T3 Code integration feasibility for Deckbridge

Research date: 2026-08-13. T3 Code source reviewed at commit
[`9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4`](https://github.com/pingdotgg/t3code/tree/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4).
Only first-party T3 Code and Herdr sources are used below.

## Verdict

**Yes—T3 Code has a substantially better machine-readable integration surface than terminal
scraping.** Deckbridge should support it as a new connector backed by T3 Code's authenticated
Effect RPC WebSocket. The shell subscription exposes stable thread IDs, titles, provider identity,
turn/session lifecycle, explicit approval and user-input flags, and incremental upsert/removal
events. That is enough to implement reliable `working`, `needs you`, `done`, deletion, sorting, and
new-session actions.

There are three important qualifications:

1. T3 Code is an agent harness, not a terminal multiplexer. Its RPC enumerates **threads owned by
   the T3 Code server**, not arbitrary Claude/Codex processes started elsewhere. Existing provider
   sessions attached to a T3 thread can resume after restart, but the reviewed public contracts do
   not provide a global importer/discovery API for unrelated CLI sessions.
2. Exact browser-thread focus is straightforward through T3's canonical thread route. The stock
   Electron app reliably supports app/window reveal, but its custom `t3code://` scheme is an
   internal renderer origin/OAuth callback surface; the reviewed desktop code does not route an
   external URL or second-instance arguments to a requested thread. Exact native-app thread focus
   therefore needs accessibility automation, an upstream/fork patch, or use of the web surface.
3. T3 Code has no Hermes provider and no first-party dictation UI. It complements Herdr; it does
   not replace the existing Hermes/Herdr path.

## Capability matrix

| Deckbridge need | T3 Code evidence | Assessment |
| --- | --- | --- |
| macOS install | Official desktop app via `brew install --cask t3-code`; web/server mode via `npx t3@latest`. Windows and Arch packages are also documented. [Install guide](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/docs/user/install.md) | Good. Prefer the macOS desktop app for the user and connect Deckbridge to its local server. |
| Providers | Five built-in drivers: Codex, Claude, Cursor, Grok, and OpenCode. The CLIs are installed/authenticated separately. [Provider architecture](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/docs/internals/providers.md), [install guide](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/docs/user/install.md) | Excellent for Claude/Codex/Cursor; no Hermes driver. |
| Session discovery | `orchestration.subscribeShell` returns a snapshot then sequenced `thread-upserted` / `thread-removed` events, with catch-up via `afterSequence`. [Contract](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/packages/contracts/src/orchestration.ts#L434-L546) | Excellent for T3-owned threads; event-driven and reconnectable. |
| Stable identity/title | Each shell has a `ThreadId`, `projectId`, `title`, model/provider selection, timestamps, branch/worktree metadata, and session. IDs are branded non-empty strings and clients normally generate UUIDs. Titles can be updated or regenerated. [Shell schema](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/packages/contracts/src/orchestration.ts#L434-L484), [ID schemas](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/packages/contracts/src/baseSchemas.ts#L45-L72), [metadata command](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/packages/contracts/src/orchestration.ts#L751-L767) | Excellent. Use T3 `ThreadId` as Deckbridge's external session key and T3's title directly. |
| `working` | Session status is `idle`, `starting`, `running`, `ready`, `interrupted`, `stopped`, or `error`; latest turns separately expose `running`, `interrupted`, `completed`, or `error`; background work can be `working` or `monitoring`. [Session and turn schemas](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/packages/contracts/src/orchestration.ts#L275-L299), [thread shell schema](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/packages/contracts/src/orchestration.ts#L434-L484) | Excellent. No terminal text inference required. |
| `needs you` | Shell fields explicitly report `hasPendingApprovals`, `hasPendingUserInput`, and `hasActionableProposedPlan`. Provider interaction boundaries flush immediately into the normalized stream. [Shell schema](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/packages/contracts/src/orchestration.ts#L434-L484), [provider flow](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/docs/internals/providers.md#how-provider-work-is-requested) | Excellent. Map approvals/user input to `needs you`; decide separately whether an actionable plan counts. |
| `done until viewed` | Turn completion and timestamps are server state, but “last visited” is UI-local Zustand state persisted under `t3code:ui-state:v1`; opening a finished thread stamps its completion time as visited. It is not in the server shell/RPC. [UI state](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/apps/web/src/uiStateStore.ts#L5-L40), [visit update](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/apps/web/src/uiStateStore.ts#L223-L246), [ChatView behavior](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/apps/web/src/components/ChatView.tsx#L1671-L1689) | Partial. Deckbridge should keep its own seen watermark and mark a key viewed when Deckbridge focuses it. Detecting manual T3 navigation needs browser/UI observation or a tiny upstream bridge. |
| Create/start sessions | Clients dispatch `thread.create` and `thread.turn.start`; a turn start can atomically bootstrap thread creation and worktree preparation. Commands use caller-supplied IDs and timestamps. [Commands](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/packages/contracts/src/orchestration.ts#L653-L667), [turn bootstrap](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/packages/contracts/src/orchestration.ts#L785-L849) | Excellent. A “new T3 session” key can create the thread and optionally send the first prompt. |
| RPC/API | Clients and server use authenticated Effect RPC over `/ws`; `orchestration.subscribeShell` and `subscribeThread` are server streams, while commands use `orchestration.dispatchCommand`. [Architecture](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/docs/internals/overview.md#the-rpc-boundary) | Strong but not a trivial REST shim. Implement a small TypeScript sidecar using the same Effect contracts, or vendor the minimum wire client. Do not scrape SQLite. |
| Authentication | A local administrative CLI can issue bearer sessions; non-browser clients exchange/authenticate and obtain a five-minute WebSocket ticket. Read and operate scopes are separate. [Authentication profile](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/docs/internals/environment-auth.md), [auth CLI](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/apps/server/src/cli/auth.ts) | Good. Installer should issue a dedicated least-privilege Deckbridge credential and store it outside the public repo. |
| Persistence/log fallback | T3 persists event-sourced state/projections in `userdata/state.sqlite`, but the supported live boundary is RPC and the project explicitly treats non-exported files as implementation details. [Architecture](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/docs/internals/overview.md#orchestration-is-event-sourced), [workspace conventions](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/docs/internals/workspace-layout.md#import-conventions) | SQLite can aid diagnostics only. Direct reads are brittle and lose live semantics; direct writes are unsafe. |
| Exact thread focus | Web uses browser history and the canonical route `/$environmentId/$threadId`. Electron uses hash history atop `t3code://app/`. [Router setup](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/apps/web/src/main.tsx#L21-L24), [thread route](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/apps/web/src/routes/_chat.%24environmentId.%24threadId.tsx#L80-L97), [desktop origin](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/apps/desktop/src/electron/ElectronProtocol.ts#L10-L25) | Browser: exact and robust. Desktop: app focus is robust, thread focus is not publicly exposed. |
| Desktop window focus | Electron restores/shows/focuses its main window, and the second-instance handler reveals the existing window. It does not consume the second instance's thread argument in the reviewed handler. [Window reveal](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/apps/desktop/src/window/DesktopWindow.ts#L713-L740), [second instance](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/apps/desktop/src/app/DesktopClerk.ts#L128-L147) | App-level focus is ready; exact thread focus needs an added bridge. |
| Dictation/voice | No first-party microphone/dictation implementation was found in the app UI. Mobile configuration explicitly disables microphone permission; desktop preview content does not allow media permission. Generated Codex schemas contain experimental audio types, but T3's public `thread.turn.start` contract takes text/images, not recorded audio. [Mobile config](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/apps/mobile/app.config.ts#L292-L301), [turn contract](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/packages/contracts/src/orchestration.ts#L811-L849) | Use Deckbridge's existing macOS dictation path after focusing T3's composer; no T3-native voice shim exists today. |
| License/maturity | MIT licensed. The README says the project is very early and to expect bugs. [License](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/LICENSE), [README](https://github.com/pingdotgg/t3code/blob/9e201941aaa9cfece3e0ffaa4cc24bbe880d1be4/README.md) | Permissive enough to reuse/vend contracts, but pin and test protocol compatibility because the product is moving quickly. |

## Recommended Deckbridge design

1. Add a `connector_t3code` plus a small TypeScript RPC bridge. Use a dedicated bearer credential,
   exchange for WebSocket tickets, subscribe to the shell, persist the last sequence, and reconnect
   with `afterSequence`.
2. Normalize state in this order:
   - `needs_you` when `hasPendingApprovals || hasPendingUserInput`;
   - `working` for session `starting|running` or `backgroundLiveness=working`;
   - `error` for session/latest-turn error;
   - `done` for a newly completed turn until Deckbridge's per-thread seen watermark catches up;
   - otherwise `idle`.
3. Use T3's title and provider instance directly, not cwd-derived naming. Persist `ThreadId` and
   environment ID as the focus identity.
4. For the fastest reliable first version, make the Stream Deck open/focus the local **web** T3
   route for the exact thread. A later macOS enhancement can target the Electron accessibility tree
   or add a small upstream `t3code://.../#/$environmentId/$threadId`/IPC handler.
5. Keep Herdr support. Official Herdr documentation describes a persistent terminal runtime with a
   CLI and local socket API, direct pane control, and automatic agent classification; it supports
   Hermes among many terminal agents. T3 instead owns normalized harness threads and provider
   events. The two cover different surfaces. [Herdr official README](https://github.com/herdrdev/herdr),
   [Herdr API documentation](https://herdr.dev/docs/api/),
   [Herdr supported-agent documentation](https://herdr.dev/docs/agents/).

## Bottom line

T3 Code is a **go** for Deckbridge, especially for correct Claude/Codex/Cursor state. The work is a
real connector—not just another shell shim—but the server already supplies the hard parts that have
been fragile elsewhere: durable identities, authoritative normalized states, sequenced events, and
commands. Start with exact browser-route focusing and preserve Herdr for Hermes and terminal-native
sessions.
