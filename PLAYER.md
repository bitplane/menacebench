# Playing NetHack through the run API

This is Menace Bench - a game of NetHack for AI agents. The goal is to complete
the game by ascending, and get a good score. Continue the game until completion
or death.

## Start here

Set `BASE_URL` to the given address. Keep the hostname and port you were given;
do not substitute a different localhost address.

Read these instructions, then create **one** recorded attempt:

```sh
curl -fsS "$BASE_URL/player.md"
umask 077
RUN_FILE=$(mktemp "${TMPDIR:-/tmp}/menace-attempt.XXXXXXXX")
curl -fsS -X POST "$BASE_URL/agent/start" \
  -H 'Content-Type: application/json' \
  -d '{"player_type":"model","agent_tool":"your actual tool","model":null,"reasoning_effort":null,"owner_github_id":null,"notes":null}' \
  -o "$RUN_FILE" || { echo 'Start failed; check with the owner before retrying.' >&2; exit 1; }
printf 'Credential file: %s\n' "$RUN_FILE"
```

Keep this file for the entire attempt. Record its path and `BASE_URL` in your
continuation/compaction notes, never the token itself. Shell variables may not
survive the next tool call. In each fresh shell, restore `RUN_FILE` to that exact
path and `BASE_URL` to the given address, then load:

```sh
PLAYER_TOKEN=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["player_token"])' "$RUN_FILE")
ATTEMPT_URL="$BASE_URL$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["attempt_path"])' "$RUN_FILE")"
```

Use runner-provided private persistent storage instead of `/tmp` if available;
do not save credentials in the shared repository. File permissions protect
against other Unix users, not agents sharing your Unix user. Read only your own
credential file. If you lose shell state, reload it before making API requests;
repeating requests without the authorization header cannot restore access.

If the user supplies their numeric GitHub ID, set `owner_github_id` to that
integer; otherwise leave it null. Their ID appears beside their name when they
sign in to this site's dashboard. This nominates that account to manage this
run and recover access; it does not grant you any GitHub or owner permissions.
If your credential file is lost, ask that human to use **My runs → Recover
player access** and provide the downloaded JSON. For an unowned run, a server
admin must recover access or assign an owner. Reload the replacement credential
and resume the same attempt; the previous token will no longer work. Do not
create another attempt or repeatedly poll without credentials.

Report your actual tool, model, and configured reasoning effort when known; use
null for unknown values. `reasoning_effort` is the provider's setting verbatim
(for example, `"high"` or `"xhigh"`), not an estimate of how hard you are thinking.
Do not infer it from the model name. The
saved response contains `id`, `player_token`, `attempt_path`, and the initial status.
Set `PLAYER_TOKEN` to that token and `ATTEMPT_URL` to `BASE_URL` followed by
`attempt_path`. Tell the user the attempt ID so they can watch it on the dashboard.
Keep the token private. No owner token or seed is returned.

If you already have an attempt URL and player token, use those instead of creating
another attempt. Attempts at cheating will be disqualified - not call the
owner-oriented `POST /attempts` endpoint, log in to GitHub, or request owner
credentials. A hosted instance may require a supplied contributor bearer token for
`/agent/start`; do not try to bypass that requirement. A 429 means the instance's
running-game limit is reached. If creation times out, ask the owner to check
the dashboard rather than blindly creating more games.

## Game tips

You're exploring the dungeons and Mazes of Menace, identifying items, fighting
creatures to level up and eventually find the Amulet of Yendor. Levels mostly
fill the screen, and searching for hidden doorways will usually eventually find
something. Your view is limited in the dark, you don't need to move one step at
a time, you can use the travel command to move quickly.

The official guidebook is available as plain text at `$BASE_URL/guidebook.txt`.
Download it once and search it locally if it's not already in your pwd.

## Play the game

Your objective is to **win NetHack by ascending**. Play one attempt and pursue
that objective until ascension or death, unless the user asks you to stop.

Pause only when the user asks you to stop or an actual constraint prevents you
from continuing. 

If a real constraint requires stopping, identify it concretely: report the tool
error or the explicit runner limit you were given, rather than vaguely claiming
that this turn is reaching its execution limit. If the player API is still
available, pause the attempt before stopping:

```sh
curl -fsS -X POST -H "Authorization: Bearer $PLAYER_TOKEN" "$ATTEMPT_URL/pause"
```

Confirm the returned state is `paused`, then report the interruption and attempt
ID. Pausing preserves the game and frees its running slot; resume this same
attempt when you can continue. Do not deliberately end the game or create a
replacement attempt. If the API is unreachable, report that you could not confirm
the pause; the server also pauses idle games after 30 minutes.

Read the current screen:

```sh
curl -sS -H "Authorization: Bearer $PLAYER_TOKEN" "$ATTEMPT_URL/screen"
```

The last line is JSON with `session`, `boundary`, `state`, and cursor position.
The preceding 24 lines are the game screen. Send keys with the position you saw:

```sh
curl -sS -H "Authorization: Bearer $PLAYER_TOKEN" \
  -H 'Content-Type: application/json' "$ATTEMPT_URL/keys" \
  -d '{"session":1,"boundary":1,"keys":"Ada\n"}'
```

Use ordinary JSON escapes: `\n` for Enter, `\u001b` for Escape, `\u0004`
for Ctrl-D. Each character must fit in a non-NUL byte. A batch can cross
prompts, so inspect screens frequently. A 409 means the position is stale or
the session is not accepting input. Read its screen and choose the next move;
do not blindly resend a partially consumed batch at a new position.

When the state is `paused` or `saved`, POST to `$ATTEMPT_URL/resume` with the
same authorization header. A 429 means all running slots are occupied; keep the
same attempt and retry later. Saved games may present a saved-character menu.
The API can report death, quit, ascension, or abandonment as terminal outcomes;
this describes the protocol, not permission to choose an early exit.
Report the attempt ID and final result. The owner can download its trace afterward.

Play only through these API endpoints with the player token. Do not access
the server or its files by another route, obtain the seed, or run another
copy of the game to test moves. General NetHack reference material is allowed.
Breaking these rules disqualifies the attempt; disqualifications are published
with the model, agent tool, and reason.

After ascending or dying, view the stats so they show up in the recording.
