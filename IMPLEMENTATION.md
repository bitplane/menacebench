# Implementation acceptance status

The phase 1–7 implementation and acceptance checks are **complete**. The
rootless Podman build, live TCP/WebSocket tests, exact-image API demonstrations,
terminal clients, replay rendering, and ARM replay comparisons have passed.
Podman is the default runtime. Phase 8 (public deployment/results site) remains
outside this work.

## Implemented

- Pinned NetHack 5.0 commit `16ff59115315917b93185d026aeefea06db9b0f4`,
  hash-checked source/Lua archives, tracked patch series, reproducible native
  setup, build diagnostics, and a pinned Dockerfile.
- Input boundaries, consumed-key acknowledgments, distinct exit messages,
  game-generated character metadata, fixed date/seed, disabled privileged modes.
- One engine for live play and deterministic rebuilding; verification/export
  use that same process driver and boundary protocol.
- Owner/player credentials, serialized attempt control, key-position rejection,
  request auditing (including probing unrelated routes), usage reporting,
  contributor credentials and per-contributor limits.
- Session snapshots, save/restore, pause/resume, crash reconciliation,
  idle/duration/event limits, expiry, durable event logging, atomic packages.
- Package verification, manifest consistency checks, asciicast v3 export,
  image dispatch with an allowlist and resource limits.
- Terminal control/watch WebSockets and CLI clients; player and operating docs.

## Checks completed here

The pinned amd64 image passed the full **105-test** suite in 44.65 seconds with
real TCP/WebSocket transport and worker threads. The native live API corpus
passed **27 tests** in 22.61 seconds, covering all races/genders/alignments and a
long run through death, with mid-name and inventory-menu pause/recovery.
API packages are replayed in separate interpreter processes.

- Fresh source build using `scripts/setup.py`; build identity checks using
  `scripts/verify_build.py`.
- Real-game repeatability, distinct seeds, role/race/gender/alignment selection,
  save/restore, a long run ending in death, kill/rebuild and divergence detection.
- Fault injection at key persistence, save completion, snapshot publication,
  and package publication; a torn record; simulated loss of unsynchronized
  writes; concurrent pause interrupting a queued batch.
- Screen polling during checkpoint synchronization retains the previously
  published frame and reports that input is still advancing until sync finishes.
- In-memory HTTP/WebSocket protocol tests against real game processes, including
  authorization, role menus, save/resume, discarded type-ahead, stale positions,
  limits, expiry, and terminal control versus watchers.
- Native demonstration package at `build/protocol-demo`, replayed and exported
  by separate CLI processes. Its manifest explicitly records a null image digest.

`NETHACK_TEST_LIVE=1` enables real TCP transport and worker threads; the image
build always uses it. The default native tests retain the in-memory transport
for restricted environments. Concurrent interruption also uses real threads.

- `make image`: pinned Debian main/security snapshots, serial upstream Lua build,
  non-root runtime asset ownership, and a startup probe as container UID 1000.
- `make demo`: real API creation, inputs, pause/resume, quit, usage, package
  download, then replay and export in its exact recorded image. Both commands
  reported `verified: true`, `environment: pinned-image`. Artifacts are in
  `build/demo`; output is in `build/demo-results.txt`.
- `scripts/demo.py --save-resume --output build/demo-save`: a second live API
  demonstration saves and restores into session 2, then quits. Exact-image
  verification and export both passed (42 consumed keys). Output is in
  `build/demo-save-results.txt`.
- `scripts/terminal_smoke.py`: real PTY input, Ctrl-] disconnect, simultaneous
  watcher, and final quit output. This exposed and now covers the watcher-status
  bug: every durable terminal publication now includes status for every client,
  including moves submitted over HTTP. Output is in `build/terminal-results.txt`.
- Export opened and rendered successfully in the official asciinema-player
  3.18.0. Local preview assets and page are in `build/demo`; they are not published.

## Local browser and agent onboarding

