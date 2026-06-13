import argparse
import flet as ft
import hid
import json
import logging
import os
import queue
import sys
import threading
import time
import ctypes
from pathlib import Path
import win32gui
import win32process
import psutil
import keyboard
from dataclasses import dataclass, field
from enum import IntEnum, Flag, auto
from winotify import Notification
from battery import BatteryMonitor, BatteryState
from tray import TrayIcon, set_icon_source
from sniffer import HIDSniffer, _find_chrome, is_chromium_executable
from autostart import paths, acquire_single_instance, bring_existing_to_front

logger = logging.getLogger(__name__)

CONFIG_FILE = paths.config_path
OFFLINE_MODE = os.environ.get("QMK_OFFLINE_MODE", "1").strip().lower() not in ("0", "false", "no")
ENABLE_UPDATE_CHECK = os.environ.get("QMK_ENABLE_UPDATE_CHECK", "0").strip().lower() in ("1", "true", "yes")

_LOCAL_FLET_CLIENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "flet-windows.zip")
if os.path.isfile(_LOCAL_FLET_CLIENT) and not os.environ.get("FLET_CLIENT_URL"):
    os.environ["FLET_CLIENT_URL"] = Path(_LOCAL_FLET_CLIENT).resolve().as_uri()

KEYBOARD_TYPES = {
    "magnetic":    {"opcode": 0x04, "checksum_base": 0xFB, "profiles": 4},
    "mechanical":  {"opcode": 0x05, "checksum_base": 0xFA, "profiles": 3},
}
PROFILE_COUNT = 4


class PollingRate(IntEnum):
    HZ_125  = 125
    HZ_250  = 250
    HZ_500  = 500
    HZ_1000 = 1000
    HZ_2000 = 2000
    HZ_4000 = 4000
    HZ_8000 = 8000


POLLING_RATE_CODES: dict[PollingRate, int] = {
    PollingRate.HZ_125:  6,
    PollingRate.HZ_250:  5,
    PollingRate.HZ_500:  4,
    PollingRate.HZ_1000: 3,
    PollingRate.HZ_2000: 2,
    PollingRate.HZ_4000: 1,
    PollingRate.HZ_8000: 0,
}

VALID_POLLING_RATES = {r.value for r in PollingRate}

LIGHTING_PROFILE_COUNT = 5
VALID_LIGHTING_PROFILES = set(range(LIGHTING_PROFILE_COUNT))


# ---------------------------------------------------------------------------
# Device capability system
# ---------------------------------------------------------------------------

class DeviceCapability(Flag):
    PROFILE_SWITCH = auto()
    HOTKEYS = auto()
    LIGHTING_PROFILES = auto()
    POLLING_RATE = auto()
    PROCESS_RULES = auto()

_CAP_MAGNETIC = (
    DeviceCapability.PROFILE_SWITCH
    | DeviceCapability.HOTKEYS
    | DeviceCapability.LIGHTING_PROFILES
    | DeviceCapability.POLLING_RATE
    | DeviceCapability.PROCESS_RULES
)
_CAP_MECHANICAL = (
    DeviceCapability.PROFILE_SWITCH
    | DeviceCapability.HOTKEYS
    | DeviceCapability.PROCESS_RULES
)

_CAPABILITY_MAP: dict[str | None, DeviceCapability] = {
    "magnetic": _CAP_MAGNETIC,
    "mechanical": _CAP_MECHANICAL,
    None: DeviceCapability(0),
}

def device_capabilities(keyboard_type: str | None) -> DeviceCapability:
    return _CAPABILITY_MAP.get(keyboard_type, DeviceCapability(0))

def has_capability(keyboard_type: str | None, cap: DeviceCapability) -> bool:
    return cap in device_capabilities(keyboard_type)


# ---------------------------------------------------------------------------
# Process-rule evaluator
# ---------------------------------------------------------------------------

@dataclass
class ProcessRule:
    process: str
    profile_index: int
    enabled: bool = True

class RuleEvaluator:
    def __init__(self):
        self._rules: list[ProcessRule] = []
        self._active_index: dict[str, int] = {}

    def load(self, bindings: list[dict]):
        self._rules = [
            ProcessRule(
                process=b["process"],
                profile_index=b["profile_index"],
                enabled=b.get("enabled", True),
            )
            for b in bindings
            if "profile_index" in b
        ]
        self._rebuild_index()

    def _rebuild_index(self):
        self._active_index = {r.process: r.profile_index for r in self._rules if r.enabled}

    def match(self, process_name: str) -> int | None:
        return self._active_index.get(process_name)

    def is_disabled_match(self, process_name: str) -> ProcessRule | None:
        for r in self._rules:
            if r.process == process_name and not r.enabled:
                return r
        return None

    def set_enabled(self, process: str, enabled: bool):
        for r in self._rules:
            if r.process == process:
                r.enabled = enabled
                break
        self._rebuild_index()

    def to_config(self) -> list[dict]:
        return [
            {"process": r.process, "profile_index": r.profile_index, "enabled": r.enabled}
            for r in self._rules
        ]

    @property
    def all_rules(self) -> list[ProcessRule]:
        return list(self._rules)


def _polling_rate_payload(rate: PollingRate) -> list:
    code = POLLING_RATE_CODES[rate]
    payload = [0] * 64
    payload[0] = 0x03
    payload[2] = code
    payload[7] = (255 - sum(payload[0:7])) & 0xFF
    return payload


def _lighting_profile_payload(index: int) -> list:
    payload = [0] * 64
    payload[0] = 0x07
    payload[1] = 0x0D
    payload[2] = 0x04
    payload[3] = 0x04
    payload[4] = (index & 0xFF) * 0x10
    payload[6] = 0xC8
    payload[7] = 0xC8
    payload[8] = (511 - sum(payload[0:8])) & 0xFF
    return payload


DEFAULT_BATTERY_QUERY = [0xF7] + [0] * 63

WIRED_STAGE_DELAYS_MS = {
    "profile": 50,
    "polling": 30,
    "lighting": 30,
}

WIRELESS_STAGE_DELAYS_MS = {
    "profile": 300,
    "polling": 200,
    "lighting": 150,
}


def _resolved_cooldown_ms(entry: dict | None) -> int:
    if not entry:
        return 0
    transport = entry.get("transport") or "wired"
    kb_type = entry.get("keyboard_type")
    if transport == "wireless":
        for key in ("cooldown_wireless_ms", "cooldown_ms"):
            value = entry.get(key)
            if isinstance(value, int) and value > 0:
                return value
        if kb_type == "mechanical":
            return 2000
        return 250
    for key in ("cooldown_wired_ms", "cooldown_ms"):
        value = entry.get(key)
        if isinstance(value, int) and value > 0:
            return value
    if kb_type == "mechanical":
        return 1000
    return 100


def _stage_delay_ms(entry: dict | None, stage: str) -> int:
    transport = (entry or {}).get("transport") or "wired"
    delays = WIRELESS_STAGE_DELAYS_MS if transport == "wireless" else WIRED_STAGE_DELAYS_MS
    return delays.get(stage, 0)


def _setup_logging(debug: bool) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    if debug:
        handler = logging.FileHandler(paths.log_path, mode="w", encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "[%(asctime)s.%(msecs)03d] [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        for noisy in ("flet", "flet_core", "flet_runtime", "flet_controls",
                       "flet_transport", "flet_desktop", "PIL", "PIL.Image"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    else:
        root.setLevel(logging.WARNING)


def _load_local_update_state() -> dict:
    return {
        "enabled": False,
        "checked_at": None,
        "latest_version": None,
        "error": None,
    }


def _default_profile_payload(idx: int, opcode: int = 0x04) -> list:
    kb_info = next((v for v in KEYBOARD_TYPES.values() if v["opcode"] == opcode), None)
    checksum_base = kb_info["checksum_base"] if kb_info else 0xFB
    payload = [0] * 64
    payload[0] = opcode
    payload[1] = idx & 0xFF
    payload[7] = (checksum_base - idx) & 0xFF
    return payload

def _release_all_keys():
    user32 = ctypes.windll.user32
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_EXTENDEDKEY = 0x0001
    _modifiers = [
        (0xA0, 0x2A, False),   # VK_LSHIFT
        (0xA1, 0x36, False),   # VK_RSHIFT
        (0xA2, 0x1D, False),   # VK_LCONTROL
        (0xA3, 0x1D, True),    # VK_RCONTROL (extended)
        (0xA4, 0x38, False),   # VK_LMENU (Alt)
        (0xA5, 0x38, True),    # VK_RMENU (extended)
        (0x5B, 0x5B, True),    # VK_LWIN
        (0x5C, 0x5C, True),    # VK_RWIN
    ]
    for vk, scan, extended in _modifiers:
        flags = KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if extended else 0)
        user32.keybd_event(vk, scan, flags, 0)
    for vk in range(0x08, 0xFF):
        if user32.GetAsyncKeyState(vk) & 0x8000:
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def _suppress_keyboard(duration_ms):
    def _suppress_callback(event):
        if event.event_type == keyboard.KEY_UP:
            return True
        return False

    hook = keyboard.hook(_suppress_callback, suppress=True)
    logger.debug("_suppress_keyboard: hook installed for %dms", duration_ms)

    _release_all_keys()

    def _unhook_later():
        time.sleep(duration_ms / 1000.0)
        keyboard.unhook(hook)
        logger.debug("_suppress_keyboard: hook removed")

    threading.Thread(target=_unhook_later, daemon=True).start()


def _suppress_keyboard_start():
    def _suppress_callback(event):
        if event.event_type == keyboard.KEY_UP:
            return True
        return False
    hook = keyboard.hook(_suppress_callback, suppress=True)
    _release_all_keys()
    logger.debug("_suppress_keyboard_start: hook installed (transaction-bound)")
    return hook


try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("QMK.Top.Manager.1")
except Exception:
    pass


