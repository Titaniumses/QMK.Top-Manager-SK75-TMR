# Wired/Wireless transport support + sniffer learn-mode

**Date:** 2026-05-14
**Scope:** `app_flet.py`, `sniffer.py` is **not** changed (filtering is done in Python).

## Problem

The keyboard exposes itself as **two distinct HID devices** depending on connection:

| Mode      | product_string             | VID/PID/usage_page                | HID payloads |
|-----------|----------------------------|-----------------------------------|--------------|
| Wireless  | `2.4G Wireless Keyboard`   | currently `3151:5038:ffff`        | known/working |
| Wired     | `SK75TMR`                  | different (to be discovered)      | **unknown**   |

Each transport uses a **different HID protocol** for profile switching and battery query. The current `_matches_profile_pattern` in `app_flet.py:1442` is hard-coded to the wireless opcode (`0x04 + idx + 0xFB-idx`), so wired-mode TX frames are silently dropped from the sniffer log and the user has no way to discover the wired payloads.

## Goals

1. The user can plug the keyboard via USB, see the wired device appear in the dropdown labeled `[WIRED]`, open the sniffer in **learn mode**, see every TX frame, and manually save them as profile / battery payloads.
2. Each transport keeps its own independent set of payloads in `profiles_config.json` (already true at the storage level — `devices[VID:PID:usage_page]`). No cross-contamination.
3. UI clearly shows whether the active device is wired or wireless.
4. When both transports are physically present, wired wins on auto-selection.

## Non-goals

- Merging both transports into "one logical keyboard" in the UI. Each stays its own entry; users tune them independently.
- Background polling for transport changes. Auto-selection runs on the existing `refresh_devices()` path; user can press the manual refresh.
- Reverse-engineering the wired protocol for the user. We give them tools; they find the bytes.

## Design

### 1. Config schema — `transport` field

Each entry in `devices[key]` gets a new field:

```json
"transport": "wired" | "wireless" | null
```

- Populated on first encounter via `_detect_transport(hid_dev)`:
  - product_string or manufacturer_string contains any of `2.4g`, `2.4 g`, `wireless`, `dongle`, `rf receiver` → `"wireless"`
  - otherwise → `"wired"`
- Persisted in JSON; users can override via the UI (chip toggle, see §2).
- Backwards compatible: existing entries without `transport` get the auto-detect result on next load (in `_normalize_device_entry`).

### 2. UI — dropdown badge + override chip

**Dropdown labels** (`refresh_devices`, `app_flet.py:843-851`):

```
[WIRED]   SK75TMR · VID 0x3151 · PID 0x5038 · Page 0xff60
[WIRELESS] 2.4G Wireless Keyboard · VID 0x3151 · PID 0x5038 · Page 0xffff
```

The badge is a literal text prefix in the dropdown text (Flet `Dropdown` doesn't render rich segments). Color differentiation lives elsewhere (chip below).

**Override chip** — a small `SegmentedButton` ("Wired" / "Wireless") near the dropdown, bound to the active device's `transport` field. Changing it writes to config and updates the dropdown label on next refresh.

**Header transport icon** — next to the existing battery badge in the app header, show `Icons.USB` (wired) or `Icons.WIFI_TETHERING_ROUNDED` (wireless), tinted by transport. Source of truth: `_active_device()["transport"]`.

### 3. Auto-selection — wired wins

Modify `refresh_devices()`:

When choosing `target_key` after a refresh:

1. If `active_device` is set **and still present in `filtered_devices`** → keep it (no surprise switch while user is working).
2. Otherwise: among detected devices, prefer one whose `transport == "wired"`. Tie-break by first-seen.
3. If only wireless is present, pick wireless.

When the active device is changed by auto-selection, the existing `_activate_device()` path runs — it already stops the service, swaps payloads/battery refs, and restarts. No new code path needed.

### 4. Sniffer Learn Mode

Add a `Switch` control in the sniffer modal: **«Learn mode (показать все TX)»** (default off).

**Off (current behavior):** `_on_sniff_event` filter unchanged — only matches `_matches_profile_pattern` or `_matches_battery_pattern` are logged with PROFILE/BATTERY tags.

**On:** every TX event is logged, with three tag states:
- `PROFILE?` (amber) if `_matches_profile_pattern(data)` is true
- `BATTERY?` (light blue) if it's a feature-report and not profile (current battery heuristic)
- `TX` (grey) for everything else

Each row gets two new action buttons (always present in learn mode):

- **«В слот ▾»** — popup menu with «Профиль 1/2/3/4», calls `_save_profile_payload_from_sniff(idx, data)`. Reuses existing logic.
- **«Как battery query»** — calls a new `_save_battery_query_from_sniff(data, report_id)`:
  - writes `data` to `entry["battery"]["query"]`
  - writes `report_id` to `entry["battery"]["report_id"]`
  - calls `save_config()` and triggers a manual battery refresh

Auto-save behavior (the existing `_battery_locked` path) stays only when learn mode is **off**, so wireless keeps working unchanged. In learn mode there is no auto-save — the user explicitly clicks.

### 5. Battery test panel

Add a small panel inside the sniffer modal (collapsible `ExpansionTile`):

| Field             | Default (from active device) |
|-------------------|------------------------------|
| `report_id`       | int                          |
| `response_length` | int                          |
| `response_offset` | int                          |
| `response_scale`  | float                        |
| `charging_offset` | int or empty                 |
| `charging_mask`   | int (hex)                    |

Buttons:
- **«Сохранить»** — write fields back to active device's `battery` dict + `save_config()`.
- **«Тест»** — perform one synchronous battery read using the *current* values of the fields (not yet saved); display result inline: `→ percent=87, charging=false` or the parse error.

This lets the user iterate on parsing parameters without restarting the app.

## Architecture summary

- No new modules. All changes live in `app_flet.py`.
- `sniffer.py` and `sniffer.js` untouched — filtering is purely Python-side.
- `BatteryMonitor` untouched — it already pulls everything from `entry["battery"]`.
- Config migration is additive: missing `transport` field is filled lazily on load.

## Failure modes / edge cases

- **`product_string` is empty or non-Latin** → falls into the "wired" default. User can override via chip.
- **User overrides transport, then unplugs** → override is persisted on the device entry, restored when device returns.
- **Both transports present, user wants to use wireless explicitly** → they pick wireless in the dropdown; auto-selection respects the existing-active rule and won't switch back.
- **Learn-mode left on accidentally** → log gets noisy but nothing breaks. Setting is per-session (not persisted to config), defaults to off on every app start.

## Test plan

Manual (no automated tests for HID/UI):

1. Start app with only wireless connected → dropdown shows `[WIRELESS]`, profile switching works as before.
2. Plug USB cable → after manual refresh, dropdown gains `[WIRED]` entry, auto-selects wired.
3. Open sniffer → enable learn mode → click profile button on qmk.top → verify TX frame appears with `PROFILE?` or `TX` tag and "В слот ▾" button.
4. Save frame to slot 1 → close sniffer → press profile-1 hotkey → keyboard physically switches.
5. Capture battery frame → save via "Как battery query" → tweak offset in test panel → "Тест" returns sensible percent.
6. Unplug USB → after manual refresh, dropdown auto-selects wireless again, profile switching still works.

## Out of scope (future iterations)

- Background polling for hot-plug events.
- Sharing a "logical keyboard" across two transport entries (one set of profile names + bindings).
- Auto-detection of wired payload format (would need ML/pattern-mining over many frames).
