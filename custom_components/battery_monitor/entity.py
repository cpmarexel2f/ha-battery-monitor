"""Base entity: one service device, entities refreshed with the battery list."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .monitor import BatteryMonitor


class BatteryMonitorEntity(Entity):
    """Shared device and update listener."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, monitor: BatteryMonitor, entry_id: str, key: str) -> None:
        self.monitor = monitor
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Battery Monitor",
            manufacturer="juju35",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://github.com/juju35/ha-battery-monitor",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.monitor.async_add_listener(self.async_write_ha_state))
