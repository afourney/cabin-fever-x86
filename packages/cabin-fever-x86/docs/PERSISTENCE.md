# Persistence Specification

## Status and scope

This document specifies which Cabin Fever x86 state survives guest shutdowns, launcher upgrades,
and rebuilds. The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are
normative.

## Home directory

All launcher-managed state MUST live beneath one home directory. The launcher MUST create the home,
`vm`, and `data` directories when absent. It MUST NOT overwrite an existing configuration file.

The default home MUST be platform appropriate:

- Linux and WSL: `$XDG_CONFIG_HOME/cabin-fever-x86`, or `~/.config/cabin-fever-x86` when unset.
- macOS: `~/Library/Application Support/cabin-fever-x86`.
- Windows: `%APPDATA%\cabin-fever-x86`, with a conventional roaming-profile fallback.

`CABIN_FEVER_X86_HOME` and `--home` MAY select another home. `--home` takes precedence for that
launch.

## Persistent classes

`config.yaml` MUST persist launcher and game configuration. It MAY contain secret references and
SHOULD contain references rather than literal credentials.

`data` MUST contain user-owned and game-night state, including downloaded or user-supplied games,
sessions, transcripts, saved games, game memories, and runtime logs. It MUST be writable from the guest
and MUST survive guest shutdown, prepared-guest replacement, and `--rebuild`.

A saved game MAY carry a story signature made from the Z-machine release number, serial, and
checksum. A missing signature identifies a legacy save and MUST NOT prevent it from loading. When a
signature is present, the runtime SHOULD reject a restore against a different story build before
handing the saved state to the interpreter.

Each session's known map records routes the player and companion have discovered across runs of a
game. It MUST be stored separately from the run map embedded in a saved game: restoring an older
save MAY rewind the protagonist's route history, but MUST NOT erase shared route knowledge.

Game memories MUST live beneath `game-memories/<rom-name>/`. `game.json` MUST identify the ROM name,
the game-memory format version, and the story signature used to validate every memory in that
directory before using Z-machine object numbers. `map.json` MUST contain the independently versioned
known-map data. Memories for a mismatched story signature MUST be moved aside as one directory so
that memory types cannot become separated from their identity. Other memory files MAY be added to
this directory in the future. `reload_reasons.jsonl` MAY record significant reasons for restoring an
earlier save, one JSON object per line containing the room name before the restore and a concise
reason. Routine or unexplained restores need not be recorded. These reasons SHOULD be recalled as
private context when the companion deliberately starts a new run, whether through the dedicated
tool or at the DOS prompt, but not when a save is restored or a session is resumed. This
specification does not currently define a general history file.

`vm` MUST contain prepared guest saves. These saves are replaceable caches and MAY be rebuilt or
discarded without affecting `data` or `config.yaml`.

`launcher-pypi-version-cache.json` MAY cache the latest launcher version observed on PyPI. Its loss,
corruption, or deletion MUST have no effect beyond causing another update check.

## Guest boundary

The guest's `/cabin-fever-x86/data` path MUST refer to the host's persistent `data` directory while
the VM is running. Writes beneath that path are durable host writes.

Other guest filesystem writes are ephemeral unless incorporated into a prepared save. The active
configuration MUST be transferred anew for every launch so host edits take effect without rebuilding
the guest.

## Rebuild and upgrade guarantees

A rebuild MUST preserve `config.yaml`, `data`, and the launcher update cache. It MAY replace the
prepared save for the current compatibility series.

A launcher upgrade MUST reuse compatible prepared and persistent state according to `VERSIONING.md`.
A major/minor compatibility change MAY create an additional prepared save; it MUST NOT delete older
series automatically.

The launcher SHOULD treat loss of prepared VM state as recoverable. It MUST treat loss of persistent
user data as a materially different and more serious condition.
