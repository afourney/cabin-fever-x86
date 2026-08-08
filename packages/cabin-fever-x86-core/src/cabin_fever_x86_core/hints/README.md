# Hint Material

This directory holds the packaged hint books that back the in-game hint feature
(see `../hints.py`). Each file is named after the game's ROM basename — for
example, `zork1.rot13` serves hints for `zork1.z3`.

## Provenance

All hint material here is **original material**. Each file was written with 
GPT-5.6 Sol, working from:

- solution trajectories bundled with [Jericho](https://github.com/microsoft/jericho),
  which provide verified, playable command sequences for the supported games; and
- consultation of the freely available reference materials hosted on
  [ifarchive.org](https://ifarchive.org), the Interactive Fiction Archive.

## Why the files are ROT-13 encoded

Hint files are stored ROT-13 encoded so that nobody stumbles into a spoiler by
opening a file, grepping the repository, or scrolling past a diff.

ROT-13 ("rotate by 13") is a simple letter substitution cipher: each ASCII
letter is replaced by the letter 13 positions later in the alphabet, wrapping
around at the end (`A` → `N`, `B` → `O`, ..., `N` → `A`). Case is preserved, and
digits, punctuation, and whitespace are left alone. Because the alphabet has 26
letters, applying ROT-13 twice returns the original text — the same operation
both encodes and decodes.

ROT-13 is **not** encryption and provides no security. It exists purely to make
the text unreadable at a glance.

## Reading and writing hint files

Use any online ROT-13 tool, or the `rot13_file.py` script in the project root.
Since ROT-13 is symmetric, the same command both decodes and encodes:

```bash
# Decode a hint file to stdout
uv run rot13_file.py packages/cabin-fever-x86-core/src/cabin_fever_x86_core/hints/zork1.rot13

# Encode a new plaintext walkthrough into place
uv run rot13_file.py zork1.txt --output packages/cabin-fever-x86-core/src/cabin_fever_x86_core/hints/zork1.rot13
```

Please keep committed hint files encoded — only plaintext working copies should
stay outside the repository.
