"""Button: send the low-battery notification now."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BatteryMonitorConfigEntry
from .entity import BatteryMonitorEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BatteryMonitorConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([NotifyNowButton(entry.runtime_data, entry.entry_id, "notify_now")])


class NotifyNowButton(BatteryMonitorEntity, ButtonEntity):
    """Every low battery right away, whatever the duration."""

    async def async_press(self) -> None:
        await self.monitor.async_notify(manual=True)
