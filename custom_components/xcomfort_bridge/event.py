"""Event platform for the Eaton xComfort Bridge integration."""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .bridge_client import ButtonEvent, XComfortBridgeClient
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up xComfort event entities for a config entry."""
    client: XComfortBridgeClient = entry.runtime_data
    async_add_entities(
        [XComfortEventEntity(client, device_id) for device_id in client.get_event_device_ids()]
    )


class XComfortEventEntity(EventEntity):
    """Represent a bridge pushbutton or rocker as an HA event entity."""

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = ["press_up", "press_down"]
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, client: XComfortBridgeClient, device_id: int) -> None:
        """Initialize the event entity."""
        self._client = client
        self._device_id = device_id
        self._attr_name = client.get_device_name(device_id)
        self._attr_unique_id = f"{DOMAIN}_{client.unique_prefix}_event_{device_id}"

    async def async_added_to_hass(self) -> None:
        """Subscribe to runtime updates when the entity is added."""
        self.async_on_remove(
            self._client.subscribe_device_updates(self._device_id, self._handle_device_update)
        )
        self.async_on_remove(
            self._client.subscribe_button_events(self._device_id, self._handle_button_event)
        )

    @callback
    def _handle_device_update(self) -> None:
        """Update availability when the runtime changes."""
        self.async_write_ha_state()

    @callback
    def _handle_button_event(self, event: ButtonEvent) -> None:
        """Emit a Home Assistant event from a bridge button update."""
        self._trigger_event(event.event_type, {"value": event.value})
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return whether the event device is currently available."""
        return self._client.available and self._client.get_device(self._device_id) is not None

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device registry metadata."""
        return self._client.get_component_device_info(self._device_id)
