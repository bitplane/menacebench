# NetHack Run Harness

## Purpose

This project lets human players and model-driven players play the same terminal NetHack build, records each attempt as a seed plus the keys the game consumed, reproduces any attempt from that record, and exports watchable replays. The game runs behind an HTTP API. Model players are agent tools (Claude Code, Antigravity, and similar) that play by calling the API with `curl`. Results are submitted on trust among known contributors and checked by replay.

Everything lives in one repository, `nethack-run`: the game build, API server, recorder, verifier, and replay exporter. The harness is written in Python 3.10+ and tested with pytest. A results repository and public site are the last implementation phase.

## Game build

`nethack-run` pins an official NetHack 5.0 source commit, a patch series, build tools, configuration, and build profile. `make setup` fetches the source into a generated directory, verifies the commit, applies the tracked patches in order, and builds the executable. The source checkout and binaries are ignored by Git. The server and game ship as one pinned Linux container image, which is the official run environment for players and CI. A local content-addressed image ID can be used during development; distributed images use their registry digest reference. Native development packages explicitly have a null image digest and do not satisfy the official verification contract. The image digest is the build identity: it is recorded in every run package, and the verifier replays a package in the image with that digest. `make verify-build` reports the image digest along with the source commit, patch-set digest, options digest, compiler identity, and executable digest for diagnosis.

Replay outside the image (including on ARM) is covered by a cross-platform test and fixed as divergences are found. It is not part of the verification contract.

The patch series provides:

- a harness-supplied RNG seed, applied at the start of every session
- a fixed game date and time, chosen so that moon phase and Friday-the-13th effects are inactive
- stable behavior for platform-dependent operations such as sort order
- an input-boundary hook. Boundary *n* is the game's *n*th key read in a session, numbered from 1. When the game blocks at boundary *n* it reports *n* and the 80×24 screen text and cursor position as they stand before the key is consumed; it then reports the key it consumed. After consuming that key, the next input boundary is *n + 1*, unless the game exits first
- explore mode, wizard mode, shell escape, suspend, and mail disabled

NetHack options are pinned in a tracked configuration, with bones disabled. The terminal is 80 columns by 24 rows with a fixed terminal type and locale, and cannot be resized during a session. Character creation is part of play: the player supplies the ordinary role, race, gender, alignment, and name inputs.

## Attempts and sessions

An attempt is one game, from character creation to death, quit, or ascension. Voluntarily escaping the dungeon without ascending is classified as `quit`, with the game's escape record preserved in the result. It consists of one or more sessions. A session runs from a game start or restore until the game exits (save, death, quit, ascension). It is normally one NetHack process, and more than one if it is paused and rebuilt.

The server generates a random seed when an attempt is created; the API has no way to supply one. The RNG seed for session *n* is derived from the attempt seed and *n*, so restored sessions are deterministic regardless of what the save file contains.

Each attempt has a state directory on the server, holding the seed, the event log, and a clean game directory with the save file between sessions. Scores, saves, and bones from other attempts are never visible to it. The seed and log are available only in the run package, after the attempt ends.

A session can be paused by player or owner request, idle timeout, duration limit, server shutdown, or a server crash. Pausing kills the game process and discards any queued keys that the game has not consumed; nothing is sent to the game and no save is written.

The server snapshots the game directory at the start of every session and never modifies the snapshot. Resuming a paused session discards the working game directory, copies the snapshot into a fresh one, starts a new process with the same session seed, and replays the session's recorded keys to rebuild the state, then continues at the next boundary. The durable event log is authoritative: a key the game consumed before a crash but that never reached the durable log did not happen. Pauses therefore never affect game state, and replay verification ignores them.

Recovery also handles a recorded key that causes a save or final exit before the session-end event becomes durable. Replaying the durable keys may reach either the next input boundary or a game exit. On exit, recovery regenerates the save or end-of-game record and completes the session-end transition exactly once; it does not invent another input boundary or accept more keys into that session. A saved session becomes resumable and a death, quit, or ascension ends the attempt. A new session cannot start until the preceding session-end event and the new session's immutable starting snapshot are durable. Missing working saves can be regenerated by replaying preceding sessions. Recovery must reconcile these transitions before expiry can mark an attempt abandoned.

