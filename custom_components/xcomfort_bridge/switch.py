"""Switch platform for the Eaton xComfort Bridge integration.

Switching and dimming actuators whose bridge `usage` is not LIGHT (loads,
appliances, heating/cooling outputs, ...) are exposed as switches so they stay
out of light groups, "all lights off" and voice-assistant light commands.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .bridge_client import XComfortBridgeClient
from .const import DOMAIN, USAGE_LABELS
from .discovery import async_setup_dynamic_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up xComfort switch entities, including devices added later."""
    client: XComfortBridgeClient = entry.runtime_data
    async_setup_dynamic_entities(
        entry,
        client,
        async_add_entities,
        lambda c: c.get_switch_device_ids(),
        lambda device_id: XComfortSwitchEntity(client, device_id),
    )


class XComfortSwitchEntity(SwitchEntity):
    """Represent an actuator output that drives a non-light load."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, client: XComfortBridgeClient, device_id: int) -> None:
        """Initialize the switch entity."""
        self._client = client
        self._device_id = device_id
        self._attr_name = None
        self._attr_unique_id = f"{DOMAIN}_{client.unique_prefix}_switch_{device_id}"

    async def async_added_to_hass(self) -> None:
        """Subscribe to runtime updates when the entity is added."""
        self.async_on_remove(
            self._client.subscribe_device_updates(self._device_id, self._handle_device_update)
        )

    @callback
    def _handle_device_update(self) -> None:
        """Write updated entity state after a runtime change."""
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return whether the device is currently available."""
        return self._client.available and self._client.get_device(self._device_id) is not None

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device registry metadata."""
        return self._client.get_component_device_info(self._device_id)

    @property
    def is_on(self) -> bool:
        """Return whether the output is on."""
        device = self._client.get_device(self._device_id) or {}
        return bool(device.get("switch"))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the bridge usage that made this a switch."""
        usage = (self._client.get_device(self._device_id) or {}).get("usage")
        if usage is None:
            return None
        return {"usage": USAGE_LABELS.get(usage, str(usage))}

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the output on."""
        await self._client.async_switch_device(self._device_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the output off."""
        await self._client.async_switch_device(self._device_id, False)
