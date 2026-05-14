import flet as ft
import hid
import json
import os
import sys
import threading
import time
import ctypes
import win32gui
import win32process
import psutil
import keyboard
from winotify import Notification
from battery import BatteryMonitor, BatteryState
from tray import TrayIcon, set_icon_source
from sniffer import HIDSniffer, _find_chrome, is_chromium_executable

CONFIG_FILE = "profiles_config.json"
PROFILE_COUNT = 4


def _default_profile_payload(idx: int) -> list:
    """qmk.top static profile-switch frame for slot `idx` (0..3).
    Frame: [0x04, idx, 0,0,0,0,0, 0xFB - idx, 0×56] (64 bytes)."""
    payload = [0] * 64
    payload[0] = 0x04
    payload[1] = idx & 0xFF
    payload[7] = (0xFB - idx) & 0xFF
    return payload

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
        self._ensure_active_device_aliases()
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
        )
        self.battery_thread = None
        self.current_binding = None
        self.last_active_window = None
        self.binds_dict = {}
        _entry = self._active_device()
        _dpi = _entry.get("default_profile_index") if _entry else None
        self.default_profile_index = _dpi if isinstance(_dpi, int) and 0 <= _dpi < PROFILE_COUNT else None
        self.devices = []
        self.filtered_devices = []
        self.sniffer = None
        self.sniff_events = []
        self._battery_captured_this_session = False
        self._battery_capture_attempts = 0
        self._battery_locked = False
        self._captured_profile_indices = set()
        self.detected_browser_path = _find_chrome()

        self._build_page()
        self._build_ui()
        self.refresh_devices()
        self.update_payloads_list()
        self.update_bindings_list()

        self.tray.start()

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
        """Auto-detect 'wired' vs 'wireless' from HID device strings.
        Wireless markers: '2.4g', '2.4 g', 'wireless', 'dongle', 'rf receiver'.
        Default: 'wired'."""
        markers = ("2.4g", "2.4 g", "wireless", "dongle", "rf receiver")
        haystack = " ".join(
            (hid_dev.get(k) or "")
            for k in ("product_string", "manufacturer_string")
        ).lower()
        return "wireless" if any(m in haystack for m in markers) else "wired"

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
            "payloads": {
                f"Профиль {i + 1}": {"data": _default_profile_payload(i), "hotkey": ""}
                for i in range(PROFILE_COUNT)
            },
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
        payloads = entry.get("payloads") or {}
        if not isinstance(payloads, dict):
            payloads = {}
        items = list(payloads.items())[:PROFILE_COUNT]
        while len(items) < PROFILE_COUNT:
            items.append((f"Профиль {len(items) + 1}", {"data": [], "hotkey": ""}))
        new_payloads = {}
        for slot_idx, (name, info) in enumerate(items):
            if not isinstance(info, dict):
                info = {"data": info if isinstance(info, list) else [], "hotkey": ""}
            info.setdefault("data", [])
            info.setdefault("hotkey", "")
            if not info["data"]:
                info["data"] = _default_profile_payload(slot_idx)
            new_payloads[name] = info
        entry["payloads"] = new_payloads

        name_to_idx = {n: i for i, n in enumerate(new_payloads.keys())}
        new_bindings = []
        for b in entry.get("bindings", []) or []:
            if not isinstance(b, dict) or "process" not in b:
                continue
            if "profile_index" in b and isinstance(b["profile_index"], int) and 0 <= b["profile_index"] < PROFILE_COUNT:
                new_bindings.append({"process": b["process"], "profile_index": b["profile_index"]})
                continue
            old_name = b.get("profile_name")
            if old_name in name_to_idx:
                new_bindings.append({"process": b["process"], "profile_index": name_to_idx[old_name]})
        entry["bindings"] = new_bindings

        # Migrate legacy "default" pseudo-binding into a dedicated field.
        dpi = entry.get("default_profile_index")
        if not isinstance(dpi, int) or not (0 <= dpi < PROFILE_COUNT):
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
        default_config = {
            "mode": "auto",
            "settings": {
                "start_minimized": False,
                "autostart_service": True,
                "browser_path": "",
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
        return True

    def _ensure_device_entry(self, hid_dev):
        """Create an empty config entry for an HID device if missing. Returns key.
        Also lazily fills the `transport` field from device metadata."""
        key = self._device_key_of(hid_dev)
        if key not in self.config["devices"]:
            self.config["devices"][key] = self._empty_device_entry(
                hid_dev["vendor_id"], hid_dev["product_id"], hid_dev["usage_page"],
                label=self._device_label_for(hid_dev),
            )
        entry = self.config["devices"][key]
        if entry.get("transport") is None:
            entry["transport"] = self._detect_transport(hid_dev)
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

    def reload_runtime_state(self):
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        registered = 0
        for prof_name, info in self._profile_items():
            hk = info.get("hotkey")
            data = info.get("data") or []
            if not hk or not data:
                continue
            try:
                keyboard.add_hotkey(
                    hk,
                    lambda name=prof_name, data=data:
                        self.apply_payload(name, data, manual=True)
                )
                registered += 1
            except Exception as e:
                print(f"[Хоткей] Ошибка регистрации {hk}: {e}")
        self.binds_dict = {b["process"]: b["profile_index"] for b in self.config["bindings"] if "profile_index" in b}
        entry = self._active_device()
        dpi = entry.get("default_profile_index") if entry else None
        self.default_profile_index = dpi if isinstance(dpi, int) and 0 <= dpi < PROFILE_COUNT else None
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
                    ft.Row([self.battery_chip, self.status_badge], spacing=8),
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

        device_card = self._card(
            icon=ft.Icons.USB_ROUNDED,
            title="Устройство",
            subtitle="Выберите QMK-клавиатуру из списка HID-устройств.",
            content=ft.Container(
                ft.Column(
                    [
                        ft.Row([self.device_dropdown, refresh_btn], spacing=8),
                        ft.Row([sniffer_open_btn], alignment=ft.MainAxisAlignment.END),
                    ],
                    spacing=10,
                ),
                margin=ft.Margin.only(top=12),
            ),
        )

        self.payloads_column = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO)
        profiles_card = self._card(
            icon=ft.Icons.LAYERS_ROUNDED,
            title="Профили",
            subtitle="Всегда 4 фиксированных профиля. Имя и хоткей — редактируемые, payload подгружается снифером.",
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
                ],
                spacing=12,
            ),
        )

        self.sniff_log = ft.ListView(
            spacing=2, padding=8, auto_scroll=True,
        )
        self.sniff_status = ft.Text("Сниффер остановлен.", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        self.sniff_button = ft.FilledTonalButton(
            "Запустить sniff",
            icon=ft.Icons.SENSORS_ROUNDED,
            on_click=lambda e: self.toggle_sniffer(),
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
    def refresh_devices(self):
        self.devices = hid.enumerate()
        custom_devices = [d for d in self.devices if d['usage_page'] >= 0xFF00]
        seen = set()
        deduped = []
        for d in custom_devices:
            key = (d['vendor_id'], d['product_id'], d['usage_page'], d.get('usage', 0))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(d)
        custom_devices = deduped
        self.filtered_devices = custom_devices

        options = []
        for i, d in enumerate(custom_devices):
            key = self._device_key_of(d)
            saved = self.config["devices"].get(key)
            label_prefix = (saved.get("label") or self._device_label_for(d)) if saved else self._device_label_for(d)
            label = (
                f"{label_prefix} · VID {hex(d['vendor_id'])} · PID {hex(d['product_id'])} · Page {hex(d['usage_page'])}"
            )
            options.append(ft.dropdown.Option(key=key, text=label))
        self.device_dropdown.options = options

        # Make sure every present device has a config entry (with transport
        # auto-detected) BEFORE deciding which one wins.
        for d in self.filtered_devices:
            self._ensure_device_entry(d)
        present_keys = [self._device_key_of(d) for d in self.filtered_devices]
        active_key = self.config.get("active_device")
        target_key = self._pick_active_target(active_key, present_keys, self.config["devices"])
        self.device_dropdown.value = target_key
        if target_key and target_key != active_key:
            self._activate_device(target_key)
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
        try:
            ww = int(self.page.window.width or 1100)
            wh = int(self.page.window.height or 740)
        except Exception:
            ww, wh = 1100, 740
        content_w = max(720, ww - 80)
        content_h = max(480, wh - 200)
        log_h = max(280, content_h - 180)

        def on_close(e):
            try:
                self.page.pop_dialog()
            except Exception:
                pass

        body = ft.Column(
            [
                ft.Row(
                    [self.sniff_button, self.sniff_clear_button, self.sniff_copy_button],
                    spacing=8, wrap=True,
                ),
                ft.Row(
                    [self.browser_pick_button, self.browser_path_text],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                self.sniff_status,
                ft.Container(
                    content=self.sniff_log,
                    bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                    border_radius=12,
                    padding=4,
                    height=log_h,
                ),
            ],
            spacing=10,
            tight=True,
        )

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("HID Sniffer — qmk.top"),
            content=ft.Container(
                content=body,
                width=content_w,
            ),
            actions=[
                ft.TextButton("Закрыть", on_click=on_close),
            ],
            shape=ft.RoundedRectangleBorder(radius=20),
        )
        self.page.show_dialog(dlg)
    def open_profile_dialog(self, index):
        info = self._profile_info_at(index) or {"data": [], "hotkey": ""}
        current_name = self._profile_name_at(index) or f"Профиль {index + 1}"

        name_field = ft.TextField(
            label="Название профиля",
            hint_text="Например: Gaming, Typing",
            value=current_name,
            border_radius=12,
            filled=True,
        )
        hotkey_field = ft.TextField(
            label="Горячая клавиша",
            hint_text="Например: ctrl+shift+1",
            value=info.get("hotkey", ""),
            border_radius=12,
            filled=True,
        )

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"Профиль {index + 1}"),
            content=ft.Container(
                content=ft.Column(
                    [name_field, hotkey_field],
                    spacing=12,
                    tight=True,
                ),
                width=440,
            ),
            shape=ft.RoundedRectangleBorder(radius=24),
        )

        def on_cancel(e):
            self.page.pop_dialog()

        def on_save(e):
            new_name = name_field.value.strip()
            hk_val = (hotkey_field.value or "").strip().lower()
            if not new_name:
                self._snack("Имя профиля обязательно")
                return
            if not self._rename_profile_at(index, new_name):
                self._snack("Имя занято другим профилем")
                return
            self.config["payloads"][new_name]["hotkey"] = hk_val
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
        for index in range(PROFILE_COUNT):
            name, info = items[index] if index < len(items) else (f"Профиль {index + 1}", {"data": [], "hotkey": ""})
            hk = info.get("hotkey") or ""
            data = info.get("data") or []
            if data:
                preview = ", ".join(hex(b) for b in data[:4]) + ("…" if len(data) > 4 else "")
            else:
                preview = "payload не загружен — запусти sniff и нажми «волшебную палочку»"

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
                                        ft.Text(preview, size=11,
                                                color=ft.Colors.ON_SURFACE_VARIANT,
                                                font_family="Consolas"),
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
                row = ft.Container(
                    content=ft.Row(
                        [
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

    def _on_sniff_status(self, msg: str):
        def upd():
            self.sniff_status.value = msg
            self.page.update()
        try:
            self.page.run_thread(upd)
        except Exception:
            upd()

    def _on_sniff_event(self, ev: dict):
        self.sniff_events.append(ev)
        data = ev.get("data") or []
        if not data:
            return
        direction = (ev.get("dir") or "").upper()
        if direction != "TX":
            return

        is_profile = self._matches_profile_pattern(data)
        # Battery candidate must be a TX *feature* report (sendFeatureReport),
        # not an output report — and not the profile opcode.
        ev_type = (ev.get("type") or "").lower()
        is_battery = (
            ev_type == "feature"
            and not is_profile
            and self._matches_battery_pattern(data, ev_type)
        )

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

        hex_str = ", ".join(f"0x{b:02x}" for b in data[:64])
        if len(data) > 64:
            hex_str += f", … (+{len(data) - 64})"
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
            ft.Text(hex_str, size=11, selectable=True, font_family="Consolas", expand=True),
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

        line = ft.Row(
            controls,
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        def upd():
            self.sniff_log.controls.append(line)
            if len(self.sniff_log.controls) > 500:
                self.sniff_log.controls = self.sniff_log.controls[-500:]
            self.page.update()
        try:
            self.page.run_thread(upd)
        except Exception:
            upd()

    @staticmethod
    def _matches_profile_pattern(data: list) -> bool:
        # qmk.top profile-switch frame (verified static):
        # [0x04, idx, 0,0,0,0,0, 0xFB-idx, 0×56] (64 bytes)
        # idx 0 → 0xFB, 1 → 0xFA, 2 → 0xF9, 3 → 0xF8.
        if len(data) < 8:
            return False
        if data[0] != 0x04:
            return False
        if data[1] not in (0, 1, 2, 3):
            return False
        if any(b != 0 for b in data[2:7]):
            return False
        if data[7] != (0xFB - data[1]) & 0xFF:
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

    def _capture_profile_payload(self, index: int, sample_data: list):
        """Save the actually-observed payload for the specific slot only.
        Preserves other slots' existing payloads — no synthesizing payload[1]=idx,
        which produced bytes the keyboard never sent and silently broke switching."""
        if not (0 <= index < PROFILE_COUNT):
            return
        items = self._profile_items()
        while len(items) < PROFILE_COUNT:
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
        items = self._profile_items()
        while len(items) < PROFILE_COUNT:
            items.append((f"Профиль {len(items) + 1}", {"data": [], "hotkey": ""}))
        new_payloads = {}
        for idx in range(PROFILE_COUNT):
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
        vid = self.config["device"]["vid"]
        pid = self.config["device"]["pid"]
        usage_page = self.config["device"]["usage_page"]
        for d in hid.enumerate(vid, pid):
            if d['usage_page'] == usage_page:
                return d['path']
        return None

    def get_keyboard_path_safe(self):
        """Like get_keyboard_path but returns None if device isn't configured."""
        if not self.config.get("device"):
            return None
        return self.get_keyboard_path()

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

    def apply_payload(self, profile_name, payload_data, manual=False):
        with self.usb_lock:
            path = self.get_keyboard_path()
            if not path:
                print("[Ошибка] Устройство USB не найдено для отправки.")
                return
            try:
                device = hid.device()
                device.open_path(path)
                device.set_nonblocking(1)
                device.send_feature_report([0x00] + payload_data)
                device.close()

                if manual:
                    try:
                        hwnd = win32gui.GetForegroundWindow()
                        _, pid = win32process.GetWindowThreadProcessId(hwnd)
                        self.last_active_window = psutil.Process(pid).name().lower()
                    except Exception:
                        pass

                self.current_binding = profile_name
                trigger = "Hotkey" if manual else "Авто"
                print(f"[{trigger}] Успешно применен профиль: {profile_name}")

                try:
                    Notification(
                        app_id='QMK.Top Manager',
                        title=f'Профиль: {profile_name.upper()}',
                        msg=f'Применен профиль ({trigger})',
                        duration='short',
                    ).show()
                except Exception as e:
                    print(f"[Уведомление] Ошибка: {e}")
            except Exception as e:
                print(f"[Ошибка USB] Не удалось отправить HID пакет: {e}")

    def background_task(self):
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

                        target_pi = None
                        if active_process in self.binds_dict:
                            target_pi = self.binds_dict[active_process]
                        elif self.default_profile_index is not None:
                            target_pi = self.default_profile_index
                        if target_pi is not None:
                            info = self._profile_info_at(target_pi)
                            name = self._profile_name_at(target_pi)
                            if info and info.get("data") and name and name != self.current_binding:
                                self.apply_payload(name, info["data"], manual=False)
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
    if _should_start_minimized():
        ft.run(main, view=ft.AppView.FLET_APP_HIDDEN)
    else:
        ft.run(main)
