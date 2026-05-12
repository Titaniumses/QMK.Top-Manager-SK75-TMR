# Tray Icon + Battery Indicator — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Windows system tray icon with a live battery-level indicator for the QMK keyboard, hybrid window-close behavior, and two new user-configurable startup flags (`start_minimized`, `autostart_service`).

**Architecture:** Three new modules — `sniffer.js` (extended), `battery.py` (HID battery reader), `tray.py` (pystray icon + menu) — plus targeted integration in `app_flet.py`. Single shared `usb_lock` serializes all HID I/O. A 60-second polling thread feeds battery state to both the tray icon and a badge in the main window.

**Tech Stack:** Python 3, Flet, hid (cython-hidapi), pystray, Pillow, win32gui, winotify, threading, JavaScript (WebHID, browser DevTools).

**Spec:** `docs/superpowers/specs/2026-05-12-tray-and-battery-indicator-design.md`

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `sniffer.js` | Modify | Capture both outbound writes AND inbound reads (`receiveFeatureReport`, `inputreport`). Add `armBattery()` mode, dual-list export. |
| `battery.py` | Create | `BatteryState` dataclass + `BatteryMonitor` class. Single method `read_once()` that talks HID under `usb_lock` and stores last result. |
| `tray.py` | Create | `TrayIcon` class wrapping `pystray.Icon`. Renders battery PNG with Pillow, wires context menu and click handler. |
| `app_flet.py` | Modify | Config migration for new `settings` + `battery` blocks. Wire `BatteryMonitor`, `TrayIcon`, polling thread, hybrid-close handler, settings UI card, header battery badge. |
| `tests/test_battery.py` | Create | Unit tests for `BatteryMonitor.read_once()` parsing and error handling using a fake HID device. |
| `tests/test_tray_render.py` | Create | Unit tests for icon rendering (color thresholds, stale state) using a synthetic `BatteryState`. |
| `requirements.txt` | Create or modify | Pin `pystray`, `Pillow`. (Project currently has no requirements file — create one with all deps.) |
| `profiles_config.json` | Modify (auto-migrated) | Gains `settings` and `battery` sections on first launch. |

---

## Phase 0 — Extend `sniffer.js` to capture battery protocol

User must run this on https://qmk.top to determine the actual battery query/response bytes before Phase 2 of this plan can be useful in production. The code below ships even before bytes are known.

### Task 1: Hook inbound HID in `sniffer.js`

**Files:**
- Modify: `sniffer.js` (full rewrite — current file is small enough that it's clearer to replace)

- [ ] **Step 1: Replace `sniffer.js` with the extended version below**

```javascript
// QMK Profile Sniffer v3
// Captures BOTH outbound writes (sendFeatureReport / sendReport)
// AND inbound responses (receiveFeatureReport return values + inputreport events).
// Paste into DevTools at https://qmk.top, then exercise the UI.
// Use qmkSniffer.armBattery() before clicking the battery refresh in the web UI.

(function () {
    if (window.qmkSniffer) {
        console.warn("Sniffer уже активен. Сброс старого состояния.");
        window.qmkSniffer.stop();
    }

    const captured = []; // unified ordered log: { dir, type, reportId, data, label, ts }
    let armed = false;
    let batteryMode = false;
    let pendingLabel = null;

    const origSendFeature = HIDDevice.prototype.sendFeatureReport;
    const origSendOutput = HIDDevice.prototype.sendReport;
    const origRecvFeature = HIDDevice.prototype.receiveFeatureReport;

    function toHex(arr) {
        return Array.from(arr).map(b => '0x' + b.toString(16).padStart(2, '0')).join(', ');
    }

    function toBytes(data) {
        if (data instanceof DataView) {
            return Array.from(new Uint8Array(data.buffer, data.byteOffset, data.byteLength));
        }
        return Array.from(new Uint8Array(data.buffer || data));
    }

    function record(dir, type, reportId, bytes) {
        if (!armed && !batteryMode) return;
        const entry = {
            dir, type, reportId,
            data: bytes,
            label: pendingLabel || `${dir}_${captured.length + 1}`,
            ts: new Date().toISOString()
        };
        captured.push(entry);
        const color = dir === 'tx' ? '#ffaa00' : '#00ff88';
        console.log(
            `%c[#${captured.length}] ${dir.toUpperCase()} ${type} reportId=${reportId} (${entry.label})`,
            `color:${color};font-weight:bold`
        );
        console.log("  Bytes:", toHex(bytes));
        pendingLabel = null;
    }

    HIDDevice.prototype.sendFeatureReport = function (reportId, data) {
        record('tx', 'feature', reportId, toBytes(data));
        return origSendFeature.apply(this, arguments);
    };
    HIDDevice.prototype.sendReport = function (reportId, data) {
        record('tx', 'output', reportId, toBytes(data));
        return origSendOutput.apply(this, arguments);
    };
    HIDDevice.prototype.receiveFeatureReport = function (reportId) {
        const promise = origRecvFeature.apply(this, arguments);
        return promise.then(view => {
            record('rx', 'feature', reportId, toBytes(view));
            return view;
        });
    };

    function attachInputListener(device) {
        if (device.__qmkInputAttached) return;
        device.__qmkInputAttached = true;
        device.addEventListener('inputreport', (event) => {
            record('rx', 'input', event.reportId, toBytes(event.data));
        });
    }

    if (navigator.hid && navigator.hid.getDevices) {
        navigator.hid.getDevices().then(devs => devs.forEach(attachInputListener));
    }

    window.qmkSniffer = {
        arm(label) {
            pendingLabel = label || null;
            armed = true;
            console.log(
                `%c[ARMED] Жду следующий пакет${label ? ` для "${label}"` : ""}.`,
                "color:#ffaa00;font-weight:bold"
            );
        },
        armBattery() {
            batteryMode = true;
            armed = false;
            pendingLabel = null;
            console.log(
                "%c[BATTERY MODE] Логирую ВСЕ tx/rx. Нажми кнопку обновления батареи на qmk.top, потом qmkSniffer.export().",
                "color:#00aaff;font-weight:bold"
            );
        },
        disarm() {
            armed = false;
            batteryMode = false;
            console.log("[DISARMED]");
        },
        list() {
            console.table(captured.map((c, i) => ({
                idx: i, dir: c.dir, type: c.type, reportId: c.reportId,
                label: c.label, firstBytes: toHex(c.data.slice(0, 8))
            })));
        },
        rename(idx, label) {
            if (captured[idx]) { captured[idx].label = label; }
        },
        remove(idx) { captured.splice(idx, 1); },
        clear() { captured.length = 0; },
        export() {
            const outgoing = captured.filter(c => c.dir === 'tx');
            const incoming = captured.filter(c => c.dir === 'rx');
            const result = { outgoing, incoming, all: captured };
            const json = JSON.stringify(result, null, 2);
            console.log("%c=== sniffer export ===", "color:#00aaff;font-weight:bold");
            console.log(json);
            try { navigator.clipboard.writeText(json); console.log("(copied to clipboard)"); }
            catch (e) { }
            return result;
        },
        stop() {
            HIDDevice.prototype.sendFeatureReport = origSendFeature;
            HIDDevice.prototype.sendReport = origSendOutput;
            HIDDevice.prototype.receiveFeatureReport = origRecvFeature;
            delete window.qmkSniffer;
            console.log("Sniffer остановлен.");
        }
    };

    console.log("%cQMK Sniffer v3 готов", "color:#00ff00;font-size:14px;font-weight:bold");
    console.log("Команды:");
    console.log("  qmkSniffer.armBattery()   — режим логирования всех tx/rx (для батареи)");
    console.log("  qmkSniffer.arm('Gaming')  — поймать следующий tx (как раньше)");
    console.log("  qmkSniffer.list()         — таблица пойманного");
    console.log("  qmkSniffer.export()       — JSON {outgoing, incoming} в консоль + буфер");
    console.log("  qmkSniffer.clear()        — очистить");
    console.log("  qmkSniffer.stop()         — снять хуки");
})();
```

- [ ] **Step 2: Manual smoke test (user runs in browser)**

In a browser, open https://qmk.top, connect the keyboard, paste the new `sniffer.js` into DevTools console. Expect: `QMK Sniffer v3 готов` log line. Then run `qmkSniffer.armBattery()`, click whatever refreshes battery in the web UI, and `qmkSniffer.export()`. Expected: `outgoing` array contains the query bytes, `incoming` array contains the response bytes. Identify the byte that equals the displayed battery percent.

- [ ] **Step 3: Commit**

```bash
git add sniffer.js
git commit -m "sniffer: capture inbound HID (receiveFeatureReport + inputreport)"
```

---

## Phase 1 — `battery.py` module (TDD)

Build the reader against a fake HID device first; real bytes get plugged in at config time, not in code.

### Task 2: Create `BatteryState` dataclass and failing test

**Files:**
- Create: `battery.py`
- Create: `tests/__init__.py` (empty)
- Create: `tests/test_battery.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_battery.py`:

```python
import threading
from datetime import datetime, timedelta

