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

Each command accepts `--help`. The server, gateways, and text client can be run on different Linux machines by setting their interfaces, hosts, and ports in `config.yaml` or with command-line options. To bind the web gateway for an HTTPS reverse proxy:

```bash
cf86-web --web-host 0.0.0.0
```

Set `web_gateway.public_origin` as described below; remote HTTP access is rejected.
Review your firewall and network trust before binding a service beyond localhost.

### Browser callsigns and passwords

With just one user, `user_id: guest` **and** a `type: guest` identity keep the
existing “Click here to turn on your radio” experience (even if that user also
has Telegram or Zello identities). With multiple users, the page asks for a
Callsign and Password. “Login as Guest” appears only when that reserved guest
user has a guest identity, and must be selected explicitly. A user merely named
`guest` is not anonymous access. `users: []`, or only platform identities without
a guest identity, shows a browser-access denial. Omitting `users` retains the
default sole guest. Explicit empty or null values (including unset environment
references) are configuration errors; use `users: []` to configure no users.
The text client remains a trusted, anonymous guest adapter.

After signing in (or clicking through as guest), the next screen introduces Sam
and the cabin and lets you choose **New Session** or a saved session. Saved
sessions show when you last played, newest first; only New Session appears if
you have none. Microphone access and the game connection start after you confirm
your choice. A `?resume=<session-id>` link preselects that session when it belongs
to the signed-in user, while still showing the picker.

Add browser identities alongside the platform identities for a user's shared games:

```yaml
users:
  - user_id: operator
    identities:
      - type: login
        username: Night Owl
        password_hash: ${RADIO_PASSWORD_HASH}
  # Optional anonymous access:
  - user_id: guest
    identities:
      - type: guest
```

The Callsign is `username`, **not** `user_id`. Callsigns are trimmed and matched
case-insensitively, and must be unique across all login identities. Multiple
login identities can belong to one user. Only Argon2id password hashes are
accepted; the server verifies the supplied password, never accepts a hash as
a credential, and never falls back to guest after an invalid login. Generate
a hash using a hidden password prompt (not a password on the command line):

```bash
uv run python -c "from getpass import getpass; from argon2 import PasswordHasher; print(PasswordHasher().hash(getpass('Password: ')))"
```

Use the output in the private configuration or export `RADIO_PASSWORD_HASH`.
Keep hashes private too. The default Argon2id parameters are supported; custom
hashes require version 19, 8–256 MiB memory, 1–10 iterations, 1–8 parallelism,
and at least 16-byte salts and hashes. Password verification runs outside the
event loop, with at most two concurrent checks and 20 attempts/minute per
gateway worker. For public deployments, also apply rate limiting at the proxy.
GitHub sign-in is not implemented.

### Browser sessions and HTTPS

The default accepts only loopback browser URLs, preserving local HTTP and the
launcher's forwarded localhost port without a `Secure` cookie that browsers
would discard. For **any public or LAN hostname**, configure an exact HTTPS origin:

```yaml
web_gateway:
  public_origin: https://radio.example.com
  data_dir: data/web_gateway
  # Optional; otherwise a random signing.key is created once in data_dir:
  signing_secret: ${CF86_WEB_SIGNING_SECRET}
  session_idle_seconds: 3600
  session_max_seconds: 604800
```

Terminate TLS at a reverse proxy that preserves `Host` and sets
`X-Forwarded-Proto: https`. Uvicorn trusts loopback proxies by default; for a proxy
elsewhere set `FORWARDED_ALLOW_IPS` to its IPs, **never `*`**, and firewall the
gateway's HTTP port so clients cannot bypass the proxy. An origin includes an
explicit non-default port, but no URL path. Host validation and exact same-origin
checks protect login, guest selection, logout, refresh, uploads and WebSockets;
cross-origin and missing-Origin mutations/handshakes are refused, with no CORS
wildcards. Public cookies are `Secure`; all cookies are host-only, `HttpOnly`,
and `SameSite=Strict`. Use a dedicated host for the radio.

The signed cookie references revocable server-side state in `sessions.sqlite3`.
Both it and the generated `signing.key` live under persistent gateway data, not
a game's session directory. Keep this directory private and back it up; workers
must share it and the same configuration/secret. Alternatively provide a random
`signing_secret` of at least 32 characters (for example, generated by
`secrets.token_urlsafe(48)`) through the environment. Never commit a real secret.
Deleting the registry, or changing the secret, invalidates existing logins.

The page refreshes sessions over HTTP every five minutes (sooner with a shorter
idle timeout). A WebSocket keep-alive alone does not renew authentication.
Renewal cannot exceed the seven-day absolute default; a fresh sign-in is then
required. Every private HTTP request checks expiry and that the user and matching
identity still exist. Live sockets recheck at least every five seconds, including
after logout or expiry; refresh/logout state is shared across workers.
Restart gateways after editing configuration: removal of a user/login identity,
or changing its password hash, invalidates old sessions against the new config.
Recorded audio, uploads, session listing and resumed/new server connections are
scoped to the authenticated user. Browser-supplied `X-CF86-User-ID` is ignored;
only the gateway sends that trusted assertion upstream.

