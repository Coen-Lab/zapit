# Bonsai example: driving Zapit trials

`Zapit.bonsai` is a minimal Bonsai workflow that runs Zapit trials over its ZeroMQ server and fires the hardware trigger from a Harp Behavior board. Each trial is two steps: a JSON request arms the DAQ with a random condition, then a 5 ms pulse on the Harp board's DO0 line, wired to the DAQ's PFI0, starts the stimulus at the moment the task needs it. The protocol itself is documented for any client in [`docs/bonsai.md`](../docs/bonsai.md).

## Layout

```
bonsai/
  Setup.cmd                    Double-click once: downloads Bonsai and restores packages
  .bonsai/
    Zapit.bonsai               The workflow
    Bonsai.config              Pinned package versions and assembly locations
    NuGet.config               Package feed (nuget.org only, no local packages)
    Setup.ps1                  Bootstrap script run by Setup.cmd
    Bonsai.exe                 Downloaded by Setup.ps1, gitignored
    Packages/                  Restored from Bonsai.config, gitignored
```

This follows the lab's Bonsai environment layout (same as `hw-behaviour-box`): the folder is self-contained, the Bonsai version and every package are pinned in `Bonsai.config`, and nothing is installed system-wide.

## Setup

Run `Setup.cmd`. It downloads Bonsai 2.9.1 into `.bonsai/`, then runs `Bonsai.exe --no-editor` to restore the packages pinned in `Bonsai.config` from nuget.org. The first restore also rewrites `Bonsai.config` with the exact assembly locations of every transitive dependency, so a diff on that file after the first run is expected and can be committed.