import pytest

from battery import BatteryMonitor, BatteryState


class FakeHidDevice:
    """Stand-in for hid.device(). Records sent feature reports, returns canned response."""
    def __init__(self, response_bytes=None, raise_on_open=False, raise_on_send=False):
        self._response = response_bytes or []
        self._raise_open = raise_on_open
        self._raise_send = raise_on_send
        self.opened_path = None
        self.sent = []
        self.closed = False

    def open_path(self, path):
        if self._raise_open:
            raise IOError("device unavailable")
        self.opened_path = path

    def set_nonblocking(self, value):
        pass

    def send_feature_report(self, data):
        if self._raise_send:
            raise IOError("send failed")
        self.sent.append(list(data))
        return len(data)

    def get_feature_report(self, report_id, length):
        return list(self._response[:length])

    def close(self):
        self.closed = True


def make_monitor(fake_device, config_battery=None, path="\\\\fake\\path"):
    config = config_battery or {
        "query": [0xAB, 0xCD],
        "report_id": 0,
        "response_length": 8,
        "response_offset": 2,
        "response_scale": 1,
        "charging_offset": None,
        "charging_mask": 0,
    }
    return BatteryMonitor(
        config_battery=config,
        usb_lock=threading.Lock(),
        get_device_path=lambda: path,
        hid_device_factory=lambda: fake_device,
    )


def test_initial_state_is_unknown_and_stale():
    monitor = make_monitor(FakeHidDevice(response_bytes=[0, 0, 85, 0, 0, 0, 0, 0]))
    assert monitor.state.percent is None
    assert monitor.state.is_stale is True
    assert monitor.state.charging is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_battery.py::test_initial_state_is_unknown_and_stale -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'battery'`.

- [ ] **Step 3: Create minimal `battery.py`**

```python
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Callable, Optional


@dataclass
class BatteryState:
    percent: Optional[int] = None
    charging: bool = False
    updated_at: datetime = field(default_factory=datetime.now)
    is_stale: bool = True


class BatteryMonitor:
    def __init__(
        self,
        config_battery: dict,
        usb_lock: Lock,
        get_device_path: Callable[[], Optional[str]],
        hid_device_factory: Optional[Callable[[], object]] = None,
    ):
        self._config = config_battery
        self._usb_lock = usb_lock
        self._get_path = get_device_path
        if hid_device_factory is None:
            import hid
            hid_device_factory = hid.device
        self._make_device = hid_device_factory
        self._state = BatteryState()

    @property
    def state(self) -> BatteryState:
        return self._state

    def read_once(self) -> None:
        # Implemented in next task.
        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_battery.py::test_initial_state_is_unknown_and_stale -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add battery.py tests/__init__.py tests/test_battery.py