### Telegram

The Telegram gateway uses the `telegram_gateway` section of `config.yaml` for
its bot token, Telegram API ID and API hash. Access is configured separately in
the top-level `users` list:

```yaml
users:
  - user_id: guest
    identities:
      - type: guest
      - type: telegram
        account_id: "123456789"
  - user_id: player2
    identities:
      - type: telegram
        account_id: "987654321"
```

User IDs are stable storage names, not Telegram usernames. Each numeric Telegram
account ID must belong to exactly one user; duplicate users or identities are
configuration errors. Several accounts can share a user, including `guest`, and
can then list and resume that user's games. A `guest` identity is allowed only
under `user_id: guest`; it never authorizes unlisted Telegram accounts.
Omitting `users` defaults to a guest user with no authorized Telegram accounts.
An explicit `users: []` also rejects all Telegram accounts.

Send the bot a private message to have a rejected account ID written to its log;
then add a Telegram identity for that ID and restart the gateway. The former
`telegram_gateway.allowed_accounts` setting is no longer accepted.

Private text messages and voice notes are
accepted. Voice notes are transcribed with the configured ElevenLabs key and
forwarded silently. When that key is available, the first companion transmission
is a captioned voice note; later replies match the player's most recent input —
voice answers voice, and text answers text. Replies too long for a Telegram
caption, and replies whose synthesis fails, are sent as separate text so no
content is lost.

### Zello

The Zello gateway is a long-running, voice-only multi-channel service. One
`cf86-zello` process joins every channel named by a Zello identity in `users`,
with an independent Zello connection, server connection, and shared game per
channel. Configure each channel's contributors under the user who owns its game:

```yaml
users:
  - user_id: andrew
    identities:
      - type: zello
        channel: "Cabin Fever x86"
        account_id: "your-zello-username"
      - type: zello
        channel: "Cabin Fever x86"
        account_id: "another-contributor"
      - type: zello
        channel: "Another game"
        account_id: "your-zello-username"

zello:
  credentials_file: ~/.apikeys/zello.yaml
```

Zello `account_id` is the sender name supplied by Zello, matched without regard
to case, not a numeric account ID. Channel names are case-sensitive after
trimming surrounding whitespace. Permission is specific to the
channel/sender pair; an account authorized on another channel cannot contribute.
All contributors for a channel must map to one internal user (including `guest`
if desired). Conflicting owners and duplicate pairs are configuration errors.
Without any configured Zello identities, the gateway refuses to start instead
of opening a guest game. Multiple channels may share an owner, but each keeps
its own game; replies stay in the originating channel.

Other people who can join the channel can listen to replies. Their voice messages
are ignored before recording, transcription, or forwarding to the game; their
speech may still be heard by other channel listeners. Text is always ignored.
The gateway does not manage channel membership or make a private channel public.

Each channel automatically resumes its saved session on startup. A new game is
started only when that channel has no saved association under its current owner.
Invalid state, unavailable sessions, and connection failures are explicit errors:
the gateway stops and closes all channels rather than silently replacing a game.
Ctrl-C also closes every channel. There are no `--resume` or `--list-sessions`
options; `--help`, `--config`, `--host`, and `--port` remain available.
Credentials are read from the YAML file named by `zello.credentials_file`.
The former `zello.channel` and `zello.authorized_users` settings are not accepted.

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
A session belonging to another user is reported as nonexistent. Telegram sends
the configured user ID on every server connection, including session listing and
resuming. Its transcripts and audio live in
`data/users/<user_id>/sessions/<session_id>/telegram_gateway/`; account-to-session
associations live in `data/users/<user_id>/telegram_gateway/sessions.json`.
Moving an account to another user does not move its saved games or remembered
session.

Zello sends the shared owner's user ID on every server connection and stores
transcripts and audio in
`data/users/<user_id>/sessions/<session_id>/zello_gateway/`. Channel-to-session UUID
associations are stored atomically in
`data/users/<user_id>/zello_gateway/sessions.json` relative to the gateway's working
directory. Keep this state across restarts. Moving a channel to another owner
does not move its saved association or games. If saved state is invalid or the
server cannot resume a session, repair the state or restore the server data
(an interrupted write is reported via a leftover `sessions.tmp` file);
the gateway never falls back to a new game. Removing a channel's association
explicitly allows a new game on its next startup.

The web gateway sends the authenticated user's ID on every server connection,
and stores transcripts and audio in
`data/users/<user_id>/sessions/<session_id>/web_gateway/`. The text client sends
no header and still stores its fixed guest data in
`data/users/guest/sessions/<session_id>/text_client/`. Downloaded games remain
shared in `data/games/`. No data migration is performed.

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
