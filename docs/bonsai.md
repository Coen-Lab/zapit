# The Zapit ZeroMQ protocol

Zapit runs a ZeroMQ REP socket so an external program can drive trials. Bonsai is the usual client (see [`bonsai/README.md`](../bonsai/README.md)), but any language with a ZeroMQ binding works, and `zapit/client.py` is a 60-line Python reference client.

## Transport

| Item       | Value                                                                                  |
|------------|----------------------------------------------------------------------------------------|
| Pattern    | ZeroMQ REQ (client) to REP (Zapit), strictly one request then one reply               |
| Bind       | `tcp://127.0.0.1:5555` by default, set by `server.bind` in the settings YAML           |
| Framing    | One single-frame message per request and per reply                                     |
| Encoding   | UTF-8 JSON object both ways                                                            |
| Request    | `{"cmd": "<name>", ...arguments}`                                                      |
| Reply      | `{"ok": true, ...fields}` on success, `{"ok": false, "error": "<Type>: <message>"}` on failure |

The server starts only when `server.enable` is true (the default). The default bind accepts connections from the same machine only; to drive Zapit from another machine, set `server.bind` to `tcp://*:5555` explicitly.

Every request gets exactly one reply, including malformed ones. A REQ socket that sends and never receives is stuck, so set a receive timeout (the Python client uses 5 s) and rebuild the socket on timeout, which is the only way to recover a REQ socket in ZeroMQ.

The server runs on a thread inside the Zapit GUI process. Commands execute on that thread while the GUI keeps running, and the reply is sent after the command has finished, so a successful `send_samples` reply means the DAQ task is already loaded and armed.

Key order in replies is not significant. On success `ok` happens to come last; do not rely on it.

## Commands

| Command               | Arguments                                                                                                                                          | Reply fields                                                                                          |
|-----------------------|----------------------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------|
| `ping`                | none                                                                                                                                               | `version`, `time`                                                                                     |
| `status`              | none                                                                                                                                               | `state`, `ready`, `scanners_calibrated`, `sample_calibrated`, `config`, `n_conditions`, `experiment_path`, `simulated` |
| `state`               | none                                                                                                                                               | `state`                                                                                               |
| `n_conditions`        | none                                                                                                                                               | `n_conditions`                                                                                        |
| `send_samples`        | `condition`, `laser_on`, `stim_duration_s`, `start_delay_s`, `laser_power_mw`, `hardware_triggered`, `logging` (all optional)                      | `condition`, `laser_on`, `laser_power_mw`, `hardware_triggered`, `stim_duration_s`                     |
| `stop`                | `rampdown_ms` (optional)                                                                                                                           | `state`                                                                                               |
| `load_config`         | `path`                                                                                                                                             | `config`, `n_conditions`                                                                              |
| `set_experiment_path` | `path` (null clears)                                                                                                                               | `experiment_path`                                                                                     |
| `simulate_trigger`    | none (simulated DAQ only)                                                                                                                          | none                                                                                                  |

### `send_samples` arguments

| Argument             | Type            | Default  | Meaning                                                                                                            |
|----------------------|-----------------|----------|--------------------------------------------------------------------------------------------------------------------|
| `condition`          | int or null     | null     | 1-based index into the loaded config. null or -1 picks a random condition, never the same one twice running        |
| `laser_on`           | bool or null    | true     | false moves the scanners with the laser off (a sham trial). null or -1 picks randomly                               |
| `stim_duration_s`    | number          | -1       | Greater than 0 plays for that long then ramps down. Anything else is continuous until `stop`                        |
| `start_delay_s`      | number          | 0        | Leading delay before the stimulus, only applied to finite trials                                                    |
| `laser_power_mw`     | number or null  | null     | Time-averaged power override. null or -1 uses the config value for that condition                                   |
| `hardware_triggered` | bool            | true     | true loads the waveforms and waits for a rising TTL on the DAQ trigger line (PFI0). false starts immediately         |
| `logging`            | bool            | true     | Append a row to the trial log in the experiment path, if one is set                                                 |

The reply reports what was actually chosen, which matters when `condition` or `laser_on` were random. `laser_power_mw` is 0 when the laser is off. `stim_duration_s` is -1 for a continuous stimulus.

### `state` values

| State      | Meaning                                                             |
|------------|---------------------------------------------------------------------|
| `idle`     | Nothing armed or playing, including once a finite trial has finished playing |
| `stim`     | A trial is loaded, either armed for a trigger or playing             |
| `rampdown` | `stop` was called on a continuous stimulus and the laser is ramping down; it returns to `idle` about 200 ms after the last ramp step has been written |

While a trial is armed or playing (`stim` or `rampdown`), unclocked beam and laser writes are refused, so pointing the beam from the GUI cannot disturb a trial.

## A trial, step by step

1. Client sends `send_samples` with `hardware_triggered` true. Zapit computes the waveforms, loads them into the DAQ and arms the task on PFI0, then replies.
2. Client waits for the reply and checks `ok`. The reply arriving is the signal that the DAQ is armed.
3. Client raises its TTL line (the Bonsai example uses a Harp Behavior board digital output wired to PFI0). The stimulus starts on the rising edge, with no software in the path.
4. A finite trial ends by itself. A continuous one runs until `stop`.

Arming takes tens of milliseconds, so send `send_samples` in the inter-trial interval and keep only the TTL at the time-critical moment.

