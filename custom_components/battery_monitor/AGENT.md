# Battery Monitor — Agent Context

## Purpose
Home Assistant custom integration that finds every battery, tracks how long each one has been below a threshold, and notifies twice a day the ones that have stayed low for a configurable duration. It bundles a Lovelace card listing the batteries (icon, name, room, level).

## Architecture

```
custom_components/battery_monitor/
├── __init__.py      # Static path + dashboard resource for the card, action notify, entry setup: state listener, 10-min refresh, two time triggers
├── monitor.py       # BatteryMonitor: discovery, low / due tracking (persisted in .storage), notifications
├── config_flow.py   # Single entry; options: threshold (%), duration (h), morning_time, evening_time, notify_services, ignored_entities, ignored_integrations
├── sensor.py        # low_batteries (count + batteries list for the card), last_notification (timestamp + notified)
├── button.py        # notify_now
├── entity.py        # Shared service device, listener on BatteryMonitor
├── frontend/battery-monitor-card.js   # custom:battery-monitor-card, reads sensor.*_low_batteries attributes
├── const.py / icons.json / services.yaml / translations/{en,fr}.json / brand/
```

## Rules
- A battery is a `sensor` (level %) or `binary_sensor` (`on` = low) with device class `battery`. One per device: the level sensor wins over the binary sensor. Hidden entities, `ignored_entities` and entities of `ignored_integrations` (default `mobile_app`) are left out.
- *Low*: level < threshold, or binary `on`. `below_since` is set when a battery becomes low, kept while it is unavailable (often a dead battery), cleared when it is back above the threshold. Persisted in `.storage/battery_monitor.below_since` (delayed save, immediate save on unload so a reload never resets it).
- A battery already low when first seen (not in the store): `below_since` is read from the recorder history (up to `MAX_DURATION` h): right after its last state above the threshold, or its first recorded state when low all along. Recorded states are `LazyState` (no `.domain`): read the domain from the entity ID.
- *Due*: low for at least `duration` hours. Only due batteries are notified on schedule; the button and the action (`manual=True`) send every low battery, or a *none* message.
- Notifications at `morning_time` and `evening_time` (local time), from the button and from the `battery_monitor.notify` action: persistent notification id `battery_monitor_low` (replaced; dismissed when nothing is due) + each `notify.*` action. Texts in translations `exceptions.*` (rendered with `str.format`). A failing notifier is logged and doesn't stop the others.
- The list is rebuilt on every battery state change and every 10 minutes (durations move on without state changes). Sorted low first, lowest level first.
- The card is served at `/battery_monitor/battery-monitor-card.js` and added to the dashboard resources (storage mode) or loaded with `add_extra_js_url` (YAML mode); removed with the entry.

## Release
Bump the version in `manifest.json`, `const.py` (`VERSION`) and the card (`BATTERY_MONITOR_CARD_VERSION`), add its CHANGELOG.md section; pushing to main publishes `vX.Y.Z` with `battery_monitor.zip` when that tag doesn't exist yet. Rerun notes with the "Update release notes" workflow (input `tag`).

## Tests
`pip install -r requirements_test.txt && pytest` — batteries are registry entities on mock devices (`tests/conftest.py`), notify actions are mocked, time is moved with `freezer`.
Screenshots: `python scripts/screenshots.py` (Playwright, mock batteries, light EN / dark FR).
