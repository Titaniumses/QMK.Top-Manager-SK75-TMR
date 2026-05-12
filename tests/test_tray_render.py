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
    white_px = sum(c for px, c in counts.items() if px[0] == 255 and px[1] == 255 and px[2] == 255 and px[3] > 0)
    assert white_px > 0