`send_samples` refuses (reply `ok` false, `NotReadyError`) when the scanners or sample are not calibrated, no config is loaded, a check pattern is running, or a trial is still armed or playing. A continuous stimulus must be stopped first, so it always gets its ramp-down; arming during that ramp-down is accepted and zeroes the outputs before the new trial is loaded. `load_config` is refused while a trial is armed or playing.

## Examples

Requests and replies as sent on the wire, one JSON object per message.

### ping

```json
{"cmd": "ping"}
```
```json
{"version": "2.0.0", "time": "2026-10-12T14:03:22.418", "ok": true}
```

### status

```json
{"cmd": "status"}
```
```json
{"state": "idle", "ready": true, "scanners_calibrated": true, "sample_calibrated": true, "config": "uniAndBilateral_5_conditions.yml", "n_conditions": 5, "experiment_path": "D:\\data\\sub-001\\ses-03", "simulated": false, "ok": true}
```

`ready` is true when both calibrations are done and a config is loaded. `config` and `experiment_path` are null when not set.

### state

```json
{"cmd": "state"}
```
```json
{"state": "stim", "ok": true}
```

### n_conditions

```json
{"cmd": "n_conditions"}
```
```json
{"n_conditions": 5, "ok": true}
```

### send_samples, hardware triggered, random condition and laser state

This is what the Bonsai example sends.

```json
{"cmd": "send_samples", "condition": null, "laser_on": null, "hardware_triggered": true, "stim_duration_s": 1.5}
```
```json
{"condition": 3, "laser_on": true, "laser_power_mw": 10.0, "hardware_triggered": true, "stim_duration_s": 1.5, "ok": true}
```

### send_samples, a specific condition, software start, continuous

```json
{"cmd": "send_samples", "condition": 2, "laser_on": true, "hardware_triggered": false, "stim_duration_s": -1}
```
```json
{"condition": 2, "laser_on": true, "laser_power_mw": 10.0, "hardware_triggered": false, "stim_duration_s": -1.0, "ok": true}
```

### send_samples, sham trial with a power override and a start delay

```json
{"cmd": "send_samples", "condition": 1, "laser_on": false, "laser_power_mw": 5.0, "stim_duration_s": 2.0, "start_delay_s": 0.25}
```
```json
{"condition": 1, "laser_on": false, "laser_power_mw": 0.0, "hardware_triggered": true, "stim_duration_s": 2.0, "ok": true}
```

### send_samples refused

```json
{"cmd": "send_samples", "condition": 9}
```
```json
{"ok": false, "error": "ValueError: Condition must be 1..5, got 9"}
```
```json
{"ok": false, "error": "NotReadyError: Not ready: scanners and sample must be calibrated and a config loaded"}
```

### stop

```json
{"cmd": "stop"}
```
```json
{"state": "rampdown", "ok": true}
```

With an explicit ramp:

```json
{"cmd": "stop", "rampdown_ms": 100}
```

`stop` on an idle system or on a finished finite trial replies `{"state": "idle", "ok": true}`. `stop` on a trial that is armed but has not been triggered, finite or continuous, cancels it: the DAQ is disarmed, nothing plays, the outputs go to 0 V, a `disarmed` row is added to the trials log beside that trial's `armed` row, and the reply is `{"state": "idle", "ok": true}`. `rampdown_ms` omitted or null uses the ramp from the last condition's config.

### load_config

```json
{"cmd": "load_config", "path": "C:\\zapit\\configs\\uniAndBilateral_5_conditions.yml"}
```
```json
{"config": "uniAndBilateral_5_conditions.yml", "n_conditions": 5, "ok": true}
```

### set_experiment_path

```json
{"cmd": "set_experiment_path", "path": "D:\\data\\sub-001\\ses-03"}
```
```json
{"experiment_path": "D:\\data\\sub-001\\ses-03", "ok": true}
```

`{"cmd": "set_experiment_path", "path": null}` clears it and replies with `experiment_path` null. The folder is created if it does not exist; if it cannot be created the reply is an error and the path is unchanged.

### simulate_trigger

Only with the simulated DAQ (`simulated` true in `status`). Pretends a TTL arrived so a hardware-triggered trial plays without hardware.

```json
{"cmd": "simulate_trigger"}
```
```json
{"ok": true}
```

On real hardware it replies `{"ok": false, "error": "RuntimeError: Only available with the simulated DAQ"}`.

### Errors in the envelope

```json
{"cmd": "frobnicate"}
```
```json
{"ok": false, "error": "Unknown command 'frobnicate'"}
```
```json
[1, 2, 3]
```
```json
{"ok": false, "error": "Request must be a JSON object with a 'cmd' field"}
```

A message that is not valid JSON gets `{"ok": false, "error": "JSONDecodeError: ..."}`.

## Writing a client

The Python client in `zapit/client.py` is the reference. In outline, for any language:

1. Create a REQ socket, set a receive timeout, connect to `tcp://<host>:5555`.
2. Send one JSON object as a single UTF-8 frame. Receive one frame, parse JSON, check `ok`.
3. On timeout, close and recreate the socket before the next request. A REQ socket cannot send again until it has received.
4. Treat `ok` false as an exception with the `error` text.

```python
from zapit.client import ZapitClient

z = ZapitClient("tcp://localhost:5555")
z.ping()
r = z.send_samples(condition=None, laser_on=None, stim_duration_s=1.5, hardware_triggered=True)
print(r["condition"], r["laser_on"])   # now raise the TTL on PFI0
```

A client in any other language with a ZeroMQ binding, or a Bonsai workflow with `Bonsai.ZeroMQ`, does the same three calls. Nothing in the protocol is specific to Python.