git commit -m "battery: scaffold BatteryMonitor + BatteryState"
```

### Task 3: Implement `read_once()` happy path

**Files:**
- Modify: `battery.py:read_once`
- Modify: `tests/test_battery.py` (add tests)

- [ ] **Step 1: Add failing tests for happy path**

Append to `tests/test_battery.py`:

```python
def test_read_once_parses_percent_at_offset():
    fake = FakeHidDevice(response_bytes=[0xAA, 0xBB, 87, 0, 0, 0, 0, 0])
    monitor = make_monitor(fake)
    monitor.read_once()
    assert monitor.state.percent == 87
    assert monitor.state.is_stale is False
    assert monitor.state.charging is False
    assert fake.sent == [[0xAB, 0xCD]]
    assert fake.closed is True


def test_read_once_applies_response_scale():
    fake = FakeHidDevice(response_bytes=[0, 0, 50, 0, 0, 0, 0, 0])
    config = {
        "query": [0x01],
        "report_id": 0,
        "response_length": 8,
        "response_offset": 2,
        "response_scale": 2,
        "charging_offset": None,
        "charging_mask": 0,
    }
    monitor = make_monitor(fake, config_battery=config)
    monitor.read_once()
    assert monitor.state.percent == 100  # 50 * 2


def test_read_once_clamps_above_100():
    fake = FakeHidDevice(response_bytes=[0, 0, 200, 0, 0, 0, 0, 0])
    monitor = make_monitor(fake)
    monitor.read_once()
    assert monitor.state.percent == 100


def test_read_once_clamps_below_0():
    # Negative shouldn't happen, but guard anyway. response_scale=-1 to force it.
    fake = FakeHidDevice(response_bytes=[0, 0, 5, 0, 0, 0, 0, 0])
    config = {
        "query": [0x01], "report_id": 0, "response_length": 8,
        "response_offset": 2, "response_scale": -1,
        "charging_offset": None, "charging_mask": 0,
    }
    monitor = make_monitor(fake, config_battery=config)
    monitor.read_once()
    assert monitor.state.percent == 0


def test_read_once_detects_charging():
    # Bit 7 set in byte 3 means charging.
    fake = FakeHidDevice(response_bytes=[0, 0, 60, 0x80, 0, 0, 0, 0])
    config = {
        "query": [0x01], "report_id": 0, "response_length": 8,
        "response_offset": 2, "response_scale": 1,
        "charging_offset": 3, "charging_mask": 0x80,
    }
    monitor = make_monitor(fake, config_battery=config)
    monitor.read_once()
    assert monitor.state.percent == 60
    assert monitor.state.charging is True


def test_read_once_charging_false_when_mask_unset():
    fake = FakeHidDevice(response_bytes=[0, 0, 60, 0x00, 0, 0, 0, 0])
    config = {
        "query": [0x01], "report_id": 0, "response_length": 8,
        "response_offset": 2, "response_scale": 1,
        "charging_offset": 3, "charging_mask": 0x80,
    }
    monitor = make_monitor(fake, config_battery=config)
    monitor.read_once()
    assert monitor.state.charging is False
```

- [ ] **Step 2: Run tests, expect failures**

Run: `pytest tests/test_battery.py -v`
Expected: 6 failures (initial test still passes, the 5 new ones fail because `read_once` is a no-op).

- [ ] **Step 3: Implement `read_once()`**

Replace the `read_once` method body in `battery.py`:

```python
    def read_once(self) -> None:
        path = self._get_path()
        if path is None:
            self._mark_failure("device path unavailable")
            return
        with self._usb_lock:
            device = self._make_device()
            try:
                device.open_path(path)
                device.set_nonblocking(1)
                report_id = self._config.get("report_id", 0)
                query = [report_id] + list(self._config["query"])
                device.send_feature_report(query)
                response_length = self._config.get("response_length", 32)
                response = device.get_feature_report(report_id, response_length)
            except Exception as exc:
                self._mark_failure(f"HID error: {exc}")
                try:
                    device.close()
                except Exception:
                    pass
                return
            try:
                device.close()
            except Exception:
                pass

        try:
            offset = self._config["response_offset"]
            scale = self._config.get("response_scale", 1)
            raw = response[offset]
            percent = max(0, min(100, int(raw * scale)))

            charging = False
            ch_offset = self._config.get("charging_offset")
            ch_mask = self._config.get("charging_mask", 0)
            if ch_offset is not None and ch_mask:
                charging = bool(response[ch_offset] & ch_mask)

            self._state = BatteryState(
                percent=percent,
                charging=charging,
                updated_at=datetime.now(),
                is_stale=False,
            )
        except (IndexError, KeyError, TypeError) as exc:
            self._mark_failure(f"parse error: {exc}")

    def _mark_failure(self, reason: str) -> None:
        import sys
        print(f"[BatteryMonitor] read failed: {reason}", file=sys.stderr)
        self._state = BatteryState(
            percent=None,
            charging=False,
            updated_at=datetime.now(),
            is_stale=True,
        )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_battery.py -v`
Expected: all 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add battery.py tests/test_battery.py
git commit -m "battery: implement read_once with parse + charging detection"
```

### Task 4: Error-handling tests

**Files:**
- Modify: `tests/test_battery.py`

- [ ] **Step 1: Add failing tests**

Append:

