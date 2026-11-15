# nethack-run

A NetHack 5.0 input-boundary recorder, HTTP run server, and replay verifier.
The behavior and implementation scope are specified in [DESIGN.md](DESIGN.md).

The native and pinned amd64 image builds pass real-game replay/recovery and live
TCP/WebSocket tests. The live API demo verifies and exports its package in the
exact recorded image; the real play/watch clients are exercised through a PTY.
ARM replay comparisons also pass for one-session and save/restore packages using
workspace-only user-space emulation. See [IMPLEMENTATION.md](IMPLEMENTATION.md)
for acceptance evidence.
Native packages identify themselves as `native-development`, with a null image digest.

## Native development

Use Linux with Python 3.12 for the locked development environment, GCC, make,
git, curl, bison, flex, ncurses development files, and groff. The Python source
supports Python 3.10+, but the supplied wheel hashes target Python 3.12 Linux
amd64 and arm64. On Debian/Ubuntu the build packages are:

```sh
sudo apt-get install gcc make git curl bison flex libncurses-dev groff bsdextrautils python3-venv
make venv                         # or VENV_PYTHON=/path/to/python3.12
make setup
make verify-build
make test
NETHACK_TEST_LIVE=1 make test      # requires local socket access
```

`make setup` verifies SHA-256 hashes of the pinned NetHack commit archive and
Lua 5.4.8, applies the tracked patches in an isolated generated checkout, builds,
and installs under `build/install`. It replaces the generated source tree.
`build/build.json` records the source, compiler, patches, options and executable
identity. `make verify-build` checks those files against the recorded build.

For this workspace, the ncurses headers were extracted under `build/deps` from
the matching Ubuntu `libncurses-dev` package without modifying the host. The
setup script uses those headers if present. A normal development host should
install its own ncurses development package instead.

Start a development server:

```sh
./nethack-run serve --state runs --host 127.0.0.1 --port 8000
```

Alternatively, `--unix-socket /tmp/nethack.sock` selects a Unix listener. Curl
then uses `--unix-socket /tmp/nethack.sock http://localhost/...`.
The state directory is locked against a second server and must be on storage
that honors `fsync`. Attempt directories contain private seeds and token hashes.

## Container environment

Podman is the default container engine for build, run, replay, and export.
The Dockerfile pins the base image by digest and Debian build packages through
a dated package snapshot. Python dependencies are version- and hash-locked.
The game runs as UID 1000. Build and start it with:

```sh
make image
make run
```

If needed, `NETHACK_CONTAINER_ENGINE` selects another compatible executable;
set it consistently for Make targets and direct CLI commands. No rootful daemon
is required by the default Podman workflow.

`make image` runs the engine and recovery tests during the build and records the
local content-addressed image ID in `build/image-id`. `make run` passes that ID
to the server and stores attempts in the `nethack-runs` Podman volume. An image
intended for distribution should use its registry `repository@sha256:...`
reference as `NETHACK_IMAGE_DIGEST`, and that exact reference must be added to
`config/official-images.json` after its checks pass. That allowlist is currently
empty; this checkout does not claim an official published image exists.

Replay automatically selects the recorded image when necessary. It permits only
tracked official image references or the locally built image ID, mounts the
package read-only, disables network access, and sets time, memory, CPU and
process limits. A diagnostic comparison against another build is available via
`--diagnostic-cross-build`; it reports reproduction without claiming official
verification.

## Play through the API

Create an attempt:

```sh
curl -sS -H 'Content-Type: application/json' http://127.0.0.1:8000/attempts \
  -d '{"player_type":"model","agent_tool":"Codex","model":null}'
```

The response contains `id`, `owner_token`, and `player_token`. Give the agent
only the player token and the attempt URL. Instructions are in [PLAYER.md](PLAYER.md)
and at `GET /player.md`.

Each screen response contains 24 text lines followed by a JSON status line.
Use its `session` and `boundary` in a key request. A 409 returns the current
published screen; it never repeats previously consumed keys. Keys are non-NUL
bytes represented as JSON characters, with a maximum of 4096 per request.

