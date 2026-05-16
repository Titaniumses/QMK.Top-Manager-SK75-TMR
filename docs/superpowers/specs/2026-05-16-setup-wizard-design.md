# First-Launch Setup Wizard & Keyboard Type Safety

**Date:** 2026-05-16
**Status:** Draft

## Problem

The app supports two keyboard types — magnetic (opcode `0x04`) and mechanical (opcode `0x05`) — with incompatible HID payloads. Sending the wrong opcode can cause unstable behavior, stuck keys, or corrupted profile state on the device.

Currently, new devices default to `keyboard_type: "magnetic"` without user confirmation. If the device is actually mechanical, the app silently sends wrong opcodes on the very first profile switch.

## Solution

A modal setup wizard that **blocks all HID write operations** until the user explicitly configures the keyboard type for each device. The wizard triggers automatically when a device with `keyboard_type: null` is selected.

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Wizard style | Modal `AlertDialog` over disabled main UI | User can see the app context; less disorienting than fullscreen |
| Safety UX | Warning block + confirmation checkbox | Sufficient friction without being hostile |
| First-launch vs new-device | Same wizard for both | Reduces code; both cases are "unconfigured device" |
| Auto-detection | None — always ask user | No reliable PID→type mapping exists |
| HID during wizard | Enumeration allowed, writes blocked | Wizard needs device list; writes are the dangerous part |

---

## 1. KEYBOARD_TYPES Dict

Centralizes all type-dependent values at module level in `app_flet.py`:

```python
KEYBOARD_TYPES = {
    "magnetic":    {"opcode": 0x04, "checksum_base": 0xFB, "profiles": 4, "cooldown": False},
    "mechanical":  {"opcode": 0x05, "checksum_base": 0xFA, "profiles": 3, "cooldown": True},
}
```

**Replaces these scattered inline checks:**
- `_device_opcode()` — ternary on `"mechanical"` → dict lookup
- `_device_profile_count()` — ternary on `"mechanical"` → dict lookup
- `_default_profile_payload()` — `checksum_base` ternary → dict lookup
- `_matches_profile_pattern()` — checksum validation → dict lookup
- `apply_payload()` — cooldown decision `if kb_type == "mechanical"` → dict lookup

Adding a future keyboard type means adding one dict entry.

---

## 2. HID Safety Guard

Three guard points block HID writes when `keyboard_type` is `None`:

### 2.1 `apply_payload()`
Before sending any feature report, check:
```
device = active device config
if device["keyboard_type"] is None:
    show wizard modal
    return (abort payload send)
```

