# Launch Protocol Specification

## Status and scope

This document specifies the ordered behavior of one launcher invocation. The key words **MUST**,
**MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are normative.

## Startup sequence

The launcher MUST perform startup in this order:

1. Parse command-line arguments and reject invalid syntax.
2. Display launcher identity and installed version.
3. Resolve and prepare the home directory.
4. Perform the best-effort launcher update notice specified in `VERSIONING.md`.
5. Select and verify the configuration path.
6. Discover referenced environment variables and obtain required values.
7. Resolve the core package locator.
8. Select a prepared guest or clean base guest.
9. Initialize and save a clean guest when required.
10. Attach persistent data and transfer the active configuration.
11. Start the game server and verify that it remains running through its startup interval.
12. Start the web client and publish its loopback URL.
13. Wait for the web client, user interrupt, or interactive end-of-file.
14. Cancel remaining wait operations and shut down the guest.

A failure in a prerequisite MUST prevent dependent stages from running.

## Preparation protocol

When preparation is required, the launcher MUST stream progress to the terminal. It MUST accept
`GUEST INIT COMPLETE` only after a successful preparation process. A zero exit status without the
completion signal MUST be treated as incomplete initialization.

The launcher MUST save a successfully prepared guest before transferring configuration or mounting
persistent data.

## Service startup

The game server MUST start before the web client. The launcher SHOULD allow a short settling period
and MUST fail startup if the server exits during it. Diagnostic output from the server SHOULD be
shown to the user.

The web client MUST listen on the fixed guest web port. The selected host `--port` controls only the
loopback forwarding endpoint.

After service startup, the launcher MUST display the complete loopback URL and supported interactive
hang-up controls.

## Runtime and shutdown

The first terminal condition ends the run: web-client exit, supported interrupt, or end-of-file from
interactive input. Closed or redirected non-interactive input MUST NOT be interpreted as a hang-up.

On a user hang-up, the launcher SHOULD report that it is hanging up. On web-client termination, it
SHOULD report the client's exit status. In either case, the guest MUST be allowed to unwind through
the sandbox lifecycle rather than being abandoned as a running process.

The launcher MUST NOT automatically persist arbitrary runtime guest changes. Persistence occurs only
through the operations specified in `PERSISTENCE.md` and `GUEST_LIFECYCLE.md`.

