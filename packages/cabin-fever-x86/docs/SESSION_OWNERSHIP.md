# Session Ownership Specification

## Status and scope

This document specifies session ownership and takeover behavior implemented by the game server and
clients, including web, Telegram, Zello, and the text client. The key words **MUST**, **MUST NOT**,
**SHOULD**, **SHOULD NOT**, and **MAY** are normative.

The initial scope is one game server process serving a session store. Keeping a game alive after
disconnection and allowing multiple clients to participate in one running game are future designs.
Running multiple server processes against the same store requires coordination beyond the in-memory
registry described here and is outside this proposal's supported configuration.

## Problem

Before session ownership was enforced, each server connection owned a separate `Game`. The check for
an already running game was local to that connection. Two connections could resume the same user's
session and operate on the same conversation journal and saved games independently.

This can happen when Telegram and web open the same session, when two browser tabs resume it, or when
a browser reconnects before the gateway notices that its previous connection has failed. Calling
`WebSocket.close()` in the browser does not establish that server-side cleanup has completed.
Replacing the web gateway's `live` entry also does not stop the previous handler or its upstream game.

Overlapping games can produce duplicate cabin events, conflicting conversation histories, concurrent
save writes, and unnecessary provider work. Ownership MUST therefore be enforced by the game server,
where all clients meet.

## Ownership invariant

The ownership key MUST be `(user_id, session_id)`. At most one `Game` for that key MAY be starting,
running, or shutting down at a time. A replacement MUST NOT begin loading persistent state until the
previous game has completed cleanup. Different sessions MAY run simultaneously, including sessions
belonging to the same user.

The server MUST maintain a shared registry with the owning connection, game lifecycle, ownership
token, and cleanup completion for each key. A per-session lock or equivalent mechanism MUST serialize
creation, recovery, takeover, and release. Starting a new game MUST register its newly allocated
session ID under the same rules.

Cleanup MUST remove or change an entry only if it still belongs to that connection. A late finalizer
from a displaced connection MUST NOT remove a replacement's ownership. Cleanup completion MUST be
signalled independently of acquiring the lock held by a takeover waiting for it.

## Explicit takeover

An authenticated user's explicit resume request MAY take ownership from another client. Authorization
and session existence MUST be checked before disturbing the current owner. For an accepted takeover,
the server MUST perform these steps in order:

1. Reserve the ownership transition for this session and invalidate the previous ownership token.
2. Stop accepting commands from the previous connection and signal that its session was taken over.
3. Cancel the old game worker, cabin events, and queued work; complete game cleanup and outstanding
   state writes.
4. Create the replacement game with the same user and session IDs, restoring the conversation journal
   and latest autosave.
5. Establish the replacement's ownership and return the session result and new ownership token.

The old browser or gateway MUST NOT need to acknowledge displacement for server-side cleanup to
proceed. Closing the transport alone MUST NOT count as completion of game cleanup. In particular,
cancelling a coroutine awaiting work in a background thread does not necessarily stop that work;
state-changing operations MUST finish or be prevented from writing before the replacement starts.

If cleanup fails or cannot be confirmed, the server MUST keep the session unavailable for a new game
and return an actionable error. A takeover waits up to 30 seconds for cleanup; a `cleanup_pending`
response leaves cleanup running and ownership reserved. `cleanup_failed` requires a server restart.
If replacement startup fails, its partial resources MUST be cleaned
up before another attempt. Request cancellation MUST NOT release ownership while cleanup is pending.

Takeover preserves persisted state. It does not continue an in-flight model response, replay queued
input, or preserve temporary state such as AFK timers. This retains the existing restore-from-disk
session model.

## Automatic recovery and ownership tokens

The protocol MUST distinguish an explicit takeover from automatic connection recovery. A sleeping
browser may miss the displacement notice and later retry; that retry MUST NOT evict a client that
has since taken ownership.

Each ownership grant MUST have an opaque token scoped to its user and session. Automatic recovery
MUST present the current token. Explicit takeover MUST replace it. Tokens MUST NOT replace identity
checks or grant access to another user's session, and SHOULD NOT appear in URLs or diagnostic logs.

An accepted recovery MAY replace a stale connection belonging to that owner, but MUST follow the
same cleanup ordering as takeover. Recovery MUST retain the ownership token so that losing the
session acknowledgement does not invalidate the client's next retry. A connection-specific identity
MUST still distinguish the replacement from the old connection during command handling and cleanup.

The registry MUST remember the current token after ordinary disconnection, even when no game is
running. It MUST NOT accept an invalidated token merely because the replacement has also disconnected.
For the initial in-memory design, a server restart invalidates ownership tokens and requires explicit
resume. Persistent recovery across server restarts can be specified separately.

