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
./nethack-run serve --state runs --host 127.0.0.1 --port 8756
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
make up                           # starts in the background at http://127.0.0.1:8756
make logs
make down                         # stops the server; recordings persist
```

If needed, `NETHACK_CONTAINER_ENGINE` selects another compatible executable;
set it consistently for Make targets and direct CLI commands. No rootful daemon
is required by the default Podman workflow.

`make image` runs the full integration suite during the build and records the
local content-addressed image ID in `build/image-id`. `make up` passes that ID
to the server and stores attempts in the `nethack-runs` Podman volume. An image
intended for distribution should use its registry `repository@sha256:...`
reference as `NETHACK_IMAGE_DIGEST`, and that exact reference must be added to
`config/official-images.json` after its checks pass.

Pushing a `v*` tag runs `.github/workflows/image.yml`, which builds the image and
publishes it to `ghcr.io/bitplane/menacebench` (the package must be public so
results CI can pull it). The job summary prints the `ghcr.io/...@sha256:...`
reference. Add it to `config/official-images.json`, then deploy that exact image
with `make pull up IMAGE=<reference>`: `make pull` records the registry digest in
`build/image-id`, so new runs carry a digest anyone can pull and replay.

`make up` replaces the named `nethack-run` container with the current built image,
retaining its volume. Running it again restarts the service and applies updated
login configuration. `make run` is the foreground alternative. `PORT=8756` is the
default host port; the container listens on 8000 internally. Only loopback is
published. Changing `PORT` also requires updating the OAuth origin/callback.

Replay automatically selects the recorded image when necessary. It permits only
tracked official image references or the locally built image ID, mounts the
package read-only, disables network access, and sets time, memory, CPU and
process limits. A diagnostic comparison against another build is available via
`--diagnostic-cross-build`; it reports reproduction without claiming official
verification.

## Browser login

The dashboard at `http://127.0.0.1:8756` lets anyone with a GitHub account view
ongoing and completed attempts, current screens, the leaderboard, and replays,
and play NetHack themselves: **Play NetHack** starts your game in an in-page
terminal, or continues it. Each account has one unfinished game at a time; quit in
the game (`#quit`) to start over. Refreshing or opening another window moves
control to the newest window. Human games are ordinary attempts with
verifiable packages and replays.
Run owners can pause/resume their own games and download their trace packages;
configured server admins can manage every run. Select a completed attempt to play its asciinema recording, seek,
change playback speed, or download the `.cast` file. Recordings are prepared on
first viewing and cached, including for attempts completed before the viewer
was added. Browser sessions are separate from bot player tokens. The UI
never returns live seeds, token hashes, or private snapshots.
The public landing page has separate human sign-in and agent-start instructions.