For a human terminal, set `NETHACK_OWNER_TOKEN` and run:

```sh
./nethack-run play http://127.0.0.1:8000/attempts/ATTEMPT_ID
./nethack-run watch http://127.0.0.1:8000/attempts/ATTEMPT_ID
```

The first `play` connection controls input; other connections watch. Ctrl-]
disconnects the controlling terminal. Resize does not resize the game: keep
an 80×24 viewing area. A controlling client resumes a paused/saved session when
it next sends input. `watch` never sends game input.

An owner can POST `/attempts/ID/pause` and PUT `/attempts/ID/usage` with optional
`input_tokens`, `output_tokens`, and `cost`. Token counts are integers; cost is
a finite nonnegative number. Values can be null. Report usage before the first
package download; that download freezes the package. Later requests are audited
separately and cannot mutate it.

The finished package endpoint returns a tar containing exactly `attempt.json`
and `events.jsonl.gz`. Extract it into a directory, then run:

```sh
./nethack-run replay path/to/package
./nethack-run export path/to/package --output replay.cast
```

The exported file uses [asciicast v3](https://docs.asciinema.org/manual/asciicast/v3/)
and can be viewed with a compatible asciinema player (tested with
[asciinema-player 3.18.0](https://github.com/asciinema/asciinema-player/releases/tag/v3.18.0)).
Older players may reject asciicast v3. Replay omits think time;
export reconstructs pacing from recorded key timestamps.

## Hosted configuration

Pass `--contributors contributors.json` to require contributor credentials for
attempt creation. The file maps SHA-256 hashes of contributor bearer keys to
contributor names. This is separate from per-attempt owner/player credentials.
`--concurrency 2` limits unfinished attempts per contributor; without a
contributor file it limits unfinished local attempts in total.

Configure `--instance`, `--idle` (seconds without player input), `--duration`
(active seconds per session), `--events` (event-count threshold that pauses play),
and `--expiry` (seconds left paused or saved before abandonment). Defaults are
1800, 86400, 1000000, and 604800 respectively. A duration/event limit remains in
force on resume. Audit records continue to be retained after play is capped.
Use an HTTPS reverse proxy when exposing the server remotely.

## Verification

```sh
make test
make demo                         # requires a running server on port 8000
.venv/bin/python scripts/terminal_smoke.py  # real PTY play/watch clients
make cross-platform-test PACKAGE=build/demo
# Without host ARM/binfmt support (Debian/Ubuntu amd64):
make cross-platform-user-test PACKAGE=build/demo
```

The demo creates and plays an attempt through HTTP, pauses/resumes it, quits,
downloads its package, verifies it in its recorded environment, and writes
`build/demo/replay.cast`. It does not submit or publish anything.
The container ARM test needs a Podman runtime with ARM execution support. The
user-space alternative downloads PRoot/QEMU packages into `build/arm-tools`,
exports the pinned ARM base filesystem with Podman, and builds there using the
same pinned Debian repositories. It does not install host packages or register
binfmt handlers. It requires network and unprivileged `ptrace` access and is
slower than native execution. After a successful build, repeat just the replay
with `python3 scripts/arm_user_test.py build/demo --replay-only`.
Cross-build comparison is diagnostic and remains outside the official
verification contract.

The test corpus includes character selection, movement, menus, a long run to
death, save/restore, interrupted batches, retry rejection, process recovery,
torn log writes, simulated loss of unsynchronized writes, snapshot/package
publication failures, manifest tampering, HTTP authorization, terminal control,
and export. HTTP/WebSocket protocol tests use an in-memory byte transport and
inline workers so they run without socket permissions. They do not establish
that the live socket deployment works; the HTTP demo is a separate required check.

See [IMPLEMENTATION.md](IMPLEMENTATION.md) for the current acceptance status.
NetHack and the upstream recorder patches retain their original notices and the
[NetHack license](LICENSE-NetHack). Patch provenance is in
[patches/ORIGIN.md](patches/ORIGIN.md).