Attempts left unresumed past an expiry period end with outcome `abandoned`.

## Deployment

`nethack-run serve` runs the server. The same server is used three ways:

- **Local**: the user runs the container on the machine where the agent runs.
- **Remote**: the user runs the container on another machine and gives the agent its URL.
- **Hosted**: the maintainer runs a public instance; contributors create attempts on it and run their own agents against it.

The API is the only interface to the game in every case. The server names its instance in each run package. A hosted instance requires a contributor key to create attempts, limits running games per key (100 by default), and keeps attempts, including abandoned ones, unless a server admin explicitly deletes them.

## API

Creating an attempt returns its ID, a random UUID that stays fixed for the life of the attempt, and two bearer tokens. The **owner token** is for the user: pausing sessions, reporting usage, watching, and fetching the run package. The **player token** is what the user gives the agent: it can only send keys, read the screen, pause, and resume.

```text
POST /attempts                    create an attempt and start its first session;
                                  body is player metadata; returns ID and tokens
GET  /player.md                   the instructions for model players

player token:
POST /attempts/{id}/pause         pause the current session and release its slot
POST /attempts/{id}/keys          queue keys, wait for the next idle boundary,
                                  return the screen
GET  /attempts/{id}/screen        return the current screen
POST /attempts/{id}/resume        start the next session after a save, or
                                  rebuild a paused session

owner token:
POST /attempts/{id}/pause         pause the current session
PUT  /attempts/{id}/usage         report token usage
GET  /attempts/{id}               status and result so far
GET  /attempts/{id}/package       the finished run package
WS   /attempts/{id}/tty           raw terminal stream; one controlling client,
                                  further clients read-only
```

`keys` takes a JSON body with a string of keys and the position the client last saw, which is a session index and the next key-read boundary number; control characters and Escape use ordinary JSON escapes. The server accepts the keys only if the game is blocked at that position with an empty queue. Otherwise it returns `409` with the latest published screen and state and changes no game state, so a request retried after a timeout cannot send keys twice and a request from an earlier session cannot match a later one. After a pause and resume, if the last durable consumed-key event names boundary *n*, the position is *n + 1*; with no consumed keys it is 1. If replay reaches an exit, there is no input position. A retry is therefore accepted only if none of its keys were recorded. Accepted keys are consumed one at a time at input boundaries, so type-ahead within one request simply queues. Keys still queued when the game exits are discarded.

The response is plain text: the 24 screen lines followed by a status line with cursor position, session index, boundary number, session state, and the number of keys consumed and discarded. When the game has exited there is no further boundary and no screen; the response carries how the game exited and the result instead. Screens are text only, without colour or attributes.

Model players use `keys`, `screen`, `pause`, and `resume`. Human players use `nethack-run play`, which connects a local terminal to the `tty` WebSocket as the controlling client. `nethack-run watch` connects read-only.

The public main page also links agent instructions and offers GitHub sign-in for
human viewers. An agent given only the base URL reads `/player.md`, then calls
`POST /agent/start` with player metadata. This creates an attempt subject to the
same contributor authentication and concurrency limits, returning only its player
token and relative attempt path. The owner token is not returned through this
bootstrap endpoint. An existing player token cannot create additional attempts.

The browser dashboard authenticates any GitHub account using OAuth state and
PKCE. All signed-in users can view attempts, scores, screens, and completed
replays. Configured numeric account IDs are server admins. Each run can also have a
nominated GitHub owner; its account must authenticate before managing that run. Browser sessions are separate from attempt tokens,
expire after seven days, and end on logout or server restart. Run owners can pause/resume and export their own attempts; admins can manage
all attempts. Other viewers cannot use those controls or export packages. Human
play is a future permission separate from viewing and owner administration.
Mutations require a same-origin CSRF token. Live seeds, private logs,
token hashes, and snapshots are not exposed. OAuth credentials remain outside the
image and repository; provider access tokens are used only to obtain identity.
The canonical browser origin is configured explicitly so SSH-forwarded loopback
login callbacks and cookies use the same hostname and port.

