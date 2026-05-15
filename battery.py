import logging
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Callable, Optional

logger = logging.getLogger(__name__)


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
        get_device_paths: Optional[Callable[[], list]] = None,
        on_working_path: Optional[Callable[[bytes], None]] = None,
        hid_device_factory: Optional[Callable[[], object]] = None,
    ):
        self._config = config_battery
        self._usb_lock = usb_lock
        self._get_path = get_device_path
        self._get_paths = get_device_paths
        self._on_working_path = on_working_path
        if hid_device_factory is None:
            import hid
            hid_device_factory = hid.device
        self._make_device = hid_device_factory
        self._state = BatteryState()

    @property
    def state(self) -> BatteryState:
        return self._state

    def read_once(self) -> None:
        paths = self._get_paths() if self._get_paths else []
        if not paths:
            path = self._get_path()
            paths = [path] if path else []
        if not paths:
            self._mark_failure("device path unavailable")
            return
        report_id = self._config.get("report_id", 0)
        query = [report_id] + list(self._config.get("query") or [])
        if len(query) < 2:
            self._mark_failure("no battery query configured")
            return
        response_length = self._config.get("response_length", 32)
        logger.debug("battery read_once %d path(s) to try", len(paths))
        with self._usb_lock:
            for path in paths:
                try:
                    device = self._make_device()
                    device.open_path(path)
                    logger.debug("battery send_feature_report path=%s query=%s",
                                 path, [f"0x{b:02x}" for b in query])
                    device.send_feature_report(query)
                    response = device.get_feature_report(report_id, response_length)
                    logger.debug("battery get_feature_report response=%s",
                                 [f"0x{b:02x}" for b in response[:16]])
                    device.close()
                except Exception as exc:
                    logger.debug("battery read HID error on path=%s: %s", path, exc)
                    try:
                        device.close()
                    except Exception:
                        pass
                    continue
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

                    logger.debug("battery parsed: raw=%d percent=%d charging=%s path=%s",
                                 raw, percent, charging, path)
                    self._state = BatteryState(
                        percent=percent,
                        charging=charging,
                        updated_at=datetime.now(),
                        is_stale=False,
                    )
                    if self._on_working_path:
                        self._on_working_path(path)
                    return
                except (IndexError, KeyError, TypeError) as exc:
                    logger.debug("battery parse error on path=%s: %s", path, exc)
                    continue
        self._mark_failure(f"all {len(paths)} paths failed")

    def probe_battery(self, packet_data: list, path: str) -> Optional[int]:
        """Try packet_data as a battery query. Returns percent (0-100) or None."""
        report_id = self._config.get("report_id", 0)
        response_length = self._config.get("response_length", 32)
        offset = self._config.get("response_offset")
        scale = self._config.get("response_scale", 1)
        if offset is None:
            return None
        query = [report_id] + list(packet_data)
        logger.debug("battery probe query=%s path=%s", [f"0x{b:02x}" for b in query], path)
        with self._usb_lock:
            try:
                device = self._make_device()
                device.open_path(path)
                device.set_nonblocking(1)
                device.send_feature_report(query)
                response = device.get_feature_report(report_id, response_length)
                device.close()
            except Exception as exc:
                logger.debug("battery probe HID error: %s", exc)
                try:
                    device.close()
                except Exception:
                    pass
                return None
        try:
            raw = response[offset]
            percent = int(raw * scale)
            logger.debug("battery probe response=%s raw=%d percent=%d",
                         [f"0x{b:02x}" for b in response[:16]], raw, percent)
            if 0 <= percent <= 100:
                return percent
            return None
        except (IndexError, TypeError):
            return None

    def _mark_failure(self, reason: str) -> None:
        logger.debug("battery read failed: %s", reason)
        self._state = BatteryState(
            percent=None,
            charging=False,
            updated_at=datetime.now(),
            is_stale=True,
        )
