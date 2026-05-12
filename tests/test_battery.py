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
