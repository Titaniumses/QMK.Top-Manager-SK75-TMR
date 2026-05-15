# Auto Battery Probe in LearnMode + Debug Logging

## Overview

Two features for the QMK Profile Switcher app:

1. **Auto battery probe in LearnMode** — automatically test each sniffer packet as a battery query and display the result inline
2. **Debug logging** — comprehensive logging controlled by a config flag, covering all HID communication, profile switching, battery reads, and sniffer events

## 1. Debug Logging

### Config

Add `"debug": true` to `profiles_config.json` → `settings`:

```json
{
  "settings": {
    "start_minimized": false,
    "autostart_service": false,
    "debug": false
  }
}
```

### Initialization

At app startup in `app_flet.py`, before any other logic:

- Read `settings.debug` from config (default `false`)
- If `true`: configure Python `logging` at `DEBUG` level, write to `debug.log` in app directory, overwrite on each launch (`filemode='w'`)
- If `false`: configure at `WARNING` level, no file handler
- Format: `[2026-05-16 12:00:00.123] [DEBUG] [module_name] message`

### What to log

| Area | What | Level |
|------|------|-------|
| HID dispatch (`apply_payload`) | path, payload hex, report type, return code, duration | DEBUG |
| Battery read (`BatteryMonitor.read_once`) | query hex, raw response hex, parsed percent, errors | DEBUG |
| Profile switch (`background_task`) | active process, matched profile, payload sent, result | DEBUG |
| Sniffer events (`_on_sniff_event`) | raw CDP event, classification, filter result | DEBUG |
| Auto battery probe (new) | packet hex, response hex, parsed result or error | DEBUG |
| Config load/save | loaded values, changed fields | INFO |
| Errors | any exception in HID/battery/sniffer | ERROR |

### Module setup

Each module (`battery.py`, `sniffer.py`, `tray.py`) uses `logger = logging.getLogger(__name__)`. Configuration happens once in `app_flet.py`.

## 2. Auto Battery Probe in LearnMode

### Preconditions

Auto probe activates only when ALL of these are true:

- LearnMode switch is ON (`self._sniff_learn_mode = True`)
- Active device has a `battery` config with at least `response_length`, `response_offset`, `response_scale`
- A sniffer packet has been displayed in the UI (passed all existing filters)

### Architecture

```
Sniffer CDP events
      │
      ▼
  _on_sniff_event()  ──► display packet row in ListView
      │                        │
      │                        ▼
      │               battery_result_text = ft.Text("") 
      │                        │
      └──► put packet data into self._battery_probe_queue (queue.Queue)
                               │
                               ▼
                    _battery_probe_worker() [daemon thread]
                               │
                     ┌─── while learn_mode: ────┐
                     │  queue.get(timeout=1)     │
                     │  sleep(0.2) cooldown      │
                     │  acquire usb_lock         │
                     │  send_feature_report()    │
                     │  get_feature_report()     │
                     │  parse response           │
                     │  update UI via            │
                     │    page.run_thread()      │
                     └───────────────────────────┘
```

### Worker thread

- Single daemon thread `_battery_probe_worker`, started when LearnMode is toggled ON, stops when toggled OFF
- Reads from `self._battery_probe_queue` (a `queue.Queue`)
- For each packet:
  1. Sleep 200ms (cooldown between probes)
  2. Acquire `self.usb_lock`
  3. Open HID device, `send_feature_report([report_id] + packet_data)`, `get_feature_report(report_id, response_length)`
  4. Parse: `response[response_offset] * response_scale`, clamp 0-100
  5. Release lock
  6. Update the packet's UI row via `page.run_thread()`
- Timeout: `get_feature_report` wrapped in try/except, 1s HID read timeout (nonblocking mode + polling)
- If packet doesn't produce valid battery %: show nothing or grey dash
- All steps logged at DEBUG level

### Queue item format

```python
@dataclass
class BatteryProbeItem:
    packet_data: list[int]
    result_text: ft.Text  # reference to UI element to update
```

### UI changes in sniffer modal

Each packet row in LearnMode gets an additional `ft.Text` at the end:

- Initially empty
- On successful probe: green text `🔋 87%`
- On failed probe: grey `—`
- Existing buttons ("В слот" / "Как battery query") remain unchanged

### HID safety

- Same `usb_lock` as `apply_payload()` and `BatteryMonitor.read_once()` — no concurrent device access
- One worker thread, sequential processing — no parallel HID writes
- 200ms cooldown between probes — device not flooded
- Worker stops immediately when LearnMode is toggled OFF (queue cleared, thread joins)
- If device is busy (lock contention), worker blocks on lock — no data corruption

### Battery config source

Uses the current device's `battery` config from `profiles_config.json`:

- `query` field is IGNORED (we're testing different queries — that's the point)
- `report_id`, `response_length`, `response_offset`, `response_scale` are used for parsing the response
- If no battery config exists for the device, auto probe is disabled (buttons still work for manual assignment)

### Future extensibility (not implemented now)

Option B: try multiple known parsing templates when no battery config exists. This would require a list of common `(response_offset, response_scale, response_length)` tuples. Architecture supports this — the worker just iterates templates instead of using a single config.

## Files modified

| File | Changes |
|------|---------|
| `app_flet.py` | Logging init, `_battery_probe_queue`, `_battery_probe_worker()`, LearnMode toggle start/stop worker, sniffer row UI update, `settings.debug` read/write |
| `battery.py` | Add `logging.getLogger(__name__)` calls throughout `read_once()` |
| `sniffer.py` | Add `logging.getLogger(__name__)` calls throughout `_listen()`, `_on_sniff_event()` |
| `profiles_config.json` | Add `"debug": false` to settings |

## Not in scope

- Mechanical keyboard investigation (will use the logs produced by this work)
- Multiple parsing template probing (Option B — future)
- Log rotation (single file, overwritten per session)
