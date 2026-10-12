# Backlog

## Audit 2026-10-12: not recommended (steelman holds)

- Bonsai `Request` node has no receive timeout: a Bonsai.ZeroMQ 0.2.0 limitation, documented in `bonsai/README.md`.
- Trials file is `.csv` rather than CSV content in a `.yml`: deliberate, in the changelog.
- `stim_duration_ms` keeps the `- blanking_time_ms / 2` term for multi-point conditions: the power maths stay identical to the measured behaviour of v1.
- The config editor rounds points to 0.01 mm on add: 10 um, below the beam size.
- A `NI.triggerChannel` change at runtime does not rebuild an existing task: settings are not editable at runtime.
- No GitHub-issue or support-report menu items, and no undo in the stim config editor: dropped deliberately; v1 had no undo either.
- A read-only settings file stops start-up: fails loudly, which is right for a file the app must be able to write.
- Shared `zmq.Context.instance()` between server and client in one process, and one directory glob per logged trial: no effect at the rates involved.
