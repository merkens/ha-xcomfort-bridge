import logging
import asyncio
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, VERBOSE
from .hub import XComfortHub, BinarySensor

_LOGGER = logging.getLogger(__name__)

def log(msg: str):
    if VERBOSE:
        _LOGGER.info(msg)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub = XComfortHub.get_hub(hass, entry)

    devices = hub.devices

    _LOGGER.info(f"Found {len(devices)} xcomfort devices")

    entities = []
    for device in devices:
        if isinstance(device, BinarySensor):
            log(f"Adding {device.name}")
            entity = HASSXComfortBinarySensor(hass, hub, device)
            entities.append(entity)

    _LOGGER.info(f"Added {len(entities)} entities")
    async_add_entities(entities)

class HASSXComfortBinarySensor(BinarySensorEntity):
    def __init__(self, hass: HomeAssistant, hub: XComfortHub, device: BinarySensor):
        self.hass = hass
        self._hub = hub

        self._device = device
        self._name = device.name
        self._state = False
        self._last_button_pressed = "none"
        self._has_ignored_initial = False
        
        # Workaround for XComfort rockets switches being named just
        # "Rocker 1" etc, prefix with component name when possible
        comp_name = hub.get_component_name(device.comp_id)
        if comp_name is not None:
            self._name = f"{comp_name} - {self._name}"
        
        self._unique_id = f"binary_sensor_{DOMAIN}_{hub.identifier}-{device.device_id}"

    async def async_added_to_hass(self):
        log(f"Added to hass {self._name} ")
        if self._device.state is None:
            log(f"State is null for {self._name}")
        else:
            self._device.state.subscribe(lambda state: self._state_change(state))

    def _state_change(self, state):
        if not self._has_ignored_initial:
            log(f"Ignoring initial state change for {self._name}")
            self._has_ignored_initial = True
            return

        self._state = True
        log(f"State changed {self._name} - isTop : {state.isTop}")

        self._last_button_pressed = "top" if state.isTop else "bottom"
        self.async_write_ha_state()
        self.hass.create_task(self.async_reset_state())

    async def async_reset_state(self):
        await asyncio.sleep(0.5)
        self._state = False
        log(f"State changed {self._name} : {self._state}")
        self.async_write_ha_state()

    @property
    def is_on(self):
        return self._state

    @property
    def extra_state_attributes(self):
        return {"last_button_pressed": self._last_button_pressed}

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.unique_id)},
            "name": self.name,
            "manufacturer": "Eaton",
            "model": "XXX",
            "sw_version": "Unknown",
            "via_device": self._hub.hub_id,
        }

    @property
    def name(self):
        return self._name

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def should_poll(self) -> bool:
        return False