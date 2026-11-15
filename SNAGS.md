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

## Implemented (pending deployment)

- **Record model reasoning effort.** Optional nullable `reasoning_effort` is
  accepted by both creation endpoints, preserved verbatim in events/packages,
  and displayed in the dashboard. Instructions say to report the configured
  value or null, never infer it. Existing traces without the field remain valid.
