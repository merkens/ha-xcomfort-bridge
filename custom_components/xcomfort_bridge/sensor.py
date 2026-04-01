"""Sensor platform for the Eaton xComfort Bridge integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .bridge_client import XComfortBridgeClient
from .const import CLIMATE_MODE_LABELS, DOMAIN, SIGNAL_LABELS


# ---------------------------------------------------------------------------
# Component-scoped sensors (keyed by device_id, grouped under component)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ComponentSensorSpec:
    """Describe a component-scoped sensor entity."""

    key: str
    name: str
    value_getter: Callable[[XComfortBridgeClient, int], Any]
    device_ids_getter: Callable[[XComfortBridgeClient], list[int]]
    subscribe_mode: str  # "device" or "bridge"
    unique_id_prefix: str = "device"  # "rct", "device", or "comp"
    use_comp_id: bool = False
    device_class: SensorDeviceClass | None = None
    unit: str | None = None
    state_class: SensorStateClass | None = None
    icon: str | None = None
    entity_category: EntityCategory | None = None
    options: list[str] | None = None
    extra_available_check: Callable[[XComfortBridgeClient, int], bool] | None = None


COMPONENT_SENSOR_SPECS: tuple[ComponentSensorSpec, ...] = (
    ComponentSensorSpec(
        key="temperature",
        name="Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        subscribe_mode="device",
        unique_id_prefix="rct",
        value_getter=lambda client, device_id: client.get_rct_temperature(device_id),
        device_ids_getter=lambda client: client.get_rct_device_ids(),
        extra_available_check=lambda client, device_id: (
            client.get_device(device_id) is not None
            and client.get_linked_room_id(device_id) is not None
        ),
    ),
    ComponentSensorSpec(
        key="humidity",
        name="Humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        unit=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        subscribe_mode="device",
        unique_id_prefix="rct",
        value_getter=lambda client, device_id: client.get_rct_humidity(device_id),
        device_ids_getter=lambda client: client.get_rct_device_ids(),
        extra_available_check=lambda client, device_id: (
            client.get_device(device_id) is not None
            and client.get_linked_room_id(device_id) is not None
        ),
    ),
    ComponentSensorSpec(
        key="power",
        name="Power",
        device_class=SensorDeviceClass.POWER,
        unit=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        subscribe_mode="device",
        value_getter=lambda client, device_id: client.get_device_power(device_id),
        device_ids_getter=lambda client: client.get_power_device_ids(),
    ),
    ComponentSensorSpec(
        key="temperature",
        name="Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        subscribe_mode="device",
        value_getter=lambda client, device_id: client.get_actuator_temperature(device_id),
        device_ids_getter=lambda client: client.get_actuator_temperature_device_ids(),
    ),
    ComponentSensorSpec(
        key="battery",
        name="Battery",
        device_class=SensorDeviceClass.BATTERY,
        unit=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        subscribe_mode="bridge",
        unique_id_prefix="comp",
        use_comp_id=True,
        value_getter=lambda client, device_id: client.get_component_battery_percentage(device_id),
        device_ids_getter=lambda client: client.get_battery_device_ids(),
    ),
    ComponentSensorSpec(
        key="signal",
        name="Signal",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        subscribe_mode="bridge",
        unique_id_prefix="comp",
        use_comp_id=True,
        options=list(SIGNAL_LABELS.values()),
        value_getter=lambda client, device_id: client.get_component_signal_label(device_id),
        device_ids_getter=lambda client: client.get_signal_device_ids(),
    ),
)


class XComfortComponentSensorEntity(SensorEntity):
    """Represent a sensor attached to a physical component."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, client: XComfortBridgeClient, device_id: int, spec: ComponentSensorSpec
    ) -> None:
        """Initialize the component sensor entity."""
        self._client = client
        self._device_id = device_id
        self._spec = spec

        if spec.use_comp_id:
            id_part = f"{spec.unique_id_prefix}_{client.get_component_id(device_id)}"
        else:
            id_part = f"{spec.unique_id_prefix}_{device_id}"
        self._attr_unique_id = f"{DOMAIN}_{client.unique_prefix}_{id_part}_{spec.key}"

        self.entity_description = SensorEntityDescription(
            key=spec.key,
            name=spec.name,
            device_class=spec.device_class,
            native_unit_of_measurement=spec.unit,
            state_class=spec.state_class,
            icon=spec.icon,
            entity_registry_enabled_default=True,
        )
        self._attr_name = spec.name
        self._attr_entity_category = spec.entity_category
        if spec.options is not None:
            self._attr_options = spec.options

    async def async_added_to_hass(self) -> None:
        """Subscribe to runtime updates when the entity is added."""
        if self._spec.subscribe_mode == "device":
            self.async_on_remove(
                self._client.subscribe_device_updates(
                    self._device_id, self._handle_update
                )
            )
        else:
            self.async_on_remove(
                self._client.subscribe_bridge_updates(self._handle_update)
            )

    @callback
    def _handle_update(self) -> None:
        """Write updated state when the runtime changes."""
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return whether the sensor is currently available."""
        if not self._client.available:
            return False
        if self._spec.extra_available_check is not None:
            if not self._spec.extra_available_check(self._client, self._device_id):
                return False
        return self.native_value is not None

    @property
    def native_value(self) -> Any:
        """Return the current sensor value."""
        return self._spec.value_getter(self._client, self._device_id)

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device registry metadata."""
        return self._client.get_component_device_info(self._device_id)


