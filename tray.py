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

    body = (3, 8, 26, 24)
    nub = (26, 13, 29, 19)

    if state.is_stale or state.percent is None:
        draw.rounded_rectangle(body, radius=3, outline=_GREY_COLOR, width=2)
        draw.rectangle(nub, fill=_GREY_COLOR)
        try:
            font = ImageFont.truetype("arial.ttf", 14)
        except OSError:
            font = ImageFont.load_default()
        draw.text((11, 7), "?", fill=_GREY_COLOR, font=font)
        return img

    draw.rounded_rectangle(body, radius=3, outline=_OUTLINE_COLOR, width=2)
    draw.rectangle(nub, fill=_OUTLINE_COLOR)

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
