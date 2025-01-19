"""Support for Xcomfort sensors."""

from __future__ import annotations

import logging
import math
import time
from typing import cast

from xcomfort.bridge import Room

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy

from homeassistant.const import (
    UnitOfTemperature,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .hub import XComfortHub

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub = XComfortHub.get_hub(hass, entry)

    async def _wait_for_hub_then_setup():
        await hub.has_done_initial_load.wait()

        rooms = hub.rooms
        devices = hub.devices

        _LOGGER.debug(f"Found {len(rooms)} xcomfort rooms")
        _LOGGER.debug(f"Found {len(devices)} xcomfort devices")

        sensors = list()
        for room in rooms:
            if room.state.value is not None:
                if room.state.value.power is not None:
                    _LOGGER.debug(f"Adding energy and power sensors for room {room.name}")
                    sensors.append(XComfortPowerSensor(hub, room))
                    sensors.append(XComfortEnergySensor(hub, room))

                if room.state.value.humidity is not None:
                    _LOGGER.debug(f"Adding humidity sensor for room {room.name}")
                    sensors.append(XComfortHumiditySensor(hub, room))

                if room.state.value.temperature is not None:
                    _LOGGER.debug(f"Adding temperature sensor for room {room.name}")
                    sensors.append(XComfortTemperatureSensor(hub, room))

        _LOGGER.debug(f"Added {len(sensors)} rc touch units")
        async_add_entities(sensors)

    entry.async_create_task(hass, _wait_for_hub_then_setup())


class XComfortPowerSensor(SensorEntity):
    def __init__(self, hub: XComfortHub, room: Room):
        self.entity_description = SensorEntityDescription(
            key="current_consumption",
            device_class=SensorDeviceClass.POWER,
            native_unit_of_measurement=UnitOfPower.WATT,
            state_class=SensorStateClass.MEASUREMENT,
            name="Current consumption",
        )
        self.hub = hub
        self._room = room
        self._attr_name = f"{self._room.name} Power"
        self._attr_unique_id = f"energy_{self._room.room_id}"
        self._state = None

    async def async_added_to_hass(self):
        _LOGGER.debug(f"Added to hass {self._attr_name} ")
        if self._room.state is None:
            _LOGGER.warning(f"State is null for {self._attr_name}")
        else:
            self._room.state.subscribe(lambda state: self._state_change(state))

    def _state_change(self, state):
        self._state = state
        should_update = self._state is not None
        if should_update:
            self.async_write_ha_state()

    @property
    def native_value(self):
        return self._state and self._state.power


class XComfortEnergySensor(RestoreSensor):
    def __init__(self, hub: XComfortHub, room: Room):
        self.entity_description = SensorEntityDescription(
            key="energy_used",
            device_class=SensorDeviceClass.ENERGY,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            state_class=SensorStateClass.TOTAL_INCREASING,
            name="Energy consumption",
        )
        self.hub = hub
        self._room = room
        self._attr_name = f"{self._room.name} Energy"
        self._attr_unique_id = f"energy_kwh_{self._room.room_id}"
        self._state = None
        self._room.state.subscribe(lambda state: self._state_change(state))
        self._updateTime = time.monotonic()
        self._consumption = 0

    async def async_added_to_hass(self) -> None:
        """Call when entity about to be added to hass."""
        await super().async_added_to_hass()
        savedstate = await self.async_get_last_sensor_data()
        if savedstate:
            self._consumption = cast(float, savedstate.native_value)

    def _state_change(self, state):
        should_update = self._state is not None
        self._state = state
        if should_update:
            self.async_write_ha_state()

    def calculate(self, power):
        now = time.monotonic()
        timediff = math.floor(
            now - self._updateTime
        )  # number of seconds since last update
        self._consumption += (
            power / 3600 / 1000 * timediff
        )  # Calculate, in kWh, energy consumption since last update.
        self._updateTime = now

    @property
    def native_value(self):
        if self._state and self._state.power is not None:
            self.calculate(self._state.power)
            return self._consumption
        return None


class XComfortHumiditySensor(SensorEntity):
    def __init__(self, hub: XComfortHub, room: Room):
        self.entity_description = SensorEntityDescription(
            key="humidity",
            device_class=SensorDeviceClass.HUMIDITY,
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            name="Humidity",
        )
        self.hub = hub
        self._room = room
        self._attr_name = f"{self._room.name} Humidity"
        self._attr_unique_id = f"humidity_{self._room.room_id}"
        self._state = None

    async def async_added_to_hass(self):
        _LOGGER.debug(f"Added to hass {self._attr_name} ")
        if self._room.state is None:
            _LOGGER.warning(f"State is null for {self._attr_name}")
        else:
            self._room.state.subscribe(lambda state: self._state_change(state))

    def _state_change(self, state):
        self._state = state
        should_update = self._state is not None
        _LOGGER.debug(f"State changed {self._attr_name} : {state}")
        if should_update:
            self.async_write_ha_state()

    @property
    def native_value(self):
        return self._state and self._state.humidity


class XComfortTemperatureSensor(SensorEntity):
    def __init__(self, hub: XComfortHub, room: Room):
        self._attr_device_class = SensorEntityDescription(
            key="temperature",
            device_class=SensorDeviceClass.TEMPERATURE,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            state_class=SensorStateClass.MEASUREMENT,
            name="Temperature",)
        self.hub = hub
        self._room = room
        self._attr_name = f"{self._room.name} Temperature"
        self._attr_unique_id = f"temperature_{self._room.room_id}"
        self._state = None

    async def async_added_to_hass(self):
        _LOGGER.debug(f"Added to hass {self._attr_name} ")
        if self._room.state is None:
            _LOGGER.debug(f"State is null for {self._attr_name}")
        else:
            self._room.state.subscribe(lambda state: self._state_change(state))

    def _state_change(self, state):
        self._state = state
        should_update = self._state is not None
        _LOGGER.debug(f"State changed {self._attr_name} : {state}")
        if should_update:
            self.async_write_ha_state()

    @property
    def device_class(self):
        return SensorDeviceClass.TEMPERATURE

    @property
    def native_unit_of_measurement(self):
        return UnitOfTemperature.CELSIUS

    @property
    def native_value(self):
        if self._state is None:
            return None
        return self._state.temperature