# ---------------------------------------------------------------------------
# Room-scoped sensors (keyed by room_id)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoomSensorSpec:
    """Describe a single room sensor entity."""

    key: str
    name: str
    value_getter: Callable[[XComfortBridgeClient, int], Any]
    icon: str | None = None
    device_class: SensorDeviceClass | None = None
    unit: str | None = None
    state_class: SensorStateClass | None = None
    requires_climate: bool = False
    requires_value: bool = False
    required_field: str | None = None


ROOM_SENSOR_SPECS: tuple[RoomSensorSpec, ...] = (
    RoomSensorSpec(
        key="lights_on",
        name="Lights On",
        icon="mdi:lightbulb-on",
        required_field="lightsOn",
        value_getter=lambda client, room_id: (client.get_room(room_id) or {}).get("lightsOn"),
    ),
    RoomSensorSpec(
        key="windows_open",
        name="Windows Open",
        icon="mdi:window-open-variant",
        required_field="windowsOpen",
        value_getter=lambda client, room_id: (client.get_room(room_id) or {}).get("windowsOpen"),
    ),
    RoomSensorSpec(
        key="doors_open",
        name="Doors Open",
        icon="mdi:door-open",
        required_field="doorsOpen",
        value_getter=lambda client, room_id: (client.get_room(room_id) or {}).get("doorsOpen"),
    ),
    RoomSensorSpec(
        key="temperature",
        name="Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        requires_value=True,
        required_field="temp",
        value_getter=lambda client, room_id: client.get_room_temperature(room_id),
    ),
    RoomSensorSpec(
        key="humidity",
        name="Humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        unit=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        requires_value=True,
        required_field="humidity",
        value_getter=lambda client, room_id: client.get_room_humidity(room_id),
    ),
    RoomSensorSpec(
        key="heating_demand",
        name="Heating Demand",
        icon="mdi:radiator",
        unit=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        requires_climate=True,
        value_getter=lambda client, room_id: client.get_room_valve(room_id),
    ),
    RoomSensorSpec(
        key="power",
        name="Power",
        device_class=SensorDeviceClass.POWER,
        unit=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        requires_climate=True,
        value_getter=lambda client, room_id: client.get_room_power(room_id),
    ),
    RoomSensorSpec(
        key="current_mode",
        name="Current Mode",
        icon="mdi:thermostat",
        requires_climate=True,
        value_getter=lambda client, room_id: CLIMATE_MODE_LABELS.get(
            client.get_room_mode_code(room_id)
        ),
    ),
)


