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

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub = XComfortHub.get_hub(hass, entry)
    devices = hub.devices
    log(f"Found {len(devices)} xcomfort devices")

    entities = []
    for device in devices:
        if isinstance(device, BinarySensor):
            log(f"Adding {device}")
            entity = HASSXComfortBinarySensor(hass, hub, device)
            entities.append(entity)

    log(f"Added {len(entities)} entities")
    async_add_entities(entities)

class HASSXComfortBinarySensor(BinarySensorEntity):
    def __init__(self, hass: HomeAssistant, hub: XComfortHub, device: BinarySensor):
        self.hass = hass
        self._hub = hub
        self._device = device
        self._state = False
        self._last_button_pressed = "none"
        self._name = device.name
        self._unique_id = f"binary_sensor_{DOMAIN}_{hub.identifier}-{device.device_id}"

        comp_name = hub.get_component_name(device.comp_id)
        if comp_name is not None:
            self._name = f"{comp_name} - {self._name}"

    async def async_added_to_hass(self):
        log(f"Added binary sensor to hass: {self._name}")
        self._device.subscribe(self._state_change)
        log(f"Subscribed {self._name} to state changes.")

    def _state_change(self, button_pressed):
        log(f"_state_change: Entry. Sensor={self._name}, Button Pressed={'top' if button_pressed else 'bottom'}")
        self._state = True
        self._last_button_pressed = "top" if button_pressed else "bottom"
        log(f"_state_change: State updated. Sensor={self._name}, _state={self._state}, _last_button_pressed={self._last_button_pressed}")
        self.async_write_ha_state()
        self.hass.loop.create_task(self.async_reset_state())
        log(f"_state_change: Exit. Sensor={self._name}")


    async def async_reset_state(self):
        await asyncio.sleep(0.5)
        self._state = False
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
