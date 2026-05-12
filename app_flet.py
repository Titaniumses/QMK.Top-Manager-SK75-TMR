import flet as ft
import hid
import json
import os
import threading
import time
import win32gui
import win32process
import psutil
import keyboard
from winotify import Notification
from battery import BatteryMonitor, BatteryState
from tray import TrayIcon

CONFIG_FILE = "profiles_config.json"


class QMKManager:
    def __init__(self, page: ft.Page):
        self.page = page
        self.config = self.load_config()
        self.is_running = False
        self.worker_thread = None
        self.usb_lock = threading.Lock()
        self.app_alive = True
        self._first_minimize_notified = False
        self.tray = TrayIcon(
            on_toggle_window=self._tray_toggle_window,
            on_show=self._tray_show_window,
            on_hide=self._tray_hide_window,
            on_quit=self._tray_quit,
        )
        self.tray.start()
        self.battery_monitor = BatteryMonitor(
            config_battery=self.config["battery"],
            usb_lock=self.usb_lock,
            get_device_path=self.get_keyboard_path_safe,
        )
        self.battery_thread = None
        self.current_binding = None
        self.last_active_window = None
        self.binds_dict = {}
        self.default_profile_name = None
        self.devices = []
        self.filtered_devices = []

        self._build_page()
        self._build_ui()
        self.refresh_devices()
        self.update_payloads_list()
        self.update_bindings_list()

        self.battery_thread = threading.Thread(target=self.battery_poll_loop, daemon=True)
        self.battery_thread.start()

    # ---------- Config ----------
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

    def _current_mode(self):
        return self.mode_segmented.selected[0] if self.mode_segmented.selected else "auto"

    def save_config(self):
        self.config["mode"] = self._current_mode()
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.config, f, indent=4, ensure_ascii=False)
        if self.is_running:
            self.reload_runtime_state()
        if self.is_running:
            self._set_status(True)

    def reload_runtime_state(self):
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        registered = 0
        for prof_name, info in self.config["payloads"].items():
            hk = info.get("hotkey")
            if not hk:
                continue
            try:
                keyboard.add_hotkey(
                    hk,
                    lambda name=prof_name, data=info["data"]:
                        self.apply_payload(name, data, manual=True)
                )
                registered += 1
            except Exception as e:
                print(f"[Хоткей] Ошибка регистрации {hk}: {e}")
        self.binds_dict = {b["process"]: b["profile_name"] for b in self.config["bindings"]}
        self.default_profile_name = self.binds_dict.get("default")
        self.last_active_window = None
        print(f"[Reload] Хоткеев: {registered}, биндингов: {len(self.binds_dict)}")

    # ---------- Page setup ----------
    def _build_page(self):
        self.page.title = "QMK Profile Manager"
        self.page.window_width = 920
        self.page.window_height = 880
        self.page.window_min_width = 720
        self.page.window_min_height = 640
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

        self.page.window_prevent_close = True
        self.page.on_window_event = self._handle_window_event

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
                                    ft.Text("QMK Profile Manager", size=20, weight=ft.FontWeight.W_600),
                                    ft.Text("Переключение профилей клавиатуры", size=12,
                                            color=ft.Colors.ON_SURFACE_VARIANT),
                                ],
                                spacing=0,
                                tight=True,
                            ),
                        ],
                        spacing=12,
                    ),
                    self.status_badge,
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

        device_card = self._card(
            icon=ft.Icons.USB_ROUNDED,
            title="Устройство",
            subtitle="Выберите QMK-клавиатуру из списка HID-устройств.",
            content=ft.Container(
                ft.Row([self.device_dropdown, refresh_btn], spacing=8),
                margin=ft.Margin.only(top=12),
            ),
        )

        self.payloads_column = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO)
        add_profile_btn = ft.FilledTonalButton(
            "Добавить профиль",
            icon=ft.Icons.ADD_ROUNDED,
            on_click=lambda e: self.open_payload_dialog("Новый профиль"),
        )
        profiles_card = self._card(
            icon=ft.Icons.LAYERS_ROUNDED,
            title="Профили",
            subtitle="Payload для клавиатуры + опциональная горячая клавиша.",
            content=ft.Column(
                [
                    ft.Container(
                        content=self.payloads_column,
                        margin=ft.Margin.only(top=12),
                    ),
                    ft.Row([add_profile_btn], alignment=ft.MainAxisAlignment.END),
                ],
                spacing=8,
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
        self.filtered_devices = custom_devices

        options = []
        for i, d in enumerate(custom_devices):
            label = (
                f"{d.get('manufacturer_string') or 'Unknown'} "
                f"{d.get('product_string') or 'Device'} "
                f"· VID {hex(d['vendor_id'])} · PID {hex(d['product_id'])} · Page {hex(d['usage_page'])}"
            )
            options.append(ft.dropdown.Option(key=str(i), text=label))
        self.device_dropdown.options = options

        self.device_dropdown.value = None
        if self.config.get("device"):
            saved_vid = self.config["device"]["vid"]
            saved_page = self.config["device"]["usage_page"]
            for i, d in enumerate(custom_devices):
                if d['vendor_id'] == saved_vid and d['usage_page'] == saved_page:
                    self.device_dropdown.value = str(i)
                    break
        self.page.update()

    # ---------- Profiles / Payloads ----------
    def open_payload_dialog(self, title, old_name=None):
        old = self.config["payloads"].get(old_name, {"data": [], "hotkey": ""}) if old_name else {"data": [], "hotkey": ""}

        name_field = ft.TextField(
            label="Название профиля",
            hint_text="Например: Gaming, Typing",
            value=old_name or "",
            border_radius=12,
            filled=True,
        )
        payload_str = ", ".join([hex(x) for x in old["data"]]) if old["data"] else ""
        payload_field = ft.TextField(
            label="Payload (массив байт)",
            hint_text="0x04, 0x01, 0x00, ...",
            value=payload_str,
            multiline=True,
            min_lines=2,
            max_lines=4,
            border_radius=12,
            filled=True,
        )
        hotkey_field = ft.TextField(
            label="Горячая клавиша",
            hint_text="Например: ctrl+shift+1",
            value=old["hotkey"],
            border_radius=12,
            filled=True,
        )

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(title),
            content=ft.Container(
                content=ft.Column(
                    [name_field, payload_field, hotkey_field],
                    spacing=12,
                    tight=True,
                ),
                width=480,
            ),
            shape=ft.RoundedRectangleBorder(radius=24),
        )

        def on_cancel(e):
            self.page.pop_dialog()

        def on_save(e):
            name = name_field.value.strip()
            p_val = payload_field.value.strip()
            hk_val = hotkey_field.value.strip().lower()

            if not name or not p_val:
                self._snack("Имя и payload обязательны")
                return
            try:
                payload = [int(x.strip(), 16) for x in p_val.split(',')]
            except Exception as ex:
                self._snack(f"Неверный формат payload: {ex}")
                return
            if len(payload) < 64:
                payload += [0] * (64 - len(payload))
            elif len(payload) > 64:
                payload = payload[:64]

            if old_name and old_name != name:
                del self.config["payloads"][old_name]
                for b in self.config["bindings"]:
                    if b["profile_name"] == old_name:
                        b["profile_name"] = name

            self.config["payloads"][name] = {"data": payload, "hotkey": hk_val}
            self.save_config()
            self.update_payloads_list()
            self.update_bindings_list()
            self.page.pop_dialog()

        dlg.actions = [
            ft.TextButton("Отмена", on_click=on_cancel),
            ft.FilledButton("Сохранить", on_click=on_save),
        ]
        self.page.show_dialog(dlg)

    def delete_payload(self, name):
        used = [b["process"] for b in self.config["bindings"] if b["profile_name"] == name]
        if used:
            self._snack(f"Используется в: {', '.join(used)}")
            return
        del self.config["payloads"][name]
        self.save_config()
        self.update_payloads_list()

    def update_payloads_list(self):
        self.payloads_column.controls.clear()
        if not self.config["payloads"]:
            self.payloads_column.controls.append(
                ft.Container(
                    content=ft.Text(
                        "Профилей пока нет. Добавьте первый, чтобы начать.",
                        color=ft.Colors.ON_SURFACE_VARIANT,
                        size=13,
                    ),
                    padding=16,
                    alignment=ft.Alignment.CENTER,
                )
            )
        else:
            for name, info in self.config["payloads"].items():
                hk = info.get("hotkey") or ""
                data = info["data"]
                preview = ", ".join(hex(b) for b in data[:4]) + "…"

                hotkey_chip = (
                    ft.Container(
                        content=ft.Text(hk, size=11, weight=ft.FontWeight.W_500),
                        padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                        bgcolor=ft.Colors.TERTIARY_CONTAINER,
                        border_radius=100,
                    ) if hk else ft.Container(
                        content=ft.Text("нет хоткея", size=11,
                                        color=ft.Colors.ON_SURFACE_VARIANT),
                        padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                    )
                )

                row = ft.Container(
                    content=ft.Row(
                        [
                            ft.Row(
                                [
                                    ft.Icon(ft.Icons.DASHBOARD_CUSTOMIZE_ROUNDED,
                                            color=ft.Colors.PRIMARY, size=20),
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
                                    hotkey_chip,
                                    ft.IconButton(
                                        icon=ft.Icons.EDIT_ROUNDED,
                                        tooltip="Редактировать",
                                        icon_size=18,
                                        on_click=lambda e, n=name: self.open_payload_dialog("Редактировать", n),
                                    ),
                                    ft.IconButton(
                                        icon=ft.Icons.DELETE_ROUNDED,
                                        tooltip="Удалить",
                                        icon_size=18,
                                        icon_color=ft.Colors.ERROR,
                                        on_click=lambda e, n=name: self.delete_payload(n),
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

    # ---------- Bindings ----------
    def open_binding_dialog(self, title, edit_idx=None):
        if not self.config["payloads"]:
            self._snack("Сначала создайте хотя бы один профиль")
            return

        b_data = self.config["bindings"][edit_idx] if edit_idx is not None else None

        proc_field = ft.TextField(
            label="Процесс",
            hint_text="cs2.exe или 'default' для фолбэка",
            value=b_data["process"] if b_data else "",
            border_radius=12,
            filled=True,
        )
        prof_dropdown = ft.Dropdown(
            label="Профиль",
            options=[ft.dropdown.Option(k) for k in self.config["payloads"].keys()],
            value=b_data["profile_name"] if b_data and b_data["profile_name"] in self.config["payloads"]
            else next(iter(self.config["payloads"].keys())),
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
            prof_name = prof_dropdown.value
            if not proc or not prof_name:
                self._snack("Процесс и профиль обязательны")
                return
            new_bind = {"process": proc, "profile_name": prof_name}
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
                                        content=ft.Text(b["profile_name"], size=12,
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
            if self.device_dropdown.value is None:
                self._snack("Выберите HID-устройство")
                return
            idx = int(self.device_dropdown.value)
            selected = self.filtered_devices[idx]
            self.config["device"] = {
                "vid": selected["vendor_id"],
                "pid": selected["product_id"],
                "usage_page": selected["usage_page"],
            }
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
        # Wired by Task 10 (header battery badge). Stub for now.
        pass

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
                        app_id='QMK Manager',
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

                        if active_process in self.binds_dict:
                            prof_name = self.binds_dict[active_process]
                            info = self.config["payloads"].get(prof_name)
                            if info:
                                self.apply_payload(prof_name, info["data"], manual=False)
                        elif self.default_profile_name and self.current_binding != self.default_profile_name:
                            info = self.config["payloads"].get(self.default_profile_name)
                            if info:
                                self.apply_payload(self.default_profile_name, info["data"], manual=False)
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

    # ---------- Utilities ----------
    def _snack(self, text):
        self.page.open(ft.SnackBar(ft.Text(text), duration=2500))


def main(page: ft.Page):
    QMKManager(page)


if __name__ == "__main__":
    ft.run(main)
