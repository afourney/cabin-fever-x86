# cabin-fever-x86-core

A shared game night with an AI companion over a simulated radio: play old text
adventures together.

This is the game itself — the z-machine, the companion, and the three clients —
running natively on the host. **Linux only.** It builds
[jericho](https://github.com/microsoft/jericho) from source, which ships no
wheels and supports no other platform.

```bash
pip install cabin-fever-x86-core

cf86-server            # hosts the game session
cf86-web               # the radio, in a browser tab
cf86-text              # the same session, typed instead of spoken
```

Run it as a server and bind it where you like — `cf86-web --web-host 0.0.0.0`
puts the radio on your LAN for someone on the couch.

**On macOS or Windows, or if you would rather the interpreter not run on your
own machine, install [`cabin-fever-x86`](https://pypi.org/project/cabin-fever-x86/)
instead.** It runs this exact package inside a sandbox VM and forwards the web
client back out — which also keeps a memory-unsafe frotz fork, parsing game
files off the internet, off your host.

Full documentation lives in the
[repository](https://github.com/afourney/cabin-fever-x86).