The server now provides a public human/agent landing page, GitHub owner login,
and a sessions dashboard. Owner access uses numeric GitHub account IDs, PKCE,
browser-bound single-use OAuth state, HttpOnly cookies, and CSRF protection.
The sign-in entry normalizes localhost aliases to the configured callback origin.
The user confirmed real GitHub login works through the SSH-forwarded
`http://127.0.0.1:8756` address.

`make up` starts/replaces the named rootless container in the background on port
8756; `make down` preserves the existing recordings volume. OAuth configuration
is excluded from Git and image builds and mounted using a Podman secret.

The main page links `/player.md`. Agents can use `POST /agent/start` to receive
only a player token and attempt path; they do not need human login or owner
credentials. Tests cover contributor limits, owner-route rejection, invalid
login state, expiry, wrong accounts, and cross-origin mutations. A live URL-only
onboarding smoke test also created, played, and finished an attempt successfully.

## Completed-game replay viewer

The dashboard prepares and caches an asciicast v3 recording when a completed
attempt is selected. Tests cover cache reuse, mutable usage after viewing,
old-image diagnostic reproduction, refusal of divergent/unfinished recordings,
single-flight preparation, and owner-only serving/downloads. The player assets
are vendored from asciinema-player 3.18.0 with its Apache 2.0 license.

An isolated browser check exercised loading, playback, seeking, and pausing
without adding runs to the live dashboard. A private copy of Luna's completed
run reproduced all 152 consumed keys and its quit outcome. Its cached recording
is in `build/recording-check/luna/replay.cast`; this validation did not modify
the live attempt or finalize its package.

## ARM comparison

The host has no ARM binfmt handler, so the ordinary container ARM build fails
with `Exec format error`. The workspace-only alternative builds the same pinned
source/configuration inside the pinned ARM base filesystem using PRoot/QEMU.
It downloads and extracts its tools under `build/arm-tools`, without installing
host packages or changing binfmt settings. `PROOT_NO_SECCOMP=1` disables an
optional PRoot acceleration mode that failed on this host.

The generated game is an ARM aarch64 ELF executable. Both `build/demo` (one
session, 36 keys) and `build/demo-save` (two sessions, 42 keys) replayed with
matching checkpoints and final results. The reusable helper confirmed the ARM
runtime and build inputs before each replay. Results correctly report
`reproduced: true`, `verified: false`, and `environment: cross-build-diagnostic`:
ARM comparison is outside the exact-image verification contract.

Logs: `build/arm-user-results.txt`, `build/arm-one-results.txt`, and
`build/arm-save-results.txt`. Evidence records in
`build/arm-tools/comparison-*.json` identify the source, base image, executable,
emulators, and compared event-log digests.

Reproduce the complete emulated build with
`make cross-platform-user-test PACKAGE=build/demo`; repeat against the existing
validated build with `python3 scripts/arm_user_test.py build/demo --replay-only`.
The normal `make cross-platform-test` remains available on ARM-capable runtimes.

## Acceptance coverage

| Phase | Evidence |
| --- | --- |
| 1: pinned game and deterministic engine | Hash-checked build; engine repeatability, save/restore, kill/rebuild, role/race and long-run/death tests |
| 2: API and recorder | Live HTTP lifecycle, position/retry checks, authentication and consumed-key durability tests |
| 3: lifecycle and recovery | Pause/resume, expiry, final-key recovery, snapshot and package fault injection, torn/unsynchronized log tests |
| 4: replay and cross-platform corpus | Separate-process API package replay; exact-image demos; ARM one- and two-session comparisons |
| 5: instructions, metadata, usage, audit, export | PLAYER.md and README; manifest/usage/tampering/probing tests; rendered asciicast v3 export |
| 6: human clients | Live WebSocket tests and actual PTY play/watch smoke test |
| 7: hosted limits | Contributor authentication, concurrency, idle/duration/event and expiry tests |

No official image has been registered, no remote repository/site has been
created, and no result has been submitted. Phase 8 remains outside this goal.
