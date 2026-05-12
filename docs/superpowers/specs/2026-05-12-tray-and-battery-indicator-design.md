# Tray Icon + Battery Indicator — Design

**Date:** 2026-05-12
**Target file:** `app_flet.py` (the Tkinter `app.py.deprecated` is not touched)
**Scope:** Add a Windows system tray icon with a live battery-level indicator for the QMK keyboard, hybrid window close behavior, and two new user-configurable startup flags.

---

## 1. Goals

1. Read the wireless keyboard's battery level over HID (same channel already used for profile switching).
2. Display a battery icon in the Windows system tray, color-coded by charge level. Exact percent shown in the tooltip.
3. Hybrid window behavior: closing the main window hides it to tray; only the tray context menu can fully quit the app.
4. Two new flags in `profiles_config.json` → `settings`:
   - `start_minimized` — launch directly into tray, main window hidden.
   - `autostart_service` — start the background profile-switching service automatically on app launch.

## 2. Non-goals

- Battery support for the deprecated Tkinter `app.py.deprecated`.
- Battery support for wired keyboards (the feature simply shows "no data" if the device doesn't respond).
- Push notifications when battery is low (can be added later; out of scope here).
- Cross-platform tray (Windows only; matches existing `winotify` / `win32gui` dependencies).

## 3. Phases

This work is sequenced in three phases. Phase 0 is research and must succeed before phases 1–2 ship.

### Phase 0 — Reverse-engineer the battery HID protocol

The vendor's WebHID configurator at **https://qmk.top** displays the battery level, which proves the firmware exposes it. The existing `sniffer.js` only captures **outbound** writes (`sendFeatureReport`, `sendReport`); battery readings are **inbound** responses, so the sniffer must be extended.

**Sniffer changes (`sniffer.js`):**

- Hook `HIDDevice.prototype.receiveFeatureReport` — log `reportId` plus the `DataView` returned by the Promise.
- Capture `inputreport` events — wrap `HIDDevice.prototype.addEventListener` so listeners registered after the hook are observed, and also attach a global listener to every device returned by `navigator.hid.getDevices()` at hook-install time.
- New commands:
  - `qmkSniffer.armBattery()` — sets a mode where every TX and RX packet is captured and tagged (`tx` / `rx`), preserving order so request/response pairs can be matched.
  - `qmkSniffer.export()` — now returns two arrays: `outgoing` (existing) and `incoming` (new), each with `reportId`, `data`, `ts`, and `label`.

**Workflow for the user:**

1. Open https://qmk.top, connect the keyboard, run the updated sniffer in DevTools.
2. Call `qmkSniffer.armBattery()`.
3. Trigger the battery readout in the web UI (refresh button or whatever exposes the level).
4. Read the captured pair: the TX bytes are the query; in the RX bytes look for a byte whose value matches the percentage shown in the web UI (e.g. `0x55` for 85%). Note the offset.
5. Record the result in the config (see §4).

This phase produces concrete values; without them, phases 1–2 cannot ship.

### Phase 1 — `battery.py` module

A self-contained reader. No knowledge of UI or tray.

```python
# battery.py
@dataclass
class BatteryState:
    percent: int | None       # None ⇒ unknown / read failed
    charging: bool            # False if firmware doesn't expose this
    updated_at: datetime
    is_stale: bool            # True when last read failed

class BatteryMonitor:
    def __init__(self, config_battery: dict, usb_lock: threading.Lock,
                 get_device_path: Callable[[], str | None]): ...
    def read_once(self) -> None: ...
    @property
    def state(self) -> BatteryState: ...
```

**Behavior:**

- `read_once()` acquires `usb_lock` (same lock as profile writes), opens the HID path, sends `config_battery["query"]` as a feature report, calls `get_feature_report(report_id, length)` to read the response, extracts the byte at `config_battery["response_offset"]`, multiplies by `config_battery["response_scale"]` (default 1), and stores the result.
- If `charging_offset` / `charging_mask` are present in the config, parse those too. Otherwise `charging = False`.
- Any exception (`IOError`, `OSError`, device-not-found, malformed response) → `state.percent = None`, `is_stale = True`. The previous good value is **not** retained — this is the user-confirmed "grey question-mark" behavior.
- Logs to stderr via `print(...)`; never raises out.
- `read_once()` typical duration target: <50 ms while holding `usb_lock`, so profile-switching latency is unaffected.

**Polling:** `battery.py` does not own a thread. A polling thread in `app_flet.py` calls `read_once()` every 60 seconds, then pushes `state` to consumers.

### Phase 2 — `tray.py` module

Tray icon, context menu, icon rendering. No HID knowledge.

**Library:** `pystray` (mature, pure-Python tray for Windows, used by many production apps). Pillow is used to render the icon image. Both added to dependencies.

**API:**

```python
class TrayIcon:
    def __init__(self,
                 on_toggle_window: Callable[[], None],
                 on_show: Callable[[], None],
                 on_hide: Callable[[], None],
                 on_quit: Callable[[], None]): ...
    def start(self) -> None: ...                 # runs pystray in its own thread
    def stop(self) -> None: ...
    def update_battery(self, state: BatteryState) -> None: ...
```

**Icon rendering** (`Pillow`, 32×32 RGBA):

- Battery outline (rounded rectangle with a small "nub" on the right) drawn in white/grey.
- Inner fill width proportional to `percent` (0–100 → 0–22 px inside the outline).
- Fill color:
  - `>= 50` → green (`#3CB371`)
  - `20–49` → yellow (`#E5A50A`)
  - `< 20` → red (`#D04437`)
- If `charging` → overlay a small white lightning bolt centered on the fill.
- If `percent is None` (stale) → grey outline + centered "?" glyph, no fill.

**Tooltip text** (set on every `update_battery`):

- Normal: `QMK Manager — Battery: 87%` (or `87% ⚡` if charging).
- Stale: `QMK Manager — Battery: no data`.

**Click behavior:**

- Left-click on the tray icon → `on_toggle_window()` (show if hidden, hide if visible).
- Right-click → context menu:
  - **Show** — `on_show()` (disabled if already visible).
  - **Hide** — `on_hide()` (disabled if already hidden).
  - separator
  - **Exit** — `on_quit()`.

Visibility-driven enable/disable of Show/Hide items is updated whenever `app_flet.py` reports a window-state change to the tray (a `set_window_visible(bool)` method on `TrayIcon` keeps the menu in sync).

### Phase 3 — `app_flet.py` integration

The existing file gains:

**Startup sequence:**

1. Load config (existing).
2. Apply config migration: ensure `settings.start_minimized` (default `false`) and `settings.autostart_service` (default `true`) exist; if not, write them back.
3. Construct `BatteryMonitor` and `TrayIcon`.
4. Start the tray thread (always — tray exists even before service starts).
5. If `settings.autostart_service` → start the existing background scanner thread.
6. Start a new daemon thread `battery_poll_loop`:
   ```
   while app_alive:
       monitor.read_once()
       tray.update_battery(monitor.state)
       main_window.publish_battery(monitor.state)  # for the UI badge
       sleep(60)
   ```
7. Build the Flet page. If `settings.start_minimized` is `true`, call `page.window_visible = False` immediately before `page.update()`; otherwise show normally.

**Window close hook (hybrid behavior):**

- Register `page.on_window_event = handle_window_event`.
- When `e.data == "close"`: set `page.window_prevent_close = True`, `page.window_visible = False`, `page.update()`, and call `tray.set_window_visible(False)`.
- The first time this happens in a given app run, also show a `winotify` toast: *"QMK Manager продолжает работать в трее. Выйти можно из меню иконки."* — gated by an in-memory flag, not persisted.

**Show / hide from tray:**

- `on_show` callback → `page.window_visible = True; page.window_to_front(); page.update(); tray.set_window_visible(True)`.
- `on_hide` callback → mirror of close-to-tray.
- `on_toggle_window` → flip based on current `page.window_visible`.
- All three callbacks run on the pystray thread; they must marshal Flet updates through `page.run_thread(...)` (Flet's thread-safe entry point) — otherwise Flet's event loop can race.

**Quit from tray:**

- `on_quit` callback → set `app_alive = False` (stops both the scanner thread and the battery poll), call `tray.stop()`, then `page.window_destroy()`. Daemon threads exit with the process.

**New UI: Settings section in the main window:**

A new collapsible card (or tab — match existing Material 3 layout) with two switches:

- `Запускать свёрнутым в трей` — bound to `settings.start_minimized`.
- `Автоматически запускать службу` — bound to `settings.autostart_service`.

Toggling a switch writes the config to disk immediately (reuses existing `save_config()` flow). No restart prompt — both flags only take effect on next launch; show small italic helper text under each switch: *"применится при следующем запуске"*.

**Battery badge inside the main window:**

A small chip in the top-right of the existing header: battery icon + `87%` text + last-update relative time on hover (Flet tooltip). Updates from the same `monitor.state` pushed by the polling loop. A small refresh-icon button next to it triggers a one-off `monitor.read_once()` on a worker thread (must not block the UI thread).

## 4. Config schema additions

`profiles_config.json` gains two top-level sections; both are added by a migration step on first launch:

```json
{
  "settings": {
    "start_minimized": false,
    "autostart_service": true
  },
  "battery": {
    "query": [0, 0, 0],
    "report_id": 0,
    "response_length": 32,
    "response_offset": 0,
    "response_scale": 1,
    "charging_offset": null,
    "charging_mask": 0
  }
}
```

The `battery` block ships with placeholder values; the user fills `query`, `report_id`, `response_offset` (and optionally `charging_*`) after Phase 0 sniffing. While the placeholders are present, `read_once()` will detect a malformed/no response and the tray shows the grey "?" state — no crash, no nag.

## 5. Threading & locking summary

| Thread                | Owner            | Touches                                  | Lock          |
|-----------------------|------------------|------------------------------------------|---------------|
| Flet event loop       | Flet             | UI                                       | —             |
| Window scanner        | `app_flet.py`    | HID writes (profile switch)              | `usb_lock`    |
| Battery poll (60 s)   | `app_flet.py`    | HID write+read (query/response)          | `usb_lock`    |
| Tray (`pystray`)      | `tray.py`        | Callbacks marshal back to Flet via `page.run_thread` | —    |

Single `usb_lock` ensures only one HID transaction at a time. Battery reads are short (<50 ms) so they don't block profile switching meaningfully.

## 6. Dependencies added

- `pystray` — tray icon.
- `Pillow` — icon rendering (pystray already depends on it transitively, but pin it explicitly).

No removals.

## 7. Error modes & user-visible behavior

| Condition                                    | Tray icon            | Tooltip                                | Main window badge       |
|----------------------------------------------|----------------------|----------------------------------------|-------------------------|
| Healthy read                                 | Color-coded battery  | `Battery: 87%`                         | `87%`                   |
| Charging                                     | Battery + bolt       | `Battery: 87% ⚡`                       | `87% ⚡`                 |
| HID failure / device not found / malformed   | Grey outline + `?`   | `Battery: no data`                     | `—`                     |
| Service stopped (manual)                     | Same as above states | unchanged                              | unchanged               |

The service running/stopped state does **not** affect battery reads — battery polling continues independently.

## 8. Testing notes

- `battery.py` is unit-testable with a mock `hid.device` stand-in: feed canned response bytes, assert `state.percent`.
- `tray.py` icon rendering is testable by calling `update_battery(...)` with synthetic states and inspecting the produced `PIL.Image` (e.g. histogram of fill color).
- End-to-end tray click behavior requires a manual smoke test on Windows: launch, close window, click tray, right-click → Show / Hide / Exit, restart with each combination of the two new flags.

## 9. Out-of-scope follow-ups (not in this spec)

- Low-battery toast notifications.
- Charging-detection if Phase 0 doesn't surface it (revisit later).
- Localization of tray menu strings (currently mixed RU/EN to match existing UI).
- Cross-platform tray.