### 2.2 Battery read callers (`battery_poll_loop`, `_refresh_battery_for_tray`)
Before calling `BatteryMonitor.read_once()`:
```
if device["keyboard_type"] is None:
    skip battery read (do not call read_once)
```
The guard is in `app_flet.py` callers, not inside `BatteryMonitor` itself (which lives in `battery.py` and shouldn't know about config).

### 2.3 `background_task()`
Before auto-profile switching via window bindings:
```
if device["keyboard_type"] is None:
    skip (do not attempt payload lookup or send)
```

**Unguarded:** `hid.enumerate()` — read-only, safe, needed by the wizard to show device info.

---

## 3. Config Changes

### 3.1 New Device Default

When `refresh_devices()` discovers an HID device not yet in `config["devices"]`, the new entry is created with:

```json
{
  "keyboard_type": null,
  "payloads": {}
}
```

Changed from the current default of `"magnetic"` with pre-generated payloads.

Payloads remain empty until the wizard completes and generates them with the correct opcode.

### 3.2 Validation on Load

During `load_config()`, each device entry is validated:
- If `keyboard_type` is not `None` and not in `KEYBOARD_TYPES` → reset to `null`
- This handles corrupted or hand-edited configs

### 3.3 No Schema Version Bump

No new top-level fields. No `initialized` flag. The guard logic uses the existing per-device `keyboard_type` field. Existing configs with `keyboard_type: "magnetic"` or `"mechanical"` are already valid — those users see no change.

---

## 4. Wizard Modal UI

A single method `_show_setup_wizard(device_key: str)` creates and opens an `ft.AlertDialog(modal=True)`.

### 4.1 Layout

```
┌─────────────────────────────────────────────┐
│  ⌨ Configure Keyboard                       │
│                                             │
│  Device: "2.4G Wireless Keyboard"           │
│  VID: 3151  PID: 5038                       │
│                                             │
│  Keyboard Type                              │
│  ┌─────────────────────────────────┐        │
│  │ ▼ Select type...               │        │
│  │   ● Magnetic (4 profiles)      │        │
│  │   ● Mechanical (3 profiles)    │        │
│  └─────────────────────────────────┘        │
│                                             │
│  ⚠ WARNING                                  │
│  Selecting the wrong keyboard type will     │
│  send incompatible HID commands to your     │
│  device. This may cause unstable behavior,  │
│  stuck keys, or corrupted profile state.    │
│  Make sure you know your keyboard type      │
│  before proceeding.                         │
│                                             │
│  ☐ I confirm my keyboard type is correct    │
│                                             │
│              [ Cancel ]  [ Save ]           │
└─────────────────────────────────────────────┘
```

### 4.2 Components

| Element | Flet control | Details |
|---|---|---|
| Title | `ft.Text("⌨ Configure Keyboard", size=20, weight="bold")` | Static |
| Device info | `ft.Text` | Shows label, VID, PID from device config |
| Type dropdown | `ft.Dropdown` | Options from `KEYBOARD_TYPES.keys()`, each with profile count hint |
| Warning block | `ft.Container` with `bgcolor=ft.Colors.AMBER_50` | Always visible, not dismissable |
| Checkbox | `ft.Checkbox("I confirm my keyboard type is correct")` | Unchecked by default |
| Cancel button | `ft.TextButton("Cancel")` | Always enabled |
| Save button | `ft.ElevatedButton("Save")` | Disabled until dropdown selected AND checkbox checked |

### 4.3 Behavior

**Save clicked:**
1. Write `keyboard_type` to `devices[device_key]`
2. Call `_set_keyboard_type()` to regenerate default payloads with correct opcode
3. Close dialog
4. Activate the device normally (start service, battery monitor, etc.)
5. `save_config()`

**Cancel clicked:**
1. Close dialog
2. Deselect the device in the dropdown (set `active_device` to a previously configured device, or none)
3. No config entry created with null type — the device stays unconfigured

**Triggers:**
1. App startup → `refresh_devices()` → active device has `keyboard_type: null`
2. User selects unconfigured device from dropdown → `_activate_device()` detects null type → wizard

---

## 5. Edge Cases

| Scenario | Handling |
|---|---|
| User cancels wizard | Device deselected; no HID writes possible; user can re-select later |
| Keyboard disconnected during setup | Wizard is display-only; config saved normally; device works when reconnected |
| Multiple unconfigured devices | Only the selected device triggers wizard; others wait in dropdown |
| User selected wrong type | Existing keyboard type toggle buttons in UI allow changing type at any time |
| Corrupted config (invalid JSON) | Existing `load_config()` catches `JSONDecodeError`, recreates default config |
| Unknown `keyboard_type` value in config | Validated to `null` during `load_config()`; wizard re-triggers |
| App crash during save | Partial write → invalid JSON on next load → existing error handling → fresh config |

---

## 6. Logging

When `debug: true`, these events are logged via the existing `logging` setup:

| Event | Message |
|---|---|
| Guard blocks HID write | `"HID write blocked: device {key} has no keyboard_type configured"` |
| Wizard opens | `"Setup wizard opened for device {key} ({label})"` |
| User selects type | `"Keyboard type selected: {type} for device {key}"` |
| Wizard completed | `"Setup completed: device {key} configured as {type}, {n} default payloads generated"` |
| Wizard cancelled | `"Setup wizard cancelled for device {key}"` |
| Config validation reset | `"Unknown keyboard_type '{val}' for device {key}, reset to null"` |

---

## 7. Scope of Changes

**Files modified:** `app_flet.py` only.

**Changes:**
- Add `KEYBOARD_TYPES` dict (~5 lines)
- Add `_show_setup_wizard()` method (~80 lines)
- Add guard checks in `apply_payload()`, `BatteryMonitor.read_once()`, `background_task()` (~15 lines)
- Refactor `_device_opcode()`, `_device_profile_count()`, `_default_profile_payload()`, `_matches_profile_pattern()`, cooldown logic to use `KEYBOARD_TYPES` (~20 lines changed)
- Change new-device default from `"magnetic"` to `null` (~2 lines)
- Add `keyboard_type` validation in `load_config()` (~5 lines)
- Add logging calls (~10 lines)

**Estimated total:** ~130 lines new/modified.

**Not in scope:**
- No new files
- No class hierarchy / strategy pattern
- No schema version system
- No safe-mode startup
- No separate recovery UI (existing type toggle covers this)
