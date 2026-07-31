# cabin-fever-x86

A shared game night with an AI companion over a simulated radio: play old text
adventures together.

This package is the launcher. It boots a sandbox VM, runs
[`cabin-fever-x86-core`](https://pypi.org/project/cabin-fever-x86-core/) inside
it, and forwards the radio back out to your browser. Everything Linux-only
stays in the guest, so this installs anywhere QEMU does — Linux, macOS, or
Windows.

```bash
pip install cabin-fever-x86

cf86
# open http://127.0.0.1:8000
```

The VM image is fetched on first run and is large; the first boot takes a while,
later ones do not.

Two consequences worth knowing about. The forwarded port is bound to
`127.0.0.1` and cannot be widened — if you want the radio on your LAN, run
`cabin-fever-x86-core` directly on a Linux box instead. And the game runs
somewhere other than your machine, which is the point: the z-machine is a
memory-unsafe C interpreter parsing files you downloaded, and it belongs behind
a boundary.

> **Upgrading from 0.0.1a1?** That release was the whole game, natively. This
> name is now the sandboxed launcher; the native install moved to
> `cabin-fever-x86-core`.

Full documentation lives in the
[repository](https://github.com/afourney/cabin-fever-x86).
