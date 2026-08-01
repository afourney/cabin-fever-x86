# cabin-fever-x86-core

The native, Linux-only runtime for [Cabin Fever x86](https://github.com/afourney/cabin-fever-x86): the game server, AI companion, Z-machine interpreter, web radio, and text client.

This package is intended for Linux users who want to run the services directly, expose the web client to a LAN, customize the configuration, or develop Cabin Fever itself. It builds [jericho](https://github.com/microsoft/jericho) and its frotz fork from source, so a C toolchain is required and other operating systems are not supported.

For a self-contained installation on Linux, macOS, or Windows, use the sandboxed [`cabin-fever-x86`](https://pypi.org/project/cabin-fever-x86/) launcher instead. It runs this package inside a QEMU VM, keeping the memory-unsafe interpreter and downloaded game files off the host.

## Native installation

Cabin Fever x86 Core requires Linux, Python 3.12 or newer, and standard C build tools. On Debian or Ubuntu, for example:

```bash
sudo apt-get install build-essential python3-dev
python3 -m venv .venv
source .venv/bin/activate
pip install cabin-fever-x86-core
```

Set the API keys used by the default configuration:

```bash
export OPENAI_API_KEY=...
export ELEVENLABS_API_KEY=...
```

An ElevenLabs key is optional if you only need text interaction. For full configuration options, start from [`config.example.yaml`](https://github.com/afourney/cabin-fever-x86/blob/main/config.example.yaml) and save it as `config.yaml` in the directory where you run the commands, or pass its location with `--config`.

## Running the services

Start the game server:

```bash
cf86-server
```

Then start one of the clients in another terminal:

```bash
cf86-web   # browser-based radio at http://127.0.0.1:8000
# or
cf86-text  # terminal-based text client
```

Each command accepts `--help`. The server and clients can be run on different Linux machines by setting their interfaces, hosts, and ports in `config.yaml` or with command-line options. For example, to expose only the web frontend on the local network:

```bash
cf86-web --web-host 0.0.0.0
```

Review your firewall and network trust before binding a service beyond localhost.

## Z-machine games

When `cf86-server` runs directly, it looks for `.z3`–`.z8` game files in `data/games/` relative to its working directory. If that directory contains nothing playable, the server downloads the [z-machine-games](https://github.com/BYU-PCCL/z-machine-games) archive and unpacks only the 57 games in its `jericho-game-suite` folder.

Add other games by copying them into `data/games/` before starting the server. Only use game files you have the right to run.

## Development

The repository is a [uv](https://docs.astral.sh/uv)-managed workspace. On Linux or WSL:

```bash
git clone https://github.com/afourney/cabin-fever-x86.git
cd cabin-fever-x86
uv venv --python 3.12
uv sync --all-packages
cp config.example.yaml config.yaml
```

Then run the native entry points from the repository root:

```bash
uv run cf86-server
uv run cf86-web
# or: uv run cf86-text
```

See the [repository README](https://github.com/afourney/cabin-fever-x86#development) for the complete project setup.
