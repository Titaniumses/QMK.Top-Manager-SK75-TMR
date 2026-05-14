"""Unit tests for transport detection and active-device selection logic."""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app_flet import QMKManager


class TestDetectTransport:
    def test_wireless_when_product_contains_2_4g(self):
        dev = {"product_string": "2.4G Wireless Keyboard", "manufacturer_string": ""}
        assert QMKManager._detect_transport(dev) == "wireless"

    def test_wireless_when_product_contains_wireless(self):
        dev = {"product_string": "Some Wireless KB", "manufacturer_string": ""}
        assert QMKManager._detect_transport(dev) == "wireless"

    def test_wireless_case_insensitive(self):
        dev = {"product_string": "DONGLE RX", "manufacturer_string": ""}
        assert QMKManager._detect_transport(dev) == "wireless"

    def test_wireless_match_in_manufacturer(self):
        dev = {"product_string": "SK75TMR", "manufacturer_string": "Acme 2.4G"}
        assert QMKManager._detect_transport(dev) == "wireless"

    def test_wired_when_no_keywords(self):
        dev = {"product_string": "SK75TMR", "manufacturer_string": "Acme"}
        assert QMKManager._detect_transport(dev) == "wired"

    def test_wired_when_strings_missing(self):
        assert QMKManager._detect_transport({}) == "wired"

    def test_wired_when_strings_none(self):
        dev = {"product_string": None, "manufacturer_string": None}
        assert QMKManager._detect_transport(dev) == "wired"


class TestPickActiveTarget:
    def test_keep_existing_active_when_still_present(self):
        cfg = {"wired-key": {"transport": "wired"}, "wireless-key": {"transport": "wireless"}}
        assert QMKManager._pick_active_target(
            current_active="wireless-key",
            present_keys=["wired-key", "wireless-key"],
            devices_cfg=cfg,
        ) == "wireless-key"

    def test_prefer_wired_when_active_missing(self):
        cfg = {"wired-key": {"transport": "wired"}, "wireless-key": {"transport": "wireless"}}
        assert QMKManager._pick_active_target(
            current_active=None,
            present_keys=["wired-key", "wireless-key"],
            devices_cfg=cfg,
        ) == "wired-key"

    def test_prefer_wired_even_if_listed_second(self):
        cfg = {"wireless-key": {"transport": "wireless"}, "wired-key": {"transport": "wired"}}
        assert QMKManager._pick_active_target(
            current_active=None,
            present_keys=["wireless-key", "wired-key"],
            devices_cfg=cfg,
        ) == "wired-key"

    def test_pick_only_wireless_when_no_wired(self):
        cfg = {"wireless-key": {"transport": "wireless"}}
        assert QMKManager._pick_active_target(
            current_active=None,
            present_keys=["wireless-key"],
            devices_cfg=cfg,
        ) == "wireless-key"

    def test_returns_none_when_no_devices(self):
        assert QMKManager._pick_active_target(None, [], {}) is None

    def test_active_present_but_missing_from_cfg_falls_back(self):
        cfg = {"wired-key": {"transport": "wired"}}
        assert QMKManager._pick_active_target(
            current_active="ghost-key",
            present_keys=["wired-key"],
            devices_cfg=cfg,
        ) == "wired-key"

    def test_first_present_when_no_transport_metadata(self):
        cfg = {"a": {}, "b": {}}
        assert QMKManager._pick_active_target(None, ["a", "b"], cfg) == "a"


class TestTransportPersistence:
    def _stub(self):
        return QMKManager.__new__(QMKManager)

    def test_normalize_adds_missing_transport_as_none(self):
        mgr = self._stub()
        entry = {"vid": 1, "pid": 2, "usage_page": 3, "label": "X",
                 "payloads": {}, "bindings": [], "battery": {}}
        mgr._normalize_device_entry(entry)
        assert entry["transport"] is None

    def test_normalize_preserves_existing_transport(self):
        mgr = self._stub()
        entry = {"vid": 1, "pid": 2, "usage_page": 3, "label": "X",
                 "payloads": {}, "bindings": [], "battery": {},
                 "transport": "wireless"}
        mgr._normalize_device_entry(entry)
        assert entry["transport"] == "wireless"

    def test_empty_device_entry_has_transport_field(self):
        mgr = self._stub()
        entry = mgr._empty_device_entry(1, 2, 3, label="X")
        assert "transport" in entry
        assert entry["transport"] is None
