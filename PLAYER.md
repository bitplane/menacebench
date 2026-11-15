# Playing NetHack through the run API

## Start here

If the user gives you only the site's URL, you can start playing without a human
login. Set `BASE_URL` to that URL without a trailing slash. Keep the hostname and
port you were given; do not substitute a different localhost address. For example,
an SSH-forwarded `http://127.0.0.1:8756` works when you run on the machine with
that forward.

Read these instructions, then create **one** recorded attempt:

```sh
curl -fsS "$BASE_URL/player.md"
curl -fsS -X POST "$BASE_URL/agent/start" \
  -H 'Content-Type: application/json' \
  -d '{"player_type":"model","agent_tool":"your actual tool","model":null,"notes":null}'
```

Report your actual tool and model when known; use null for unknown values. The
response contains `id`, `player_token`, `attempt_path`, and the initial status.
Set `PLAYER_TOKEN` to that token and `ATTEMPT_URL` to `BASE_URL` followed by
`attempt_path`. Tell the user the attempt ID so they can watch it on the dashboard.
Keep the token private. No owner token or seed is returned.

If you already have an attempt URL and player token, use those instead of creating
another attempt. Do not call the owner-oriented `POST /attempts` endpoint, log in
to GitHub, or request owner credentials. A hosted instance may require a supplied
contributor bearer token for `/agent/start`; do not try to bypass that requirement.
A 429 means the instance's concurrent-attempt limit is reached. If creation times
out, ask the owner to check the dashboard rather than blindly creating more games.

## Play the game

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
same authorization header. Saved games may present a saved-character menu.
An attempt ends on death, quit, ascension, or abandonment.
Continue making decisions until the game ends or the user asks you to stop.
Report the attempt ID and final result. The owner can download its trace afterward.

Play only through these API endpoints with the player token. Do not access
the server or its files by another route, obtain the seed, or run another
copy of the game to test moves. General NetHack reference material is allowed.
Breaking these rules disqualifies the attempt; disqualifications are published
with the model, agent tool, and reason.
