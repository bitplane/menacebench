# Benchmarking snag list

## Open

- **Allow 100 concurrently running games by default.** The current
  default of two unfinished attempts blocks model benchmarking. Count running
  game processes, not all unfinished attempts: paused and saved attempts must
  release their slots. Check admission consistently when creating or resuming
  through the player API, owner dashboard, and terminal client; serialize
  admission to prevent concurrent resumes exceeding the cap. Make the limit
  configurable through `make up` and explain a full-capacity response clearly.
  Preserve existing attempts and their tokens when deploying the change.
  - Current lifecycle: pause after 1,800 seconds without game input; resume
    reconstructs the same session from its durable keys. The player API requires
    an explicit resume request; merely polling the screen does not resume play.
    Saved sessions resume into the next session. Paused/saved attempts expire
    after 604,800 seconds (seven days).
  - Observed baseline: Podman reported about 37 MB for the server container,
    with a single NetHack process around 7 MB RSS. The requested target is 100
    running games on this 132 GB host; validate with an isolated load test rather than adding
    test traces to the live dashboard. Event logs remain loaded in server memory,
    so long histories can grow memory even after game processes have stopped.

- **Record model reasoning effort.** Add nullable `reasoning_effort` to player
  metadata, preserve the provider's string verbatim, and carry it into packages,
  agent instructions, and the dashboard. Unknown values should remain null;
  retain compatibility with existing traces that lack the field.
