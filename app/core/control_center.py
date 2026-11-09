# SPDX-License-Identifier: GPL-3.0-or-later
"""Device operations for the macOS Control Center extension.

The host supplies its authenticated device list and runs these operations on
its existing service queue. The extension never needs Xiaomi credentials.

Every device with a switch property is offered, including the LKRQ PC power
card (pzg.plug.1127): like its card in the main window, a toggle writes the
opposite of its current `on` state, which simulates pressing the power button.
Writes are never retried automatically (see MijiaService.set_power_state).
"""

from collections.abc import Callable, Sequence

from app.core.models import DeviceInfo
from app.core.service import MijiaService, ServiceError


class DeviceControls:
    """Expose switches supported by the current MiHome session."""

    def __init__(self, service: MijiaService):
        self._service = service

    def devices(self, devices: Sequence[DeviceInfo]) -> list[dict]:
        """All devices with a switch, offline ones included (the control shows 离线)."""
        if not devices:
            return []
        states = self._service.power_states([device.did for device in devices])
        return [
            {"id": device.did, "name": device.name, "room": device.room_name,
             "home": self.home_label(device.home_name), "online": device.online,
             "model": device.model, "symbol": self.symbol(device),
             "isOn": states[device.did]}
            for device in devices if type(states.get(device.did)) is bool
        ]

    @staticmethod
    def home_label(home_name: str) -> str:
        """控件配置里「家庭」的名称；别人共享来的设备不属于自己的任何家庭。"""
        return home_name if home_name and home_name != "未知" else "共享设备"

    @staticmethod
    def symbol(device: DeviceInfo) -> str:
        """优先用型号和米家设备类型，通用插座也可按用途命名。"""
        if device.model == "pzg.plug.1127":
            return "desktopcomputer"
        kind = device.spec_type.split(":")[3:4] or device.model.split(".")[1:2]
        kind = kind[0] if kind else ""
        if kind == "electric-blanket" or "电热毯" in device.name:
            return "bed.double"
        if kind in {"water-heater", "electric-water-heater", "gas-water-heater"} or "热水器" in device.name:
            return "drop"
        return {
            "air-conditioner": "air.conditioner.horizontal",
            "acpartner": "powerplug",
            "plug": "powerplug",
            "outlet": "powerplug",
            "light": "lightbulb",
            "fan": "fan",
            "switch": "switch.2",
        }.get(kind, "power")

    @classmethod
    def describe(cls, devices: Sequence[DeviceInfo], device_id: str) -> dict:
        """控件标题用的当前名称、房间和家庭（在 MiHome 里改名后跟着变）。"""
        device = next((device for device in devices if device.did == device_id), None)
        if device is None:
            return {}
        return {"name": device.name, "room": device.room_name,
                "home": cls.home_label(device.home_name), "symbol": cls.symbol(device)}

    def state(self, devices: Sequence[DeviceInfo], device_id: str) -> bool:
        device = next((device for device in devices if device.did == device_id), None)
        if device is None:
            raise ServiceError("这台设备已不在当前账号的设备列表里")
        if not device.online:
            raise ServiceError("设备离线")
        state = self._service.power_state(device_id)
        if type(state) is not bool:
            raise ServiceError("这台设备不支持控制中心开关")
        return state

    def set_state(self, devices: Sequence[DeviceInfo], device_id: str, is_on: bool,
                  *, still_active: Callable[[], bool] = lambda: True) -> bool:
        if type(is_on) is not bool:
            raise ServiceError("无效的开关状态")
        if self.state(devices, device_id) != is_on:
            if not still_active():
                raise ServiceError("控制请求已过期")
            self._service.set_power_state(device_id, is_on)
        return is_on
