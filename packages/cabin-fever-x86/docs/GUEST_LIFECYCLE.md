# Guest Lifecycle Specification

## Status and scope

This document specifies how the Cabin Fever x86 launcher creates, selects, runs, saves, and
replaces its guest virtual machine. The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**,
and **MAY** are normative.

## Guest states

A guest is either a clean base guest, a prepared guest, or a running guest. The clean base guest
contains the operating system and build toolchain. A prepared guest additionally contains an
installed and validated core environment. A running guest is ephemeral except for explicitly
mounted persistent data and a successfully written prepared-guest save.

A save MUST be considered prepared only when its save manifest exists. A directory without a
manifest is incomplete and MUST NOT be booted as a prepared guest.

## Selection

The launcher MUST select a prepared guest using the launcher's major/minor compatibility series as
specified in `VERSIONING.md`. If a valid save exists for that series, the launcher SHOULD boot it.
If none exists, the launcher MUST boot the clean base guest and prepare it.

`--rebuild` MUST bypass an existing prepared guest and boot the clean base guest. It MUST NOT run
initialization on top of the existing prepared guest.

## Preparation

Preparation MUST create an isolated core environment, install the selected core package, and prove
that both the game server and web client entry points are usable. Preparation succeeds only after
all validation completes and the guest emits `GUEST INIT COMPLETE`.

A failed, timed-out, or incomplete preparation MUST NOT replace a working prepared guest. A clean
preparation SHOULD be allowed up to 15 minutes because native dependencies may require compilation.

After successful preparation, the launcher MUST save the guest before mounting persistent user data
or transferring the active configuration. The prepared image MUST NOT capture a particular game
night's data, configuration, or credentials.

## Runtime attachment and startup

After preparation or boot, the launcher MUST attach the persistent data directory, transfer the
current configuration, and provide only the environment variables referenced by that configuration.
It MUST start and verify the game server before starting the web client.

The server MUST remain private to the guest. The web client MUST be the only forwarded guest port.

## Termination

The launcher SHOULD run until the web client exits, the user sends an interrupt, or interactive
standard input reaches end-of-file. End-of-file on non-interactive input MUST NOT terminate a run.

On termination, outstanding launcher tasks MUST be cancelled and the guest context MUST be allowed
to shut down normally. Ordinary runtime disk changes MUST be discarded unless a lifecycle operation
explicitly saves them.

## Save replacement

Prepared guests are caches. Replacing or losing one MUST NOT remove persistent user data. A save
replacement MUST be attempted only after the guest operation that motivated it reports success.

Live replacement of a prepared guest booted from that same save is currently unsafe and MUST remain
disabled pending [issue #12](https://github.com/afourney/cabin-fever-x86/issues/12). Rebuilds remain
safe because they boot from the clean base image before replacing the prepared save.