The server records each key at the moment the game consumes it, not when a client sends it. Crash recovery covers both process failure and machine failure on storage that honors `fsync`. Before publishing a boundary screen or exit, the server flushes and `fsync`s the events needed to reproduce it. Snapshots and atomically published packages likewise require file and parent-directory synchronization. Recovery ignores an incomplete trailing log record and uses the complete surviving event prefix.

This publication rule applies to `keys`, `screen`, `409` responses, and the `tty` WebSocket. Terminal output is buffered until the next input boundary or game exit and released only after the corresponding events are durable. While the game is advancing, reads return the latest published screen with a state indicating that it is not accepting input. Rebuilding a paused session does not stream replay output to clients; reconnecting terminal clients receive a fresh rendering of the recovered screen. A pause between boundaries is reconciled by replaying the durable keys to the next boundary or exit before publishing its recovered state and checkpoint.

The server enforces an idle timeout, a maximum session duration, and a maximum event count, and records the reason for every pause. Every request made with an attempt's tokens is logged, including accepted and rejected requests, so probing appears in the attempt's record. Request records exclude bearer tokens. Requests after package finalization go to a separate server audit log and do not mutate the finished package.

## Player metadata

Attempt creation records metadata reported by the user: player type (`human` or `model`), and for models the agent tool, tool version, model identifier, reasoning effort, and free-text notes. `reasoning_effort` is an optional nullable string preserving the provider setting verbatim (for example `high` or `xhigh`); unknown settings are null, and older records may omit it. `usage` records optional input tokens, output tokens, and cost as the tool reports them; the latest report wins. All of these are nullable. The server itself measures wall-clock duration per session and counts keys. On crash recovery, duration is retained through the last durable timing event; an unrecorded tail before the crash cannot be reconstructed.

`PLAYER.md` is the instructions given to every model player: how to call the API with `curl`, and the rules. The rules are to play only through the API with the player token, not to try to reach the server or its files by any other route, not to obtain the seed, and not to run another copy of the game to test moves. Looking up general NetHack knowledge is allowed, as it is for humans. `PLAYER.md` states that breaking the rules disqualifies the attempt and that disqualifications are published. The server records the digest of the `PLAYER.md` it serves in each attempt.

## Recording and run package

The server writes an append-only event log while a session is live. Events are:

- session start and end, with session index, how the game exited, and duration
- one event per consumed key: boundary number, key, and elapsed monotonic time since session start
- a screen-hash checkpoint every fixed number of boundaries (initially every boundary), at every pause that reaches an input boundary, and at the last boundary before the game exits, each naming the next key-read boundary and hashing its pre-key screen. The last boundary screen is retained so its checkpoint can be written after an exit; an exit has no new boundary
- pause and resume, with the reason and the boundary reached; paused time is excluded from durations
- metadata and usage reports
- accepted and rejected requests, with method or WebSocket message kind, path, token role, and disposition; requested keys are distinct from consumed-key events

A completed attempt produces:

```text
attempt.json       schema version, attempt ID, server instance, player metadata,
                   PLAYER.md digest, image digest, seed, fixed game date,
                   character, outcome, result, per-session and total duration,
                   key count, usage, and event_log_digest
events.jsonl.gz    the ordered event log for all sessions
```

`event_log_digest` is the SHA-256 of the uncompressed event log bytes, so it does not depend on gzip settings. It covers only the event log, not `attempt.json`; verification separately validates the manifest and checks its replay-derived fields against the log and replay result.

