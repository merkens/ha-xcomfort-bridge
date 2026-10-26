"""Binary sensor platform for the Eaton xComfort Bridge integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .bridge_client import XComfortBridgeClient
from .const import DOMAIN
from .discovery import async_setup_dynamic_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up xComfort binary sensor entities, including devices added later."""
    client: XComfortBridgeClient = entry.runtime_data

    async_setup_dynamic_entities(
        entry,
        client,
        async_add_entities,
        lambda c: c.get_binary_sensor_device_ids(),
        lambda device_id: XComfortDoorWindowSensor(client, device_id),
    )
    async_setup_dynamic_entities(
        entry,
        client,
        async_add_entities,
        lambda c: c.get_binary_input_device_ids(),
        lambda device_id: XComfortBinaryInputSensor(client, device_id),
    )
    async_setup_dynamic_entities(
        entry,
        client,
        async_add_entities,
        lambda c: c.get_sum_request_device_ids(),
        lambda device_id: XComfortSumRequestBinarySensor(client, device_id),
    )


class XComfortDoorWindowSensor(BinarySensorEntity):
    """Represent a door or window contact (devType 202 on a compType 76 component).

    Modelled on the Eaton app; not yet validated on real hardware.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, client: XComfortBridgeClient, device_id: int) -> None:
        """Initialize the binary sensor."""
        self._client = client
        self._device_id = device_id
        self._attr_name = None
        self._attr_unique_id = f"{DOMAIN}_{client.unique_prefix}_binary_{device_id}"

    async def async_added_to_hass(self) -> None:
        """Subscribe to runtime updates when the entity is added."""
        # The open/closed state lives in the component info; component updates
        # are dispatched to the component's device listeners too.
        self.async_on_remove(
            self._client.subscribe_device_updates(self._device_id, self._handle_device_update)
        )

    @property
    def device_class(self) -> BinarySensorDeviceClass:
        """Return door or window, from the component mode set in the app."""
        if self._client.get_binary_sensor_is_door(self._device_id):
            return BinarySensorDeviceClass.DOOR
        return BinarySensorDeviceClass.WINDOW

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


class XComfortBinaryInputSensor(BinarySensorEntity):
    """Represent one channel of a binary-input module (devType 211).

    Exposes the raw contact state (``curstate``) without a device class. The
    module cannot tell whether a channel is a door, window, or generic contact,
    and the wiring polarity is unknown, so the user assigns a device class in HA.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, client: XComfortBridgeClient, device_id: int) -> None:
        """Initialize the binary-input channel sensor."""
        self._client = client
        self._device_id = device_id
        self._attr_name = client.get_device_name(device_id)
        self._attr_unique_id = f"{DOMAIN}_{client.unique_prefix}_binary_input_{device_id}"

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
        """Return whether the channel is currently available."""
        return self._client.available and self._client.get_device(self._device_id) is not None

    @property
    def is_on(self) -> bool | None:
        """Return the raw contact state (on = curstate 1)."""
        return self._client.get_binary_input_is_on(self._device_id)

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device registry metadata."""
        return self._client.get_component_device_info(self._device_id)


class XComfortSumRequestBinarySensor(BinarySensorEntity):
    """Represent the sum-request (heat demand) output of a heating hub."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Heat demand"
    _attr_device_class = BinarySensorDeviceClass.HEAT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, client: XComfortBridgeClient, device_id: int) -> None:
        """Initialize the sum-request binary sensor."""
        self._client = client
        self._device_id = device_id
        comp_id = client.get_component_id(device_id)
        self._attr_unique_id = (
            f"{DOMAIN}_{client.unique_prefix}_comp_{comp_id}_sum_request"
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to bridge-level updates when the entity is added."""
        self.async_on_remove(
            self._client.subscribe_bridge_updates(self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        """Write updated state when bridge state changes."""
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return whether the sensor is currently available.

        A hub that exists but has not reported a sum-request value yet
        renders as unknown rather than unavailable.
        """
        return self._client.available and self._client.get_device(self._device_id) is not None

    @property
    def is_on(self) -> bool | None:
        """Return whether any heating channel is currently requesting heat."""
        value = self._client.get_component_sum_request(self._device_id)
        if value is None:
            return None
        return value > 0

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device registry metadata."""
        return self._client.get_component_device_info(self._device_id)