1. [Register a GitHub OAuth app](https://github.com/settings/applications/new)
   with homepage `http://127.0.0.1:8756` and callback
   `http://127.0.0.1:8756/auth/github/callback`.
2. Copy `config/github-auth.example.json` to `config/github-auth.local.json`,
   set its permissions to `600`, and fill in the client ID and client secret.
   Set `owner_ids` to your numeric GitHub account ID, available from
   `https://api.github.com/users/YOUR_LOGIN`. The checked-in example uses this
   workspace owner's account. Keep `public_url` equal to the browser origin,
   without a trailing slash.
3. Run `make image` then `make up`, open the dashboard, and sign in.

The local credentials file is ignored by Git and container builds. Podman
mounts it as a secret readable by the container's non-root user. Override its
location with `GITHUB_AUTH_CONFIG=/absolute/path/to/private.json`. For native
development, use `./nethack-run serve --github-auth config/github-auth.local.json`.
Without configuration, the game API remains available but browser login is disabled.

Login uses GitHub's authorization-code flow with state and PKCE, requesting
`public_repo` so a signed-in person can submit results as themselves. GitHub asks
for consent at first login, or at the next login when upgrading an existing grant.
This permission covers public repositories the user can access, not only the
results repository; no private-repository or email scope is requested. Any valid GitHub account receives viewer access;
the configured numeric account IDs in `owner_ids` receive server admin access.
`GET /dashboard/me` reports `role` as `viewer` or `admin`. Viewer replay preparation
still requires CSRF protection. Login alone does not grant a player token or
permission to control other people's games. Signed-in people can start their own
browser game. Browser cookies are HttpOnly and SameSite=Lax (also Secure on
HTTPS); mutations require a same-origin CSRF token. Sessions expire after seven
days and are cleared by server restart or sign-out. GitHub access tokens are
kept only in the server-side session, never in cookies, browser responses, run
traces, packages, or on disk. Expired or revoked credentials require signing in
again. OAuth codes and secrets are not included in run traces.

For access over SSH/Tailscale, forward the port (for example,
`ssh -L 8756:127.0.0.1:8756 YOUR_SERVER`) and open `http://127.0.0.1:8756`
on the client machine. GitHub's loopback callback then returns through the same
forward. Opening `localhost` is also supported: sign-in first redirects to the
configured origin so its state cookie and callback use the same hostname.
If the server restarts during sign-in, start again from the main page instead of
refreshing the old callback URL.

## Give an agent the URL

For Luna or another agent running on the machine with the SSH forward, use:

> Open http://127.0.0.1:8756, read the agent instructions, create one recorded
> attempt, and try to win NetHack by ascending. Continue until ascension or death;
> do not quit or escape to end the task. Report your actual tool/model/reasoning-effort metadata,
> the attempt ID, and the result. Use only the player API.

The agent reads `/player.md` and calls `POST /agent/start` with its metadata.
The response supplies a player token and relative attempt path, but no owner
token or seed. It can then read screens, send keys, pause, and resume. You can sign in
and watch the same attempt on the dashboard. Use a base URL reachable from the
agent's own machine; `localhost` always refers to the machine running that agent.
Contributor authentication and concurrent-attempt limits apply to agent starts.
`POST /attempts` remains the owner-oriented API for existing integrations; agents
are instructed to use `/agent/start` instead.

## Where recordings live

Container runs persist in the `nethack-runs` Podman volume, mounted at `/runs`.
Find its host location with:

```sh
podman volume inspect nethack-runs --format '{{.Mountpoint}}'
```

Each attempt has an ID-named directory containing `events.jsonl` (consumed keys,
checkpoints and lifecycle/audit events), `private.json` (seed and token hashes),
`work/`, and immutable `snapshot-N/` session directories. After the first finished
package download, `package/` contains `attempt.json` and `events.jsonl.gz`.
Later requests are audited separately in `audit.jsonl`. `make down` and container
replacement preserve this volume. Native runs use `--state` (default `runs/`).

The dashboard caches gzip-compressed asciicast v3 recordings as
`/runs/<attempt-id>/replay.cast.gz`, with reproduction details in `replay.json`.
Recordings are only a cache: they are rebuilt from the event log on demand and
pruned by `--cast-ttl` (default one week unwatched), `--cast-cache-bytes` (default
2 GiB in total) and `--min-free-bytes` (default 2 GiB of free disk). Older plain
`replay.cast` files are compressed the next time they are watched. Finished
attempts are listed from a rebuildable SQLite index, `/runs/.index.sqlite`, and are
only loaded into memory while in use. It reconstructs terminal output from
the durable recorded inputs, comparing every checkpoint and final result before
publishing the file. Generation runs off the request loop, one recording at a
time. Older image recordings are allowed only when they reproduce under the
running build; their sidecar marks this as a cross-build diagnostic rather than
verification in the original image. On divergence, use CLI export in the
original recorded image instead.

Watching does not finalize the trace package or freeze its usage report. The
asciinema player is bundled locally: no recordings are uploaded to asciinema.org
and playback does not require a CDN. Thinking gaps are shortened to two seconds
in the viewer; the downloaded recording retains its recorded timing.

`nethack-run export` also produces standalone recordings; the demos write them
under `build/demo/` and `build/demo-save/`. Trace-package downloads still contain
only the two package files, not recordings or the private working directory.

## Play through the API

Create an attempt:

```sh
curl -sS -H 'Content-Type: application/json' http://127.0.0.1:8756/attempts \
  -d '{"player_type":"model","agent_tool":"Codex","model":null,"reasoning_effort":null}'
```

The response contains `id`, `owner_token`, and `player_token`. Give the agent
only the player token and the attempt URL. Instructions are in [PLAYER.md](PLAYER.md)
and at `GET /player.md`.

Model metadata accepts optional `reasoning_effort`: a string such as `"high"`,
`"xhigh"`, or another provider-specific setting. Use null when unknown; agents
must not infer it from the model name. It is preserved in the creation event and
`attempt.json` under `player_metadata.reasoning_effort`, and shown in the
dashboard. Existing packages without this field remain valid and unchanged.

Each screen response contains 24 text lines followed by a JSON status line.
Use its `session` and `boundary` in a key request. A 409 returns the current
published screen; it never repeats previously consumed keys. Keys are non-NUL
bytes represented as JSON characters, with a maximum of 4096 per request.

For a human terminal, set `NETHACK_OWNER_TOKEN` and run:

```sh
./nethack-run play http://127.0.0.1:8756/attempts/ATTEMPT_ID
./nethack-run watch http://127.0.0.1:8756/attempts/ATTEMPT_ID
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
`--concurrency 100` (the default) limits running games per contributor; without a
contributor file it limits running local games in total. Paused and saved attempts
free their slots. Creation and all resume paths return a capacity error when full;
existing attempts and tokens remain valid. Use `make up CONCURRENCY=100` to set
this limit for the container. Players can POST `/attempts/ID/pause` with their own
token when blocked, then resume the same attempt later.

Configure `--instance`, `--idle` (seconds without player input), `--duration`
(active seconds per session), `--events` (event-count threshold that pauses play),
and `--expiry` (seconds left paused or saved before abandonment). Defaults are
1800, 86400, 1000000, and 604800 respectively. A duration/event limit remains in
force on resume. Audit records continue to be retained after play is capped.
Use an HTTPS reverse proxy when exposing the server remotely.

## Verification

```sh
make test
make demo                         # requires a running server on port 8756
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

## Release metadata

Attempts record `harness_version` separately from `schema_version` (the package
format), the game build identity, and container image digest. The harness version
is captured at creation and survives restarts and exports. Existing unversioned
packages remain readable. Release `v0.0.1` identifies commit `69e794c`; new
attempts read `[project].version` from `pyproject.toml` (or installed package
metadata). There is no separate runtime version constant to maintain.

Use `make bump` for the next patch release, or `make bump PART=minor` / `major`.
This installs the pinned Bump My Version tool in `build/release-tools`, updates
`project.version`, commits the change, and creates a matching `vX.Y.Z` tag. It
requires a clean working tree. `make bump-dry` previews the changes. Nothing is
pushed automatically. Build and deploy the release with `make image && make up`;
an already running server keeps its existing image and recorded attempts retain
the version they started with.

To backfill legacy data, stop the server and run
`PYTHONPATH=src .venv/bin/python scripts/backfill_version.py STATE 0.0.1 BACKUP`.
The tool locks the store, backs up every affected file, updates creation records
and any existing package checksums, and preserves seeds, tokens, and game events.
Keep the backup private: it contains the original private attempt data.

## Submitting results

Finished runs are published through pull requests to
[`bitplane/menacebench-results`](https://github.com/bitplane/menacebench-results),
whose CI checks each package and builds the public site at `menace.bitplane.net`.
The server opens these pull requests as the signed-in person using the authorization
already granted at GitHub login. There is no personal-token file or separate
submission setup. Existing users sign in again to grant `public_repo`. For another
results repository, pass `--results-repo OWNER/NAME` to the server.

A run's owner or an admin sees **Submit to results** on a finished run. Submission
finalizes the package (freezing its usage report, like a download). If the person
has write access, the server adds branch `attempt/<id>` to the results repository;
otherwise it creates or reuses their fork. It adds only
`attempts/<id>/attempt.json` and `events.jsonl.gz`, then opens a PR into the results
repository. Fork preparation may require retrying shortly. Repeated submissions
return the existing PR; conflicting branches are never overwritten.

The PR is authored by the person who clicks Submit. An admin submitting another
person's run acts as the admin; player identity and run ownership stay unchanged.
Login itself does not create a fork or publish anything. Authorization lost through
expiry or revocation sends the person back through GitHub login; they then click
Submit again. Restarting or signing out clears the server's copy of the credentials.
Old `RESULTS_TOKEN` configuration is no longer used and `--results-token` has been
removed; remove any old local token file and revoke its token in GitHub.

The results CI uses two commands from this repository. `nethack-run check` classifies
packages: **verified** when replayed in their recorded official image (network off,
read-only), **unverified** when recorded with a development build or an image not in
`config/official-images.json`, and **failed** otherwise; with `--results DIR
--changed-from REF` it also rejects pull requests that change anything except new
`attempts/<id>/` package files or a `disqualified.json`. `nethack-run build-site`
writes the static site: the shared leaderboard (verified runs only, with the Humans
filter), every listed attempt with its status, and the disqualified page.

Before enabling public result submission for a release:

1. Publish the release image, make the registry package public, and add its exact
   `repository@sha256:...` reference to `config/official-images.json` in the
   checkout used by results CI. A local image ID is not a published reference.
2. At the scheduled restart, deploy that reference with `make pull up IMAGE=<reference>`.
3. Sign in with GitHub and submit a finished smoke-test run and confirm that results CI replays it and
   labels it verified before collecting benchmark runs for the public rankings.

## Leaderboard and guidebook

Games at `/` are grouped into Playing, Paused / saved, and Finished, with 25 games
per page. Finished games are sorted by score. Live arrivals can be revealed with
the new-games button. Filters and page numbers are shareable in the URL.
Open `/games/{id}` to watch, play, or replay an attempt.

The separate `/leaderboard` ranks groups by their highest recorded score.
Equal scores share a rank. Max moves is the largest NetHack turn count among
the group's completed scored games. Each record links to its own replay.
The version and human-player filters apply to this page. Models group by name
(ignoring case, spaces and punctuation) and reasoning effort. Humans group by
GitHub account. Scored quits and zero scores are included. The JSON endpoint
at `/dashboard/leaderboard` also retains the group's average score.

`GET /guidebook.txt` serves upstream's unmodified plain-text guidebook from the
pinned NetHack source, without authentication (player tokens also work). Agents
can download and grep it as described in `PLAYER.md`. `make setup` prepares the
file; existing source builds can run `python3 scripts/prepare_guidebook.py`.
The container includes this file and the NetHack license; Markdown conversion
is unnecessary.

## Run ownership and recovery

Both creation endpoints accept optional `owner_github_id`: a positive numeric
GitHub ID or null, separate from player metadata and contributor/quota identity.
Anyone creating an attempt can nominate an owner; doing so grants that account
rights over this run, not rights to the caller. Only GitHub authentication proves
the account's identity. This nomination does not award contribution credit.

The dashboard shows your GitHub ID, a **My runs** filter, the assigned owner,
and controls according to each run's `permissions`. Any signed-in user can still
view runs and recordings. The nominated owner and server admins can pause,
resume, export finished packages, and recover player access for unfinished runs.

`POST /dashboard/attempts/ID/recover-player` requires their browser session,
same-origin request, and CSRF token. It returns a fresh player credential once;
save the downloadable JSON privately. It replaces the old player token without
resuming, resetting, or replacing the game. Only hashes are stored. A lost
recovery response can be handled by recovering again. Tokens remain valid across
restarts unless explicitly replaced.

Only server admins can `POST /dashboard/attempts/ID/owner` with
`{"owner_github_id":123}` (or null to remove ownership). This supports old unowned
runs and corrections. Changing the owner revokes both existing player and legacy
owner bearer tokens and disconnects legacy terminal clients. The new owner then
recovers a player token. Assigning the same owner is a no-op. Ownership changes
are recorded with the actor, previous owner, new owner, and time; recovery records
the actor, credential generation, and time. These records and current hashes are
committed together in the private attempt file. No raw credential is logged.

Already accepted API actions finish before credentials change; later requests
must authenticate against the new hashes. Administrative changes are separate
from the gameplay journal and never rewrite finalized packages. New journals
and packages record `initial_owner_github_id`; current ownership belongs to the
live server's control data. Old traces without this field remain valid and old
attempts remain unowned until assigned. Keep the upgraded server when using
these per-run permissions; older versions do not implement them.

Human browser games stay owned by the account that started them and cannot be
reassigned, including by admins. Signed-in people can play from the dashboard.
Contribution accounting remains a separate follow-up.
Ownership is neither player identity nor proof of token usage or payment.

## Deleting failed runs

Server admins have a **Delete run** button with an explicit confirmation.
`POST /dashboard/attempts/ID/delete` requires an admin browser session and CSRF
protection; run owners, viewers, and bearer tokens cannot delete runs. Deletion
waits for accepted API actions and replay generation, stops a running game,
revokes its credentials, and removes its directory, packages and cached replay.
Other attempts are unaffected. Deleted attempts disappear from leaderboards.

A durable `.deleted/ID.json` record retains only the deleted attempt ID, admin
GitHub ID, action and time. This record is written before removal, preventing a
crash or filesystem cleanup failure from resurrecting the attempt. Failed file
cleanup is retried after restart and periodically while running. The API's
`files_removed` field distinguishes completed file removal from pending cleanup.
External backups are managed separately.
