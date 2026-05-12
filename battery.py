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