```python
def test_read_once_no_device_path_marks_stale():
    fake = FakeHidDevice(response_bytes=[0, 0, 50, 0, 0, 0, 0, 0])
    config = {
        "query": [0x01], "report_id": 0, "response_length": 8,
        "response_offset": 2, "response_scale": 1,
        "charging_offset": None, "charging_mask": 0,
    }
    monitor = BatteryMonitor(
        config_battery=config,
        usb_lock=threading.Lock(),
        get_device_path=lambda: None,
        hid_device_factory=lambda: fake,
    )
    monitor.read_once()
    assert monitor.state.percent is None
    assert monitor.state.is_stale is True


def test_read_once_open_failure_marks_stale():
    fake = FakeHidDevice(raise_on_open=True)
    monitor = make_monitor(fake)
    monitor.read_once()
    assert monitor.state.percent is None
    assert monitor.state.is_stale is True


def test_read_once_send_failure_marks_stale_and_closes():
    fake = FakeHidDevice(response_bytes=[0]*8, raise_on_send=True)
    monitor = make_monitor(fake)
    monitor.read_once()
    assert monitor.state.percent is None
    assert monitor.state.is_stale is True
    assert fake.closed is True


def test_read_once_short_response_marks_stale():
    fake = FakeHidDevice(response_bytes=[0, 0])  # offset 2 will IndexError
    monitor = make_monitor(fake)
    monitor.read_once()
    assert monitor.state.percent is None
    assert monitor.state.is_stale is True


def test_read_once_does_not_retain_previous_value_on_failure():
    fake_good = FakeHidDevice(response_bytes=[0, 0, 75, 0, 0, 0, 0, 0])
    monitor = make_monitor(fake_good)
    monitor.read_once()
    assert monitor.state.percent == 75

    # Now swap factory to a failing device.
    fake_bad = FakeHidDevice(raise_on_open=True)
    monitor._make_device = lambda: fake_bad
    monitor.read_once()
    assert monitor.state.percent is None  # Last good value NOT retained.
    assert monitor.state.is_stale is True
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_battery.py -v`
Expected: all 11 PASS (no implementation changes needed — these confirm existing behavior).

- [ ] **Step 3: Commit**

```bash
git add tests/test_battery.py
git commit -m "battery: error-path tests (no path / open / send / short response / no retention)"
```

---

## Phase 2 — `tray.py` module (TDD where possible)

### Task 5: Scaffold `TrayIcon` class with rendering test

**Files:**
- Create: `tray.py`
- Create: `tests/test_tray_render.py`

- [ ] **Step 1: Add Pillow + pystray to a `requirements.txt`**

Create `requirements.txt`:

```
flet
hid
pywin32
psutil
keyboard
winotify
pystray
Pillow
```

- [ ] **Step 2: Install new deps**

```bash
pip install pystray Pillow
```

Expected: both install successfully.

- [ ] **Step 3: Write failing rendering test**

Create `tests/test_tray_render.py`:

```python
from datetime import datetime

from PIL import Image

from battery import BatteryState
from tray import render_battery_image


def _color_counts(img: Image.Image):
    counts = {}
    for px in img.getdata():
        counts[px] = counts.get(px, 0) + 1
    return counts


def test_render_returns_32x32_rgba():
    state = BatteryState(percent=80, charging=False, updated_at=datetime.now(), is_stale=False)
    img = render_battery_image(state)
    assert img.size == (32, 32)
    assert img.mode == "RGBA"


def test_high_charge_uses_green():
    state = BatteryState(percent=80, charging=False, updated_at=datetime.now(), is_stale=False)
    img = render_battery_image(state)
    counts = _color_counts(img)
    # Green fill #3CB371 must dominate over yellow/red.
    green_px = sum(c for px, c in counts.items() if px[0] < 100 and px[1] > 150 and px[2] < 150)
    red_px = sum(c for px, c in counts.items() if px[0] > 180 and px[1] < 100 and px[2] < 100)
    assert green_px > 0
    assert green_px > red_px


def test_low_charge_uses_red():
    state = BatteryState(percent=10, charging=False, updated_at=datetime.now(), is_stale=False)
    img = render_battery_image(state)
    counts = _color_counts(img)
    red_px = sum(c for px, c in counts.items() if px[0] > 180 and px[1] < 100 and px[2] < 100)
    green_px = sum(c for px, c in counts.items() if px[0] < 100 and px[1] > 150 and px[2] < 150)
    assert red_px > 0
    assert red_px > green_px


def test_mid_charge_uses_yellow():
    state = BatteryState(percent=35, charging=False, updated_at=datetime.now(), is_stale=False)
    img = render_battery_image(state)
    counts = _color_counts(img)
    yellow_px = sum(c for px, c in counts.items() if px[0] > 200 and px[1] > 130 and px[2] < 100)
    assert yellow_px > 0


def test_stale_state_renders_no_color_fill():
    state = BatteryState(percent=None, charging=False, updated_at=datetime.now(), is_stale=True)
    img = render_battery_image(state)
    counts = _color_counts(img)
    green_px = sum(c for px, c in counts.items() if px[0] < 100 and px[1] > 150 and px[2] < 150)
    red_px = sum(c for px, c in counts.items() if px[0] > 180 and px[1] < 100 and px[2] < 100)
    assert green_px == 0
    assert red_px == 0


def test_charging_overlay_present():
    state = BatteryState(percent=80, charging=True, updated_at=datetime.now(), is_stale=False)
    img = render_battery_image(state)
    counts = _color_counts(img)
    # Charging overlay is white (255,255,255,*); must exist somewhere inside the fill area.
    white_px = sum(c for px, c in counts.items() if px[0] == 255 and px[1] == 255 and px[2] == 255 and px[3] > 0)
    assert white_px > 0
```

- [ ] **Step 4: Run tests, expect failures**

Run: `pytest tests/test_tray_render.py -v`
Expected: 6 ImportErrors (`tray` module missing).

- [ ] **Step 5: Implement `tray.py` rendering function**

Create `tray.py`:

