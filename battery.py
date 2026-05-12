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
        # Implemented in next task.
        pass