Character metadata is also emitted by the game at input boundaries after character creation, so it remains available for abandoned attempts. The result is read from the game's own end-of-game record: score, turns, maximum dungeon depth, ascended or cause of death, and conducts. An abandoned attempt has no end-of-game record; its outcome is `abandoned`, its result fields are null, and its character is recorded only if character creation finished. The package stores no terminal output and no save files. The package is finalized atomically on its first download after the attempt ends. The owner may report usage after play ends but before that download; subsequent usage changes are rejected. Requests after finalization are retained in the separate server audit log.

## Replay and verification

`nethack-run replay <package>` runs in the image with the recorded digest, creates a clean game directory, and runs each session in order with its derived seed, feeding the recorded keys at input boundaries. Saves carry over between replayed sessions exactly as they did in play. Resuming a paused session uses this same replay path.

Verification compares the screen hash at every checkpoint, how each session's game exited, and the final end-of-game record. For an abandoned attempt it replays the recorded keys, compares every checkpoint, and requires the same ending: either the game is still running at the boundary of the final pause, or the last session exited by saving. It also recomputes `event_log_digest`. On mismatch it reports the first divergent checkpoint with the expected hash and the actual screen. Replay does not wait for recorded think time.

`nethack-run export <package>` replays the attempt, captures the raw terminal output, and writes an asciicast v3 file paced by the recorded key timestamps. Output is timed to the key that produced it, so the pacing is a close reconstruction and not a capture of when each terminal update originally appeared. Pauses and gaps between sessions are collapsed.

The owner dashboard embeds a locally bundled asciinema player for completed
attempts. On first viewing, it reconstructs and atomically caches
`replay.cast.gz` beside the attempt, using the same verifier/exporter and an immutable temporary
package snapshot. This does not finalize the public package or freeze usage.
Only one recording is generated at a time. Every checkpoint and final result
must match before publication; older-image reproduction is marked diagnostic in
the `replay.json` sidecar. Divergent recordings are not served. The owner may
download the cast, while player tokens cannot access replay endpoints. The
two-file run-package format remains unchanged.

Recordings are a disposable cache, never a record. They are stored gzip-compressed
and served as-is with `Content-Encoding: gzip` (decompressed on the fly for clients
that do not accept gzip). The server deletes recordings unwatched for a week, then
the least recently watched while the cache exceeds its size budget or free disk
space runs low. A deleted recording is rebuilt from the event log when next watched.

The server keeps unfinished attempts in memory. Finished attempts are summarized in
a SQLite index (`.index.sqlite` in the state directory) and loaded from their folder
only when used, then dropped again after five idle minutes. The attempt folders stay
authoritative: each index row carries a fingerprint of `private.json` and
`events.jsonl`, rows are rebuilt at startup when it no longer matches, and deleting
the database only costs one full scan. The dashboard and leaderboard read summaries;
the leaderboard is a pure function of summary rows so CI can reuse it.

The test corpus contains startup and every character-selection path, ordinary movement, menus, type-ahead across an unexpected prompt, a retried `keys` request, death, save and resume, pause and resume at several points including mid-prompt and mid-menu, and a long run. Tests play each case through the API and replay it in a separate process. A build change must pass the corpus.

Recovery tests inject failures before and after consumed-key persistence, save creation, session-end persistence, next-session snapshot publication, and package finalization, including a truncated final log record. They verify that a recorded final key completes its save or exit exactly once, that boundary positions survive recovery without duplicated keys, and that no HTTP response or WebSocket output exposes state ahead of the durable log. Storage fault tests distinguish buffered writes from synchronized writes to exercise the machine-failure contract.

## Results

Reported per attempt: score, turns, maximum depth, ascended, conducts, character, total duration, key count, and token usage and cost where reported. Rows are labelled by player type and, for models, by model and agent tool.

Results are published in a separate repository in the final phase. A submission is a PR, opened by the user and not by the agent, adding one `attempts/<attempt-id>/` directory containing the two package files. The maintainer adds packages from the hosted instance the same way. CI treats PR contents as data and runs with a read-only token and no secrets. It checks schema, size limits, and digests, and rejects any package whose image digest is not in the repository's tracked list of official images before running anything. It then replays the package in that image under time and memory limits. Every verified attempt is listed, including deaths, quits, and abandoned attempts.