```python
import threading
from typing import Callable, Optional

import pystray
from PIL import Image, ImageDraw, ImageFont

from battery import BatteryState


_ICON_SIZE = 32

_OUTLINE_COLOR = (220, 220, 220, 255)
_GREY_COLOR = (140, 140, 140, 255)
_GREEN = (60, 179, 113, 255)    # >=50
_YELLOW = (229, 165, 10, 255)   # 20..49
_RED = (208, 68, 55, 255)       # <20


def _fill_color(percent: int) -> tuple:
    if percent >= 50:
        return _GREEN
    if percent >= 20:
        return _YELLOW
    return _RED


def render_battery_image(state: BatteryState) -> Image.Image:
    img = Image.new("RGBA", (_ICON_SIZE, _ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Battery body: rounded rect, leaves room for nub on right.
    body = (3, 8, 26, 24)  # left, top, right, bottom
    nub = (26, 13, 29, 19)

    if state.is_stale or state.percent is None:
        # Grey outline only + "?" centered.
        draw.rounded_rectangle(body, radius=3, outline=_GREY_COLOR, width=2)
        draw.rectangle(nub, fill=_GREY_COLOR)
        try:
            font = ImageFont.truetype("arial.ttf", 14)
        except OSError:
            font = ImageFont.load_default()
        draw.text((11, 7), "?", fill=_GREY_COLOR, font=font)
        return img

    # Outline.
    draw.rounded_rectangle(body, radius=3, outline=_OUTLINE_COLOR, width=2)
    draw.rectangle(nub, fill=_OUTLINE_COLOR)

    # Inner fill.
    inner_left, inner_top, inner_right, inner_bottom = 5, 10, 24, 22
    inner_width = inner_right - inner_left
    fill_width = int(inner_width * (max(0, min(100, state.percent)) / 100))
    if fill_width > 0:
        color = _fill_color(state.percent)
        draw.rectangle(
            (inner_left, inner_top, inner_left + fill_width, inner_bottom),
            fill=color,
        )

    if state.charging:
        # Simple lightning bolt: two triangles forming a Z-shape, white.
        bolt = [
            (15, 11), (12, 17), (14, 17),
            (13, 21), (16, 15), (14, 15),
        ]
        draw.polygon(bolt, fill=(255, 255, 255, 255))

    return img


class TrayIcon:
    """pystray wrapper. Owns its own thread; all callbacks fire on that thread."""

    def __init__(
        self,
        on_toggle_window: Callable[[], None],
        on_show: Callable[[], None],
        on_hide: Callable[[], None],
        on_quit: Callable[[], None],
    ):
        self._on_toggle = on_toggle_window
        self._on_show = on_show
        self._on_hide = on_hide
        self._on_quit = on_quit
        self._window_visible = True
        self._icon: Optional[pystray.Icon] = None
        self._thread: Optional[threading.Thread] = None

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(
                "Показать", lambda icon, item: self._on_show(),
                enabled=lambda item: not self._window_visible,
            ),
            pystray.MenuItem(
                "Скрыть", lambda icon, item: self._on_hide(),
                enabled=lambda item: self._window_visible,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda icon, item: self._on_quit()),
        )

    def start(self) -> None:
        initial = render_battery_image(BatteryState())
        self._icon = pystray.Icon(
            name="qmk_manager",
            icon=initial,
            title="QMK Manager — Battery: no data",
            menu=self._build_menu(),
        )
        self._icon.on_activate = lambda icon: self._on_toggle()
        self._thread = threading.Thread(target=self._icon.run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._icon is not None:
            self._icon.stop()

    def update_battery(self, state: BatteryState) -> None:
        if self._icon is None:
            return
        self._icon.icon = render_battery_image(state)
        if state.is_stale or state.percent is None:
            tooltip = "QMK Manager — Battery: no data"
        else:
            suffix = " ⚡" if state.charging else ""
            tooltip = f"QMK Manager — Battery: {state.percent}%{suffix}"
        self._icon.title = tooltip

    def set_window_visible(self, visible: bool) -> None:
        self._window_visible = visible
        if self._icon is not None:
            self._icon.update_menu()
```

- [ ] **Step 6: Run rendering tests**

Run: `pytest tests/test_tray_render.py -v`
Expected: all 6 PASS.

- [ ] **Step 7: Commit**

```bash
git add tray.py tests/test_tray_render.py requirements.txt
git commit -m "tray: pystray icon with battery image rendering + context menu"
```

---

## Phase 3 — `app_flet.py` integration

### Task 6: Config migration for `settings` and `battery` blocks

**Files:**
- Modify: `app_flet.py:load_config` (lines ~37–64)

- [ ] **Step 1: Update `load_config()` to inject defaults for new sections**

In `app_flet.py`, replace the `default_config` dict and the migration block in `load_config`:

```python
    def load_config(self):
        default_config = {
            "device": None,
            "mode": "auto",
            "payloads": {},
            "bindings": [],
            "settings": {
                "start_minimized": False,
                "autostart_service": True,
            },
            "battery": {
                "query": [],
                "report_id": 0,
                "response_length": 32,
                "response_offset": 0,
                "response_scale": 1,
                "charging_offset": None,
                "charging_mask": 0,
            },
        }
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # Existing payload migration (keep as-is) ...
                    if "payloads" in data:
                        for k, v in list(data["payloads"].items()):
                            if isinstance(v, list):
                                old_hk = ""
                                if "bindings" in data:
                                    for b in data["bindings"]:
                                        if b.get("profile_name") == k and b.get("hotkey"):
                                            old_hk = b["hotkey"]
                                data["payloads"][k] = {"data": v, "hotkey": old_hk}
                    if "bindings" in data:
                        for b in data["bindings"]:
                            if "hotkey" in b:
                                del b["hotkey"]
                    # NEW: ensure settings + battery blocks exist with defaults.
                    if "settings" not in data:
                        data["settings"] = default_config["settings"]
                    else:
                        for k, v in default_config["settings"].items():
                            data["settings"].setdefault(k, v)
                    if "battery" not in data:
                        data["battery"] = default_config["battery"]
                    else:
                        for k, v in default_config["battery"].items():
                            data["battery"].setdefault(k, v)
                    return data
            except Exception:
                pass
        return default_config
```

