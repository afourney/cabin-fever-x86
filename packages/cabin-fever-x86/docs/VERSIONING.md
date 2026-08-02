# Cabin Fever x86 Versioning and Update Specification

## Status and scope

This document specifies version compatibility and update behavior for the Cabin Fever x86
launcher and the `cabin-fever-x86-core` package installed in its guest virtual machine.

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are to be interpreted
as normative requirements.

## Version scheme

Both packages MUST use PEP 440 versions. Release numbers have major, minor, and patch components
and MAY include a prerelease suffix such as an alpha, beta, or release-candidate number.

The launcher and core versions are related by a compatibility series, but they are not required to
be identical. Either package MAY advance its patch or prerelease number ahead of the other.

### Compatibility series

A compatibility series consists of all releases sharing the same major and minor components.
For example, `0.0.1a1`, `0.0.1`, `0.0.2a1`, and `0.0.9` belong to the `0.0` series. `0.1.0` belongs
to a different series.

The launcher MUST use its own major and minor components to select the compatible core series. Its
patch and prerelease components MUST NOT establish the minimum core version.

For a prerelease launcher, the default core requirement MUST begin at `.0a0` in the same series.
For example, launcher `0.0.1a5` requires `cabin-fever-x86-core~=0.0.0a0`. This admits prerelease
and stable core releases in the `0.0` series, including `0.0.1a1` and `0.0.2`.

For a stable launcher, the default core requirement MUST begin at `.0` in the same series. For
example, launcher `1.2.3` requires `cabin-fever-x86-core~=1.2.0`. This does not explicitly opt into
prerelease core releases and prefers stable candidates.

Neither default requirement may admit a core release from the next minor series.

## Launcher releases and prepared guests

The launcher runs the core inside a prepared guest VM. Prepared guest names MUST be derived from
the launcher's major and minor version components. Patch and prerelease launcher updates MUST reuse
the prepared guest for their compatibility series. A launcher release that changes either the
major or minor component MUST select a different prepared guest and therefore build it before use.

The user MAY request a rebuild explicitly with `--rebuild`. A rebuild MUST ignore the existing
prepared guest, start from the clean base image, install the configured core package, and replace
the prepared guest only after initialization succeeds. Rebuilding MUST NOT remove persistent
configuration, games, sessions, transcripts, or saved games.

## Core installation

When no custom package locator is configured, the launcher MUST install the default core
requirement for its compatibility series.

A package locator that is absent, empty, or exactly equal to the generated default requirement is
considered the default locator. Any other non-empty locator is custom. A custom locator is taken as
an explicit request to install and retain a particular package source or constraint.

The generated configuration template leaves the package locator commented out. Consequently, an
unmodified configuration uses the generated default requirement and needs no migration when the
default requirement changes.

## Launcher update check

The launcher SHOULD perform a best-effort check for newer launcher releases on PyPI during startup.
The check MUST compare the installed launcher version with the version published for the
`cabin-fever-x86` project using PEP 440 ordering.

The launcher MUST cache the latest observed PyPI version in
`<home>/launcher-pypi-version-cache.json`. A valid cached result MAY be reused for 24 hours. After
that period, the launcher SHOULD refresh it with a request whose timeout is short enough not to
materially delay startup.

Failure to reach PyPI, parse its response, read or write the cache, or determine the installed
version MUST NOT prevent the game from launching. Development executions without installed package
metadata MUST skip the check.

When a newer launcher is available, the launcher SHOULD display upgrade guidance but MUST NOT
upgrade itself automatically.

If installation metadata identifies uv as the installer, the notice MUST show only the following
two workflows:

- For a uv-managed tool: `uv tool upgrade cabin-fever-x86`.
- For a uv-managed virtual environment: `uv pip install --upgrade cabin-fever-x86`.

For pip, missing installer metadata, or an unknown installer, the notice MUST show
`python -m pip install --upgrade cabin-fever-x86`.

## Guest core update protocol

> **Current status:** This protocol is disabled pending resolution of
> [issue #12](https://github.com/afourney/cabin-fever-x86/issues/12). A loaded prepared guest cannot
> currently be saved safely over the image from which it booted. The requirements below specify
> the intended behavior after safe live saving is available.

Automatic core updates MUST run only for an existing prepared guest using the default package
locator. They MUST NOT run during a fresh build or rebuild, because those operations already install
the selected core requirement. They MUST NOT run for a custom package locator.

A fresh build or rebuild MUST create `/cabin-fever-x86/.version_check` after core installation and
validation succeed.

On an eligible prepared-guest launch, the guest MUST inspect the modification time of
`/cabin-fever-x86/.version_check`. If the file is less than four hours old, the guest MUST skip the
core update. If the file is missing or at least four hours old, the guest SHOULD ask uv to upgrade
`cabin-fever-x86-core` using the same generated default requirement used for initial installation.

The upgrade operation MAY determine that no newer compatible core exists. That outcome is still a
successful version check. On success, the guest MUST update the timestamp and emit the exact line
`VERSION CHECK COMPLETE`. On failure, it MUST NOT update the timestamp or emit the completion line,
so a later launch can retry.

The launcher MUST treat `VERSION CHECK COMPLETE` as a request to preserve the running guest. It MUST
save the guest live so both the updated core environment and the new timestamp survive subsequent
launches. A successful check that installs nothing still requires preservation because the timestamp
changed.

Core update failures SHOULD be non-fatal to the current game launch. The previously installed core
SHOULD remain usable whenever the package installer can preserve it safely.

## Safety properties

Automatic update behavior MUST observe these boundaries:

- A launcher update check is advisory and never mutates the launcher's environment.
- A default core update remains inside the launcher's major/minor compatibility series.
- A custom core locator disables automatic core updates.
- Update-service and network failures do not prevent ordinary launches.
- A prepared guest is not replaced until initialization or update work reports success.
- Persistent user data remains outside the replaceable prepared guest.