Package versions chosen, all confirmed present on nuget.org (they are in this machine's NuGet cache, restored from nuget.org):

| Package                     | Version   | Why                                                                                   |
|-----------------------------|-----------|---------------------------------------------------------------------------------------|
| Bonsai                      | 2.9.1     | Same release as `hw-behaviour-box`, so one Bonsai per lab                             |
| Bonsai.ZeroMQ               | 0.2.0     | `Request` and `ConvertToString` nodes; pulls in NetMQ 4.0.1.12, AsyncIO, NaCl.Net      |
| Bonsai.Harp                 | 3.6.1     | Harp device and message plumbing                                                      |
| Harp.Behavior               | 0.2.0     | `Device`, `CreateMessage` and the `OutputSet`/`OutputClear` payloads                   |
| Bonsai.Windows.Input        | 2.9.0     | `KeyDown` for the demo trigger                                                        |
| Bonsai.Scripting.Expressions | 2.10.0   | The `ExpressionTransform` inside the reply filter                                     |
| Bonsai.System               | 2.9.0     | `CsvWriter`                                                                           |

Bonsai.ZeroMQ 0.2.0 is the newest release I am confident exists; if the package manager offers a newer one, it can be taken as long as the `Request` node still returns a `NetMQMessage`.

Then open the workflow by starting `.bonsai/Bonsai.exe` and choosing File, Open, `Zapit.bonsai`. Click the blank workspace to see the four settings in the Properties panel:

| Property              | Default                 | Meaning                                                                          |
|-----------------------|-------------------------|----------------------------------------------------------------------------------|
| `ZapitAddress`        | `tcp://localhost:5555`  | Zapit server endpoint. The server binds `tcp://*:5555`; use the host name across a network |
| `StimDurationSeconds` | `1.5`                   | `stim_duration_s` sent with every trial. `-1` means continuous until stop         |
| `PortName`            | `COM3`                  | Serial port of the Harp Behavior board (Device Manager, Ports (COM & LPT))        |
| `LogFileName`         | `zapit_replies.csv`     | CSV of every reply, with a Bonsai timestamp. Appended, relative to the workflow folder |

`PortName` is wrong on almost every machine. Set it before starting the workflow, or the Harp `Device` node fails on start.

## Running

1. Start Zapit, calibrate the scanners and the sample, load a stimulus config. The server starts with the GUI and prints `Zapit server listening on tcp://*:5555`.
2. Start the workflow (the green play button).
3. Press **Space**. The workflow sends `send_samples` with a random condition and random laser state, logs the reply, and if the reply is a successful arm it pulses DO0 for 5 ms. The stimulus starts on that rising edge and runs for `StimDurationSeconds`.
4. For a continuous stimulus set `StimDurationSeconds` to `-1`, press Space to start it, and press **S** to send `stop`, which ramps the laser down.

Keyboard events reach the `KeyDown` nodes whichever window has focus, so there is no need to click into Bonsai first.

## Wiring

| From                              | To                          | Note                                                                  |
|-----------------------------------|-----------------------------|-----------------------------------------------------------------------|
| Harp Behavior board DO0 (BNC)     | NI DAQ PFI0                 | The trigger. Zapit's DAQ task waits for a rising edge on PFI0          |
| Harp Behavior board GND           | NI DAQ D GND                | Common ground, or the edge is not seen reliably                        |
| Harp Behavior board USB           | Bonsai PC                   | Appears as a COM port                                                  |

DO0 is a 5 V digital output and PFI0 is 5 V TTL tolerant, so no level shifting is needed. If DO0 is already in use, change `OutputSet` and `OutputClear` in the `TriggerPulse` group to DO1, DO2 or DO3 (the DOPortN lines on the RJ45 ports also work).

The pulse is 5 ms high then low, timed in software with an `rx:Delay`. Only the rising edge matters to Zapit, so jitter on the falling edge is harmless. The rising edge itself is sent as a single Harp write as soon as the reply arrives, so its latency is one USB round trip after the reply, typically under a millisecond. If you need a hardware-timed pulse width, write `PulseDO0` (duration in ms) and `OutputPulseEnable` (DO0) once at startup and the board times every subsequent `OutputSet` itself.

## How the workflow is built

Two groups, wired by edges inside and by one subject between them. Everything a user changes is externalized to the top level.

`ZapitClient`:

1. `KeyDown` (Space) combined with the `StimDurationSeconds` property by `WithLatestFrom`, then a `Format` node builds the request string `{"cmd":"send_samples","condition":null,"laser_on":null,"hardware_triggered":true,"stim_duration_s":1.5}`.
2. `KeyDown` (S) feeds a second `Format` node that emits `{"cmd":"stop"}`.
3. Both are merged into one `Request` node (one REQ socket, `ConnectionString` externalized as `ZapitAddress`). It returns a `NetMQMessage`; `MemberSelector First` takes its only frame and `ConvertToString` decodes it.
4. The reply string goes through `Timestamp` into `CsvWriter` (two columns, `Timestamp` and `Value`), and separately into a `Condition` named `ArmedOk`, which passes only replies that contain `"condition"` and not `"error"`, so stop replies and errors never fire the trigger. The survivors are published as the `ZapitArmed` subject.

`TriggerPulse`:

1. `SubscribeSubject ZapitArmed`.
2. `CreateMessage` with `CreateOutputSetPayload` DO0, and in parallel the same event delayed 5 ms into `CreateMessage` with `CreateOutputClearPayload` DO0.
3. Both merged into the Harp `Device` node, whose `PortName` is externalized.

A `Format` node uses `String.Format`, so the number formatting follows the Windows locale. On a machine set to a comma decimal separator `1,5` would be sent and Zapit would reject it; UK and US locales are fine.

## Swapping the KeyDown for a real task event

The annotation node inside `ZapitClient` marks the spot. Delete the Space `KeyDown` and connect your trial-start source to the `WithLatestFrom` node as `Source1`. Any observable works, since the value is discarded and only the timing is used. The normal shape in a task is a `SubscribeSubject` carrying the trial-start or stimulus-onset event from the task's own workflow, or an `IncludeWorkflow` of this file into the task with `ZapitArmed` subscribed from the task side.

Two timing facts to design around:

- The `send_samples` request takes a few tens of milliseconds end to end (ZeroMQ round trip plus loading the waveforms into the DAQ). Arm during the inter-trial interval, not at stimulus onset.
- The pulse fires as soon as the reply comes back. If you want the stimulus to start at a later event rather than immediately after arming, split the groups: arm on the inter-trial event, and move the `SubscribeSubject ZapitArmed` in `TriggerPulse` to a `Zip` of the armed reply with your onset event, so the pulse waits for both.

`stop` is only needed for continuous stimuli (`stim_duration_s` of `-1`). A finite trial ramps itself down.

## Checking in the Bonsai editor

This workflow was built from the package metadata and validated structurally (edges, scopes, subjects), not run, because Bonsai cannot run here. Before relying on it, open it once in the editor and confirm these nodes load without a red error outline:

| Node                                    | What to check                                                                                |
|-----------------------------------------|----------------------------------------------------------------------------------------------|
| `Request` (Bonsai.ZeroMQ)               | It appears as a combinator taking a string and emitting `NetMQMessage`                       |
| `MemberSelector First` then `ConvertToString` | `First` must resolve on `NetMQMessage`; if not, `Last` is the same frame                |
| `Device` (Harp.Behavior)                | Property names as written (`OperationMode`, `Heartbeat`, `IgnoreErrors`, `PortName`)         |
| `CreateOutputSetPayload` / `CreateOutputClearPayload` | Enum value `DO0` accepted                                                      |
| `ArmedOk` condition                     | The expression `it.Contains("\"condition\"") && !it.Contains("\"error\"")` compiles          |
| `Annotation`                            | Needs Bonsai 2.7 or later; delete it if an older editor complains                            |

If any node fails to load, the fix is to drop it from the toolbox in the editor and reconnect, then save; the editor rewrites the XML with the correct serialization.
