# Benchmarking snag list

## Fixed

- **100 running games by default.** Paused and saved attempts release slots.
  Creation, player resume, dashboard resume, and terminal resume share serialized
  admission per contributor (all local attempts share one pool). Configure with
  `make up CONCURRENCY=100`. Full capacity returns 429 without invalidating tokens.
  An isolated 100-process test measured 615.7 MiB combined game RSS and 39.8 MiB
  server RSS; ten paused games resumed successfully. Finished attempts leave
  memory after five idle minutes and are listed from a SQLite index; paused and
  saved attempts keep their event history in memory until they finish or expire.
- **Player pause when blocked.** Players can pause their own attempt. PLAYER.md
  explains confirmation, reporting the interruption, and resuming the same game.
  Idle pause remains 30 minutes; paused/saved expiry remains seven days.
- **Record model reasoning effort.** Optional nullable `reasoning_effort` is
  preserved in events/packages and displayed in the dashboard. Report the actual
  configured setting or null; existing traces remain valid.
- **Harness release version.** New attempts record `harness_version: "0.0.2"` in
  private data, creation events, and exported manifests. Schema version remains
  1. Legacy traces can be backed up and backfilled using
  `scripts/backfill_version.py` while the server is stopped.
