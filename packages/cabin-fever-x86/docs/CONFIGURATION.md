# Configuration Specification

## Status and scope

This document specifies launcher configuration discovery, creation, interpretation, and credential
handling. Core-specific setting semantics are outside its scope. The key words **MUST**, **MUST NOT**,
**SHOULD**, **SHOULD NOT**, and **MAY** are normative.

## Discovery and creation

Without `--config`, the launcher MUST use `<home>/config.yaml`. If that default file does not exist,
the launcher MUST create it from the packaged template and MUST NOT require first-run editing.

With `--config`, the launcher MUST use the supplied path exactly after user-directory expansion. It
MUST report an error if the explicit file does not exist and MUST NOT silently substitute the home
configuration.

An existing configuration MUST never be overwritten automatically.

## Document shape

The configuration MUST be YAML with a top-level mapping. The optional `launcher` value MUST be a
mapping when present. `launcher.package_locator` MUST be a string when present.

The launcher interprets only launcher-owned settings. It MUST transfer the complete configuration to
the guest so the core can validate and interpret client and server settings.

## Environment references

String values MAY contain references of the form `${NAME}`, where `NAME` is a valid shell-style
environment variable name. References in comments or mapping keys MUST NOT create requirements.
Each referenced name SHOULD be requested at most once.

If `NAME` is unset and `CF86_NAME` exists, the launcher SHOULD use `CF86_NAME` as the value of
`NAME`. An explicit `NAME` value takes precedence.

The launcher MUST pass only referenced variables into the guest game processes. Values MUST be
transferred without shell interpretation.

When referenced variables are missing and input is interactive, the launcher SHOULD prompt without
echoing entered values. Empty answers MUST NOT satisfy a required value. When input is
non-interactive, missing referenced variables MUST cause startup to fail with the complete missing
name list.

## Package locator

An absent, null, empty-after-substitution, or fully unresolved package locator MUST select the
generated default core requirement. A non-empty locator MUST be passed as one package-source value
without shell expansion or command execution.

A custom locator MAY identify a PyPI requirement, local artifact, archive URL, or another source
accepted by uv. Its compatibility and availability are the operator's responsibility. Automatic core
updates MUST be skipped for custom locators.

## Transfer and confidentiality

The launcher MUST transfer the active configuration as a file inside the guest on every run. It MUST
NOT expose the configuration's containing host directory merely to transfer one file.

Environment references SHOULD be preferred over literal credentials. Configuration and referenced
credentials are visible to software running inside the guest and MUST be treated according to
`SECURITY_MODEL.md`.