class XComfortRoomSensorEntity(SensorEntity):
    """Represent a sensor exposed by a logical xComfort room."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, client: XComfortBridgeClient, room_id: int, spec: RoomSensorSpec) -> None:
        """Initialize the room sensor entity."""
        self._client = client
        self._room_id = room_id
        self._value_getter = spec.value_getter
        self._requires_value = spec.requires_value
        self.entity_description = SensorEntityDescription(
            key=spec.key,
            name=spec.name,
            icon=spec.icon,
            device_class=spec.device_class,
            native_unit_of_measurement=spec.unit,
            state_class=spec.state_class,
        )
        self._attr_name = spec.name
        self._attr_unique_id = f"{DOMAIN}_{client.unique_prefix}_room_{room_id}_{spec.key}"

    async def async_added_to_hass(self) -> None:
        """Subscribe to runtime updates when the entity is added."""
        self.async_on_remove(
            self._client.subscribe_room_updates(self._room_id, self._handle_room_update)
        )

    @callback
    def _handle_room_update(self) -> None:
        """Write updated state when the runtime changes."""
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return whether the room is currently available."""
        if not self._client.available or self._client.get_room(self._room_id) is None:
            return False
        if self._requires_value and self.native_value is None:
            return False
        return True

    @property
    def native_value(self) -> Any:
        """Return the current room sensor value."""
        return self._value_getter(self._client, self._room_id)

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device registry metadata."""
        return self._client.get_room_device_info(self._room_id)


# ---------------------------------------------------------------------------
# Bridge-scoped sensors (not component-keyed)
# ---------------------------------------------------------------------------


class XComfortBridgePowerSensor(SensorEntity):
    """Represent the total power consumption reported by the bridge."""

    _attr_has_entity_name = True
    _attr_name = "Power"
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, client: XComfortBridgeClient) -> None:
        """Initialize the bridge power sensor."""
        self._client = client
        self._attr_unique_id = f"{DOMAIN}_{client.unique_prefix}_bridge_power"

    async def async_added_to_hass(self) -> None:
        """Subscribe to bridge-level state updates."""
        self.async_on_remove(
            self._client.subscribe_bridge_updates(self._handle_bridge_update)
        )

    @callback
    def _handle_bridge_update(self) -> None:
        """Write updated state when the bridge state changes."""
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return whether the bridge is currently available."""
        return self._client.available and self.native_value is not None

    @property
    def native_value(self) -> float | None:
        """Return the current total power consumption."""
        return self._client.get_bridge_power()

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device registry metadata."""
        return self._client.get_bridge_device_info()


class XComfortBridgeIpSensor(SensorEntity):
    """Represent the IP address of the primary bridge."""

    _attr_has_entity_name = True
    _attr_name = "IP Address"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:ip-network"

    def __init__(self, client: XComfortBridgeClient) -> None:
        """Initialize the bridge IP sensor."""
        self._client = client
        self._attr_unique_id = f"{DOMAIN}_{client.unique_prefix}_bridge_ip"

    @property
    def available(self) -> bool:
        """Return whether the bridge is currently available."""
        return self._client.available

    @property
    def native_value(self) -> str:
        """Return the bridge IP address."""
        return self._client.host

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device registry metadata."""
        return self._client.get_bridge_device_info()


class XComfortSlaveBridgeIpSensor(SensorEntity):
    """Represent the IP address of a slave bridge."""

    _attr_has_entity_name = True
    _attr_name = "IP Address"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:ip-network"

    def __init__(self, client: XComfortBridgeClient, client_id: int) -> None:
        """Initialize the slave bridge IP sensor."""
        self._client = client
        self._client_id = client_id
        slave = client.slave_bridges.get(client_id, {})
        slave_id = slave.get("mdnsName", f"slave_{client_id}")
        self._attr_unique_id = f"{DOMAIN}_{client.unique_prefix}_slave_{slave_id}_ip"

    @property
    def available(self) -> bool:
        """Return whether the bridge is currently available."""
        return self._client.available

    @property
    def native_value(self) -> str | None:
        """Return the slave bridge IP address."""
        return self._client.get_slave_bridge_ip(self._client_id)

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device registry metadata."""
        return self._client.get_slave_bridge_device_info(self._client_id)


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up xComfort sensor entities for a config entry."""
    client: XComfortBridgeClient = entry.runtime_data

    entities: list[SensorEntity] = []

    for spec in COMPONENT_SENSOR_SPECS:
        for device_id in spec.device_ids_getter(client):
            entities.append(XComfortComponentSensorEntity(client, device_id, spec))

    for room_id in client.get_room_ids():
        room = client.get_room(room_id) or {}
        for spec in ROOM_SENSOR_SPECS:
            if spec.requires_climate and not client.room_has_climate(room_id):
                continue
            if spec.required_field is not None and spec.required_field not in room:
                continue
            entities.append(XComfortRoomSensorEntity(client, room_id, spec))

    entities.append(XComfortBridgePowerSensor(client))
    entities.append(XComfortBridgeIpSensor(client))
    for slave_client_id in client.slave_bridges:
        entities.append(XComfortSlaveBridgeIpSensor(client, slave_client_id))

    async_add_entities(entities)