Missing, stale, or unknown recovery tokens MUST produce a distinct result requiring explicit resume.
Clients MUST NOT silently convert a rejected recovery into a takeover. The initial session setup
without a confirmed session ID MUST remain a user-directed retry, since automatic repetition of a
new-game request can create duplicate sessions.

## Client and gateway behavior

The protocol MUST provide a machine-readable displacement signal, distinct from a transient network
failure and authentication expiry. Gateways MUST preserve that distinction when notifying clients.

`resume_game` carries `mode` (`takeover`, `recover`, or the backward-compatible default `legacy`)
and an optional `owner_token`. Session results include the ownership token. Errors include a `code`,
such as `resume_required`, `session_in_use`, `cleanup_pending`, or `cleanup_failed`. WebSocket close
code `4001` means displaced; the web gateway uses `4002` when explicit resume is required. Browser
authentication expiry continues to use `4401`.

The browser connects to `/ws?protocol=2` and sends a JSON `open` message with `resume`, `mode`, and
`owner_token` after the socket opens. Session results also include a per-connection `connection_id`.
Uploads send `X-CF86-Owner-Token` and `X-CF86-Connection` headers. Legacy web connections use the
original resume query parameter and cannot displace an active owner.

| Situation | Required behavior |
| --- | --- |
| Network failure after session setup | Recover using the current ownership token and bounded retry delays. |
| Session taken over elsewhere | Stop automatic recovery and explain that the session moved. |
| Missing or invalid recovery token | Require explicit resume; do not acquire ownership automatically. |
| Authentication expiry | Stop recovery and require authentication. |
| Explicit resume | Request takeover after authorization. |

The web gateway MUST retire the old relay and remove its active upload route when displaced. Uploads
MUST be associated with the initiating ownership token and connection, not just the session ID and
login. Ownership MUST be rechecked after asynchronous transcription and before forwarding a take.
An old request MUST NOT be redirected into the replacement game. Pending playback and partial
recordings SHOULD be discarded when their connection is retired.

A displaced browser MUST stop retrying and offer an explicit resume action. Visibility and network
events MUST NOT turn that state back into automatic recovery. It MUST retain enough ownership state
to recognize a rejected recovery even if it missed the original displacement notification.

Telegram MUST remove its displaced active connection while retaining the saved session association.
After displacement, ordinary text or voice messages MUST NOT lazily reopen that session. The gateway
SHOULD explain that `/resume` or `/continue` explicitly takes control again. Zello and other adapters
MUST likewise avoid automatic takeover after displacement; each MUST expose a deliberate user or
operator action to resume. The Zello gateway logs displacement and retires that channel; restarting
the gateway is an explicit operator resume of its saved sessions. The text client prints a notice
and exits; running it again with `--resume` explicitly takes control. Displacement of one session
SHOULD NOT stop unrelated sessions.

## Implementation and compatibility

`server/_ownership.py` contains the server registry and lifecycle coordination; `server/_main.py`
routes commands through it. `Game.__aexit__` in `server/_game.py` owns game cleanup. Critical tool
operations and journal replacement finish before cancellation releases their state. The web
gateway's `live` map remains routing state and MUST NOT become the authority for session ownership.

Clients without ownership-token support MUST NOT gain implicit takeover rights through legacy resume
requests. Legacy resume requests are allowed for idle sessions and rejected when another owner is
active or cleanup is pending. The server and adapters SHOULD be upgraded together. Existing browser
pages need one reload to load the new protocol and upload headers.

## Validation

Tests MUST establish the following behavior without requiring external model or voice providers:

- Web and Telegram resuming the same user and session never run two games concurrently.
- Simultaneous resumes serialize, while different sessions and users remain independent.
- Replacement startup waits for deliberately delayed cleanup, including background state writes.
- Late close events and finalizers cannot remove or send commands to the replacement game.
- A displaced client that misses its notification cannot take ownership back through recovery.
- A dropped recovery acknowledgement allows retry with the same token.
- Disconnection of the replacement does not make an older ownership token valid again.
- Ordinary Telegram messages and browser wake events do not reverse a deliberate takeover.
- Pending uploads cannot reach a replacement after ownership changes during transcription.
- Cleanup failures, startup failures, and cancelled requests leave no overlapping game instances.
- Server restart requires explicit resume, and legacy clients cannot bypass active ownership.

## Future extensions

A later design MAY keep a game alive briefly for reattachment or allow several clients to exchange
messages with one shared `Game`. Both would retain central ownership of the game lifecycle and
persistent state, but would require separate rules for input ordering, output delivery, and client
attachment. Neither behavior is provided by this takeover implementation.