class QMKManager:
    @staticmethod
    def _resource_path(rel_path: str) -> str:
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base, rel_path)

    def __init__(self, page: ft.Page):
        self.page = page
        self.config = self.load_config()
        _setup_logging(self.config.get("settings", {}).get("debug", False))
        logger.info("app started, config loaded")
        self._ensure_active_device_aliases()
        dev = self.config.get("device") or {}
        if dev and self.config.get("settings", {}).get("debug"):
            vid, pid = dev.get("vid", 0), dev.get("pid", 0)
            logger.debug("=== HID device map for VID=0x%04x PID=0x%04x ===", vid, pid)
            for d in hid.enumerate(vid, pid):
                logger.debug("  path=%s usage_page=0x%04x usage=0x%04x "
                             "interface=%d product=%s",
                             d["path"], d["usage_page"], d["usage"],
                             d.get("interface_number", -1),
                             d.get("product_string", "?"))
            logger.debug("=== end HID device map ===")
            self._diagnose_hid_endpoints()
        self.is_running = False
        self.worker_thread = None
        self.usb_lock = threading.Lock()
        self.app_alive = True
        self._first_minimize_notified = False
        try:
            icon_path = self._resource_path(os.path.join("docs", "Microsoft-Fluentui-Emoji-Flat-Keyboard-Flat.512.ico"))
            if os.path.exists(icon_path):
                set_icon_source(icon_path)
        except Exception:
            pass
        self.tray = TrayIcon(
            on_toggle_window=self._tray_toggle_window,
            on_show=self._tray_show_window,
            on_hide=self._tray_hide_window,
            on_quit=self._tray_quit,
        )
        self.battery_monitor = BatteryMonitor(
            config_battery=self.config["battery"],
            usb_lock=self.usb_lock,
            get_device_path=self.get_keyboard_path_safe,
            get_device_paths=self.get_keyboard_paths,
            on_working_path=self._cache_working_path,
            default_query=DEFAULT_BATTERY_QUERY,
        )
        self.battery_thread = None
        self.current_binding = None
        self.last_active_window = None
        self.binds_dict = {}
        self.rule_evaluator = RuleEvaluator()
        _entry = self._active_device()
        _dpi = _entry.get("default_profile_index") if _entry else None
        _pc = self._device_profile_count()
        self.default_profile_index = _dpi if isinstance(_dpi, int) and 0 <= _dpi < _pc else None
        self.devices = []
        self.filtered_devices = []
        self.sniffer = None
        self.sniff_events = []
        self._battery_captured_this_session = False
        self._battery_capture_attempts = 0
        self._battery_locked = False
        self._captured_profile_indices = set()
        # Кэш «рабочего» HID-интерфейса по ключу VID:PID:usage_page.
        # Многие клавиатуры (и в проводе, и в 2.4G) выставляют несколько
        # путей под одним usage_page; пишем во ВСЕ — а потом запоминаем тот,
        # что отвечает на feature-write успехом, чтобы дальше не перебирать.
        self._working_hid_path = {}
        # Sniffer "learn mode": when ON, _on_sniff_event bypasses the strict
        # pattern filter and logs every TX frame (and feature-report RX) with a
        # classification tag. Per spec §4 — per-session, never persisted.
        self._sniff_learn_mode = False
        self._battery_probe_queue = queue.Queue()
        self._battery_probe_thread = None
        self._battery_probe_stop = threading.Event()
        self.bt_report_id = None
        self.bt_response_length = None
        self.bt_response_offset = None
        self.bt_response_scale = None
        self.bt_charging_offset = None
        self.bt_charging_mask = None
        self.bt_result = None
        self.detected_browser_path = _find_chrome()
        self.update_check_state = _load_local_update_state()

        self._build_page()
        self._build_ui()
        self.refresh_devices()
        self.update_payloads_list()
        self.update_bindings_list()

        self.tray.start()

        def _initial_battery():
            time.sleep(2)
            self._refresh_battery_for_tray()
        threading.Thread(target=_initial_battery, daemon=True).start()

        if self.config.get("settings", {}).get("start_minimized", False):
            self.tray.set_window_visible(False)

        if self.config.get("settings", {}).get("autostart_service", True):
            def _deferred_auto_start():
                # Let Flet finish first paint before touching global keyboard
                # hooks — `keyboard.add_hotkey` installs a low-level Win32 hook
                # synchronously and can block the UI thread for 1-3s,
                # leaving the window blank and unresponsive on cold start.
                time.sleep(0.4)
                try:
                    if self.config.get("device") and self.device_dropdown.value is not None:
                        self._ui_call(self.toggle_service)
                except Exception as exc:
                    print(f"[AutoStart] failed: {exc}")
            threading.Thread(target=_deferred_auto_start, daemon=True).start()

        self.battery_thread = threading.Thread(target=self.battery_poll_loop, daemon=True)
        self.battery_thread.start()

    # ---------- Config ----------
    @staticmethod
    def _device_key(vid, pid, usage_page):
        return f"{int(vid):04x}:{int(pid):04x}:{int(usage_page):04x}"

    @staticmethod
    def _device_key_of(hid_dev):
        return f"{hid_dev['vendor_id']:04x}:{hid_dev['product_id']:04x}:{hid_dev['usage_page']:04x}"

    @staticmethod
    def _device_label_for(hid_dev):
        return f"{(hid_dev.get('manufacturer_string') or 'Unknown').strip()} {(hid_dev.get('product_string') or 'Device').strip()}"

    @staticmethod
    def _detect_transport(hid_dev) -> str:
        text = " ".join([
            hid_dev.get("product_string") or "",
            hid_dev.get("manufacturer_string") or "",
        ]).lower()
        wireless_markers = ("2.4g", "2.4 g", "wireless", "dongle", "rf receiver")
        return "wireless" if any(marker in text for marker in wireless_markers) else "wired"

    def _device_profile_count(self) -> int:
        entry = self._active_device()
        kb_type = entry.get("keyboard_type") if entry else None
        info = KEYBOARD_TYPES.get(kb_type)
        return info["profiles"] if info else KEYBOARD_TYPES["magnetic"]["profiles"]

    def _device_opcode(self) -> int:
        entry = self._active_device()
        kb_type = entry.get("keyboard_type") if entry else None
        info = KEYBOARD_TYPES.get(kb_type)
        return info["opcode"] if info else KEYBOARD_TYPES["magnetic"]["opcode"]

    def _profile_payload_at(self, index: int) -> list:
        info = self._profile_info_at(index)
        if info and info.get("data"):
            return info["data"]
        return _default_profile_payload(index, self._device_opcode())

    def _detect_transport_for_active(self) -> str:
        dev = self.config.get("device") or {}
        if not dev:
            return "wired"
        vid, pid = dev.get("vid", 0), dev.get("pid", 0)
        for d in hid.enumerate(vid, pid):
            return self._detect_transport(d)
        return "wired"

    def _probe_battery_percent(self, hid_dev):
        """Synchronously query battery on a specific HID device and return percent (0..100) or None.

        Used by refresh_devices() to classify wired vs wireless: a working battery
        response means wireless; no response / no sane percent means wired.
        Tries every HID interface for this VID:PID:usage_page (some are deaf).
        """
        key = self._device_key_of(hid_dev)
        entry = self.config["devices"].get(key) or {}
        if entry.get("keyboard_type") is None:
            return None
        batt = entry.get("battery") or {}
        query = batt.get("query") or []
        if not query:
            return None
        report_id = batt.get("report_id", 0)
        response_length = batt.get("response_length", 65)
        response_offset = batt.get("response_offset", 2)
        response_scale = batt.get("response_scale", 1)

        try:
            import hid as _hid
        except Exception:
            return None

        vid, pid, up = hid_dev["vendor_id"], hid_dev["product_id"], hid_dev["usage_page"]
        paths = [d["path"] for d in _hid.enumerate(vid, pid) if d.get("usage_page") == up]

        with self.usb_lock:
            for path in paths:
                device = None
                try:
                    device = _hid.device()
                    device.open_path(path)
                    device.set_nonblocking(1)
                    device.send_feature_report([report_id] + list(query))
                    response = device.get_feature_report(report_id, response_length)
                except Exception:
                    try:
                        if device is not None:
                            device.close()
                    except Exception:
                        pass
                    continue
                try:
                    device.close()
                except Exception:
                    pass
                try:
                    raw = response[response_offset]
                    percent = max(0, min(100, int(raw * response_scale)))
                except (IndexError, TypeError, ValueError):
                    continue
                # 0% can legitimately mean a flat battery, but in wired mode the
                # device commonly echoes zeros — accept only strictly > 0 as a
                # reliable wireless signal.
                if percent > 0:
                    return percent
        return None

    @staticmethod
    def _pick_active_target(current_active, present_keys, devices_cfg):
        """Decide which device key should be active after a refresh.
        1. Keep current_active if it's still present AND has a config entry.
        2. Else prefer a present device whose config transport == 'wired'.
        3. Else first present device. Else None."""
        if current_active and current_active in present_keys and current_active in devices_cfg:
            return current_active
        for k in present_keys:
            if devices_cfg.get(k, {}).get("transport") == "wired":
                return k
        return present_keys[0] if present_keys else None

    def _empty_device_entry(self, vid, pid, usage_page, label=""):
        return {
            "vid": int(vid),
            "pid": int(pid),
            "usage_page": int(usage_page),
            "label": label or "",
            "transport": None,
            "keyboard_type": None,
            "cooldown_ms": 0,
            "payloads": {},
            "bindings": [],
            "default_profile_index": None,
            "battery": {
                "query": [],
                "report_id": 0,
                "response_length": 65,
                "response_offset": 2,
                "response_scale": 1,
                "charging_offset": None,
                "charging_mask": 0,
            },
        }

    def _normalize_device_entry(self, entry):
        entry.setdefault("label", "")
        entry.setdefault("transport", None)
        entry.setdefault("keyboard_type", None)
        entry.setdefault("cooldown_ms", 0)
        kb_type = entry.get("keyboard_type")
        if kb_type is not None and kb_type not in KEYBOARD_TYPES:
            logger.warning("Unknown keyboard_type '%s', reset to null", kb_type)
            entry["keyboard_type"] = None
            kb_type = None
        if kb_type is None:
            entry.setdefault("payloads", {})
            entry.setdefault("bindings", [])
            entry.setdefault("default_profile_index", None)
            entry.setdefault("battery", {
                "query": [], "report_id": 0, "response_length": 65,
                "response_offset": 2, "response_scale": 1,
                "charging_offset": None, "charging_mask": 0,
            })
            return
        kb_info = KEYBOARD_TYPES[kb_type]
        pc = kb_info["profiles"]
        payloads = entry.get("payloads") or {}
        if not isinstance(payloads, dict):
            payloads = {}
        items = list(payloads.items())[:pc]
        while len(items) < pc:
            items.append((f"Профиль {len(items) + 1}", {"hotkey": ""}))
        new_payloads = {}
        for slot_idx, (name, info) in enumerate(items):
            if not isinstance(info, dict):
                info = {"hotkey": ""}
            info.setdefault("hotkey", "")
            new_payloads[name] = info
        entry["payloads"] = new_payloads

        name_to_idx = {n: i for i, n in enumerate(new_payloads.keys())}
        new_bindings = []
        for b in entry.get("bindings", []) or []:
            if not isinstance(b, dict) or "process" not in b:
                continue
            if "profile_index" in b and isinstance(b["profile_index"], int) and 0 <= b["profile_index"] < pc:
                new_bindings.append({"process": b["process"], "profile_index": b["profile_index"]})
                continue
            old_name = b.get("profile_name")
            if old_name in name_to_idx:
                new_bindings.append({"process": b["process"], "profile_index": name_to_idx[old_name]})
        entry["bindings"] = new_bindings

        # Migrate legacy "default" pseudo-binding into a dedicated field.
        dpi = entry.get("default_profile_index")
        if not isinstance(dpi, int) or not (0 <= dpi < pc):
            dpi = None
            for b in list(entry["bindings"]):
                if b.get("process") == "default" and isinstance(b.get("profile_index"), int):
                    dpi = b["profile_index"]
                    entry["bindings"].remove(b)
                    break
        entry["default_profile_index"] = dpi

        battery = entry.get("battery") or {}
        defaults = {
            "query": [], "report_id": 0, "response_length": 65, "response_offset": 2,
            "response_scale": 1, "charging_offset": None, "charging_mask": 0,
        }
        for k, v in defaults.items():
            battery.setdefault(k, v)
        # Heal legacy entries that were created with broken WebHID-style
        # defaults (length=32, offset=0). Those values produce 0% on hidapi
        # because byte[0] is the report_id, not the percent. Bump them only
        # if they still match the old broken pair AND no real query was
        # captured yet — leave user-tuned values alone.
        if (
            battery.get("response_length") == 32
            and battery.get("response_offset") == 0
        ):
            battery["response_length"] = 65
            battery["response_offset"] = 2
        entry["battery"] = battery

    def load_config(self):
        logger.debug("loading config from %s", CONFIG_FILE)
        default_config = {
            "mode": "auto",
            "settings": {
                "start_minimized": False,
                "autostart_service": True,
                "autostart": False,
                "startup_delay_sec": 5,
                "browser_path": "",
                "debug": False,
            },
            "devices": {},
            "active_device": None,
        }
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                data = {}
        else:
            data = {}

        # settings + mode
        if "settings" not in data or not isinstance(data.get("settings"), dict):
            data["settings"] = dict(default_config["settings"])
        else:
            for k, v in default_config["settings"].items():
                data["settings"].setdefault(k, v)
        data.setdefault("mode", "auto")

        # legacy → multi-device migration
        if "devices" not in data or not isinstance(data.get("devices"), dict):
            data["devices"] = {}
        legacy_dev = data.pop("device", None)
        legacy_payloads = data.pop("payloads", None)
        legacy_bindings = data.pop("bindings", None)
        legacy_battery = data.pop("battery", None)
        if legacy_dev and isinstance(legacy_dev, dict):
            try:
                key = self._device_key(legacy_dev["vid"], legacy_dev["pid"], legacy_dev["usage_page"])
                entry = data["devices"].get(key) or {
                    "vid": legacy_dev["vid"],
                    "pid": legacy_dev["pid"],
                    "usage_page": legacy_dev["usage_page"],
                    "label": "",
                }
                if legacy_payloads is not None:
                    entry["payloads"] = legacy_payloads
                if legacy_bindings is not None:
                    entry["bindings"] = legacy_bindings
                if legacy_battery is not None:
                    entry["battery"] = legacy_battery
                data["devices"][key] = entry
                data.setdefault("active_device", key)
            except Exception:
                pass

        for key, entry in list(data["devices"].items()):
            if not isinstance(entry, dict):
                del data["devices"][key]
                continue
            self._normalize_device_entry(entry)

        active = data.get("active_device")
        if active not in data["devices"]:
            data["active_device"] = next(iter(data["devices"].keys()), None)

        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as wf:
                json.dump(data, wf, indent=4, ensure_ascii=False)
        except Exception:
            pass
        return data

    # ---------- Active device aliases ----------
    def _active_device(self):
        key = self.config.get("active_device")
        if not key:
            return None
        return self.config.get("devices", {}).get(key)

    def _ensure_active_device_aliases(self):
        """Bind self.config['payloads'/'bindings'/'battery'/'device'] as references
        to the active device's sub-dicts, so existing call sites Just Work."""
        entry = self._active_device()
        if entry is None:
            self.config["payloads"] = {}
            self.config["bindings"] = []
            self.config["battery"] = {}
            self.config["device"] = None
        else:
            self.config["payloads"] = entry["payloads"]
            self.config["bindings"] = entry["bindings"]
            self.config["battery"] = entry["battery"]
            self.config["device"] = {
                "vid": entry["vid"], "pid": entry["pid"], "usage_page": entry["usage_page"],
            }

    def _activate_device(self, key):
        if key not in self.config.get("devices", {}):
            return False
        entry = self.config["devices"][key]
        if entry.get("keyboard_type") is None:
            self.config["active_device"] = key
            self._ensure_active_device_aliases()
            self.save_config()
            self._show_setup_wizard(key)
            return True
        was_running = self.is_running
        if was_running:
            self.is_running = False
            try:
                keyboard.unhook_all()
            except Exception:
                pass
        self.config["active_device"] = key
        self._ensure_active_device_aliases()
        self.current_binding = None
        self.last_active_window = None
        # Recreate battery monitor pointed at new device's battery dict
        try:
            self.battery_monitor = BatteryMonitor(
                config_battery=self.config["battery"],
                usb_lock=self.usb_lock,
                get_device_path=self.get_keyboard_path_safe,
                get_device_paths=self.get_keyboard_paths,
                on_working_path=self._cache_working_path,
            )
        except Exception:
            pass
        self.save_config()
        try:
            self.update_payloads_list()
        except Exception:
            pass
        try:
            self.update_bindings_list()
        except Exception:
            pass
        if was_running:
            self.is_running = True
            self.reload_runtime_state()
            self._set_status(True)
            if not self.worker_thread or not self.worker_thread.is_alive():
                self.worker_thread = threading.Thread(target=self.background_task, daemon=True)
                self.worker_thread.start()
        threading.Thread(target=self._manual_battery_refresh, daemon=True).start()
        try:
            self._battery_test_sync_from_active()
        except Exception:
            pass
        try:
            self._update_transport_icon()
        except Exception:
            pass
        return True

    def _ensure_device_entry(self, hid_dev):
        """Create an empty config entry for an HID device if missing. Returns key.
        Also lazily fills the `transport` field from device metadata.
        Saves config only when something actually changed."""
        key = self._device_key_of(hid_dev)
        dirty = False
        if key not in self.config["devices"]:
            self.config["devices"][key] = self._empty_device_entry(
                hid_dev["vendor_id"], hid_dev["product_id"], hid_dev["usage_page"],
                label=self._device_label_for(hid_dev),
            )
            dirty = True
        entry = self.config["devices"][key]
        if entry.get("transport") is None:
            entry["transport"] = self._detect_transport(hid_dev)
            dirty = True
        if dirty:
            self.save_config()
        return key

    # ---------- Profile helpers ----------
    def _profile_items(self):
        return list(self.config.get("payloads", {}).items())

    def _profile_name_at(self, index):
        items = self._profile_items()
        if 0 <= index < len(items):
            return items[index][0]
        return None

    def _profile_info_at(self, index):
        items = self._profile_items()
        if 0 <= index < len(items):
            return items[index][1]
        return None

    def _profile_info_at_by_name(self, name):
        return self.config.get("payloads", {}).get(name)

    def _rename_profile_at(self, index, new_name):
        items = self._profile_items()
        if not (0 <= index < len(items)):
            return False
        new_name = (new_name or "").strip()
        if not new_name:
            return False
        existing = {n for i, (n, _) in enumerate(items) if i != index}
        if new_name in existing:
            return False
        items[index] = (new_name, items[index][1])
        new_payloads = {n: info for n, info in items}
        self.config["payloads"] = new_payloads
        entry = self._active_device()
        if entry is not None:
            entry["payloads"] = new_payloads
        return True

    def _current_mode(self):
        return self.mode_segmented.selected[0] if self.mode_segmented.selected else "auto"

    def save_config(self):
        logger.debug("saving config to %s", CONFIG_FILE)
        try:
            self.config["mode"] = self._current_mode()
        except Exception:
            pass
        # Don't persist legacy alias keys; they are references into devices[active]
        snapshot = {k: v for k, v in self.config.items()
                    if k not in ("payloads", "bindings", "battery", "device")}
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, indent=4, ensure_ascii=False)
        if self.is_running:
            self.reload_runtime_state()
        if self.is_running:
            self._set_status(True)

    def _set_setting(self, key, value):
        self.config.setdefault("settings", {})[key] = bool(value)
        self.save_config()

    def _on_autostart_windows_changed(self, e):
        from autostart import set_autostart
        enable = e.control.value
        set_autostart(enable)
        self.config.setdefault("settings", {})["autostart"] = enable
        self.save_config()

    def reload_runtime_state(self):
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        registered = 0
        for idx, (prof_name, info) in enumerate(self._profile_items()):
            hk = info.get("hotkey")
            if not hk:
                continue
            payload = self._profile_payload_at(idx)
            try:
                keyboard.add_hotkey(
                    hk,
                    lambda name=prof_name, data=payload:
                        self.apply_payload(name, data, manual=True)
                )
                registered += 1
            except Exception as e:
                print(f"[Хоткей] Ошибка регистрации {hk}: {e}")
        self.rule_evaluator.load(self.config.get("bindings", []))
        self.binds_dict = self.rule_evaluator._active_index
        entry = self._active_device()
        dpi = entry.get("default_profile_index") if entry else None
        self.default_profile_index = dpi if isinstance(dpi, int) and 0 <= dpi < self._device_profile_count() else None
        self.last_active_window = None
        print(f"[Reload] Хоткеев: {registered}, биндингов: {len(self.binds_dict)}")

    # ---------- Page setup ----------
    def _build_page(self):
        self.page.title = "QMK.Top Manager"
        self.page.window.width = 920
        self.page.window.height = 880
        self.page.window.min_width = 720
        self.page.window.min_height = 640
        try:
            icon_path = self._resource_path(os.path.join("docs", "Microsoft-Fluentui-Emoji-Flat-Keyboard-Flat.512.ico"))
            if os.path.exists(icon_path):
                self.page.window.icon = icon_path
        except Exception:
            pass
        self.page.padding = 0
        self.page.theme_mode = ft.ThemeMode.DARK
        self.page.theme = ft.Theme(
            color_scheme_seed=ft.Colors.DEEP_PURPLE,
            use_material3=True,
        )
        self.page.dark_theme = ft.Theme(
            color_scheme_seed=ft.Colors.DEEP_PURPLE,
            use_material3=True,
        )
        self.page.bgcolor = ft.Colors.SURFACE

        self.page.window.prevent_close = True
        self.page.window.on_event = self._handle_window_event

        if self.config.get("settings", {}).get("start_minimized", False):
            self.page.window.visible = False
            self.page.window.skip_task_bar = True
            # Re-assert hidden state on the UI loop after Flet finishes its
            # initial paint — otherwise Flet sometimes shows the window anyway
            # because `visible=False` set in __init__ races with the first
            # frame being pushed to the native shell.
            def _enforce_hidden():
                time.sleep(0.3)
                def do():
                    try:
                        self.page.window.visible = False
                        self.page.window.skip_task_bar = True
                        try:
                            self.page.window.minimized = True
                        except Exception:
                            pass
                        self.page.update()
                    except Exception:
                        pass
                self._ui_call(do)
            threading.Thread(target=_enforce_hidden, daemon=True).start()

    # ---------- UI ----------
    def _build_ui(self):
        self.status_badge = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.CIRCLE, color=ft.Colors.ERROR, size=10),
                    ft.Text("Остановлено", size=12, weight=ft.FontWeight.W_500),
                ],
                spacing=6,
                tight=True,
            ),
            padding=ft.Padding.symmetric(horizontal=12, vertical=6),
            bgcolor=ft.Colors.ERROR_CONTAINER,
            border_radius=100,
        )

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

        self.transport_icon = ft.Icon(
            ft.Icons.USB,
            size=16,
            color=ft.Colors.ON_SURFACE_VARIANT,
            tooltip="Тип подключения активного устройства",
            visible=False,
        )

        header = ft.Container(
            content=ft.Row(
                [
                    ft.Row(
                        [
                            ft.Container(
                                content=ft.Icon(ft.Icons.KEYBOARD_ALT_ROUNDED, size=26, color=ft.Colors.ON_PRIMARY),
                                width=44, height=44,
                                bgcolor=ft.Colors.PRIMARY,
                                border_radius=14,
                                alignment=ft.Alignment.CENTER,
                            ),
                            ft.Column(
                                [
                                    ft.Text("QMK.Top Manager", size=20, weight=ft.FontWeight.W_600),
                                    ft.Text("Переключение профилей клавиатуры", size=12,
                                            color=ft.Colors.ON_SURFACE_VARIANT),
                                ],
                                spacing=0,
                                tight=True,
                            ),
                        ],
                        spacing=12,
                    ),
                    ft.Row([self.transport_icon, self.battery_chip, self.status_badge], spacing=8),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
            padding=ft.Padding.symmetric(horizontal=24, vertical=20),
        )

        self.mode_segmented = ft.SegmentedButton(
            selected=[self.config.get("mode", "auto")],
            allow_multiple_selection=False,
            allow_empty_selection=False,
            segments=[
                ft.Segment(value="auto", label=ft.Text("Автоматически"),
                           icon=ft.Icon(ft.Icons.AUTO_AWESOME_ROUNDED)),
                ft.Segment(value="manual", label=ft.Text("Вручную"),
                           icon=ft.Icon(ft.Icons.KEYBOARD_COMMAND_KEY_ROUNDED)),
            ],
            on_change=lambda e: self.save_config(),
        )

        mode_card = self._card(
            icon=ft.Icons.TUNE_ROUNDED,
            title="Режим работы",
            subtitle="Авто — по активному окну + хоткеи. Ручной — только хоткеи.",
            content=ft.Container(self.mode_segmented, margin=ft.Margin.only(top=12)),
        )

        self.device_dropdown = ft.Dropdown(
            label="HID устройство",
            expand=True,
            border_radius=12,
            filled=True,
            options=[],
            on_select=lambda e: self._on_device_dropdown_changed(),
        )
        self.transport_override = None
        refresh_btn = ft.IconButton(
            icon=ft.Icons.REFRESH_ROUNDED,
            tooltip="Обновить список",
            on_click=lambda e: self.refresh_devices(),
            style=ft.ButtonStyle(
                bgcolor=ft.Colors.SECONDARY_CONTAINER,
                color=ft.Colors.ON_SECONDARY_CONTAINER,
                shape=ft.RoundedRectangleBorder(radius=12),
                padding=14,
            ),
        )
        sniffer_open_btn = ft.FilledTonalButton(
            "Sniffer / настройка",
            icon=ft.Icons.SENSORS_ROUNDED,
            on_click=lambda e: self.open_sniffer_modal(),
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12)),
        )

        device_card_controls = [
            ft.Row([self.device_dropdown, refresh_btn], spacing=8),
        ]
        if self.config.get("settings", {}).get("debug", False):
            device_card_controls.append(
                ft.Row([sniffer_open_btn], alignment=ft.MainAxisAlignment.END),
            )

        device_card = self._card(
            icon=ft.Icons.USB_ROUNDED,
            title="Устройство",
            subtitle="Выберите QMK-клавиатуру из списка.",
            content=ft.Container(
                ft.Column(device_card_controls, spacing=10),
                margin=ft.Margin.only(top=12),
            ),
        )

        self.payloads_column = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO)
        profiles_card = self._card(
            icon=ft.Icons.LAYERS_ROUNDED,
            title="Профили",
            subtitle="Всегда 4 фиксированных профиля. Имя и хоткей — редактируемые.",
            content=ft.Container(
                content=self.payloads_column,
                margin=ft.Margin.only(top=12),
            ),
        )

        self.bindings_column = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO)
        add_binding_btn = ft.FilledTonalButton(
            "Создать привязку",
            icon=ft.Icons.ADD_LINK_ROUNDED,
            on_click=lambda e: self.open_binding_dialog("Новая привязка"),
        )
        bindings_card = self._card(
            icon=ft.Icons.LINK_ROUNDED,
            title="Привязки к процессам",
            subtitle="При активации окна с этим процессом автоматически применяется профиль.",
            content=ft.Column(
                [
                    ft.Container(
                        content=self.bindings_column,
                        margin=ft.Margin.only(top=12),
                    ),
                    ft.Row([add_binding_btn], alignment=ft.MainAxisAlignment.END),
                ],
                spacing=8,
            ),
        )

        settings = self.config.get("settings", {})

        self.start_minimized_switch = ft.Switch(
            value=settings.get("start_minimized", False),
            on_change=lambda e: self._set_setting("start_minimized", e.control.value),
        )
        self.autostart_switch = ft.Switch(
            value=settings.get("autostart_service", True),
            on_change=lambda e: self._set_setting("autostart_service", e.control.value),
        )
        from autostart import autostart_enabled, set_autostart
        self.autostart_windows_switch = ft.Switch(
            value=autostart_enabled(),
            on_change=self._on_autostart_windows_changed,
        )
        self.notifications_switch = ft.Switch(
            value=settings.get("notifications", True),
            on_change=lambda e: self._set_setting("notifications", e.control.value),
        )

        settings_card = self._card(
            icon=ft.Icons.SETTINGS_ROUNDED,
            title="Настройки",
            subtitle="Параметры запуска и поведения приложения.",
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Column(
                                [
                                    ft.Text("Запускать свёрнутым в трей", size=13),
                                    ft.Text(
                                        "При старте окно будет скрыто, видна только иконка в трее.",
                                        size=11, color=ft.Colors.ON_SURFACE_VARIANT, italic=True,
                                    ),
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
                                    ft.Text(
                                        "Фоновое переключение профилей включится сразу после запуска.",
                                        size=11, color=ft.Colors.ON_SURFACE_VARIANT, italic=True,
                                    ),
                                ],
                                spacing=2, expand=True,
                            ),
                            self.autostart_switch,
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Divider(height=1, opacity=0.2),
                    ft.Row(
                        [
                            ft.Column(
                                [
                                    ft.Text("Запускать с Windows", size=13),
                                    ft.Text(
                                        "Приложение будет запускаться автоматически при входе в систему.",
                                        size=11, color=ft.Colors.ON_SURFACE_VARIANT, italic=True,
                                    ),
                                ],
                                spacing=2, expand=True,
                            ),
                            self.autostart_windows_switch,
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Divider(height=1, opacity=0.2),
                    ft.Row(
                        [
                            ft.Column(
                                [
                                    ft.Text("Уведомления", size=13),
                                    ft.Text(
                                        "Показывать Windows-уведомления при переключении профиля.",
                                        size=11, color=ft.Colors.ON_SURFACE_VARIANT, italic=True,
                                    ),
                                ],
                                spacing=2, expand=True,
                            ),
                            self.notifications_switch,
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                ],
                spacing=12,
            ),
        )

        self.sniff_log = ft.ListView(
            spacing=6, padding=8, auto_scroll=False,
            on_scroll=self._on_sniff_scroll,
        )
        self._sniff_auto_scroll = True
        self.sniff_status = ft.Text("Сниффер остановлен.", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        self.sniff_button = ft.FilledTonalButton(
            "Запустить sniff",
            icon=ft.Icons.SENSORS_ROUNDED,
            on_click=lambda e: self.toggle_sniffer(),
            disabled=OFFLINE_MODE,
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12)),
        )
        self.sniff_clear_button = ft.OutlinedButton(
            "Очистить",
            icon=ft.Icons.CLEAR_ALL_ROUNDED,
            on_click=lambda e: self.clear_sniffer_log(),
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12)),
        )
        self.sniff_copy_button = ft.OutlinedButton(
            "Скопировать JSON",
            icon=ft.Icons.COPY_ROUNDED,
            on_click=lambda e: self.copy_sniffer_log(),
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12)),
        )
        self.sniff_learn_switch = ft.Switch(
            label="Learn mode (показать все TX)",
            value=False,
            on_change=self._toggle_learn_mode,
        )

        self.browser_picker = ft.FilePicker()
        self.clipboard = ft.Clipboard()
        self.page.services.append(self.browser_picker)
        self.page.services.append(self.clipboard)
        self.browser_path_text = ft.Text(
            self._browser_label(), size=11, color=ft.Colors.ON_SURFACE_VARIANT, italic=True,
        )
        self.browser_pick_button = ft.OutlinedButton(
            "Указать браузер…",
            icon=ft.Icons.FOLDER_OPEN_ROUNDED,
            on_click=lambda e: self._open_browser_picker(),
            disabled=OFFLINE_MODE,
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12)),
        )

        self.toggle_button = ft.FilledButton(
            "Запустить службу",
            icon=ft.Icons.PLAY_ARROW_ROUNDED,
            height=52,
            on_click=lambda e: self.toggle_service(),
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=16),
                padding=ft.Padding.symmetric(horizontal=24, vertical=14),
                text_style=ft.TextStyle(size=15, weight=ft.FontWeight.W_500),
            ),
        )

        control_bar = ft.Container(
            content=ft.Row(
                [self.toggle_button],
                alignment=ft.MainAxisAlignment.END,
            ),
            padding=ft.Padding.symmetric(horizontal=24, vertical=16),
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
        )

        body = ft.Container(
            content=ft.Column(
                [
                    mode_card,
                    device_card,
                    profiles_card,
                    bindings_card,
                    settings_card,
                ],
                spacing=16,
                scroll=ft.ScrollMode.AUTO,
                expand=True,
            ),
            padding=ft.Padding.symmetric(horizontal=24, vertical=8),
            expand=True,
        )

        self.page.add(
            ft.Column(
                [header, body, control_bar],
                spacing=0,
                expand=True,
            )
        )

    def _card(self, icon, title, subtitle, content):
        return ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Container(
                                content=ft.Icon(icon, size=20, color=ft.Colors.ON_SECONDARY_CONTAINER),
                                width=36, height=36,
                                bgcolor=ft.Colors.SECONDARY_CONTAINER,
                                border_radius=12,
                                alignment=ft.Alignment.CENTER,
                            ),
                            ft.Column(
                                [
                                    ft.Text(title, size=16, weight=ft.FontWeight.W_600),
                                    ft.Text(subtitle, size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                                ],
                                spacing=2,
                                tight=True,
                            ),
                        ],
                        spacing=12,
                    ),
                    content,
                ],
                spacing=4,
            ),
            padding=20,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border_radius=20,
        )

    # ---------- Devices ----------
    # VID/usage_page всех клавиатур, настраиваемых через qmk.top.
    # PID отличается у разных моделей, VID и Page — общие.
    QMK_TOP_VID = 0x3151
    QMK_TOP_USAGE_PAGE = 0xFFFF

    def refresh_devices(self):
        self.devices = hid.enumerate()
        custom_devices = [
            d for d in self.devices
            if d['vendor_id'] == self.QMK_TOP_VID and d['usage_page'] == self.QMK_TOP_USAGE_PAGE
        ]
        seen = set()
        deduped = []
        for d in custom_devices:
            # Дедуп по тому же ключу, что использует _device_key_of (VID:PID:usage_page).
            # Раньше ключ включал ещё `usage`, и устройства с одинаковым usage_page,
            # но разными usage (часто 0x00 + 0x01 на одной клавиатуре) попадали как
            # два отдельных пункта дропдауна с ОДИНАКОВЫМ key — выглядели дубликатами.
            key = (d['vendor_id'], d['product_id'], d['usage_page'])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(d)
        custom_devices = deduped
        self.filtered_devices = custom_devices

        # Make sure every present device has a config entry BEFORE we probe
        # battery — the probe needs the saved query/parse params.
        for d in self.filtered_devices:
            self._ensure_device_entry(d)

        # Auto-detect transport by device name (no manual override needed).
        cfg_dirty = False
        for d in self.filtered_devices:
            key = self._device_key_of(d)
            entry = self.config["devices"].get(key)
            if entry is None:
                continue
            new_transport = self._detect_transport(d)
            if entry.get("transport") != new_transport:
                entry["transport"] = new_transport
                cfg_dirty = True
        if cfg_dirty:
            self.save_config()

        custom_devices = self.filtered_devices

        options = []
        for i, d in enumerate(custom_devices):
            key = self._device_key_of(d)
            saved = self.config["devices"].get(key)
            label_prefix = (saved.get("label") or self._device_label_for(d)) if saved else self._device_label_for(d)
            transport = (saved or {}).get("transport") or self._detect_transport(d)
            badge = "[WIRED]" if transport == "wired" else "[WIRELESS]"
            label_text = (
                f"{badge} {label_prefix} · VID {hex(d['vendor_id'])} · PID {hex(d['product_id'])} · Page {hex(d['usage_page'])}"
            )
            vid, pid, up = d['vendor_id'], d['product_id'], d['usage_page']
            row = ft.Row(
                [
                    ft.Text(label_text, expand=True, overflow=ft.TextOverflow.ELLIPSIS),
                ],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
            options.append(ft.dropdown.Option(key=key, text=label_text, content=row))
        self.device_dropdown.options = options

        present_keys = [self._device_key_of(d) for d in self.filtered_devices]
        active_key = self.config.get("active_device")
        target_key = self._pick_active_target(active_key, present_keys, self.config["devices"])
        self.device_dropdown.value = target_key
        if target_key and target_key != active_key:
            self._activate_device(target_key)
        elif target_key:
            entry = self.config["devices"].get(target_key)
            if entry and entry.get("keyboard_type") is None:
                self._show_setup_wizard(target_key)
        try:
            self._update_transport_icon()
        except Exception:
            pass
        self.page.update()

    def _on_device_dropdown_changed(self):
        key = self.device_dropdown.value
        if not key:
            return
        hid_dev = next(
            (d for d in self.filtered_devices if self._device_key_of(d) == key),
            None,
        )
        # Profiles are static defaults — any selected device "just works".
        # Auto-create the config entry for unknown devices and activate silently.
        if key not in self.config["devices"]:
            if hid_dev is None:
                return
            self._ensure_device_entry(hid_dev)
        if key != self.config.get("active_device"):
            self._activate_device(key)

        self.page.update()

    def open_sniffer_modal(self):
        self._sniff_auto_scroll = True

        def on_close(e):
            try:
                self.page.pop_dialog()
            except Exception:
                pass

        battery_panel = self._build_battery_test_panel()
        self._battery_test_sync_from_active()

        self._sniff_scroll_btn = ft.IconButton(
            icon=ft.Icons.ARROW_DOWNWARD_ROUNDED,
            tooltip="Прокрутить вниз (авто-скролл)",
            icon_color=ft.Colors.PRIMARY,
            on_click=self._sniff_scroll_to_bottom,
        )

        body = ft.Column(
            [
                ft.Row(
                    [self.sniff_button, self.sniff_clear_button, self.sniff_copy_button,
                     self._sniff_scroll_btn],
                    spacing=8, wrap=True,
                ),
                ft.Row(
                    [self.browser_pick_button, self.browser_path_text],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Row(
                    [self.sniff_learn_switch],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                battery_panel,
                self.sniff_status,
                ft.Container(
                    content=self.sniff_log,
                    bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                    border_radius=12,
                    padding=4,
                    expand=True,
                ),
            ],
            spacing=10,
            expand=True,
        )

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("HID Sniffer — qmk.top"),
            content=ft.Container(
                content=body,
                expand=True,
            ),
            actions=[
                ft.TextButton("Закрыть", on_click=on_close),
            ],
            shape=ft.RoundedRectangleBorder(radius=20),
        )
        self.page.show_dialog(dlg)
    def open_profile_dialog(self, index):
        info = self._profile_info_at(index) or {"hotkey": ""}
        current_name = self._profile_name_at(index) or f"Профиль {index + 1}"
        current_hotkey = info.get("hotkey", "")

        other_hotkeys = {}
        for idx, (pname, pinfo) in enumerate(self._profile_items()):
            hk = (pinfo.get("hotkey") or "").strip().lower()
            if hk and idx != index:
                other_hotkeys[hk] = pname

        name_field = ft.TextField(
            label="Название профиля",
            hint_text="Например: Gaming, Typing",
            value=current_name,
            border_radius=12,
            filled=True,
        )

        hotkey_display = ft.TextField(
            label="Горячая клавиша",
            hint_text="Кликни и нажми сочетание",
            value=current_hotkey,
            border_radius=12,
            filled=True,
            read_only=True,
        )
        hotkey_conflict = ft.Text("", size=11, color=ft.Colors.ERROR, visible=False)
        hotkey_state = {"capturing": False, "hook": None, "value": current_hotkey,
                        "mods": set()}

        _MOD_NAMES = {
            "ctrl", "left ctrl", "right ctrl",
            "shift", "left shift", "right shift",
            "alt", "left alt", "right alt",
            "left windows", "right windows",
        }
        _MOD_CANONICAL = {
            "ctrl": "ctrl", "left ctrl": "ctrl", "right ctrl": "ctrl",
            "shift": "shift", "left shift": "shift", "right shift": "shift",
            "alt": "alt", "left alt": "alt", "right alt": "alt",
            "left windows": "win", "right windows": "win",
        }

        def _on_hotkey_capture(event):
            name = (event.name or "").lower()
            if event.event_type == keyboard.KEY_DOWN:
                if name in _MOD_NAMES:
                    hotkey_state["mods"].add(_MOD_CANONICAL[name])
                    return
                mod_order = ["ctrl", "shift", "alt", "win"]
                parts = [m for m in mod_order if m in hotkey_state["mods"]]
                parts.append(name)
                combo = "+".join(parts)
                hotkey_state["mods"].clear()

                conflict_profile = other_hotkeys.get(combo)
                def _update_ui():
                    if conflict_profile:
                        hotkey_conflict.value = f"Конфликт: «{combo}» уже используется в «{conflict_profile}»"
                        hotkey_conflict.visible = True
                    else:
                        hotkey_conflict.visible = False
                    hotkey_state["value"] = combo
                    hotkey_display.value = combo
                    self.page.update()
                self._ui_call(_update_ui)
            elif event.event_type == keyboard.KEY_UP:
                if name in _MOD_NAMES:
                    hotkey_state["mods"].discard(_MOD_CANONICAL[name])

        def _start_capture(e):
            if hotkey_state["capturing"]:
                return
            hotkey_state["capturing"] = True
            hotkey_state["mods"].clear()
            hotkey_display.hint_text = "Нажми сочетание клавиш..."
            hotkey_display.border_color = ft.Colors.PRIMARY
            try:
                keyboard.unhook_all()
            except Exception:
                pass
            hotkey_state["hook"] = keyboard.hook(_on_hotkey_capture, suppress=True)
            self.page.update()

        def _stop_capture():
            if not hotkey_state["capturing"]:
                return
            hotkey_state["capturing"] = False
            hotkey_state["mods"].clear()
            if hotkey_state["hook"]:
                try:
                    keyboard.unhook(hotkey_state["hook"])
                except Exception:
                    pass
                hotkey_state["hook"] = None
            hotkey_display.hint_text = "Кликни и нажми сочетание"
            hotkey_display.border_color = None
            self.reload_runtime_state()

        hotkey_display.on_focus = _start_capture

        clear_btn = ft.IconButton(
            icon=ft.Icons.CLEAR_ROUNDED,
            tooltip="Очистить хоткей",
            icon_size=18,
            on_click=lambda e: _clear_hotkey(),
        )

        def _clear_hotkey():
            hotkey_state["value"] = ""
            hotkey_display.value = ""
            hotkey_conflict.visible = False
            _stop_capture()
            self.page.update()

        hotkey_row = ft.Row([hotkey_display, clear_btn], spacing=4,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER)

        entry = self._active_device()
        kb_type = entry.get("keyboard_type") if entry else None
        caps = device_capabilities(kb_type)

        polling_dropdown = None
        lighting_dropdown = None
        if DeviceCapability.POLLING_RATE in caps:
            current_pr = info.get("polling_rate")
            pr_options = [ft.dropdown.Option(key="none", text="Не менять")]
            for r in PollingRate:
                pr_options.append(ft.dropdown.Option(key=str(r.value), text=f"{r.value} Hz"))
            polling_dropdown = ft.Dropdown(
                label="Polling Rate",
                options=pr_options,
                value=str(current_pr) if current_pr and current_pr in VALID_POLLING_RATES else "none",
                border_radius=12,
                filled=True,
            )

        if DeviceCapability.LIGHTING_PROFILES in caps:
            current_lp = info.get("lighting_profile")
            lp_options = [ft.dropdown.Option(key="none", text="Не менять")]
            for i in range(LIGHTING_PROFILE_COUNT):
                lp_options.append(ft.dropdown.Option(key=str(i), text=f"Подсветка {i + 1}"))
            lighting_dropdown = ft.Dropdown(
                label="Профиль подсветки",
                options=lp_options,
                value=str(current_lp) if current_lp is not None and current_lp in VALID_LIGHTING_PROFILES else "none",
                border_radius=12,
                filled=True,
            )

        dialog_fields = [name_field, hotkey_row, hotkey_conflict]
        if polling_dropdown:
            dialog_fields.append(polling_dropdown)
        if lighting_dropdown:
            dialog_fields.append(lighting_dropdown)

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"Профиль {index + 1}"),
            content=ft.Container(
                content=ft.Column(
                    dialog_fields,
                    spacing=12,
                    tight=True,
                ),
                width=440,
            ),
            shape=ft.RoundedRectangleBorder(radius=24),
        )

        def on_cancel(e):
            _stop_capture()
            self.page.pop_dialog()

        def on_save(e):
            _stop_capture()
            new_name = name_field.value.strip()
            hk_val = (hotkey_state["value"] or "").strip().lower()
            if not new_name:
                self._snack("Имя профиля обязательно")
                return
            if hk_val and hk_val in other_hotkeys:
                self._snack(f"Хоткей «{hk_val}» уже используется в «{other_hotkeys[hk_val]}»")
                return
            if not self._rename_profile_at(index, new_name):
                self._snack("Имя занято другим профилем")
                return
            self.config["payloads"][new_name]["hotkey"] = hk_val
            if polling_dropdown:
                pr_val = polling_dropdown.value
                if pr_val and pr_val != "none":
                    self.config["payloads"][new_name]["polling_rate"] = int(pr_val)
                else:
                    self.config["payloads"][new_name].pop("polling_rate", None)
            if lighting_dropdown:
                lp_val = lighting_dropdown.value
                if lp_val and lp_val != "none":
                    self.config["payloads"][new_name]["lighting_profile"] = int(lp_val)
                else:
                    self.config["payloads"][new_name].pop("lighting_profile", None)
            self.save_config()
            self.update_payloads_list()
            self.update_bindings_list()
            self.page.pop_dialog()

        dlg.actions = [
            ft.TextButton("Отмена", on_click=on_cancel),
            ft.FilledButton("Сохранить", on_click=on_save),
        ]
        self.page.show_dialog(dlg)

    def update_payloads_list(self):
        self.payloads_column.controls.clear()
        items = self._profile_items()
        entry = self._active_device()
        kb_type = entry.get("keyboard_type") if entry else None
        caps = device_capabilities(kb_type)
        for index in range(self._device_profile_count()):
            name, info = items[index] if index < len(items) else (f"Профиль {index + 1}", {"hotkey": ""})
            hk = info.get("hotkey") or ""
            pr = info.get("polling_rate") if DeviceCapability.POLLING_RATE in caps else None
            data = self._profile_payload_at(index)
            preview = ", ".join(hex(b) for b in data[:4]) + ("…" if len(data) > 4 else "")

            subtitle_parts = [ft.Text(preview, size=11,
                                       color=ft.Colors.ON_SURFACE_VARIANT,
                                       font_family="Consolas")]
            if pr and pr in VALID_POLLING_RATES:
                subtitle_parts.append(ft.Container(
                    content=ft.Text(f"{pr} Hz", size=10, weight=ft.FontWeight.W_500,
                                    color=ft.Colors.ON_SECONDARY_CONTAINER),
                    padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                    bgcolor=ft.Colors.SECONDARY_CONTAINER,
                    border_radius=100,
                ))

            lp = info.get("lighting_profile") if DeviceCapability.LIGHTING_PROFILES in caps else None
            if lp is not None and lp in VALID_LIGHTING_PROFILES:
                subtitle_parts.append(ft.Container(
                    content=ft.Text(f"💡 {lp + 1}", size=10, weight=ft.FontWeight.W_500,
                                    color=ft.Colors.ON_TERTIARY_CONTAINER),
                    padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                    bgcolor=ft.Colors.TERTIARY_CONTAINER,
                    border_radius=100,
                ))

            hotkey_chip = (
                ft.Container(
                    content=ft.Text(hk, size=11, weight=ft.FontWeight.W_500),
                    padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                    bgcolor=ft.Colors.TERTIARY_CONTAINER,
                    border_radius=100,
                ) if hk else ft.Container(
                    content=ft.Text("нет хоткея", size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                    padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                )
            )

            row = ft.Container(
                content=ft.Row(
                    [
                        ft.Row(
                            [
                                ft.Container(
                                    content=ft.Text(str(index + 1), size=14, weight=ft.FontWeight.W_700,
                                                    color=ft.Colors.ON_PRIMARY_CONTAINER),
                                    width=30, height=30,
                                    bgcolor=ft.Colors.PRIMARY_CONTAINER,
                                    border_radius=10,
                                    alignment=ft.Alignment.CENTER,
                                ),
                                ft.Column(
                                    [
                                        ft.Text(name, size=14, weight=ft.FontWeight.W_600),
                                        ft.Row(subtitle_parts, spacing=6),
                                    ],
                                    spacing=0,
                                    tight=True,
                                ),
                            ],
                            spacing=12,
                            expand=True,
                        ),
                        ft.Row(
                            [
                                ft.Checkbox(
                                    value=(self.default_profile_index == index),
                                    tooltip="Профиль по умолчанию (когда нет совпадений по активному окну)",
                                    on_change=lambda e, i=index: self._set_default_profile(i, e.control.value),
                                ),
                                hotkey_chip,
                                ft.IconButton(
                                    icon=ft.Icons.EDIT_ROUNDED,
                                    tooltip="Изменить имя / хоткей",
                                    icon_size=18,
                                    on_click=lambda e, i=index: self.open_profile_dialog(i),
                                ),
                            ],
                            spacing=4,
                        ),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                padding=ft.Padding.symmetric(horizontal=14, vertical=10),
                bgcolor=ft.Colors.SURFACE_CONTAINER,
                border_radius=14,
            )
            self.payloads_column.controls.append(row)
        self.page.update()

    def _set_default_profile(self, index: int, checked: bool) -> None:
        entry = self._active_device()
        if entry is None:
            return
        entry["default_profile_index"] = index if checked else None
        self.save_config()
        self.reload_runtime_state()
        self.update_payloads_list()

    # ---------- Bindings ----------
    def open_binding_dialog(self, title, edit_idx=None):
        items = self._profile_items()
        if not items:
            self._snack("Сначала создайте хотя бы один профиль")
            return

        b_data = self.config["bindings"][edit_idx] if edit_idx is not None else None
        current_pi = b_data.get("profile_index", 0) if b_data else 0
        if not (0 <= current_pi < len(items)):
            current_pi = 0

        proc_field = ft.TextField(
            label="Процесс",
            hint_text="например, cs2.exe",
            value=b_data["process"] if b_data else "",
            border_radius=12,
            filled=True,
        )
        prof_dropdown = ft.Dropdown(
            label="Профиль",
            options=[
                ft.dropdown.Option(key=str(i), text=f"{i + 1}. {name}")
                for i, (name, _) in enumerate(items)
            ],
            value=str(current_pi),
            border_radius=12,
            filled=True,
        )

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(title),
            content=ft.Container(
                content=ft.Column([proc_field, prof_dropdown], spacing=12, tight=True),
                width=440,
            ),
            shape=ft.RoundedRectangleBorder(radius=24),
        )

        def on_cancel(e):
            self.page.pop_dialog()

        def on_save(e):
            proc = proc_field.value.strip().lower()
            if not proc or prof_dropdown.value is None:
                self._snack("Процесс и профиль обязательны")
                return
            try:
                pi = int(prof_dropdown.value)
            except Exception:
                self._snack("Некорректный профиль")
                return
            new_bind = {"process": proc, "profile_index": pi}
            if edit_idx is not None:
                self.config["bindings"][edit_idx] = new_bind
            else:
                self.config["bindings"].append(new_bind)
            self.save_config()
            self.update_bindings_list()
            self.page.pop_dialog()

        dlg.actions = [
            ft.TextButton("Отмена", on_click=on_cancel),
            ft.FilledButton("Сохранить", on_click=on_save),
        ]
        self.page.show_dialog(dlg)

    def delete_binding(self, idx):
        del self.config["bindings"][idx]
        self.save_config()
        self.update_bindings_list()

    def _on_rule_toggle(self, idx: int, enabled: bool):
        bindings = self.config.get("bindings", [])
        if 0 <= idx < len(bindings):
            bindings[idx]["enabled"] = enabled
            process = bindings[idx].get("process", "?")
            self.rule_evaluator.set_enabled(process, enabled)
            logger.debug("rule toggle: process=%s enabled=%s", process, enabled)
            self.save_config()
            self.update_bindings_list()

    def update_bindings_list(self):
        self.bindings_column.controls.clear()
        if not self.config["bindings"]:
            self.bindings_column.controls.append(
                ft.Container(
                    content=ft.Text(
                        "Привязок пока нет. В ручном режиме можно обойтись хоткеями.",
                        color=ft.Colors.ON_SURFACE_VARIANT,
                        size=13,
                    ),
                    padding=16,
                    alignment=ft.Alignment.CENTER,
                )
            )
        else:
            for i, b in enumerate(self.config["bindings"]):
                pi = b.get("profile_index", 0)
                pname = self._profile_name_at(pi) or f"Профиль {pi + 1}"
                enabled = b.get("enabled", True)

                toggle = ft.Switch(
                    value=enabled,
                    on_change=lambda e, idx=i: self._on_rule_toggle(idx, e.control.value),
                )

                row = ft.Container(
                    content=ft.Row(
                        [
                            toggle,
                            ft.Row(
                                [
                                    ft.Icon(ft.Icons.APPS_ROUNDED,
                                            color=ft.Colors.TERTIARY, size=20),
                                    ft.Text(b["process"], size=14, weight=ft.FontWeight.W_500,
                                            font_family="Consolas"),
                                    ft.Icon(ft.Icons.ARROW_FORWARD_ROUNDED, size=16,
                                            color=ft.Colors.ON_SURFACE_VARIANT),
                                    ft.Container(
                                        content=ft.Text(f"{pi + 1}. {pname}", size=12,
                                                        weight=ft.FontWeight.W_500),
                                        padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                                        bgcolor=ft.Colors.PRIMARY_CONTAINER,
                                        border_radius=100,
                                    ),
                                ],
                                spacing=10,
                                expand=True,
                            ),
                            ft.Row(
                                [
                                    ft.IconButton(
                                        icon=ft.Icons.EDIT_ROUNDED,
                                        tooltip="Редактировать",
                                        icon_size=18,
                                        on_click=lambda e, idx=i: self.open_binding_dialog("Редактировать", idx),
                                    ),
                                    ft.IconButton(
                                        icon=ft.Icons.DELETE_ROUNDED,
                                        tooltip="Удалить",
                                        icon_size=18,
                                        icon_color=ft.Colors.ERROR,
                                        on_click=lambda e, idx=i: self.delete_binding(idx),
                                    ),
                                ],
                                spacing=4,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    padding=ft.Padding.symmetric(horizontal=14, vertical=10),
                    bgcolor=ft.Colors.SURFACE_CONTAINER,
                    border_radius=14,
                    opacity=1.0 if enabled else 0.5,
                )
                self.bindings_column.controls.append(row)
        self.page.update()

    # ---------- Sniffer ----------
    def _resolve_browser_path(self):
        saved = (self.config.get("settings", {}).get("browser_path") or "").strip()
        if saved and os.path.isfile(saved):
            return saved
        return self.detected_browser_path

    def _browser_label(self):
        path = self._resolve_browser_path()
        if not path:
            return "Браузер не найден — укажи путь к chrome.exe / msedge.exe вручную."
        saved = (self.config.get("settings", {}).get("browser_path") or "").strip()
        prefix = "Указан вручную" if saved and saved == path else "Найден"
        return f"{prefix}: {path}"

    def _open_browser_picker(self):
        async def _pick():
            files = await self.browser_picker.pick_files(
                dialog_title="Выбери исполняемый файл браузера (Chrome / Edge / Brave / Vivaldi)",
                allow_multiple=False,
                allowed_extensions=["exe"],
            )
            self._handle_browser_pick(files)
        self.page.run_task(_pick)

    def _handle_browser_pick(self, files):
        if not files:
            return
        path = files[0].path
        if not is_chromium_executable(path):
            self.sniff_status.value = "Файл не похож на Chromium-браузер. Жду chrome.exe / msedge.exe / brave.exe и т.п."
            self.page.update()
            return
        self.config.setdefault("settings", {})["browser_path"] = path
        self.save_config()
        self.browser_path_text.value = self._browser_label()
        self.sniff_status.value = "Браузер сохранён. Можно запускать sniff."
        self.page.update()

    def toggle_sniffer(self):
        if self.sniffer is None:
            if OFFLINE_MODE:
                self.sniff_status.value = "Sniffer disabled in offline mode. Enable it only when you need a one-time capture."
                self.page.update()
                return
            browser = self._resolve_browser_path()
            if not browser:
                self.sniff_status.value = "Браузер не выбран. Жми «Указать браузер…» и выбери chrome.exe / msedge.exe."
                self.page.update()
                self._open_browser_picker()
                return
            try:
                self.sniffer = HIDSniffer(
                    on_event=self._on_sniff_event,
                    on_status=self._on_sniff_status,
                    browser_path=browser,
                    offline_mode=False,
                )
                self._battery_captured_this_session = False
                self._battery_capture_attempts = 0
                self._battery_locked = False
                self._captured_profile_indices = set()
                self.sniffer.start()
                self.sniff_button.text = "Остановить sniff"
                self.sniff_button.icon = ft.Icons.STOP_ROUNDED
            except Exception as ex:
                self.sniffer = None
                self.sniff_status.value = f"Ошибка: {ex}"
            self.page.update()
            return
        try:
            self.sniffer.stop()
        except Exception:
            pass
        self.sniffer = None
        self.sniff_button.text = "Запустить sniff"
        self.sniff_button.icon = ft.Icons.SENSORS_ROUNDED
        self.page.update()

    def _maybe_check_updates(self):
        if not ENABLE_UPDATE_CHECK:
            self.update_check_state = _load_local_update_state()
            return
        self.update_check_state = {
            "enabled": True,
            "checked_at": None,
            "latest_version": None,
            "error": "Update check hook not wired yet.",
        }

    def _on_sniff_status(self, msg: str):
        def upd():
            self.sniff_status.value = msg
            self.page.update()
        try:
            self.page.run_thread(upd)
        except Exception:
            upd()

    def _toggle_learn_mode(self, e):
        self._sniff_learn_mode = bool(e.control.value)
        logger.debug("learn_mode toggled: %s", self._sniff_learn_mode)
        if self._sniff_learn_mode:
            self._start_battery_probe_worker()
        else:
            self._stop_battery_probe_worker()

    def _sniff_scroll_to_bottom(self, e=None):
        self._sniff_auto_scroll = True
        try:
            self.sniff_log.scroll_to(offset=-1, duration=100)
            self.page.update()
        except Exception:
            pass

    def _on_sniff_scroll(self, e: ft.OnScrollEvent):
        if e.event_type == "user":
            self._sniff_auto_scroll = False

    def _start_battery_probe_worker(self):
        entry = self._active_device()
        batt = entry.get("battery") if entry else None
        if not batt or not batt.get("response_offset"):
            logger.debug("battery probe worker not started: no battery config")
            return
        self._battery_probe_stop.clear()
        while not self._battery_probe_queue.empty():
            try:
                self._battery_probe_queue.get_nowait()
            except queue.Empty:
                break
        self._battery_probe_thread = threading.Thread(
            target=self._battery_probe_worker, daemon=True)
        self._battery_probe_thread.start()
        logger.debug("battery probe worker started")

    def _stop_battery_probe_worker(self):
        self._battery_probe_stop.set()
        while not self._battery_probe_queue.empty():
            try:
                self._battery_probe_queue.get_nowait()
            except queue.Empty:
                break
        self._battery_probe_thread = None
        logger.debug("battery probe worker stopped")

    def _battery_probe_worker(self):
        logger.debug("battery probe worker thread running")
        while not self._battery_probe_stop.is_set():
            try:
                item = self._battery_probe_queue.get(timeout=1)
            except queue.Empty:
                continue
            packet_data, result_text = item
            path = self.get_keyboard_path_safe()
            if path is None:
                logger.debug("battery probe: no device path")
                continue
            time.sleep(0.2)
            if self._battery_probe_stop.is_set():
                break
            percent = self.battery_monitor.probe_battery(packet_data, path)
            if percent is not None:
                txt = f"🔋 {percent}%"
                color = ft.Colors.GREEN_400
            else:
                txt = "—"
                color = ft.Colors.GREY_500
            logger.debug("battery probe result: packet=%s → %s",
                         [f"0x{b:02x}" for b in packet_data[:4]], txt)
            def upd(t=txt, c=color, rt=result_text):
                try:
                    rt.value = t
                    rt.color = c
                    self.page.update()
                except Exception:
                    pass
            self._ui_call(upd)
        logger.debug("battery probe worker thread exiting")

    def _on_sniff_event(self, ev: dict):
        self.sniff_events.append(ev)
        data = ev.get("data") or []
        if not data:
            return
        direction = (ev.get("dir") or "").upper()
        ev_type = (ev.get("type") or "").lower()

        if direction != "TX":
            return
        logger.debug("sniff TX event type=%s reportId=%s data=%s",
                      ev.get("type"), ev.get("reportId"),
                      [f"0x{b:02x}" for b in data[:8]])

        is_profile = self._matches_profile_pattern(data)
        ev_type = (ev.get("type") or "").lower()
        if is_profile:
            logger.debug("sniff: PROFILE detected reportId=%s ev_type=%s full_data=%s",
                          ev.get("reportId"), ev_type,
                          [f"0x{b:02x}" for b in data])
        is_battery = (
            ev_type == "feature"
            and not is_profile
            and self._matches_battery_pattern(data, ev_type)
        )

        # Learn mode: log EVERY TX frame with a classification tag, and
        # bypass auto-save (user must click "Сохранить" explicitly).
        if self._sniff_learn_mode:
            if is_profile:
                tag, color = "PROFILE?", ft.Colors.AMBER_400
            elif self._matches_polling_rate_pattern(data):
                tag, color = "POLL_RATE?", ft.Colors.GREEN_300
            elif self._matches_lighting_profile_pattern(data):
                tag, color = "LIGHTING?", ft.Colors.PURPLE_300
            elif ev_type == "feature":
                tag, color = "BATTERY?", ft.Colors.LIGHT_BLUE_300
            else:
                tag, color = "TX", ft.Colors.GREY_500
            self._render_sniff_row(tag, color, data, ev.get("reportId"), ev_type, ev)
            return

        if not (is_profile or is_battery):
            return

        # Ensure capture lands on an active device — if user opened sniffer
        # without picking a device first, we can't autofill anywhere.
        if self._active_device() is None:
            self._on_sniff_status("Нет активного устройства — выбери клавиатуру в списке.")
            return

        if is_battery:
            if self._battery_locked:
                return
            try:
                self.config["battery"]["query"] = list(data)
                rid = ev.get("reportId")
                if isinstance(rid, int):
                    self.config["battery"]["report_id"] = rid
                self.save_config()
            except Exception:
                pass
            self._battery_capture_attempts += 1
            self._battery_captured_this_session = True
            self._battery_locked = True
            self._on_sniff_status(
                "Battery: query захвачен. Закрой браузер, и через ≤60с (или по кнопке refresh) "
                "процент появится в шапке и в трее."
            )

        # Profiles are NOT auto-saved anymore — defaults work for everyone, and
        # users explicitly opt in via the per-row "Сохранить" button below.
        slot_idx = data[1] if (is_profile and len(data) > 1 and data[1] in (0, 1, 2, 3)) else None

        hex_str = " ".join(f"{b:02X}" for b in data[:64])
        if len(data) > 64:
            hex_str += f" …+{len(data) - 64}"
        type_ = ev.get("type") or ""
        rid = ev.get("reportId")
        idx = len(self.sniff_events)

        if is_profile:
            tag_text, tag_color = "PROFILE", ft.Colors.AMBER
        else:
            tag_text, tag_color = "BATTERY", ft.Colors.LIGHT_BLUE_ACCENT

        controls = [
            ft.Text(f"#{idx}", size=11, color=ft.Colors.ON_SURFACE_VARIANT, width=40),
            ft.Container(
                content=ft.Text(tag_text, size=10, weight=ft.FontWeight.BOLD, color=ft.Colors.BLACK),
                bgcolor=tag_color, padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                border_radius=6,
            ),
            ft.Text(f"{type_} id={rid}", size=11, color=ft.Colors.ON_SURFACE_VARIANT, width=110),
            ft.Text(hex_str, size=10, selectable=True, font_family="Consolas",
                    no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS, expand=True),
        ]
        if is_profile:
            label_text = f"→ слот {slot_idx + 1}" if slot_idx is not None else "→ нераспознанный слот"
            controls.append(
                ft.Container(
                    content=ft.Text(label_text, size=10, color=ft.Colors.ON_SURFACE_VARIANT, italic=True),
                    padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                )
            )
            if slot_idx is not None:
                captured = list(data)
                save_btn = ft.FilledTonalButton(
                    "Сохранить в конфиг",
                    icon=ft.Icons.SAVE_ROUNDED,
                    on_click=lambda e, i=slot_idx, p=captured: self._save_profile_payload_from_sniff(i, p),
                    style=ft.ButtonStyle(
                        shape=ft.RoundedRectangleBorder(radius=10),
                        padding=ft.Padding.symmetric(horizontal=10, vertical=6),
                        text_style=ft.TextStyle(size=11, weight=ft.FontWeight.W_500),
                    ),
                )
                controls.append(save_btn)
        else:
            battery_chip_text = ft.Text("…", size=11, weight=ft.FontWeight.W_600, color=ft.Colors.BLACK)
            battery_chip = ft.Container(
                content=ft.Row(
                    [
                        ft.Icon(ft.Icons.BATTERY_FULL_ROUNDED, size=14, color=ft.Colors.BLACK),
                        battery_chip_text,
                    ],
                    spacing=4, tight=True,
                ),
                bgcolor=ft.Colors.LIGHT_BLUE_ACCENT,
                padding=ft.Padding.symmetric(horizontal=8, vertical=2),
                border_radius=100,
            )
            controls.append(battery_chip)
            self._sniff_battery_chip = battery_chip_text
            threading.Thread(target=self._refresh_battery_for_sniff_chip, daemon=True).start()

        # NOTE: Task 8 will plug action buttons (e.g. "Try as profile", "Try as
        # battery") into the learn-mode row rendered by _render_sniff_row above.
        line = ft.Container(
            content=ft.Row(
                controls,
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            border_radius=10,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        )
        def upd():
            self.sniff_log.controls.append(line)
            if len(self.sniff_log.controls) > 500:
                self.sniff_log.controls = self.sniff_log.controls[-500:]
            if self._sniff_auto_scroll:
                self.sniff_log.scroll_to(offset=-1, duration=100)
            self.page.update()
        try:
            self.page.run_thread(upd)
        except Exception:
            upd()

    def _render_sniff_row(self, tag, color, data, report_id, ev_type, payload):
        """Render a single sniffer log row (learn mode) with auto battery probe."""
        hex_str = " ".join(f"{b:02X}" for b in data[:64])
        if len(data) > 64:
            hex_str += f" …+{len(data) - 64}"

        extra_info = ""
        if tag == "POLL_RATE?" and len(data) >= 3:
            code_to_hz = {v: k.value for k, v in POLLING_RATE_CODES.items()}
            hz = code_to_hz.get(data[2])
            if hz:
                extra_info = f" → {hz} Hz"
        elif tag == "RX" and len(data) >= 2 and all(b == data[0] and b2 == data[1] for b, b2 in zip(data[0::2], data[1::2])):
            val = data[0] | (data[1] << 8)
            if val > 0:
                extra_info = f" → repeated {val}"

        idx = len(self.sniff_events)
        battery_result_text = ft.Text("", size=11, weight=ft.FontWeight.W_600, width=80)
        controls = [
            ft.Text(f"#{idx}", size=11, color=ft.Colors.ON_SURFACE_VARIANT, width=40),
            ft.Container(
                content=ft.Text(tag, size=10, weight=ft.FontWeight.BOLD, color=ft.Colors.BLACK),
                bgcolor=color, padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                border_radius=6,
            ),
            ft.Text(f"{ev_type} id={report_id}", size=11, color=ft.Colors.ON_SURFACE_VARIANT, width=110),
            ft.Text(hex_str + extra_info, size=10, selectable=True, font_family="Consolas",
                    no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS, expand=True),
            battery_result_text,
        ]
        slot_btn = ft.PopupMenuButton(
            content=ft.Container(
                content=ft.Row(
                    [ft.Text("В слот"), ft.Icon(ft.Icons.ARROW_DROP_DOWN, size=16)],
                    spacing=2,
                ),
                padding=ft.Padding.symmetric(horizontal=8, vertical=4),
                border=ft.Border.all(1, ft.Colors.OUTLINE),
                border_radius=4,
            ),
            items=[
                ft.PopupMenuItem(
                    content=f"Профиль {i + 1}",
                    on_click=lambda e, idx=i, d=list(data): self._save_profile_payload_from_sniff(idx, d),
                )
                for i in range(4)
            ],
        )
        batt_btn = ft.OutlinedButton(
            "Как battery query",
            on_click=lambda e, d=list(data), rid=report_id: self._save_battery_query_from_sniff(d, rid),
        )
        controls.append(slot_btn)
        controls.append(batt_btn)
        line = ft.Container(
            content=ft.Row(
                controls,
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            border_radius=10,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        )
        def upd():
            self.sniff_log.controls.append(line)
            if len(self.sniff_log.controls) > 500:
                self.sniff_log.controls = self.sniff_log.controls[-500:]
            if self._sniff_auto_scroll:
                self.sniff_log.scroll_to(offset=-1, duration=100)
            self.page.update()
        try:
            self.page.run_thread(upd)
        except Exception:
            upd()
        if (self._sniff_learn_mode
                and ev_type == "feature"
                and self._battery_probe_thread is not None
                and not self._battery_probe_stop.is_set()):
            self._battery_probe_queue.put((list(data), battery_result_text))

    @staticmethod
    def _matches_polling_rate_pattern(data: list) -> bool:
        if len(data) < 8:
            return False
        if data[0] != 0x03 or data[1] != 0x00:
            return False
        if data[2] not in range(7):
            return False
        if any(b != 0 for b in data[3:7]):
            return False
        if data[7] != (255 - sum(data[0:7])) & 0xFF:
            return False
        if any(b != 0 for b in data[8:]):
            return False
        return True

    @staticmethod
    def _matches_lighting_profile_pattern(data: list) -> bool:
        if len(data) < 9:
            return False
        if data[0] != 0x07 or data[1] != 0x0D or data[2] != 0x04 or data[3] != 0x04:
            return False
        if data[4] % 0x10 != 0 or data[4] // 0x10 >= LIGHTING_PROFILE_COUNT:
            return False
        if data[5] != 0 or data[6] != 0xC8 or data[7] != 0xC8:
            return False
        if data[8] != (511 - sum(data[0:8])) & 0xFF:
            return False
        if any(b != 0 for b in data[9:]):
            return False
        return True

    @staticmethod
    def _matches_profile_pattern(data: list) -> bool:
        if len(data) < 8:
            return False
        opcode = data[0]
        kb_info = next((v for v in KEYBOARD_TYPES.values() if v["opcode"] == opcode), None)
        if kb_info is None:
            return False
        if data[1] not in range(kb_info["profiles"]):
            return False
        if any(b != 0 for b in data[2:7]):
            return False
        expected_check = (kb_info["checksum_base"] - data[1]) & 0xFF
        if data[7] != expected_check:
            return False
        if any(b != 0 for b in data[8:]):
            return False
        return True

    @staticmethod
    def _matches_battery_pattern(data: list, ev_type: str = "") -> bool:
        # qmk.top battery-query: any TX feature report that isn't a profile frame.
        # (qmk.top's battery query opcode varies between firmwares; rather than
        # over-filtering and missing it, we just exclude the known profile opcode
        # and require it be a feature report of reasonable length.)
        if len(data) < 2:
            return False
        if data[0] == 0x04:
            return False
        return True

    def _save_profile_payload_from_sniff(self, index: int, sample_data: list):
        if self._active_device() is None:
            self._on_sniff_status("Нет активного устройства — выбери клавиатуру в списке.")
            return
        self._capture_profile_payload(index, sample_data)
        name = self._profile_name_at(index) or f"Профиль {index + 1}"
        self._on_sniff_status(f"Payload сохранён в «{name}» (слот {index + 1}).")

    # ---------- Battery test panel ----------
    @staticmethod
    def _parse_int(text, default=0, base=0):
        try:
            text = (text or "").strip()
            if not text:
                return default
            if base == 0:
                return int(text, 0)
            return int(text, base)
        except Exception:
            return default

    @staticmethod
    def _parse_float(text, default=1.0):
        try:
            return float((text or "").strip())
        except Exception:
            return default

    @staticmethod
    def _parse_optional_int(text):
        text = (text or "").strip()
        if not text or text.lower() == "none":
            return None
        try:
            return int(text, 0)
        except Exception:
            return None

    def _ensure_battery_test_fields(self):
        if getattr(self, "bt_report_id", None) is not None:
            return
        self.bt_report_id = ft.TextField(label="report_id", width=110)
        self.bt_response_length = ft.TextField(label="response_length", width=140)
        self.bt_response_offset = ft.TextField(label="response_offset", width=140)
        self.bt_response_scale = ft.TextField(label="response_scale", width=140)
        self.bt_charging_offset = ft.TextField(label="charging_offset", width=140)
        self.bt_charging_mask = ft.TextField(label="charging_mask (hex)", width=160)
        self.bt_result = ft.Text(value="", size=12, selectable=True)

    def _build_battery_test_panel(self):
        self._ensure_battery_test_fields()
        save_btn = ft.FilledTonalButton("Сохранить", on_click=self._battery_test_save)
        test_btn = ft.FilledButton("Тест", on_click=self._battery_test_run)
        inner = ft.Column(
            [
                ft.Row(
                    [self.bt_report_id, self.bt_response_length, self.bt_response_offset],
                    spacing=8, wrap=True,
                ),
                ft.Row(
                    [self.bt_response_scale, self.bt_charging_offset, self.bt_charging_mask],
                    spacing=8, wrap=True,
                ),
                ft.Row([save_btn, test_btn], spacing=8),
                self.bt_result,
            ],
            spacing=8,
            tight=True,
        )
        tile_cls = getattr(ft, "ExpansionTile", None)
        if tile_cls is not None:
            try:
                return tile_cls(
                    title=ft.Text("Battery test panel"),
                    expanded=False,
                    controls=[ft.Container(content=inner, padding=12)],
                )
            except Exception:
                pass
        return ft.Container(
            content=ft.Column(
                [ft.Text("Battery test panel", weight=ft.FontWeight.W_600), inner],
                spacing=8, tight=True,
            ),
            padding=12,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
            border_radius=12,
        )

    def _battery_test_sync_from_active(self):
        self._ensure_battery_test_fields()
        entry = self._active_device()
        batt = (entry or {}).get("battery") or {}
        mapping = [
            (self.bt_report_id, batt.get("report_id", 0), False),
            (self.bt_response_length, batt.get("response_length", 65), False),
            (self.bt_response_offset, batt.get("response_offset", 2), False),
            (self.bt_response_scale, batt.get("response_scale", 1), False),
            (self.bt_charging_offset,
             "" if batt.get("charging_offset") is None else batt.get("charging_offset"), False),
            (self.bt_charging_mask, hex(int(batt.get("charging_mask", 0) or 0)), True),
        ]
        for field_widget, value, is_hex in mapping:
            if is_hex:
                field_widget.value = value if isinstance(value, str) else hex(int(value or 0))
            else:
                field_widget.value = "" if value is None else str(value)
            try:
                field_widget.update()
            except Exception:
                pass

    def _battery_test_build_config(self):
        entry = self._active_device()
        existing = (entry or {}).get("battery") or {}
        return {
            "query": list(existing.get("query") or []),
            "report_id": self._parse_int(self.bt_report_id.value, 0),
            "response_length": self._parse_int(self.bt_response_length.value, 65),
            "response_offset": self._parse_int(self.bt_response_offset.value, 0),
            "response_scale": self._parse_float(self.bt_response_scale.value, 1.0),
            "charging_offset": self._parse_optional_int(self.bt_charging_offset.value),
            "charging_mask": self._parse_int(self.bt_charging_mask.value, 0),
        }

    def _battery_test_save(self, e):
        entry = self._active_device()
        if entry is None:
            self._snack("Нет активного устройства")
            return
        cfg = self._battery_test_build_config()
        batt = entry.setdefault("battery", {})
        # Preserve query as-is (we don't expose query editing here)
        cfg["query"] = list(batt.get("query") or [])
        batt.update(cfg)
        self.save_config()
        # Recreate / repoint battery monitor at the new dict
        try:
            self.battery_monitor = BatteryMonitor(
                config_battery=self.config["battery"],
                usb_lock=self.usb_lock,
                get_device_path=self.get_keyboard_path_safe,
                get_device_paths=self.get_keyboard_paths,
                on_working_path=self._cache_working_path,
            )
        except Exception:
            pass
        self._snack("Battery конфиг сохранён")

    def _battery_test_run(self, e):
        entry = self._active_device()
        if entry is None:
            self.bt_result.value = "Нет активного устройства"
            try:
                self.bt_result.update()
            except Exception:
                pass
            return
        cfg = self._battery_test_build_config()
        try:
            monitor = BatteryMonitor(
                config_battery=cfg,
                usb_lock=self.usb_lock,
                get_device_path=self.get_keyboard_path_safe,
                get_device_paths=self.get_keyboard_paths,
                on_working_path=self._cache_working_path,
                default_query=DEFAULT_BATTERY_QUERY,
            )
            monitor.read_once()
            state = monitor.state
            if state.percent is not None:
                self.bt_result.value = f"→ percent={state.percent}, charging={state.charging}"
            else:
                self.bt_result.value = "→ ошибка (см. лог)"
        except Exception as exc:
            self.bt_result.value = f"→ ошибка: {exc}"
        try:
            self.bt_result.update()
        except Exception:
            pass

    def _save_battery_query_from_sniff(self, data, report_id):
        entry = self._active_device()
        if entry is None:
            self._snack("Нет активного устройства")
            return
        batt = entry.get("battery")
        if not isinstance(batt, dict):
            batt = {
                "query": [],
                "report_id": 0,
                "response_length": 65,
                "response_offset": 2,
                "response_scale": 1,
                "charging_offset": None,
                "charging_mask": 0,
            }
            entry["battery"] = batt
        batt["query"] = list(data)
        try:
            batt["report_id"] = int(report_id) if report_id is not None else 0
        except (TypeError, ValueError):
            batt["report_id"] = 0
        self.save_config()
        if getattr(self, "battery_monitor", None) is not None:
            try:
                threading.Thread(target=self._manual_battery_refresh, daemon=True).start()
            except Exception:
                pass
        self._snack("Battery query сохранён")

    def _capture_profile_payload(self, index: int, sample_data: list):
        """Save the actually-observed payload for the specific slot only.
        Preserves other slots' existing payloads — no synthesizing payload[1]=idx,
        which produced bytes the keyboard never sent and silently broke switching."""
        _pc = self._device_profile_count()
        if not (0 <= index < _pc):
            return
        items = self._profile_items()
        while len(items) < _pc:
            items.append((f"Профиль {len(items) + 1}", {"data": [], "hotkey": ""}))
        name, info = items[index]
        info = dict(info or {})
        info["data"] = list(sample_data)
        info.setdefault("hotkey", "")
        items[index] = (name, info)
        new_payloads = {n: i for n, i in items}
        self.config["payloads"] = new_payloads
        entry = self._active_device()
        if entry is not None:
            entry["payloads"] = new_payloads
        self.save_config()
        def upd():
            try:
                self.update_payloads_list()
            except Exception:
                pass
            try:
                self.update_bindings_list()
            except Exception:
                pass
            self.page.update()
        try:
            self.page.run_thread(upd)
        except Exception:
            upd()

    def _autofill_profiles(self, sample_data: list, silent: bool = False):
        if not self._matches_profile_pattern(sample_data):
            if not silent:
                self.sniff_status.value = "Не похоже на профильный payload."
                self.page.update()
            return
        _pc = self._device_profile_count()
        items = self._profile_items()
        while len(items) < _pc:
            items.append((f"Профиль {len(items) + 1}", {"data": [], "hotkey": ""}))
        new_payloads = {}
        for idx in range(_pc):
            name, info = items[idx]
            payload = list(sample_data)
            payload[1] = idx
            new_payloads[name] = {
                "data": payload,
                "hotkey": info.get("hotkey", ""),
            }
        self.config["payloads"] = new_payloads
        entry = self._active_device()
        if entry is not None:
            entry["payloads"] = new_payloads
        self.save_config()
        def upd():
            try:
                self.update_payloads_list()
            except Exception:
                pass
            try:
                self.update_bindings_list()
            except Exception:
                pass
            if not silent:
                self.sniff_status.value = "Payload загружен в 4 профиля."
                self.page.update()
            else:
                self.page.update()
        try:
            self.page.run_thread(upd)
        except Exception:
            upd()

    def clear_sniffer_log(self):
        self.sniff_events.clear()
        self.sniff_log.controls.clear()
        self.page.update()

    def copy_sniffer_log(self):
        payload = {
            "outgoing": [e for e in self.sniff_events if e.get("dir") == "tx"],
            "incoming": [e for e in self.sniff_events if e.get("dir") == "rx"],
            "all": self.sniff_events,
        }
        self.clipboard.set(json.dumps(payload, indent=2))
        self.sniff_status.value = f"Скопировано {len(self.sniff_events)} событий в буфер."
        self.page.update()

    # ---------- Service control ----------
    def toggle_service(self):
        if self.is_running:
            self.is_running = False
            try:
                keyboard.unhook_all()
            except Exception:
                pass
            self._set_status(False)
        else:
            if not self.config.get("active_device"):
                self._snack("Выберите HID-устройство")
                return
            self.is_running = True
            self.current_binding = None
            self.save_config()
            self.reload_runtime_state()
            self._set_status(True)
            self.worker_thread = threading.Thread(target=self.background_task, daemon=True)
            self.worker_thread.start()

    def _set_status(self, running):
        mode = self._current_mode()
        mode_text = "Авто" if mode == "auto" else "Ручной"
        if running:
            self.status_badge.bgcolor = ft.Colors.TERTIARY_CONTAINER
            self.status_badge.content = ft.Row(
                [
                    ft.Icon(ft.Icons.CIRCLE, color=ft.Colors.TERTIARY, size=10),
                    ft.Text(f"Работает · {mode_text}", size=12, weight=ft.FontWeight.W_500),
                ],
                spacing=6, tight=True,
            )
            self.toggle_button.text = "Остановить"
            self.toggle_button.icon = ft.Icons.STOP_ROUNDED
            self.toggle_button.style = ft.ButtonStyle(
                bgcolor=ft.Colors.ERROR,
                color=ft.Colors.ON_ERROR,
                shape=ft.RoundedRectangleBorder(radius=16),
                padding=ft.Padding.symmetric(horizontal=24, vertical=14),
                text_style=ft.TextStyle(size=15, weight=ft.FontWeight.W_500),
            )
        else:
            self.status_badge.bgcolor = ft.Colors.ERROR_CONTAINER
            self.status_badge.content = ft.Row(
                [
                    ft.Icon(ft.Icons.CIRCLE, color=ft.Colors.ERROR, size=10),
                    ft.Text("Остановлено", size=12, weight=ft.FontWeight.W_500),
                ],
                spacing=6, tight=True,
            )
            self.toggle_button.text = "Запустить службу"
            self.toggle_button.icon = ft.Icons.PLAY_ARROW_ROUNDED
            self.toggle_button.style = ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=16),
                padding=ft.Padding.symmetric(horizontal=24, vertical=14),
                text_style=ft.TextStyle(size=15, weight=ft.FontWeight.W_500),
            )
        self.page.update()

    # ---------- HID ----------
    def get_keyboard_path(self):
        """Возвращает первый path активного устройства (для совместимости —
        BatteryMonitor использует именно эту сигнатуру). Предпочитает кэшированный
        рабочий path, если он всё ещё перечисляется."""
        paths = self.get_keyboard_paths()
        return paths[0] if paths else None

    def get_keyboard_paths(self):
        """Все HID-пути активного устройства с подходящим usage_page.
        Кэшированный «рабочий» path выносится в начало списка."""
        dev = self.config.get("device")
        if not dev:
            return []
        vid = dev["vid"]
        pid = dev["pid"]
        usage_page = dev["usage_page"]
        paths = []
        for d in hid.enumerate(vid, pid):
            if d['usage_page'] == usage_page:
                paths.append(d['path'])
        cache_key = self._device_key(vid, pid, usage_page)
        cached = self._working_hid_path.get(cache_key)
        if cached and cached in paths:
            paths.remove(cached)
            paths.insert(0, cached)
        elif cached:
            self._working_hid_path.pop(cache_key, None)
        return paths

    def get_keyboard_path_safe(self):
        """Like get_keyboard_path but returns None if device isn't configured."""
        if not self.config.get("device"):
            return None
        return self.get_keyboard_path()

    def _cache_working_path(self, path):
        """Called by BatteryMonitor when it finds a working HID path."""
        dev = self.config.get("device") or {}
        cache_key = self._device_key(dev.get("vid", 0), dev.get("pid", 0), dev.get("usage_page", 0))
        self._working_hid_path[cache_key] = path
        logger.debug("cached working HID path from battery: %s", path)

    def _diagnose_hid_endpoints(self):
        dev = self.config.get("device") or {}
        if not dev:
            return
        vid, pid = dev.get("vid", 0), dev.get("pid", 0)
        vendor_paths = []
        for d in hid.enumerate(vid, pid):
            if d["usage_page"] == 0xFFFF:
                vendor_paths.append({
                    "path": d["path"],
                    "usage": d["usage"],
                    "interface": d.get("interface_number", -1),
                })
        safe_query_data = [0xF7] + [0x00] * 63
        test_sizes = [8, 16, 32, 33, 64]
        logger.debug("=== HID endpoint diagnostic ===")
        for ep in vendor_paths:
            path = ep["path"]
            logger.debug("--- probing path=%s usage=0x%04x interface=%d ---",
                         path, ep["usage"], ep["interface"])
            for data_size in test_sizes:
                report = [0x00] + safe_query_data[:data_size]
                try:
                    device = hid.device()
                    device.open_path(path)
                    device.set_nonblocking(1)
                    rc = device.send_feature_report(report)
                    response = None
                    if rc is not None and rc > 0:
                        try:
                            response = device.get_feature_report(0, len(report))
                        except Exception:
                            pass
                    device.close()
                    logger.debug("  size=%d(+1) rc=%s response=%s",
                                 data_size, rc,
                                 [f"0x{b:02x}" for b in response[:16]] if response else None)
                except Exception as exc:
                    logger.debug("  size=%d(+1) EXCEPTION: %s", data_size, exc)
                    try:
                        device.close()
                    except Exception:
                        pass
        logger.debug("=== end HID endpoint diagnostic ===")

    def battery_poll_loop(self):
        logger.info("battery poll loop started (60s interval)")
        print("[Battery] Поток опроса батареи запущен (каждые 60 сек).")
        while self.app_alive:
            try:
                entry = self._active_device()
                if entry and entry.get("keyboard_type") is None:
                    logger.debug("battery poll skipped: keyboard_type not configured")
                else:
                    self.battery_monitor.read_once()
                    state = self.battery_monitor.state
                    logger.debug("battery poll: percent=%s charging=%s stale=%s",
                                 state.percent, state.charging, state.is_stale)
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

    def _refresh_battery_for_tray(self):
        """Read battery and update tray icon immediately (non-blocking thread)."""
        def _do():
            try:
                entry = self._active_device()
                if entry and entry.get("keyboard_type") is None:
                    return
                self.battery_monitor.read_once()
                state = self.battery_monitor.state
                if self.tray:
                    self.tray.update_battery(state)
                self.publish_battery_to_ui(state)
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True).start()

    def _manual_battery_refresh(self):
        entry = self._active_device()
        if entry and entry.get("keyboard_type") is None:
            return
        self.battery_monitor.read_once()
        state = self.battery_monitor.state
        if self.tray:
            self.tray.update_battery(state)
        self.publish_battery_to_ui(state)

    def _update_transport_icon(self):
        """Sync header transport icon with active device's transport field."""
        icon = getattr(self, "transport_icon", None)
        if icon is None:
            return
        entry = self._active_device()
        transport = entry.get("transport") if entry else None
        if transport == "wired":
            icon.icon = ft.Icons.USB
            icon.color = ft.Colors.BLUE_400
            icon.tooltip = "Проводное подключение"
            icon.visible = True
        elif transport == "wireless":
            icon.icon = ft.Icons.WIFI_TETHERING_ROUNDED
            icon.color = ft.Colors.GREEN_400
            icon.tooltip = "Беспроводное подключение"
            icon.visible = True
        else:
            icon.visible = False
        try:
            icon.update()
        except Exception:
            pass

    def _on_transport_override_change(self, e):
        return

    def _show_setup_wizard(self, device_key: str):
        if not device_key:
            return
        entry = self.config["devices"].get(device_key)
        if not entry:
            return
        if entry.get("keyboard_type") in KEYBOARD_TYPES:
            return
        logger.info("Setup wizard opened for device %s (%s)", device_key, entry.get("label", ""))

        label = entry.get("label") or "Unknown Device"
        vid = entry.get("vid", 0)
        pid = entry.get("pid", 0)

        type_dropdown = ft.Dropdown(
            label="Тип клавиатуры",
            hint_text="Выберите тип...",
            options=[
                ft.dropdown.Option(key="magnetic", text="Магнитная"),
                ft.dropdown.Option(key="mechanical", text="Механическая"),
            ],
            border_radius=12,
            filled=True,
            width=350,
        )

        confirm_checkbox = ft.Checkbox(
            label="Я подтверждаю, что тип клавиатуры выбран правильно",
            value=False,
        )

        save_btn = ft.ElevatedButton("Сохранить", disabled=True)

        def _update_save_state(_=None):
            save_btn.disabled = not (type_dropdown.value and confirm_checkbox.value)
            try:
                self.page.update()
            except Exception:
                pass

        confirm_checkbox.on_change = lambda e: _update_save_state()

        def _on_save(_):
            kb_type = type_dropdown.value
            if kb_type not in KEYBOARD_TYPES:
                return
            logger.info("Setup completed: device %s configured as %s", device_key, kb_type)
            try:
                self.page.pop_dialog()
            except Exception:
                pass
            self._set_keyboard_type(vid, pid, entry.get("usage_page", 0), kb_type)

        def _on_cancel(_):
            logger.info("Setup wizard cancelled for device %s", device_key)
            try:
                self.page.pop_dialog()
            except Exception:
                pass
            if self.config.get("active_device") == device_key:
                prev_keys = [k for k in self.config["devices"]
                             if k != device_key and self.config["devices"][k].get("keyboard_type") in KEYBOARD_TYPES]
                new_active = prev_keys[0] if prev_keys else None
                if new_active:
                    self._activate_device(new_active)
                    self.device_dropdown.value = new_active
                else:
                    self.config["active_device"] = None
                    self._ensure_active_device_aliases()
                    self.device_dropdown.value = None
                try:
                    self.page.update()
                except Exception:
                    pass

        save_btn.on_click = _on_save

        mech_section = ft.Column([
            ft.Divider(height=1, color=ft.Colors.AMBER_200),
            ft.Row([
                ft.Icon(ft.Icons.SPEED_ROUNDED, color=ft.Colors.RED_700, size=20),
                ft.Text("ЗАДЕРЖКА ПЕРЕКЛЮЧЕНИЯ", weight=ft.FontWeight.BOLD, color=ft.Colors.RED_700, size=13),
            ], spacing=8),
            ft.Text(
                "На механических клавиатурах смена профиля происходит с задержкой. "
                "Не нажимайте клавиши, пока подсветка не выключится и не включится "
                "снова — это сигнал, что профиль применён. Держите подсветку включённой.",
                size=12,
                color=ft.Colors.RED_900,
            ),
        ], spacing=6, visible=False)

        warning_col = ft.Column([
            ft.Row([
                ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color=ft.Colors.AMBER_700, size=24),
                ft.Text("ВНИМАНИЕ", weight=ft.FontWeight.BOLD, color=ft.Colors.AMBER_700, size=16),
            ], spacing=8),
            ft.Text(
                "Неправильный выбор типа клавиатуры приведёт к отправке "
                "несовместимых HID-команд на устройство. Это может вызвать "
                "нестабильную работу, залипание клавиш или повреждение "
                "конфигурации профилей.",
                size=13,
                color=ft.Colors.AMBER_900,
            ),
            mech_section,
        ], spacing=6)

        warning_block = ft.Container(
            content=warning_col,
            bgcolor=ft.Colors.AMBER_50,
            border=ft.Border(
                ft.BorderSide(1, ft.Colors.AMBER_200),
                ft.BorderSide(1, ft.Colors.AMBER_200),
                ft.BorderSide(1, ft.Colors.AMBER_200),
                ft.BorderSide(1, ft.Colors.AMBER_200),
            ),
            border_radius=12,
            padding=ft.Padding(left=16, top=12, right=16, bottom=12),
        )

        def _on_type_change(_=None):
            mech_section.visible = (type_dropdown.value == "mechanical")
            mech_section.update()
            _update_save_state()

        type_dropdown.on_change = _on_type_change

        content = ft.Column([
            ft.Text(f"Устройство: {label}", size=14, weight=ft.FontWeight.W_500),
            ft.Text(f"VID: 0x{vid:04X}   PID: 0x{pid:04X}", size=12,
                    color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Divider(height=16, color=ft.Colors.TRANSPARENT),
            type_dropdown,
            ft.Divider(height=8, color=ft.Colors.TRANSPARENT),
            warning_block,
            ft.Divider(height=8, color=ft.Colors.TRANSPARENT),
            confirm_checkbox,
        ], spacing=4, tight=True, width=400)

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("⌨ Настройка клавиатуры", size=20, weight=ft.FontWeight.BOLD),
            content=content,
            actions=[
                ft.TextButton("Отмена", on_click=_on_cancel),
                save_btn,
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            shape=ft.RoundedRectangleBorder(radius=20),
        )

        self.page.show_dialog(dlg)

    def _set_keyboard_type(self, vid, pid, usage_page, kb_type):
        if kb_type not in ("magnetic", "mechanical"):
            return
        key = self._device_key(vid, pid, usage_page)
        entry = self.config["devices"].get(key)
        if entry is None:
            hid_dev = next(
                (d for d in getattr(self, "filtered_devices", [])
                 if d['vendor_id'] == vid and d['product_id'] == pid and d['usage_page'] == usage_page),
                None,
            )
            if hid_dev is None:
                return
            entry = self._empty_device_entry(vid, pid, usage_page, label=self._device_label_for(hid_dev))
            self.config["devices"][key] = entry
            self._normalize_device_entry(entry)
        entry["keyboard_type"] = kb_type
        transport = entry.get("transport")
        if kb_type == "mechanical":
            default_cooldown = 2000 if transport == "wireless" else 1000
        else:
            default_cooldown = 250 if transport == "wireless" else 100
        entry["cooldown_ms"] = default_cooldown
        self._normalize_device_entry(entry)
        self.save_config()
        if key == self.config.get("active_device"):
            self._ensure_active_device_aliases()
            was_running = self.is_running
            if was_running:
                self.is_running = False
                try:
                    keyboard.unhook_all()
                except Exception:
                    pass
            try:
                self.update_payloads_list()
            except Exception:
                pass
            try:
                self.update_bindings_list()
            except Exception:
                pass
            if was_running:
                self.is_running = True
                self.reload_runtime_state()
                self._set_status(True)
                if not self.worker_thread or not self.worker_thread.is_alive():
                    self.worker_thread = threading.Thread(target=self.background_task, daemon=True)
                    self.worker_thread.start()
        self.refresh_devices()
        try:
            self._update_transport_icon()
        except Exception:
            pass

    def _refresh_battery_for_sniff_chip(self):
        self._manual_battery_refresh()
        state = self.battery_monitor.state
        chip = getattr(self, "_sniff_battery_chip", None)
        if chip is None:
            return
        if state.is_stale or state.percent is None:
            txt = "—"
        else:
            suffix = " ⚡" if state.charging else ""
            txt = f"{state.percent}%{suffix}"
        def upd():
            try:
                chip.value = txt
                self.page.update()
            except Exception:
                pass
        try:
            self.page.run_thread(upd)
        except Exception:
            upd()

    def _send_hid_payload(self, payload_data, label="payload"):
        """Send a single HID feature report to all matching paths. Returns first successful path or None.
        Caller must hold self.usb_lock."""
        full_report = [0x00] + payload_data
        logger.debug("_send_hid_payload [%s] report[:%d]=%s",
                      label, min(len(full_report), 16),
                      [f"0x{b:02x}" for b in full_report[:16]])
        paths = self.get_keyboard_paths()
        if not paths:
            logger.warning("_send_hid_payload [%s]: no HID paths found", label)
            return None
        dev = self.config.get("device") or {}
        cache_key = self._device_key(dev.get("vid", 0), dev.get("pid", 0), dev.get("usage_page", 0))
        sent_path = None
        last_err = None
        for path in paths:
            try:
                device = hid.device()
                device.open_path(path)
                device.set_nonblocking(1)
                rc = device.send_feature_report(full_report)
                logger.debug("_send_hid_payload [%s] path=%s rc=%s", label, path, rc)
                if rc is not None and rc > 0:
                    try:
                        read_back = device.get_feature_report(0, min(len(full_report), 65))
                        logger.debug("_send_hid_payload [%s] read_back=%s", label,
                                     [f"0x{b:02x}" for b in read_back[:16]] if read_back else None)
                    except Exception as rb_err:
                        logger.debug("_send_hid_payload [%s] read_back failed: %s", label, rb_err)
                device.close()
            except Exception as e:
                last_err = e
                try:
                    device.close()
                except Exception:
                    pass
                continue
            if rc is None or rc > 0:
                sent_path = sent_path or path
                if rc is not None and rc > 0:
                    break
        if sent_path:
            self._working_hid_path[cache_key] = sent_path
        else:
            logger.warning("_send_hid_payload [%s] FAILED on all paths, last_err=%s", label, last_err)
        return sent_path

    def apply_payload(self, profile_name, payload_data, manual=False):
        entry = self._active_device()
        if entry and entry.get("keyboard_type") is None:
            logger.warning("HID write blocked: device %s has no keyboard_type configured",
                           self.config.get("active_device"))
            self._ui_call(lambda: self._show_setup_wizard(self.config.get("active_device")))
            return
        logger.debug("apply_payload profile=%s manual=%s payload[:%d]=%s",
                      profile_name, manual, min(len(payload_data), 16),
                      [f"0x{b:02x}" for b in payload_data[:16]])

        kb_type = entry.get("keyboard_type") if entry else None
        caps = device_capabilities(kb_type)
        logger.debug("apply_payload: keyboard_type=%s caps=%s", kb_type, caps)

        cooldown_ms = _resolved_cooldown_ms(entry)
        hook = None
        if cooldown_ms > 0:
            _release_all_keys()
            hook = _suppress_keyboard_start()
            logger.debug("apply_payload: keyboard suppressed (transaction-bound, device %s)",
                         self.config.get("active_device"))

        sent_path = None
        try:
            with self.usb_lock:
                # 1. Profile switch first — keymap is the primary operation
                sent_path = self._send_hid_payload(payload_data, label=f"profile_{profile_name}")
                if sent_path is None:
                    logger.error("profile switch FAILED for %s", profile_name)
                    print("[Ошибка USB] Не удалось отправить HID пакет ни в один интерфейс.")
                    return

                time.sleep(_stage_delay_ms(entry, "profile") / 1000.0)

                info = self._profile_info_at_by_name(profile_name)

                # 2. Polling rate (capability-gated)
                if DeviceCapability.POLLING_RATE in caps and info:
                    pr = info.get("polling_rate")
                    if pr and pr in VALID_POLLING_RATES:
                        logger.debug("apply_payload: sending polling rate %d Hz", pr)
                        pr_path = self._send_hid_payload(
                            _polling_rate_payload(PollingRate(pr)),
                            label=f"polling_rate_{pr}Hz")
                        if pr_path:
                            time.sleep(_stage_delay_ms(entry, "polling") / 1000.0)
                        else:
                            logger.warning("apply_payload: polling rate send FAILED")

                # 3. Lighting profile after profile stabilizes (capability-gated)
                if DeviceCapability.LIGHTING_PROFILES in caps and info:
                    lp = info.get("lighting_profile")
                    if lp is not None and lp in VALID_LIGHTING_PROFILES:
                        logger.debug("apply_payload: sending lighting profile %d after profile switch", lp + 1)
                        lp_path = self._send_hid_payload(
                            _lighting_profile_payload(lp),
                            label=f"lighting_profile_{lp + 1}")
                        if lp_path:
                            time.sleep(_stage_delay_ms(entry, "lighting") / 1000.0)
                        else:
                            logger.warning("apply_payload: lighting profile send FAILED")
                elif DeviceCapability.LIGHTING_PROFILES not in caps:
                    logger.debug("apply_payload: lighting subsystem DISABLED for %s", kb_type)

                if entry.get("battery", {}).get("query"):
                    self._refresh_battery_for_tray()
        finally:
            if hook is not None:
                try:
                    keyboard.unhook(hook)
                except Exception:
                    pass
                logger.debug("apply_payload: keyboard suppression removed after transaction")

        if sent_path is None:
            return

        if manual:
            try:
                hwnd = win32gui.GetForegroundWindow()
                _, pid_ = win32process.GetWindowThreadProcessId(hwnd)
                self.last_active_window = psutil.Process(pid_).name().lower()
            except Exception:
                pass

        self.current_binding = profile_name
        trigger = "Hotkey" if manual else "Авто"
        print(f"[{trigger}] Успешно применен профиль: {profile_name} (path={sent_path!r})")

        try:
            if self.config.get("settings", {}).get("notifications", True):
                Notification(
                    app_id='QMK.Top Manager',
                    title=f'Профиль: {profile_name.upper()}',
                    msg=f'Применен профиль ({trigger})',
                    duration='short',
                ).show()
        except Exception as e:
            print(f"[Уведомление] Ошибка: {e}")

    def background_task(self):
        logger.info("background window scanner started")
        print("[DEBUG] Фоновый сканер окон запущен...")
        while self.is_running:
            mode = self._current_mode()
            if mode == "manual":
                time.sleep(1)
                continue
            try:
                hwnd = win32gui.GetForegroundWindow()
                if hwnd:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    try:
                        active_process = psutil.Process(pid).name().lower()
                    except psutil.AccessDenied:
                        print(f"[ОШИБКА ДОСТУПА] Процесс защищен (PID {pid}). Нужны права Администратора!")
                        time.sleep(2)
                        continue
                    except Exception:
                        continue

                    if active_process != self.last_active_window:
                        print(f"[DEBUG] Активное окно: '{active_process}'")
                        self.last_active_window = active_process

                        target_pi = self.rule_evaluator.match(active_process)
                        if target_pi is not None:
                            logger.debug("rule matched: process=%s → profile_index=%d", active_process, target_pi)
                        else:
                            disabled = self.rule_evaluator.is_disabled_match(active_process)
                            if disabled:
                                logger.debug("rule SKIPPED (disabled): process=%s → profile_index=%d",
                                             active_process, disabled.profile_index)
                            if self.default_profile_index is not None:
                                target_pi = self.default_profile_index
                        if target_pi is not None:
                            entry = self._active_device()
                            if entry and entry.get("keyboard_type") is None:
                                continue
                            name = self._profile_name_at(target_pi)
                            payload = self._profile_payload_at(target_pi)
                            if name and name != self.current_binding:
                                time.sleep(0.2)
                                try:
                                    hwnd2 = win32gui.GetForegroundWindow()
                                    _, pid2 = win32process.GetWindowThreadProcessId(hwnd2)
                                    recheck = psutil.Process(pid2).name().lower()
                                except Exception:
                                    recheck = active_process
                                if recheck == active_process:
                                    self.apply_payload(name, payload, manual=False)
                                else:
                                    logger.debug("debounce: process changed during wait (%s→%s), skipping",
                                                 active_process, recheck)
                                    self.last_active_window = None
            except Exception as e:
                print(f"[GLOBAL ERROR] Сбой в цикле сканирования: {e}")
            time.sleep(1)

    # ---------- Tray callbacks (run on pystray thread) ----------
    def _ui_call(self, fn):
        """Marshal a UI mutation onto Flet's event loop."""
        try:
            self.page.run_thread(fn)
        except Exception:
            try:
                fn()
            except Exception:
                pass

    def _show_window(self):
        try:
            self.page.window.skip_task_bar = False
            self.page.window.visible = True
            try:
                self.page.window.minimized = False
            except Exception:
                pass
            self.page.update()
            try:
                self.page.run_task(self.page.window.to_front)
            except Exception:
                pass
        except Exception as exc:
            print(f"[Window] show failed: {exc}")
        if self.tray:
            self.tray.set_window_visible(True)

    def _hide_window(self):
        try:
            self.page.window.visible = False
            self.page.window.skip_task_bar = True
            self.page.update()
        except Exception as exc:
            print(f"[Window] hide failed: {exc}")
        if self.tray:
            self.tray.set_window_visible(False)

    def _tray_show_window(self):
        self._ui_call(self._show_window)

    def _tray_hide_window(self):
        self._ui_call(self._hide_window)

    def _tray_toggle_window(self):
        def do():
            visible = bool(getattr(self.page.window, "visible", True))
            if visible:
                self._hide_window()
            else:
                self._show_window()
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

        def _shutdown_window_and_exit():
            try:
                self.page.window.prevent_close = False
            except Exception:
                pass
            try:
                self.page.run_task(self.page.window.destroy)
            except Exception:
                try:
                    self.page.run_task(self.page.window.close)
                except Exception:
                    pass
            try:
                self.page.update()
            except Exception:
                pass
            threading.Thread(target=lambda: (time.sleep(0.25), os._exit(0)), daemon=True).start()

        self._ui_call(_shutdown_window_and_exit)

    def _handle_window_event(self, e):
        evt_type = getattr(e, "type", None)
        evt_value = getattr(evt_type, "value", evt_type)
        evt_str = str(evt_value) if evt_value is not None else getattr(e, "data", "")
        if evt_str in ("close", "WindowEventType.CLOSE") or evt_type == getattr(ft, "WindowEventType", type("x", (), {})).CLOSE:
            self._hide_window()
            if not self._first_minimize_notified:
                self._first_minimize_notified = True
                try:
                    if self.config.get("settings", {}).get("notifications", True):
                        Notification(
                            app_id='QMK.Top Manager',
                            title='QMK.Top Manager',
                            msg='Программа продолжает работать в трее. Выйти можно из меню иконки.',
                            duration='short',
                        ).show()
                except Exception:
                    pass

    # ---------- Utilities ----------
    def _snack(self, text):
        self.page.show_dialog(ft.SnackBar(ft.Text(text), duration=2500))


def main(page: ft.Page):
    QMKManager(page)


def _should_start_minimized() -> bool:
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return bool(data.get("settings", {}).get("start_minimized", False))
    except Exception:
        pass
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--startup", action="store_true",
                        help="Launched by Windows autostart")
    args = parser.parse_args()

    if not acquire_single_instance():
        bring_existing_to_front()
        sys.exit(0)

    os.chdir(paths.app_dir)

    is_startup = args.startup

    if is_startup:
        config_data = {}
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config_data = json.load(f)
        except Exception:
            pass
        delay = config_data.get("settings", {}).get("startup_delay_sec", 5)
        if delay > 0:
            time.sleep(delay)

    if is_startup or _should_start_minimized():
        ft.run(main, view=ft.AppView.FLET_APP_HIDDEN)
    else:
        ft.run(main)
