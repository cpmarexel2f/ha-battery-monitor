"""Tests for Battery Monitor."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.battery_monitor.const import (
    CONF_DURATION,
    CONF_EVENING,
    CONF_IGNORED_ENTITIES,
    CONF_IGNORED_INTEGRATIONS,
    CONF_MORNING,
    CONF_NOTIFY,
    CONF_THRESHOLD,
    DOMAIN,
    NOTIFICATION_ID,
)

from .conftest import add_battery

LOW = "sensor.battery_monitor_low_batteries"
LAST = "sensor.battery_monitor_last_notification"
BUTTON = "button.battery_monitor_notify_now"
OPTIONS = {
    CONF_THRESHOLD: 20,
    CONF_DURATION: 24,
    CONF_MORNING: "09:00:00",
    CONF_EVENING: "19:00:00",
    CONF_NOTIFY: [],
    CONF_IGNORED_ENTITIES: [],
    CONF_IGNORED_INTEGRATIONS: ["mobile_app"],
}


async def _setup(hass: HomeAssistant, **options) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options={**OPTIONS, **options})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _batteries(hass: HomeAssistant) -> list[dict]:
    return hass.states.get(LOW).attributes["batteries"]


def _notification(hass: HomeAssistant) -> dict | None:
    return persistent_notification._async_get_or_create_notifications(hass).get(NOTIFICATION_ID)


async def test_config_flow_single_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**OPTIONS, CONF_NOTIFY: ["mobile_app_phone"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_NOTIFY] == ["notify.mobile_app_phone"]
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.ABORT


async def test_options_reload(hass: HomeAssistant) -> None:
    add_battery(hass, "Door", "15")
    entry = await _setup(hass)
    assert hass.states.get(LOW).state == "1"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**OPTIONS, CONF_THRESHOLD: 10})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert hass.states.get(LOW).state == "0"
    assert hass.states.get(LOW).attributes["threshold"] == 10


async def test_discovery(hass: HomeAssistant) -> None:
    add_battery(hass, "Door", "15", area="Kitchen")
    add_battery(hass, "Lock", "64")
    # Same device: the level sensor wins over the low-battery binary sensor
    add_battery(hass, "Remote", "8")
    add_battery(hass, "Remote", "on", domain="binary_sensor")
    # Binary sensor alone
    add_battery(hass, "Smoke", "on", domain="binary_sensor")
    # Left out: phone (mobile_app), an ignored entity, a hidden one
    add_battery(hass, "Phone", "5", platform="mobile_app")
    ignored = add_battery(hass, "Thermostat", "3")
    hidden = add_battery(hass, "Hidden", "2")
    er.async_get(hass).async_update_entity(hidden, hidden_by=er.RegistryEntryHider.USER)
    # Not a battery
    hass.states.async_set("sensor.temperature", "21", {"device_class": "temperature"})

    await _setup(hass, **{CONF_IGNORED_ENTITIES: [ignored]})
    batteries = _batteries(hass)
    assert [(b["name"], b["level"], b["low"]) for b in batteries] == [
        ("Remote", 8, True),
        ("Door", 15, True),
        ("Smoke", None, True),
        ("Lock", 64, False),
    ]
    assert batteries[1]["area"] == "Kitchen"
    assert batteries[0]["entity_id"].startswith("sensor.")
    assert batteries[0]["icon"] == "mdi:battery-10"
    assert batteries[2]["icon"] == "mdi:battery-alert-variant-outline"
    assert hass.states.get(LOW).state == "3"


async def test_duration_and_recovery(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    door = add_battery(hass, "Door", "50")
    await _setup(hass)
    assert hass.states.get(LOW).attributes["due"] == 0

    hass.states.async_set(door, "12", {"device_class": "battery", "unit_of_measurement": "%"})
    await hass.async_block_till_done()
    [battery] = _batteries(hass)
    assert battery["low"] and not battery["due"]

    freezer.tick(timedelta(hours=23))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert not _batteries(hass)[0]["due"]

    freezer.tick(timedelta(hours=1, minutes=10))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _batteries(hass)[0]["due"]
    assert hass.states.get(LOW).attributes["due"] == 1

    # Replaced: no longer low, its time below the threshold is forgotten
    hass.states.async_set(door, "100", {"device_class": "battery", "unit_of_measurement": "%"})
    await hass.async_block_till_done()
    assert _batteries(hass)[0]["below_since"] is None
    hass.states.async_set(door, "10", {"device_class": "battery", "unit_of_measurement": "%"})
    await hass.async_block_till_done()
    assert not _batteries(hass)[0]["due"]


async def test_scheduled_notifications(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    calls = async_mock_service(hass, "notify", "mobile_app_phone")
    freezer.move_to(
        dt_util.as_utc(dt_util.parse_datetime("2026-10-24 08:00:00").replace(tzinfo=dt_util.get_default_time_zone()))
    )
    add_battery(hass, "Door", "12", area="Kitchen")
    add_battery(hass, "Smoke", "on", domain="binary_sensor")
    add_battery(hass, "Lock", "18")
    await _setup(hass, **{CONF_DURATION: 0, CONF_NOTIFY: ["notify.mobile_app_phone"]})

    freezer.move_to(
        dt_util.as_utc(dt_util.parse_datetime("2026-10-24 09:00:00").replace(tzinfo=dt_util.get_default_time_zone()))
    )
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert len(calls) == 1
    assert calls[0].data["title"] == "3 low batteries"
    assert calls[0].data["message"] == "• Door (Kitchen): 12 %\n• Lock: 18 %\n• Smoke: low"
    assert _notification(hass)["title"] == "3 low batteries"
    assert hass.states.get(LAST).attributes["notified"] == 3

    # All replaced before the evening: no message, the notification is cleared
    for entity_id in (e for e in hass.states.async_entity_ids() if e.endswith("_battery")):
        state = hass.states.get(entity_id)
        hass.states.async_set(entity_id, "off" if state.domain == "binary_sensor" else "100", state.attributes)
    freezer.move_to(
        dt_util.as_utc(dt_util.parse_datetime("2026-10-24 19:00:00").replace(tzinfo=dt_util.get_default_time_zone()))
    )
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert _notification(hass) is None
    assert hass.states.get(LAST).attributes["notified"] == 0


async def test_button_and_action(hass: HomeAssistant) -> None:
    calls = async_mock_service(hass, "notify", "mobile_app_phone")
    add_battery(hass, "Door", "5")
    await _setup(hass, **{CONF_DURATION: 0, CONF_NOTIFY: ["notify.mobile_app_phone", "notify.missing"]})

    await hass.services.async_call("button", "press", {"entity_id": BUTTON}, blocking=True)
    assert len(calls) == 1
    await hass.services.async_call(DOMAIN, "notify", {}, blocking=True)
    assert len(calls) == 2
    assert calls[1].data["title"] == "1 low battery"


async def test_low_since_survives_reload(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    add_battery(hass, "Door", "10")
    entry = await _setup(hass)
    since = _batteries(hass)[0]["below_since"]
    assert since is not None

    freezer.tick(timedelta(hours=2))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _batteries(hass)[0]["below_since"] == since


async def test_unavailable_keeps_low_since(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    door = add_battery(hass, "Door", "10")
    await _setup(hass, **{CONF_DURATION: 1})
    since = _batteries(hass)[0]["below_since"]

    hass.states.async_set(door, "unavailable", {"device_class": "battery", "unit_of_measurement": "%"})
    await hass.async_block_till_done()
    battery = _batteries(hass)[0]
    assert battery["unavailable"] and battery["icon"] == "mdi:battery-unknown" and not battery["due"]

    freezer.tick(timedelta(hours=2))
    hass.states.async_set(door, "9", {"device_class": "battery", "unit_of_measurement": "%"})
    await hass.async_block_till_done()
    battery = _batteries(hass)[0]
    assert battery["below_since"] == since and battery["due"]
