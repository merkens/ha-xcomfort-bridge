"""Light platform for the Eaton xComfort Bridge integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .bridge_client import XComfortBridgeClient
from .const import DEVTYPE_DIMMER, DOMAIN, DP_ON_OFF


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up xComfort light entities for a config entry."""
    client: XComfortBridgeClient = entry.runtime_data
    async_add_entities(
        [XComfortLightEntity(client, device_id) for device_id in client.get_light_device_ids()]
    )


class XComfortLightEntity(LightEntity):
    """Represent a switch or dimmer exposed by the bridge."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, client: XComfortBridgeClient, device_id: int) -> None:
        """Initialize the light entity."""
        self._client = client
        self._device_id = device_id
        self._attr_name = None
        self._attr_unique_id = f"{DOMAIN}_{client.unique_prefix}_light_{device_id}"

        device = client.get_device(device_id) or {}
        is_dimmer_hardware = device.get("devType") == DEVTYPE_DIMMER
        dimming_profile = device.get("dp")
        self._is_dimmer = is_dimmer_hardware and dimming_profile != DP_ON_OFF
        self._attr_supported_color_modes = (
            {ColorMode.BRIGHTNESS} if self._is_dimmer else {ColorMode.ONOFF}
        )

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
        """Return whether the light is on."""
        device = self._client.get_device(self._device_id) or {}
        return bool(device.get("switch"))

    @property
    def brightness(self) -> int | None:
        """Return the brightness mapped to Home Assistant's 0-255 scale."""
        if not self._is_dimmer:
            return None

        device = self._client.get_device(self._device_id) or {}
        dimmvalue = int(device.get("dimmvalue", 0))
        return round((max(0, min(100, dimmvalue)) / 100) * 255)

    @property
    def color_mode(self) -> ColorMode:
        """Return the active color mode."""
        return ColorMode.BRIGHTNESS if self._is_dimmer else ColorMode.ONOFF

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return dimming profile attributes."""
        device = self._client.get_device(self._device_id) or {}
        attrs: dict[str, Any] = {}
        if (dp := device.get("dp")) is not None:
            attrs["dimming_profile"] = dp
        if (min_level := device.get("min")) is not None:
            attrs["min_dim_level"] = min_level
        if (list_dp := device.get("listDp")) is not None:
            attrs["available_dimming_profiles"] = list_dp
        return attrs or None

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the device on."""
        if ATTR_BRIGHTNESS in kwargs and self._is_dimmer:
            brightness = max(1, round((kwargs[ATTR_BRIGHTNESS] / 255) * 100))
            await self._client.async_set_dimmer_level(self._device_id, brightness)
            return

        await self._client.async_switch_device(self._device_id, True)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the device off."""
        await self._client.async_switch_device(self._device_id, False)
