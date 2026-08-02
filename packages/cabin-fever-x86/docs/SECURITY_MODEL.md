# Launcher Security Model

## Status and scope

This document defines the launcher trust boundary and required exposure controls. It is not a claim
that the guest safely contains hostile code under every threat model. The key words **MUST**, **MUST
NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are normative.

## Trust boundary

The game server, web client, core dependencies, downloaded game files, and custom package sources run
inside a sandbox VM. The launcher and host filesystem remain outside that VM.

The guest MUST be treated as network-capable and credential-bearing. The sandbox reduces direct host
exposure, but software executing in the guest can communicate with external services and access all
guest-visible mounts, configuration, and environment variables.

## Network exposure

The guest MUST have outbound network access because preparation downloads packages and the running
core contacts configured providers. Operators MUST assume guest code can exfiltrate guest-visible
data over that connection.

Only the guest web port MAY be forwarded to the host. The host side of that forward MUST bind to
loopback. The game server port MUST remain unforwarded and reachable only inside the guest.

Changing the host web port MUST NOT widen the bind address. LAN exposure is outside the launcher's
supported security boundary.

## Host filesystem exposure

The launcher MUST mount only the persistent `data` directory into the guest for ordinary operation.
That mount is writable and guest code MAY modify or delete its contents.

The launcher MUST NOT mount the home directory, VM directory, configuration directory, repository,
or unrelated host paths merely for convenience. The configuration MUST be copied as a single file
rather than exposing its parent directory.

Prepared guests MUST be saved before persistent data or the active configuration is attached. This
prevents ordinary prepared images from capturing secrets or a particular user's game state.

## Credentials

Only environment variables referenced by configuration values MAY be passed to guest game processes.
Unreferenced host environment variables MUST remain unavailable through this transfer mechanism.

Credentials MUST be quoted and transferred as data, not interpreted as shell syntax. Interactive
credential prompts MUST suppress terminal echo.

Credentials placed literally in `config.yaml` or supplied through referenced variables are available
to trusted and compromised software inside the guest. The launcher MUST NOT describe the VM as a
secret-isolation boundary against guest code.

## Package sources and downloaded content

The default core package is trusted to the same degree as the published Cabin Fever release. A
custom package locator expands the operator's trust decision to that source and its dependencies.
Custom package locators MUST be treated as hostile input for command construction.

Downloaded games and archives SHOULD be treated as untrusted data. Their processing SHOULD occur
inside the guest, with host writes limited to the persistent data mount.

## Residual risks

Sandbox escapes, vulnerable hypervisors, malicious dependencies, provider compromise, and exposed
API credentials remain possible risks. The launcher SHOULD minimize mounts, forwards, credentials,
and host-side parsing to reduce their impact.

