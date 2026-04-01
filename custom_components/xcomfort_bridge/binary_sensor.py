"""Binary sensor platform for the Eaton xComfort Bridge integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .bridge_client import XComfortBridgeClient
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up xComfort binary sensor entities for a config entry."""
    client: XComfortBridgeClient = entry.runtime_data
    async_add_entities(
        [
            XComfortDoorWindowSensor(client, device_id)
            for device_id in client.get_binary_sensor_device_ids()
        ]
    )


class XComfortDoorWindowSensor(BinarySensorEntity):
    """Represent a door or window contact sensor."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, client: XComfortBridgeClient, device_id: int) -> None:
        """Initialize the binary sensor."""
        self._client = client
        self._device_id = device_id
        self._attr_name = None
        self._attr_unique_id = f"{DOMAIN}_{client.unique_prefix}_binary_{device_id}"
        self._attr_device_class = (
            BinarySensorDeviceClass.DOOR
            if client.get_binary_sensor_is_door(device_id)
            else BinarySensorDeviceClass.WINDOW
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to runtime updates when the entity is added."""
        self.async_on_remove(
            self._client.subscribe_device_updates(self._device_id, self._handle_device_update)
        )

    @callback
    def _handle_device_update(self) -> None:
        """Write updated state when the runtime changes."""
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return whether the sensor is currently available."""
        return self._client.available and self._client.get_device(self._device_id) is not None

    @property
    def is_on(self) -> bool | None:
        """Return whether the contact is open."""
        return self._client.get_binary_sensor_is_on(self._device_id)

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device registry metadata."""
        return self._client.get_component_device_info(self._device_id)
