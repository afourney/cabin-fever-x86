# Failure and Recovery Specification

## Status and scope

This document defines expected launcher failure behavior and operator recovery paths. The key words
**MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are normative.

## General principles

Failures MUST preserve persistent user data whenever the host filesystem remains available. A
failure MAY invalidate or require replacement of a prepared guest because it is a cache.

Errors SHOULD identify the failed stage and actionable cause. Expected user errors SHOULD be sent to
standard error. Diagnostic detail from guest initialization and service startup SHOULD remain
visible.

## Configuration and credentials

An explicitly selected missing configuration MUST fail before booting a VM. Malformed launcher-owned
configuration MUST identify the file and invalid field or structure.

Missing referenced environment variables MUST be prompted for interactively or reported together in
non-interactive mode. Failure to obtain all values MUST stop startup.

Core-owned configuration errors MAY be detected inside the guest. They MUST prevent a failed game
server from being presented as ready.

## Preparation failures

A nonzero preparation result, timeout, or missing completion signal MUST prevent creation of a new
prepared guest. If a previous prepared guest exists, a rebuild failure MUST leave it available for a
later non-rebuild launch whenever the save mechanism permits.

Network, package-resolution, compilation, and validation failures during preparation SHOULD be
reported with their guest output. The operator MAY retry after correcting connectivity or package
availability. `--rebuild` SHOULD be the standard recovery when the prepared guest is absent,
incompatible, or suspected to be corrupt.

## Save failures

A directory without a manifest MUST be treated as incomplete and ignored. Temporary or incomplete
save artifacts MAY be removed or replaced during a later successful build.

Failure to write a prepared save MUST fail the preparation operation rather than claim that future
launches can reuse it. Persistent `data` and configuration MUST remain untouched.

Live replacement of a save from which the guest booted MUST remain disabled until issue #12 is
resolved. If a guest previously underwent the unsafe operation and later lacks core executables, the
operator SHOULD recover with `--rebuild`.

## Runtime failures

A game server that exits during startup MUST abort launch and expose recent server diagnostics. A web
client that exits after startup SHOULD end the session and report its exit status.

A host-port conflict MUST fail without widening the bind address or selecting an unrequested port.
The operator MAY retry with a different `--port`.

Interrupts SHOULD result in orderly guest shutdown. Forced host termination may discard ephemeral VM
changes but MUST NOT retroactively alter already-written persistent data.

## Update failures

Launcher PyPI-check failures MUST be silent and non-fatal. A corrupt launcher update cache MUST be
treated as a cache miss.

When guest core updates are re-enabled, a failed update MUST NOT refresh its check timestamp, emit its
completion signal, or trigger a guest save. The existing core SHOULD remain usable; otherwise the
operator SHOULD rebuild.