- [ ] **Step 2: Smoke-test by running and quitting**

```bash
python app_flet.py
```

Open and immediately close the window. Then check:

```bash
cat profiles_config.json
```

Expected: file now contains `settings` block with `start_minimized: false`, `autostart_service: true`, and a `battery` block with the placeholder values.

- [ ] **Step 3: Commit**

```bash
git add app_flet.py
git commit -m "app_flet: migrate config to add settings + battery blocks"
```

### Task 7: Wire `BatteryMonitor` and battery polling thread

**Files:**
- Modify: `app_flet.py` (imports + `__init__` + new method)

- [ ] **Step 1: Add imports**

At the top of `app_flet.py`, add to the imports block:

```python
from battery import BatteryMonitor, BatteryState
```

- [ ] **Step 2: Construct monitor in `__init__`**

After `self.usb_lock = threading.Lock()` in `QMKManager.__init__`, add:

```python
        self.app_alive = True
        self.battery_monitor = BatteryMonitor(
            config_battery=self.config["battery"],
            usb_lock=self.usb_lock,
            get_device_path=self.get_keyboard_path_safe,
        )
        self.battery_thread = None
```

Then add this helper method to the class (anywhere after `get_keyboard_path`):

```python
    def get_keyboard_path_safe(self):
        """Like get_keyboard_path but returns None if device isn't configured."""
        if not self.config.get("device"):
            return None
        return self.get_keyboard_path()
```

- [ ] **Step 3: Add the polling loop method**

Add to `QMKManager`:

```python
    def battery_poll_loop(self):
        print("[Battery] Поток опроса батареи запущен (каждые 60 сек).")
        while self.app_alive:
            try:
                self.battery_monitor.read_once()
                state = self.battery_monitor.state
                if self.tray:
                    self.tray.update_battery(state)
                self.publish_battery_to_ui(state)
            except Exception as e:
                print(f"[Battery] Ошибка цикла опроса: {e}")
            for _ in range(60):
                if not self.app_alive:
                    return
                time.sleep(1)

    def publish_battery_to_ui(self, state: BatteryState):
        # Updated by Task 10 (header badge). For now: no-op stub.
        pass
```

- [ ] **Step 4: Start the thread at end of `__init__`**

After `self.update_bindings_list()`:

```python
        self.battery_thread = threading.Thread(target=self.battery_poll_loop, daemon=True)
        self.battery_thread.start()
```

Note: `self.tray` doesn't exist yet — add a placeholder before the polling thread starts:

```python
        self.tray = None  # set by Task 8
```

(Place this near the other instance attribute initializations.)

- [ ] **Step 5: Smoke test**

```bash
python app_flet.py
```

Expected log line within a couple of seconds: `[Battery] Поток опроса батареи запущен`. Then either `[BatteryMonitor] read failed: device path unavailable` (if no device configured) or successful reads. Close the app — process should exit cleanly (daemon thread).

- [ ] **Step 6: Commit**

```bash
git add app_flet.py
git commit -m "app_flet: wire BatteryMonitor + 60s polling thread"
```

### Task 8: Wire `TrayIcon` and hybrid window-close

**Files:**
- Modify: `app_flet.py` (imports + `__init__` + new methods + `_build_page`)

- [ ] **Step 1: Add import**

```python
from tray import TrayIcon
```

- [ ] **Step 2: Replace `self.tray = None` with real construction**

In `__init__`, after `self.battery_monitor = ...` and before the polling thread is started, replace `self.tray = None` with:

```python
        self._first_minimize_notified = False
        self.tray = TrayIcon(
            on_toggle_window=self._tray_toggle_window,
            on_show=self._tray_show_window,
            on_hide=self._tray_hide_window,
            on_quit=self._tray_quit,
        )
        self.tray.start()
```

(`self.tray.start()` returns immediately — pystray runs in its own thread.)

- [ ] **Step 3: Add tray callbacks and Flet thread-safe helpers**

Add to `QMKManager`:

```python
    # ---------- Tray callbacks (run on pystray thread) ----------
    def _ui_call(self, fn):
        """Marshal a UI mutation onto Flet's event loop."""
        try:
            self.page.run_thread(fn)
        except Exception:
            # Fallback: call directly. Flet may have already shut down.
            try:
                fn()
            except Exception:
                pass

    def _tray_show_window(self):
        def do():
            self.page.window_visible = True
            try:
                self.page.window_to_front()
            except Exception:
                pass
            self.page.update()
            self.tray.set_window_visible(True)
        self._ui_call(do)

    def _tray_hide_window(self):
        def do():
            self.page.window_visible = False
            self.page.update()
            self.tray.set_window_visible(False)
        self._ui_call(do)

    def _tray_toggle_window(self):
        def do():
            visible = bool(getattr(self.page, "window_visible", True))
            if visible:
                self.page.window_visible = False
                self.tray.set_window_visible(False)
            else:
                self.page.window_visible = True
                try:
                    self.page.window_to_front()
                except Exception:
                    pass
                self.tray.set_window_visible(True)
            self.page.update()
        self._ui_call(do)

    def _tray_quit(self):
        self.app_alive = False
        self.is_running = False
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        try:
            self.tray.stop()
        except Exception:
            pass
        def do():
            try:
                self.page.window_destroy()
            except Exception:
                pass
        self._ui_call(do)
```

- [ ] **Step 4: Hook Flet window-close → hide-to-tray**

In `_build_page()`, after the existing window settings (after `self.page.bgcolor = ...`), add:

```python
        self.page.window_prevent_close = True
        self.page.on_window_event = self._handle_window_event
```

And add the handler:

