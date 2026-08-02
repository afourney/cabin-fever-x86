# Launcher CLI Specification

## Status and scope

This document specifies the public command-line interface of the Cabin Fever x86 launcher. The key
words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are normative.

## Entry points

The installed commands `cabin-fever-x86` and `cf86` MUST provide equivalent launcher behavior.
Invocation without arguments MUST use platform and application defaults.

The launcher SHOULD provide conventional `--help` output containing its purpose, available options,
defaults, and argument syntax. Unknown options and invalid argument values MUST be rejected before VM
startup.

## Options

`--home PATH` selects the directory containing configuration, prepared VMs, persistent data, and
launcher cache state. It overrides `CABIN_FEVER_X86_HOME` and the platform default for that invocation.
The selected home MUST be created when absent.

`--config PATH` selects the configuration file. It does not change the home directory. The supplied
path MUST exist; otherwise launch MUST fail rather than creating or selecting another file.

`--port PORT` selects the host loopback port forwarded to the guest web client. Its default is
`8000`. The value MUST be an integer. It MUST NOT change the guest's internal service ports or bind
the host service beyond loopback.

`--rebuild` forces creation of a new prepared guest from the clean base image before launch. It MUST
preserve persistent user data and configuration.

## Output

Normal output SHOULD include the product header, installed launcher version, resolved home directory,
guest preparation or boot status, and final loopback URL. Long-running guest installation output MAY
be streamed directly so the user can observe progress.

Errors and guest diagnostics SHOULD be written to standard error. Prompts and normal lifecycle
messages MAY use standard output. Credential input MUST not be echoed.

The update notice specified in `VERSIONING.md` MAY appear during startup but MUST NOT require user
interaction.

## Interactive behavior

When standard input is interactive, the launcher MAY prompt for missing referenced environment
variables. `Ctrl-C` and `Ctrl-D` SHOULD end an active session cleanly where supported.

When standard input is non-interactive, the launcher MUST NOT interpret an already-closed input stream
as a request to terminate a successfully launched session. It MUST NOT prompt for missing credentials;
it MUST report them and fail instead.

## Exit behavior

Argument syntax errors MUST use the command-line parser's conventional nonzero exit behavior.
Configuration, credential, guest preparation, and service startup failures MUST return a nonzero
process status.

An orderly user hang-up SHOULD return success. The launcher SHOULD expose sufficient diagnostics for
unexpected guest-service termination even when cleanup completes normally.

Future options SHOULD remain backward compatible. Existing option meanings MUST NOT be repurposed;
incompatible CLI changes require an appropriate launcher version-series change.
