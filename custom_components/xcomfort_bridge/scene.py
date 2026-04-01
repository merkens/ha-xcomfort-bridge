"""Scene platform for the Eaton xComfort Bridge integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.scene import Scene
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .bridge_client import XComfortBridgeClient
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up xComfort scene entities for a config entry."""
    client: XComfortBridgeClient = entry.runtime_data
    async_add_entities(
        [XComfortSceneEntity(client, scene_id) for scene_id in client.get_scene_ids()]
    )


class XComfortSceneEntity(Scene):
    """Represent a bridge scene as an HA scene entity."""

    _attr_has_entity_name = True

    def __init__(self, client: XComfortBridgeClient, scene_id: int) -> None:
        """Initialize the scene entity."""
        self._client = client
        self._scene_id = scene_id
        scene = client.get_scene(scene_id)
        self._attr_name = scene["name"] if scene else f"Scene {scene_id}"
        self._attr_unique_id = f"{DOMAIN}_{client.unique_prefix}_scene_{scene_id}"

    async def async_activate(self, **kwargs: Any) -> None:
        """Activate the scene on the bridge."""
        await self._client.async_activate_scene(self._scene_id)

    @property
    def device_info(self) -> DeviceInfo:
        """Return the bridge device as parent."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._client.bridge_device_identifier)},
        )