Replay verification shows that a package reproduces. It does not show that the rules were followed or that every attempt was submitted. The site labels each attempt with its replay status and its server instance, and states that rule compliance rests on the submitter. Only the hosted instance gives a complete record of attempts; local attempts that are never submitted are invisible.

A static site shows the leaderboard, an attempt browser, and a terminal player for the exported asciicast, and explains how to contribute runs. Attempts that stop reproducing are removed, by author where necessary.

An attempt can be marked `disqualified` with a reason, by the submitter or the maintainer, when the agent broke the rules in `PLAYER.md`. Disqualified attempts stay in the repository, are excluded from rankings, and are listed on their own page of the site with the model, agent tool, and reason.

## Implementation sequence

1. Pin NetHack 5.0; add the source-fetch, patch, and build targets and the container image. Add a minimal recorder and replayer that drive the game directly, with no server, and prove that the same seed and keys produce identical boundary screens across repeated runs, across a save and restore with derived session seeds, and when a session is killed and rebuilt by replay.
2. Add the API server with attempt creation, tokens, `keys` with boundary checking, `screen`, and the recorder.
3. Add multi-session attempts: `resume`, `pause`, idle handling, crash recovery, expiry, and run-package finalization.
4. Add `replay` verification of run packages, the full test corpus, and the cross-platform replay test.
5. Add `PLAYER.md`, player metadata, `usage`, request logging, and `export`.
6. Add the `tty` WebSocket with `play` and `watch`.
7. Add contributor keys and per-key limits for hosted instances.
8. Create the results repository, PR validation, static leaderboard with the disqualified page, and terminal player.

## References

- [Official NetHack source](https://github.com/NetHack/NetHack) and [NetHack General Public License](https://www.nethack.org/common/license.html).
- [Teleport Coding Challenge NetHack 5.0 recorder](https://github.com/davidbau/teleport-contest/blob/main/nethack-c/README.md) and [patch descriptions](https://github.com/davidbau/teleport-contest/blob/main/nethack-c/patches/README.md): seed and date pinning, stable sort, and input-boundary screen capture.
- [NetHack Learning Environment](https://github.com/facebookresearch/nle): seeded NetHack sessions and trajectory recording on NetHack 3.6.6.
- [Asciicast v3 specification](https://docs.asciinema.org/manual/asciicast/v3/): timestamped terminal event format for the web player.
- [GitHub Actions security guidance](https://docs.github.com/en/actions/concepts/security/compromised-runners): read-only validation of untrusted PR data.

## Run access control

Creation may nominate `owner_github_id` independently of player metadata and
contributor credentials. It grants rights to the nominated account, never to the
caller. Authenticated GitHub identity controls access. The existing `owner_ids`
configuration identifies server admins; ordinary accounts own only nominated
runs. Per-run capabilities and a My runs filter expose this distinction.

Owner/admin player-token recovery is serialized against accepted operations,
commits a new hash and administrative audit atomically in `private.json`, and
invalidates the old token across restart. No automatic resume or gameplay reset
occurs. Admin-only reassignment revokes both bearer credentials and terminal
access. Unowned legacy runs are never assigned heuristically.

Initial nomination is recorded in new creation events and manifests as optional
`initial_owner_github_id`. Current ownership and credential audit stay private;
reassignments do not rewrite gameplay events or frozen packages. This leaves
room for authenticated human players and an independently attributed, idempotent
usage ledger without treating nominated ownership as contribution evidence.


Server admins may delete failed runs through the CSRF-protected dashboard.
Deletion waits for API operations and replay workers, stops the process, and
removes the attempt from authorization, browsing, and score aggregation. A
private durable deletion record outside the attempt directory is authoritative
across crashes; outstanding file removal is retried. Run owners cannot delete
runs. Deletion does not affect other attempts or shared game score files.
