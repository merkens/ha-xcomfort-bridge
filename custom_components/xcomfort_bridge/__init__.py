"""Home Assistant integration setup for Eaton xComfort Bridge."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity_platform import async_get_platforms
from homeassistant.helpers.event import async_call_later

from .bridge_client import (
    XComfortAuthenticationError,
    XComfortBridgeClient,
    XComfortConnectionError,
)
from .const import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)

# Seconds to wait after inventory growth or a registry removal before
# re-listing orphans, so entities still being added aren't reported.
ORPHAN_ISSUE_REFRESH_DELAY = 15
ORPHAN_ISSUE_MAX_LINES = 25


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the integration domain."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up xComfort Bridge from a config entry."""
    client = XComfortBridgeClient(hass, entry)

    try:
        await client.async_start()
    except XComfortAuthenticationError as exc:
        await client.async_stop()
        raise ConfigEntryAuthFailed(str(exc)) from exc
    except XComfortConnectionError as exc:
        await client.async_stop()
        raise ConfigEntryNotReady(str(exc)) from exc

    hass.data[DOMAIN][entry.entry_id] = client
    entry.runtime_data = client

    _async_register_bridge_devices(hass, entry, client)
    # Subscribed before the platforms so a slave bridge that appears later is
    # registered before its components' entities look up their parent.
    entry.async_on_unload(
        client.subscribe_inventory_updates(
            lambda: _async_register_bridge_devices(hass, entry, client)
        )
    )

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await client.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise

    client.async_mark_discovery_started()
    _async_update_orphan_issue(hass, entry, client)
    _async_track_orphans(hass, entry, client)
    _LOGGER.debug("xComfort Bridge entry %s set up for host %s", entry.entry_id, client.host)
    return True


@callback
def _async_register_bridge_devices(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: XComfortBridgeClient,
) -> None:
    """Register the primary bridge and every known slave bridge (idempotent)."""
    device_registry = dr.async_get(hass)
    bridge_name = client.bridge_name or entry.title
    bridge_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, client.bridge_device_identifier)},
        manufacturer="Eaton",
        model=client.bridge_model,
        name=bridge_name,
        serial_number=client.bridge_id,
        sw_version=client.firmware_version,
    )
    # Update name if it was previously created with a stale value (e.g. IP)
    if bridge_device.name != bridge_name:
        device_registry.async_update_device(bridge_device.id, name=bridge_name)

    bridge_device_ids = {client.bridge_device_identifier: bridge_device.id}
    for client_id, slave_info in client.slave_bridges.items():
        slave_id = slave_info.get("mdnsName", f"slave_{client_id}")
        raw_fw = slave_info.get("fwVersion")
        slave_fw = f"{raw_fw // 100}.{raw_fw % 100:02d}" if isinstance(raw_fw, int) else None
        slave_device = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, slave_id)},
            manufacturer="Eaton",
            model="xComfort Bridge",
            name=slave_info.get("name", f"xComfort Bridge (slave {client_id})"),
            sw_version=slave_fw,
        )
        # Linked separately: async_get_or_create's via_device is deprecated and
        # its via_device_id replacement only exists from HA 2026.8.
        if slave_device.via_device_id != bridge_device.id:
            device_registry.async_update_device(
                slave_device.id, via_device_id=bridge_device.id
            )
        bridge_device_ids[slave_id] = slave_device.id

    # A component can be reported before its slave bridge's record (the slave
    # list comes last in a data dump), so its device may have been linked to
    # the primary bridge; re-point any component under the wrong bridge.
    parents = client.get_component_parent_identifiers()
    for device_entry in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        for domain, identifier in device_entry.identifiers:
            if domain != DOMAIN or identifier not in parents:
                continue
            parent_id = bridge_device_ids.get(parents[identifier])
            if parent_id is not None and device_entry.via_device_id != parent_id:
                device_registry.async_update_device(device_entry.id, via_device_id=parent_id)


def _orphan_issue_id(entry: ConfigEntry) -> str:
    """Return the repair issue id for an entry's orphan list."""
    return f"orphaned_{entry.entry_id}"


