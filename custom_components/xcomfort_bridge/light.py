from functools import cached_property
import logging
from math import ceil

from xcomfort.devices import Light

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .hub import XComfortHub

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub = XComfortHub.get_hub(hass, entry)

    async def _wait_for_hub_then_setup():
        await hub.has_done_initial_load.wait()

        devices = hub.devices

        _LOGGER.debug(f"Found {len(devices)} xcomfort devices")

        lights = list()
        for device in devices:
            if isinstance(device, Light):
                _LOGGER.debug(f"Adding {device}")
                light = HASSXComfortLight(hass, hub, device)
                lights.append(light)

        _LOGGER.debug(f"Added {len(lights)} lights")
        async_add_entities(lights)

    entry.async_create_task(hass, _wait_for_hub_then_setup())


class HASSXComfortLight(LightEntity):
    def __init__(self, hass: HomeAssistant, hub: XComfortHub, device: Light):
        self.hass = hass
        self.hub = hub

        self._device = device
        self._name = device.name
        self._state = None
        self.device_id = device.device_id
        self._unique_id = f"light_{DOMAIN}_{hub.identifier}-{device.device_id}"
        self._color_mode = ColorMode.BRIGHTNESS if self._device.dimmable else ColorMode.ONOFF

    async def async_added_to_hass(self):
        _LOGGER.debug(f"Added to hass {self._name} ")
        if self._device.state is None:
            _LOGGER.debug(f"State is null for {self._name}")
        else:
            self._device.state.subscribe(lambda state: self._state_change(state))

    def _state_change(self, state):
        self._state = state

        should_update = self._state is not None

        _LOGGER.debug(f"State changed {self._name} : {state}")

        if should_update:
            self.schedule_update_ha_state()

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.unique_id)},
            "name": self.name,
            "manufacturer": "Eaton",
            "model": "XXX",
            "sw_version": "Unknown",
            "via_device": self.hub.hub_id,
        }

    @property
    def name(self):
        """Return the display name of this light."""
        return self._name

    @property
    def unique_id(self):
        """Return the unique ID."""
        return self._unique_id

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def brightness(self):
        """Return the brightness of the light.

        This method is optional. Removing it indicates to Home Assistant
        that brightness is not supported for this light.
        """
        return int(255.0 * self._state.dimmvalue / 99.0)

    @property
    def is_on(self):
        """Return true if light is on."""
        return self._state and self._state.switch

    @property
    def color_mode(self) -> ColorMode:
        return self._color_mode

    @cached_property
    def supported_color_modes(self) -> set[ColorMode] | set[str] | None:
        return {self._color_mode}

    async def async_turn_on(self, **kwargs):
        _LOGGER.debug(f"async_turn_on {self._name} : {kwargs}")
        if ATTR_BRIGHTNESS in kwargs and self._device.dimmable:
            br = ceil(kwargs[ATTR_BRIGHTNESS] * 99 / 255.0)
            _LOGGER.debug(f"async_turn_on br {self._name} : {br}")
            await self._device.dimm(br)
            self._state.dimmvalue = br
            self.schedule_update_ha_state()
            return

        switch_task = self._device.switch(True)
        await switch_task

        self._state.switch = True
        self.schedule_update_ha_state()

    async def async_turn_off(self, **kwargs):
        _LOGGER.debug(f"async_turn_off {self._name} : {kwargs}")
        switch_task = self._device.switch(False)
        await switch_task

        self._state.switch = False
        self.schedule_update_ha_state()

    def update(self):
        pass