```python
    def _handle_window_event(self, e):
        if e.data == "close":
            self.page.window_visible = False
            self.page.update()
            if self.tray:
                self.tray.set_window_visible(False)
            if not self._first_minimize_notified:
                self._first_minimize_notified = True
                try:
                    Notification(
                        app_id='QMK Manager',
                        title='QMK Manager',
                        msg='Программа продолжает работать в трее. Выйти можно из меню иконки.',
                        duration='short',
                    ).show()
                except Exception:
                    pass
```

- [ ] **Step 5: Smoke test**

```bash
python app_flet.py
```

Expected:
- Tray icon appears (grey "?" battery initially since no device configured / placeholder query bytes).
- Click X on the window → window vanishes, toast appears once.
- Right-click tray → "Показать" / "Скрыть" / "Выход". "Показать" is enabled, "Скрыть" disabled.
- Click "Показать" → window returns. Now "Скрыть" is enabled, "Показать" disabled.
- Left-click tray → toggles visibility.
- Right-click → "Выход" → process exits cleanly.

- [ ] **Step 6: Commit**

```bash
git add app_flet.py
git commit -m "app_flet: tray icon + hybrid window-close behavior"
```

### Task 9: `start_minimized` and `autostart_service` startup flags

**Files:**
- Modify: `app_flet.py` (`__init__`, `_build_page`)

- [ ] **Step 1: Honor `start_minimized` in `_build_page`**

In `_build_page()`, after setting `self.page.on_window_event = ...`:

```python
        if self.config.get("settings", {}).get("start_minimized", False):
            self.page.window_visible = False
            if self.tray is not None:
                self.tray.set_window_visible(False)
```

