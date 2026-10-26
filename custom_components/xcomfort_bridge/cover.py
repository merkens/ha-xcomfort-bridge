"""Cover platform for the Eaton xComfort Bridge integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .bridge_client import XComfortBridgeClient
from .const import DOMAIN
from .discovery import async_setup_dynamic_entities

# Shading type configured in the Eaton app (shType, APK `gm` enum).
SHADING_TYPE_DEVICE_CLASS: dict[int, CoverDeviceClass] = {
    1: CoverDeviceClass.SHADE,  # zip screen
    2: CoverDeviceClass.SHUTTER,  # shutter blinds
    3: CoverDeviceClass.AWNING,
    4: CoverDeviceClass.BLIND,  # vertical blinds
    5: CoverDeviceClass.SHADE,  # roller blinds
    6: CoverDeviceClass.SHADE,  # pleated blinds
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up xComfort cover entities, including devices added later."""
    client: XComfortBridgeClient = entry.runtime_data
    async_setup_dynamic_entities(
        entry,
        client,
        async_add_entities,
        lambda c: c.get_cover_device_ids(),
        lambda device_id: XComfortCoverEntity(client, device_id),
    )


class XComfortCoverEntity(CoverEntity):
    """Represent a shading actuator exposed by the bridge."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, client: XComfortBridgeClient, device_id: int) -> None:
        """Initialize the cover entity."""
        self._client = client
        self._device_id = device_id
        self._attr_name = None
        self._attr_unique_id = f"{DOMAIN}_{client.unique_prefix}_cover_{device_id}"

        features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.STOP
        )
        if client.cover_supports_position(device_id):
            features |= CoverEntityFeature.SET_POSITION
        self._attr_supported_features = features

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
    def device_class(self) -> CoverDeviceClass:
        """Return the cover type configured in the Eaton app."""
        shading_type = self._client.get_cover_shading_type(self._device_id)
        return SHADING_TYPE_DEVICE_CLASS.get(shading_type, CoverDeviceClass.SHUTTER)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the safety function setting and the live safety lock."""
        safety_function = self._client.get_cover_safety_function(self._device_id)
        if safety_function is None:
            return None
        return {
            "safety_function": safety_function,
            "safety_locked": self._client.get_cover_safety_locked(self._device_id),
        }

    @property
    def current_cover_position(self) -> int | None:
        """Return the current cover position (0 = closed, 100 = open)."""
        position = self._client.get_cover_position(self._device_id)
        if position is None:
            return None
        return 100 - position

    @property
    def is_closed(self) -> bool | None:
        """Return whether the cover is fully closed."""
        position = self._client.get_cover_position(self._device_id)
        if position is None:
            return None
        return position == 100

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        await self._client.async_open_cover(self._device_id)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        await self._client.async_close_cover(self._device_id)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        await self._client.async_stop_cover(self._device_id)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position."""
        if (position := kwargs.get(ATTR_POSITION)) is not None:
            await self._client.async_set_cover_position(self._device_id, 100 - position)
