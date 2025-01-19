"""Class used to communicate with xComfort bridge."""

from __future__ import annotations

import asyncio
import logging

from xcomfort.bridge import Bridge

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .xcomfort_binary_sensor import BinarySensor

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


"""Wrapper class over bridge library to emulate hub."""


class XComfortHub:
    def __init__(self, hass: HomeAssistant, identifier: str, ip: str, auth_key: str):
        """Initialize underlying bridge"""
        bridge = XComfortBridge(hass.bus, ip, auth_key)
        self.hass = hass
        self.bridge = bridge
        self.identifier = identifier
        if self.identifier is None:
            self.identifier = ip
        self._id = ip
        self.devices = list()
        self._loop = asyncio.get_event_loop()

        self.has_done_initial_load = asyncio.Event()

    def start(self):
        """Starts the event loop running the bridge."""
        self.hass.async_create_task(self.bridge.run())

    async def stop(self):
        """Stops the bridge event loop.
        Will also shut down websocket, if open.
        """
        self.has_done_initial_load.clear()
        await self.bridge.close()

    async def load_devices(self):
        """Loads devices from bridge."""
        devs = await self.bridge.get_devices()
        self.devices = devs.values()

        _LOGGER.info(f"loaded {len(self.devices)} devices")

        rooms = await self.bridge.get_rooms()
        self.rooms = rooms.values()

        _LOGGER.info(f"loaded {len(self.rooms)} rooms")

        self.has_done_initial_load.set()

    def get_component_name(self, comp_id):
        if comp_id in self.bridge._comps:
            return self.bridge._comps[comp_id].name
        else:
            return None

    @property
    def hub_id(self) -> str:
        return self._id

    async def test_connection(self) -> bool:
        await asyncio.sleep(1)
        return True

    @staticmethod
    def get_hub(hass: HomeAssistant, entry: ConfigEntry) -> XComfortHub:
        return hass.data[DOMAIN][entry.entry_id]

class XComfortBridge(Bridge):
    def __init__(self, bus: EventBus, ip_address: str, authkey: str):
        super().__init__(ip_address, authkey)

        self.bus = bus

    def _create_device_from_payload(self, payload):
        dev_type = payload["devType"]
        if dev_type == 220:
            # Rocker switch
            device_id = payload['deviceId']
            name = payload['name']
            comp_id = payload["compId"]
            return BinarySensor(self, device_id, name, comp_id)

        return super()._create_device_from_payload(payload)