(Note: `self.tray` is constructed *after* `_build_page` runs — so this branch in `_build_page` will see `self.tray` as `None`. The tray's initial `_window_visible=True` is wrong then. Fix: also set tray visibility right after `self.tray.start()`.)

After `self.tray.start()` in `__init__`, add:

```python
        if self.config.get("settings", {}).get("start_minimized", False):
            self.tray.set_window_visible(False)
```

- [ ] **Step 2: Honor `autostart_service` in `__init__`**

After tray is started but before the battery thread, add:

```python
        if self.config.get("settings", {}).get("autostart_service", True):
            # Defer to next tick so UI is ready, then auto-start the service if a device is configured.
            def auto_start():
                if self.config.get("device") and self.device_dropdown.value is not None:
                    self.toggle_service()
            try:
                self.page.run_thread(auto_start)
            except Exception:
                auto_start()
```

(`refresh_devices()` runs earlier in `__init__` and sets `device_dropdown.value` if a saved device is present.)

- [ ] **Step 3: Smoke test — `start_minimized`**

Edit `profiles_config.json` and set `"start_minimized": true`. Run:

```bash
python app_flet.py
```

Expected: no window visible; tray icon is the only UI. Right-click → Show → window appears.

- [ ] **Step 4: Smoke test — `autostart_service`**

With a known-good device configured and `"autostart_service": true`, run app. Expected: status badge in header switches to "Работает" within ~1 second of launch (without clicking the toggle button). Reset `start_minimized` to `false` if you want to verify visually in the open window.

- [ ] **Step 5: Commit**

```bash
git add app_flet.py
git commit -m "app_flet: honor start_minimized + autostart_service config flags"
```

### Task 10: Settings UI card + header battery badge

**Files:**
- Modify: `app_flet.py` (`_build_ui`, new helper methods)

- [ ] **Step 1: Add settings card to `_build_ui`**

In `_build_ui`, before `self.toggle_button = ...`, add:

```python
        settings = self.config.get("settings", {})

        self.start_minimized_switch = ft.Switch(
            value=settings.get("start_minimized", False),
            on_change=lambda e: self._set_setting("start_minimized", e.control.value),
        )
        self.autostart_switch = ft.Switch(
            value=settings.get("autostart_service", True),
            on_change=lambda e: self._set_setting("autostart_service", e.control.value),
        )

        settings_card = self._card(
            icon=ft.Icons.SETTINGS_ROUNDED,
            title="Настройки запуска",
            subtitle="Применяются при следующем запуске программы.",
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Column(
                                [
                                    ft.Text("Запускать свёрнутым в трей", size=13),
                                    ft.Text("При старте окно будет скрыто, видна только иконка в трее.",
                                            size=11, color=ft.Colors.ON_SURFACE_VARIANT, italic=True),
                                ],
                                spacing=2, expand=True,
                            ),
                            self.start_minimized_switch,
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Divider(height=1, opacity=0.2),
                    ft.Row(
                        [
                            ft.Column(
                                [
                                    ft.Text("Автоматически запускать службу", size=13),
                                    ft.Text("Фоновое переключение профилей включится сразу после запуска.",
                                            size=11, color=ft.Colors.ON_SURFACE_VARIANT, italic=True),
                                ],
                                spacing=2, expand=True,
                            ),
                            self.autostart_switch,
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                ],
                spacing=12,
            ),
        )
```

And add `settings_card` to the `body` Column's controls list, after `bindings_card`.

- [ ] **Step 2: Add `_set_setting` helper**

```python
    def _set_setting(self, key, value):
        self.config.setdefault("settings", {})[key] = bool(value)
        self.save_config()
```

- [ ] **Step 3: Add header battery badge**

In `_build_ui`, replace the `header` block. The right side currently shows only `self.status_badge`. Wrap it in a Row with a new battery chip:

```python
        self.battery_chip_icon = ft.Icon(ft.Icons.BATTERY_UNKNOWN, size=16, color=ft.Colors.ON_SURFACE_VARIANT)
        self.battery_chip_text = ft.Text("—", size=12, weight=ft.FontWeight.W_500)
        self.battery_chip_refresh = ft.IconButton(
            icon=ft.Icons.REFRESH_ROUNDED,
            icon_size=14,
            tooltip="Обновить уровень батареи",
            on_click=lambda e: threading.Thread(target=self._manual_battery_refresh, daemon=True).start(),
        )
        self.battery_chip = ft.Container(
            content=ft.Row(
                [self.battery_chip_icon, self.battery_chip_text, self.battery_chip_refresh],
                spacing=4, tight=True,
            ),
            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            border_radius=100,
            tooltip="Уровень заряда клавиатуры",
        )
```

Then change the header right-side row from `self.status_badge` to:

```python
                    ft.Row([self.battery_chip, self.status_badge], spacing=8),
```

- [ ] **Step 4: Implement `publish_battery_to_ui` for real**

Replace the stub from Task 7:

```python
    def publish_battery_to_ui(self, state: BatteryState):
        def do():
            if state.is_stale or state.percent is None:
                self.battery_chip_icon.name = ft.Icons.BATTERY_UNKNOWN
                self.battery_chip_icon.color = ft.Colors.ON_SURFACE_VARIANT
                self.battery_chip_text.value = "—"
            else:
                if state.percent >= 50:
                    self.battery_chip_icon.name = ft.Icons.BATTERY_FULL_ROUNDED
                    self.battery_chip_icon.color = ft.Colors.TERTIARY
                elif state.percent >= 20:
                    self.battery_chip_icon.name = ft.Icons.BATTERY_3_BAR_ROUNDED
                    self.battery_chip_icon.color = ft.Colors.SECONDARY
                else:
                    self.battery_chip_icon.name = ft.Icons.BATTERY_ALERT_ROUNDED
                    self.battery_chip_icon.color = ft.Colors.ERROR
                suffix = " ⚡" if state.charging else ""
                self.battery_chip_text.value = f"{state.percent}%{suffix}"
            try:
                self.page.update()
            except Exception:
                pass
        self._ui_call(do)

    def _manual_battery_refresh(self):
        self.battery_monitor.read_once()
        state = self.battery_monitor.state
        if self.tray:
            self.tray.update_battery(state)
        self.publish_battery_to_ui(state)
```

- [ ] **Step 5: Smoke test**

```bash
python app_flet.py
```

Expected:
- Header now shows a battery chip on the left of the status badge. Initially `—` (no data).
- New "Настройки запуска" card appears at the bottom of the body with two switches reflecting current config.
- Toggling either switch updates `profiles_config.json` immediately.
- Click the small refresh icon in the chip → triggers an extra read; chip stays `—` until real battery bytes are configured.

- [ ] **Step 6: Commit**

```bash
git add app_flet.py
git commit -m "app_flet: settings card + header battery badge with manual refresh"
```

---

## Phase 4 — End-to-end manual verification

### Task 11: Configure real battery bytes (depends on Phase 0 sniffer output)

**Files:**
- Modify: `profiles_config.json`

- [ ] **Step 1: Run `sniffer.js` workflow on https://qmk.top**

Refer to Task 1 Step 2. Identify:
- `query` — bytes in the captured outgoing feature report (drop the leading reportId byte, since it's stored separately).
- `report_id` — the report ID used in the request/response pair.
- `response_offset` — byte index in the response that holds the percent (0-based).
- `response_scale` — multiplier (`1` if response is 0..100; `2` if 0..50, etc.).
- `charging_offset` / `charging_mask` if a separate byte/bit indicates charging state. Set both to `null`/`0` if unknown.

- [ ] **Step 2: Edit `profiles_config.json` `battery` block with real values**

Example (real bytes will differ):

```json
"battery": {
    "query": [171, 205, 0, 0],
    "report_id": 0,
    "response_length": 32,
    "response_offset": 3,
    "response_scale": 1,
    "charging_offset": 4,
    "charging_mask": 1
}
```

- [ ] **Step 3: Run app and verify**

```bash
python app_flet.py
```

Expected within 60 seconds (or instantly via the chip's refresh button):
- Tray icon transitions from grey "?" to a colored battery matching the actual charge.
- Tooltip on tray icon shows `QMK Manager — Battery: NN%`.
- Header chip mirrors the same percent.
- Plug/unplug the keyboard's charger → next refresh, the chip and tray icon should reflect `charging` state (lightning bolt icon and `⚡` suffix) — only if `charging_offset` was set correctly.

- [ ] **Step 4: Commit**

```bash
git add profiles_config.json
git commit -m "config: real battery query/response bytes for [keyboard model]"
```

### Task 12: Final hybrid-behavior + flag matrix smoke test

- [ ] **Step 1: Walk the matrix manually**

For each combination, launch fresh and verify:

| `start_minimized` | `autostart_service` | Expected at launch |
|---|---|---|
| false | false | Window visible, status "Остановлено", tray icon present, no service. |
| false | true  | Window visible, status "Работает" within ~1s, tray icon present. |
| true  | false | No window, only tray. Right-click → Show works. Status still "Остановлено" once shown. |
| true  | true  | No window, only tray. Service running silently. |

For each case, also verify:
- Window close (X) hides to tray and shows the toast (only first time per launch).
- Tray "Выход" terminates the process.
- Battery readings continue regardless of service state.

- [ ] **Step 2: If anything fails, fix it before considering Task 12 complete.**

- [ ] **Step 3: Final commit (only if any tweaks were needed)**

```bash
git add -p
git commit -m "fixups from end-to-end smoke test"
```

---

## Done criteria

- All unit tests pass: `pytest tests/ -v` shows ≥ 17 PASS, 0 FAIL.
- Tray icon visible on Windows, color-coded by charge, tooltip shows percent.
- Window close hides to tray; tray Show/Hide/Toggle/Exit all work.
- `start_minimized` and `autostart_service` flags both honored at launch and editable via UI.
- `app.py.deprecated` is untouched.
- Battery polling is non-blocking — profile switching latency unchanged from before.
