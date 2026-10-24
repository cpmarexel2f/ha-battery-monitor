"""Battery Monitor: find the batteries, track how long each one has been low, notify twice a day.

A battery is a `sensor` with the `battery` device class (level in %) or a `binary_sensor` with the
`battery` device class (`on` = low). A device with several of them is listed once, with its level sensor.
A battery becomes *low* below the threshold, and *due* once it has stayed low for the configured duration:
the due batteries are the ones notified.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from itertools import pairwise
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.const import ATTR_DEVICE_CLASS, EVENT_STATE_CHANGED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MAX_DURATION, NOTIFICATION_ID, STORAGE_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

BATTERY = "battery"
DOMAINS = ("sensor", "binary_sensor")


@dataclass(slots=True)
class Battery:
    """One monitored device (or entity without a device)."""

    entity_id: str
    device_id: str | None
    name: str
    area: str | None
    level: int | None  # None for a binary sensor or an unavailable battery
    low: bool
    unavailable: bool
    below_since: datetime | None
    due: bool

    @property
    def icon(self) -> str:
        if self.unavailable:
            return "mdi:battery-unknown"
        if self.level is None:
            return "mdi:battery-alert-variant-outline" if self.low else "mdi:battery"
        if self.level >= 95:
            return "mdi:battery"
        if self.level < 5:
            return "mdi:battery-outline"
        return f"mdi:battery-{max(10, round(self.level / 10) * 10)}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "device_id": self.device_id,
            "name": self.name,
            "area": self.area,
            "level": self.level,
            "icon": self.icon,
            "low": self.low,
            "due": self.due,
            "unavailable": self.unavailable,
            "below_since": self.below_since.isoformat() if self.below_since else None,
        }


def _level(state: State) -> int | None:
    try:
        return round(float(state.state))
    except (TypeError, ValueError):
        return None


async def _async_history(hass: HomeAssistant, entity_ids: list[str], start: datetime) -> dict[str, list[State]]:
    """Recorded states of `entity_ids` since `start`, oldest first ({} without the recorder)."""
    if "recorder" not in hass.config.components:
        return {}
    from homeassistant.components.recorder import get_instance, history  # noqa: PLC0415 - optional dependency

    return await get_instance(hass).async_add_executor_job(
        partial(
            history.get_significant_states,
            hass,
            start,
            entity_ids=entity_ids,
            significant_changes_only=False,
            no_attributes=True,
        )
    )


class BatteryMonitor:
    """Battery list, low-battery tracking and notifications."""

    def __init__(
        self,
        hass: HomeAssistant,
        threshold: int,
        duration: timedelta,
        notify_services: Iterable[str] = (),
        ignored_entities: Iterable[str] = (),
        ignored_integrations: Iterable[str] = (),
    ) -> None:
        self.hass = hass
        self.threshold = threshold
        self.duration = duration
        self.notify_services = list(notify_services)
        self.ignored_entities = set(ignored_entities)
        self.ignored_integrations = set(ignored_integrations)
        self.batteries: list[Battery] = []
        self.last_notification: datetime | None = None
        self.last_notified: int | None = None
        self._below_since: dict[str, datetime] = {}
        self._store: Store[dict[str, str]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._listeners: list[CALLBACK_TYPE] = []

    # ── Listeners ───────────────────────────────────────────────────────────

    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        self._listeners.append(update_callback)
        return lambda: self._listeners.remove(update_callback)

    def _notify_listeners(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    # ── Batteries ───────────────────────────────────────────────────────────

    @property
    def low(self) -> list[Battery]:
        return [b for b in self.batteries if b.low]

    @property
    def due(self) -> list[Battery]:
        return [b for b in self.batteries if b.due]

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        for entity_id, value in stored.items():
            if (since := dt_util.parse_datetime(value)) is not None:
                self._below_since[entity_id] = since
        known = set(self._below_since)
        self.async_refresh()
        # Batteries already low when first seen (just installed, or newly found): look up in the
        # history when they went low, rather than starting the count now.
        new = [b.entity_id for b in self.low if b.entity_id not in known]
        if new:
            try:
                await self._async_backfill(new)
            except Exception:  # noqa: BLE001 - the history only makes the start time more accurate
                _LOGGER.exception("Could not read the history of the low batteries")

    def _is_low(self, state: State) -> bool | None:
        """True / False for a battery state, None when unavailable."""
        if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        # Recorded states (LazyState) have no `domain`: read it from the entity ID.
        if state.entity_id.startswith("binary_sensor."):
            return state.state == "on"
        level = _level(state)
        return None if level is None else level < self.threshold

    async def _async_backfill(self, entity_ids: list[str]) -> None:
        """Set when each battery went low: right after its last state above the threshold."""
        now = dt_util.utcnow()
        states = await _async_history(self.hass, entity_ids, now - timedelta(hours=MAX_DURATION))
        for entity_id in entity_ids:
            recorded = [s for s in states.get(entity_id, []) if self._is_low(s) is not None]
            if not recorded:
                continue
            # Low for the whole recorded period, or since the last time it went below the threshold.
            since = recorded[0].last_changed if self._is_low(recorded[0]) else None
            for previous, state in pairwise(recorded):
                if not self._is_low(previous) and self._is_low(state):
                    since = state.last_changed
            if since is not None and since < self._below_since.get(entity_id, now):
                self._below_since[entity_id] = since
        self.async_refresh()

    @callback
    def async_listen(self) -> CALLBACK_TYPE:
        """Refresh when a battery changes."""

        @callback
        def changed(event: Event[EventStateChangedData]) -> None:
            state = event.data["new_state"] or event.data["old_state"]
            if state is not None and state.domain in DOMAINS and state.attributes.get(ATTR_DEVICE_CLASS) == BATTERY:
                self.async_refresh()

        return self.hass.bus.async_listen(EVENT_STATE_CHANGED, changed)

    def _ignored(self, entity_id: str, entry: er.RegistryEntry | None) -> bool:
        if entity_id in self.ignored_entities:
            return True
        if entry is None:
            return False
        return entry.hidden or entry.platform in self.ignored_integrations

    @callback
    def async_refresh(self) -> None:
        """Rebuild the list from the current states, and update how long each battery has been low."""
        now = dt_util.utcnow()
        entities = er.async_get(self.hass)
        devices = dr.async_get(self.hass)
        areas = ar.async_get(self.hass)

        # One battery per device: its level sensor wins over a low-battery binary sensor.
        chosen: dict[str, tuple[State, er.RegistryEntry | None]] = {}
        for state in self.hass.states.async_all(DOMAINS):
            if state.attributes.get(ATTR_DEVICE_CLASS) != BATTERY:
                continue
            entry = entities.async_get(state.entity_id)
            if self._ignored(state.entity_id, entry):
                continue
            key = (entry.device_id if entry and entry.device_id else None) or state.entity_id
            current = chosen.get(key)
            if current is None or (current[0].domain == "binary_sensor" and state.domain == "sensor"):
                chosen[key] = (state, entry)

        batteries: list[Battery] = []
        for state, entry in chosen.values():
            device = devices.async_get(entry.device_id) if entry and entry.device_id else None
            unavailable = state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            level = None if state.domain == "binary_sensor" or unavailable else _level(state)
            if state.domain == "binary_sensor":
                low = state.state == "on"
            else:
                low = level is not None and level < self.threshold
            # An unavailable battery keeps the time it went low: it is often a dead one.
            if low:
                self._below_since.setdefault(state.entity_id, now)
            elif not unavailable:
                self._below_since.pop(state.entity_id, None)
            since = self._below_since.get(state.entity_id) if low else None
            area_id = (entry.area_id if entry else None) or (device.area_id if device else None)
            area = areas.async_get_area(area_id) if area_id else None
            name = (device.name_by_user or device.name) if device else None
            batteries.append(
                Battery(
                    entity_id=state.entity_id,
                    device_id=device.id if device else None,
                    name=name or state.name,
                    area=area.name if area else None,
                    level=level,
                    low=low,
                    unavailable=unavailable,
                    below_since=since,
                    due=since is not None and now - since >= self.duration,
                )
            )

        # Lowest first; batteries without a level (binary sensors, unavailable) after the levels.
        batteries.sort(key=lambda b: (not b.low, b.level is None, b.level or 0, b.name.casefold()))
        tracked = {b.entity_id for b in batteries}
        self._below_since = {k: v for k, v in self._below_since.items() if k in tracked}
        self.batteries = batteries
        self._store.async_delay_save(self._data, 5)
        self._notify_listeners()

    def _data(self) -> dict[str, str]:
        return {k: v.isoformat() for k, v in self._below_since.items()}

    async def async_save(self) -> None:
        """Write the times below the threshold now (on unload: a reload must not reset them)."""
        await self._store.async_save(self._data())

    # ── Notifications ───────────────────────────────────────────────────────

    async def async_notify(self, manual: bool = False) -> None:
        """Notify the batteries that have been low for the duration; clear the notification when none is.

        Asked for (button, action): every low battery, whatever the duration, and a message even
        when none is low, so the person who asked gets an answer.
        """
        self.async_refresh()
        due = [b for b in self.low if not b.unavailable] if manual else self.due
        self.last_notification = dt_util.utcnow()
        self.last_notified = len(due)
        if not due:
            persistent_notification.async_dismiss(self.hass, NOTIFICATION_ID)
            if manual:
                await self._async_send(
                    await self._text("none_title"), await self._text("none", threshold=self.threshold), persistent=False
                )
            self._notify_listeners()
            return

        strings = await async_get_translations(self.hass, self.hass.config.language, "exceptions", [DOMAIN])

        def text(key: str, **values: Any) -> str:
            template = strings.get(f"component.{DOMAIN}.exceptions.{key}.message", key)
            return template.format(**values)

        lines = [
            text(
                "line" if b.level is not None else "line_no_level",
                name=b.name,
                level=b.level,
                area=f" ({b.area})" if b.area else "",
            )
            for b in due
        ]
        title = text("title_one" if len(due) == 1 else "title_many", count=len(due))
        await self._async_send(title, "\n".join(lines))
        self._notify_listeners()

    async def _text(self, key: str, **values: Any) -> str:
        strings = await async_get_translations(self.hass, self.hass.config.language, "exceptions", [DOMAIN])
        return strings.get(f"component.{DOMAIN}.exceptions.{key}.message", key).format(**values)

    async def _async_send(self, title: str, message: str, *, persistent: bool = True) -> None:
        """Persistent notification (replaced each time), and the notify actions chosen."""
        if persistent:
            persistent_notification.async_create(self.hass, message, title=title, notification_id=NOTIFICATION_ID)
        for service in self.notify_services:
            name = service.removeprefix("notify.")
            if not self.hass.services.has_service("notify", name):
                _LOGGER.warning("Notify action notify.%s not found", name)
                continue
            try:
                await self.hass.services.async_call("notify", name, {"title": title, "message": message}, blocking=True)
            except Exception:  # noqa: BLE001 - a broken notifier must not stop the others
                _LOGGER.exception("Sending the notification with notify.%s failed", name)
