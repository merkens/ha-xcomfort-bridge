"""Home Assistant integration setup for Eaton xComfort Bridge."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .bridge_client import (
    XComfortAuthenticationError,
    XComfortBridgeClient,
    XComfortConnectionError,
)
from .const import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)


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

    for _client_id, slave_info in client.slave_bridges.items():
        slave_id = slave_info.get("mdnsName", f"slave_{_client_id}")
        raw_fw = slave_info.get("fwVersion")
        slave_fw = f"{raw_fw // 100}.{raw_fw % 100:02d}" if isinstance(raw_fw, int) else None
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, slave_id)},
            manufacturer="Eaton",
            model="xComfort Bridge",
            name=slave_info.get("name", f"xComfort Bridge (slave {_client_id})"),

            sw_version=slave_fw,
            via_device=(DOMAIN, client.bridge_device_identifier),
        )

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await client.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    _LOGGER.debug("xComfort Bridge entry %s set up for host %s", entry.entry_id, client.host)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    client: XComfortBridgeClient = entry.runtime_data

    await client.async_stop()

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload a config entry after an update."""
    await hass.config_entries.async_reload(entry.entry_id)
