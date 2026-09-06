# cabin-fever-x86-core

The native, Linux-only runtime for [Cabin Fever x86](https://github.com/afourney/cabin-fever-x86): the game server, AI companion, Z-machine interpreter, web radio, and text client.

This package is intended for Linux users who want to run the services directly, expose the web gateway to a LAN, customize the configuration, or develop Cabin Fever itself. It builds [jericho](https://github.com/microsoft/jericho) and its frotz fork from source, so a C toolchain is required and other operating systems are not supported.

For a self-contained installation on Linux, macOS, or Windows, use the sandboxed [`cabin-fever-x86`](https://pypi.org/project/cabin-fever-x86/) launcher instead. It runs this package inside a QEMU VM, keeping the memory-unsafe interpreter and downloaded game files off the host.

## Native installation

Cabin Fever x86 Core requires Linux, Python 3.12 or newer, and standard C build tools. On Debian or Ubuntu, for example:

```bash
sudo apt-get install build-essential python3-dev
python3 -m venv .venv
source .venv/bin/activate
pip install cabin-fever-x86-core
# Include the optional Telegram gateway if wanted:
# pip install 'cabin-fever-x86-core[telegram]'
# Include the optional Zello gateway if wanted:
# pip install 'cabin-fever-x86-core[zello]'
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

Then start a gateway or the text client in another terminal:

```bash
cf86-web   # browser-based radio at http://127.0.0.1:8000
# or
cf86-text  # terminal-based text client
# or, when installed with the telegram extra
cf86-telegram
# or, when installed with the zello extra
cf86-zello
```

Each command accepts `--help`. The server, gateways, and text client can be run on different Linux machines by setting their interfaces, hosts, and ports in `config.yaml` or with command-line options. For example, to expose the web gateway on the local network:

```bash
cf86-web --web-host 0.0.0.0
```

Review your firewall and network trust before binding a service beyond localhost.

The Telegram gateway uses the `telegram_gateway` section of `config.yaml`. It
requires a bot token, Telegram API ID and API hash, plus an allowlist of numeric
Telegram user IDs. Start it with an empty allowlist and send the bot a private
message to have the rejected user ID written to its log; then add that ID to
`allowed_accounts` and restart it. Private text messages and voice notes are
accepted. Voice notes are transcribed with the configured ElevenLabs key and
forwarded silently. When that key is available, the first companion transmission
is a captioned voice note; later replies match the player's most recent input —
voice answers voice, and text answers text. Replies too long for a Telegram
caption, and replies whose synthesis fails, are sent as separate text so no
content is lost.

The Zello gateway is voice-only. It joins the configured `zello.channel`, ignores
all text and unauthorized senders, transcribes authorized Ogg Opus messages,
and returns synthesized Ogg Opus audio. Its `--resume` and `--list-sessions`
options match the text client's command-line session management. Credentials
are read from the YAML file named by `zello.credentials_file`.

## Server user identities and storage

Trusted adapters can send `X-CF86-User-ID: <user-id>` in the WebSocket handshake.
User IDs contain 1–64 lowercase ASCII letters, digits, underscores, or hyphens.
A missing header selects `guest`; an empty, invalid, or repeated header returns
HTTP 400 before the WebSocket opens. The identity remains fixed for that connection.

This header asserts identity; it is not a password or access token. The server
accepts any valid user ID, without a user registry. Keep access restricted to
trusted local processes or trusted adapters through an SSH tunnel or authenticated
transport. Public-facing adapters must authenticate users before setting it.

Server data is stored relative to the working directory:

```text
data/users/<user_id>/sessions/<session_id>/server/
  messages.jsonl
  usage.jsonl
  saves/
  game-memories/
```

Session listing and resuming operate only within the connection's user directory.
A session belonging to another user is reported as nonexistent. The gateways and
text client send no header and use the fixed user `guest`. Their transcripts and audio
live in `data/users/guest/sessions/<session_id>/<component>/`, where `<component>` is
`text_client`, `web_gateway`, `telegram_gateway`, or `zello_gateway`. The Telegram gateway also
remembers which session each account was last in, in
`data/users/guest/telegram_gateway/sessions.json`. Downloaded games remain shared
in `data/games/`.

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