@callback
def _async_update_orphan_issue(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: XComfortBridgeClient,
) -> None:
    """Raise one repair issue listing registry entries the bridge no longer backs.

    Topology changes on the bridge (a device unpaired, a room losing its room
    sensor, an actuator changing usage) leave registry entries behind. Nothing
    is deleted automatically: a bridge cold boot can briefly under-report, and
    deleting would lose the user's names and areas.
    """
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    known = client.get_known_device_identifiers()

    orphan_devices: set[str] = set()
    lines: list[str] = []
    for device_entry in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        identifiers = {
            identifier
            for domain, identifier in device_entry.identifiers
            if domain == DOMAIN
        }
        if identifiers and not identifiers & known:
            orphan_devices.add(device_entry.id)
            lines.append(f"- Device: {device_entry.name_by_user or device_entry.name}")

    provided = {
        entity_id
        for platform in async_get_platforms(hass, DOMAIN)
        if platform.config_entry is not None
        and platform.config_entry.entry_id == entry.entry_id
        for entity_id in platform.entities
    }
    for entity_entry in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if entity_entry.disabled_by is not None or entity_entry.entity_id in provided:
            continue
        if entity_entry.device_id in orphan_devices:
            continue  # listed with its device
        lines.append(f"- Entity: {entity_entry.entity_id}")

    issue_id = _orphan_issue_id(entry)
    if not lines:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return

    _LOGGER.debug("%d registry entries are no longer backed by the bridge", len(lines))
    shown = lines[:ORPHAN_ISSUE_MAX_LINES]
    if len(lines) > ORPHAN_ISSUE_MAX_LINES:
        shown.append(f"- … and {len(lines) - ORPHAN_ISSUE_MAX_LINES} more")
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="orphaned_entities",
        translation_placeholders={"bridge": entry.title, "items": "\n".join(shown)},
    )


@callback
def _async_track_orphans(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: XComfortBridgeClient,
) -> None:
    """Refresh the orphan issue after inventory growth or registry removals.

    Debounced so entities that runtime discovery is still adding (e.g. battery
    sensors after a bridge cold boot) aren't reported as orphans.
    """
    cancel_refresh: CALLBACK_TYPE | None = None

    @callback
    def _refresh(_now: Any = None) -> None:
        nonlocal cancel_refresh
        cancel_refresh = None
        _async_update_orphan_issue(hass, entry, client)

    @callback
    def _schedule() -> None:
        nonlocal cancel_refresh
        # Keep a pending refresh rather than postponing it, so a steady stream
        # of events can't delay the refresh indefinitely.
        if cancel_refresh is None:
            cancel_refresh = async_call_later(hass, ORPHAN_ISSUE_REFRESH_DELAY, _refresh)

    @callback
    def _on_registry_updated(event: Event) -> None:
        if event.data.get("action") == "remove":
            _schedule()

    @callback
    def _cancel() -> None:
        if cancel_refresh is not None:
            cancel_refresh()

    entry.async_on_unload(client.subscribe_inventory_updates(_schedule))
    entry.async_on_unload(
        hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _on_registry_updated)
    )
    entry.async_on_unload(
        hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, _on_registry_updated)
    )
    entry.async_on_unload(_cancel)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    client: XComfortBridgeClient = entry.runtime_data

    await client.async_stop()

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the entry's repair issue when the entry is deleted."""
    ir.async_delete_issue(hass, DOMAIN, _orphan_issue_id(entry))


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow deleting a device once the bridge no longer provides it.

    Returns True (deletable) when none of the device's identifiers match a
    device/room/bridge the bridge currently reports, so stale entries left after
    a device is removed from the bridge can be cleaned from the UI. Live devices
    return False and cannot be deleted.
    """
    client: XComfortBridgeClient | None = getattr(config_entry, "runtime_data", None)
    if client is None:
        # Entry not loaded — nothing is currently provided, so allow removal.
        return True
    known = client.get_known_device_identifiers()
    return not any(
        domain == DOMAIN and identifier in known
        for domain, identifier in device_entry.identifiers
    )
